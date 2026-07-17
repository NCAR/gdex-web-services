import asyncio
import os
import stat
from pathlib import Path

import cfgrib
import httpx
import xarray as xr
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/files", tags=["files"])

# Magic bytes for NetCDF variants
_NETCDF_MAGIC = {
    b"CDF\x01": "CDF-1",
    b"CDF\x02": "CDF-2",
    b"CDF\x05": "CDF-5",
    b"\x89HDF\r\n\x1a\n": "NetCDF-4",
}


def _detect_format(header: bytes) -> str | None:
    for magic, fmt in _NETCDF_MAGIC.items():
        if header.startswith(magic):
            return fmt
    return None


# Magic bytes for NetCDF variants, named for the generic /format endpoint.
# netCDF-4 files are HDF5 under the hood; whether they use the restricted
# "classic model" isn't recoverable from the header alone (it takes opening
# the file with a library like netCDF4-python/h5py and inspecting groups and
# user-defined types), so all HDF5-magic files are reported as "netcdf4".
_NETCDF_FORMAT_MAGIC = {
    b"CDF\x01": "netcdf3-classic",
    b"CDF\x02": "netcdf3-64bit-offset",
    b"CDF\x05": "netcdf3-64bit-data",
    b"\x89HDF\r\n\x1a\n": "netcdf4",
}

_GRIB_MAGIC = b"GRIB"

# Editions with a well-known name. Any other edition byte is still reported,
# as "grib<N>", by _detect_file_format below.
_GRIB_EDITIONS = {
    1: "grib1",
    2: "grib2",
}

# BUFR (WMO point-observation format, e.g. soundings/buoys) shares GRIB's
# section-0 layout: 4-byte magic, then a 3-byte length, then a 1-byte edition.
_BUFR_MAGIC = b"BUFR"
_BUFR_EDITIONS = {
    3: "bufr3",
    4: "bufr4",
}

# HDF4's signature is a fixed 4-byte value (distinct from the HDF5 signature
# used by netCDF-4 above).
_HDF4_MAGIC = b"\x0e\x03\x13\x01"

# TIFF/GeoTIFF and BigTIFF, little- and big-endian. This only identifies the
# TIFF container and whether it's the 32-bit-offset ("tiff") or 64-bit-offset
# ("bigtiff") variant, read from the version field right after the byte-order
# mark - it doesn't confirm the file is actually geo-referenced, since that
# takes walking the IFD tags rather than reading a fixed header.
_TIFF_MAGIC = {
    b"II*\x00": "tiff",
    b"MM\x00*": "tiff",
    b"II+\x00": "bigtiff",
    b"MM\x00+": "bigtiff",
}


def _detect_file_format(header: bytes) -> tuple[str | None, str | None]:
    """Identify a file's format family and specific version from its header bytes.

    Returns a (family, format) tuple, e.g. ("netcdf", "netcdf4") or
    ("grib", "grib2"). Returns (None, None) if the header doesn't match a
    recognized format. GRIB/BUFR edition is read from byte 7 of the header,
    per their shared indicator-section layout.
    """
    for magic, fmt in _NETCDF_FORMAT_MAGIC.items():
        if header.startswith(magic):
            return "netcdf", fmt

    if header.startswith(_GRIB_MAGIC) and len(header) >= 8:
        edition = header[7]
        return "grib", _GRIB_EDITIONS.get(edition, f"grib{edition}")

    if header.startswith(_BUFR_MAGIC) and len(header) >= 8:
        edition = header[7]
        return "bufr", _BUFR_EDITIONS.get(edition, f"bufr{edition}")

    if header.startswith(_HDF4_MAGIC):
        return "hdf", "hdf4"

    for magic, fmt in _TIFF_MAGIC.items():
        if header.startswith(magic):
            return "tiff", fmt

    return None, None


async def _read_local_header(path: str) -> bytes:
    def _read():
        with open(path, "rb") as f:
            return f.read(8)

    return await asyncio.to_thread(_read)


async def _read_url_header(url: str) -> bytes:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(url, headers={"Range": "bytes=0-7"})
        if resp.status_code not in (200, 206):
            raise HTTPException(status_code=502, detail=f"Failed to fetch URL: HTTP {resp.status_code}")
        return resp.content[:8]


# Zarr stores are directories/prefixes, not single files with a header, so
# they're identified by a top-level metadata file rather than magic bytes:
# "zarr.json" for v3, or the legacy ".zgroup"/".zarray" for v2.
_ZARR_V3_MARKER = "zarr.json"
_ZARR_V2_MARKERS = (".zgroup", ".zarray")


def _is_zarr_path(path: str) -> bool:
    return path.rstrip("/").endswith(".zarr")


def _detect_zarr_local(path: Path) -> str | None:
    """Identify the Zarr spec version of a local `.zarr` store directory.

    Checks for `zarr.json` (v3) first, then either of the v2 sidecar files.
    Returns None if the directory has neither, i.e. it's named like a Zarr
    store but doesn't contain one.
    """
    if (path / _ZARR_V3_MARKER).is_file():
        return "zarr3"
    if any((path / marker).is_file() for marker in _ZARR_V2_MARKERS):
        return "zarr2"
    return None


async def _detect_zarr_url(url: str) -> str | None:
    """Identify the Zarr spec version of a remote `.zarr` store.

    Mirrors _detect_zarr_local but over HTTP: sends a HEAD request for each
    candidate metadata file under the store's URL prefix and returns the
    version for the first one that responds successfully.
    """
    base = url.rstrip("/") + "/"
    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
        resp = await client.head(base + _ZARR_V3_MARKER)
        if resp.status_code < 400:
            return "zarr3"
        for marker in _ZARR_V2_MARKERS:
            resp = await client.head(base + marker)
            if resp.status_code < 400:
                return "zarr2"
    return None


@router.get("/is-netcdf")
async def is_netcdf(path: str):
    """Check whether a local file path or URL points to a NetCDF file."""
    is_url = path.startswith("http://") or path.startswith("https://")

    try:
        header = await (_read_url_header(path) if is_url else _read_local_header(path))
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    fmt = _detect_format(header)
    return {"path": path, "is_netcdf": fmt is not None, "format": fmt}


@router.get("/formats")
async def list_formats():
    """List the exact family/format strings that GET /files/format can return.

    Exists so callers don't have to guess or reverse-engineer casing/naming
    (e.g. this endpoint's "netcdf4" vs. the older /files/is-netcdf's
    "NetCDF-4" for the same underlying format) from example responses.
    """
    return {
        "netcdf": {
            "values": sorted(set(_NETCDF_FORMAT_MAGIC.values())),
            "note": (
                "netcdf4 covers both the restricted 'classic model' and full "
                "netCDF-4 files, since both are HDF5-based and indistinguishable "
                "from the file header alone."
            ),
        },
        "grib": {
            "values": sorted(_GRIB_EDITIONS.values()),
            "note": "Editions without a well-known name are reported as 'grib<N>'.",
        },
        "bufr": {
            "values": sorted(_BUFR_EDITIONS.values()),
            "note": "Editions without a well-known name are reported as 'bufr<N>'.",
        },
        "hdf": {
            "values": ["hdf4"],
            "note": (
                "Only the HDF4 container is reported here. HDF5-based files - which "
                "includes generic (non-netCDF) HDF5 - share netCDF-4's signature and "
                "are reported as netcdf4 above, since the two aren't distinguishable "
                "from the file header alone."
            ),
        },
        "tiff": {
            "values": sorted(set(_TIFF_MAGIC.values())),
            "note": (
                "Identifies the TIFF/BigTIFF container only. Confirming a file is "
                "specifically a GeoTIFF (i.e. has geo-referencing tags) requires "
                "walking the image file directory, not just reading the header."
            ),
        },
        "zarr": {
            "values": ["zarr2", "zarr3"],
            "note": (
                "Detected differently from every other family above, via a "
                "top-level metadata file rather than magic bytes: 'zarr.json' for "
                "v3, or '.zgroup'/'.zarray' for v2. For local paths, any directory "
                "is checked for these markers, whether or not it's named '*.zarr'. "
                "For URLs, only paths ending in '.zarr' are checked, since a failed "
                "request doesn't reliably indicate a URL is a directory-style store "
                "the way a local stat() does. A checked path/URL with none of these "
                "markers present returns family/format as null."
            ),
        },
    }


async def _classify_path(path: str, is_url: bool) -> tuple[str | None, str | None]:
    """Determine the (family, format) of a local path or URL.

    This is GET /files/format's detection logic, factored out so other
    endpoints (e.g. /files/metadata) can identify a file before deciding how
    to read it further. Zarr stores are identified by a top-level metadata
    file; plain local directories are checked for the same Zarr markers even
    without a '*.zarr' name; everything else is identified by its first 8
    header bytes via _detect_file_format. Raises HTTPException(404) if a
    local path doesn't exist.
    """
    if _is_zarr_path(path):
        if is_url:
            fmt = await _detect_zarr_url(path)
        else:
            p = Path(path)
            if not p.exists():
                raise HTTPException(status_code=404, detail=f"Path not found: {path}")
            fmt = _detect_zarr_local(p) if p.is_dir() else None
        return ("zarr" if fmt else None), fmt

    if not is_url:
        p = Path(path)
        if p.is_dir():
            # The '*.zarr' suffix above is just a naming convention, not
            # required by the Zarr spec, so a local directory that doesn't
            # match it can still be a Zarr store - check for the same marker
            # files here. Directories have no magic bytes to sniff, so this
            # replaces header-based detection for them entirely.
            #
            # There's no analogous fallback for URLs below: unlike a local
            # stat(), a failed Range GET doesn't reliably tell us "this URL
            # is a directory-style store" - a 404 is just as likely to mean
            # the URL is missing or mistyped - and probing 3 extra marker
            # URLs on every such 404 would add latency to the common case
            # for a rare payoff. Remote Zarr stores must be named '*.zarr'
            # to be detected.
            fmt = _detect_zarr_local(p)
            return ("zarr" if fmt else None), fmt

    header = await (_read_url_header(path) if is_url else _read_local_header(path))
    return _detect_file_format(header)


@router.get("/format")
async def file_format(path: str):
    """Detect the format (and version) of a local file path or URL.

    Identifies NetCDF (classic, 64-bit offset, 64-bit data, and netCDF-4/
    HDF5-based), GRIB, BUFR, HDF4, TIFF/BigTIFF, and Zarr (v2/v3) files or
    stores. File formats are identified by sniffing the first 8 bytes,
    avoiding a full download/read of large scientific data files; Zarr
    stores (directories/prefixes, not single files) are identified instead
    by the presence of a top-level metadata file. See GET /files/formats for
    the exact set of strings this can return.
    """
    is_url = path.startswith("http://") or path.startswith("https://")

    try:
        family, fmt = await _classify_path(path, is_url)
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"path": path, "family": family, "format": fmt}


def _netcdf_dataset_str(path: str) -> str:
    """Open a local NetCDF file with xarray and return its __str__ representation.

    xarray's default engine selection (netCDF4, via the netCDF4 package)
    handles all NetCDF variants /files/format can identify - classic,
    64-bit offset, 64-bit data, and netCDF-4/HDF5-based - without needing to
    pick an engine per format.
    """
    with xr.open_dataset(path) as ds:
        return str(ds)


def _grib_dataset_str(path: str) -> str:
    """Open a local GRIB file with cfgrib and return combined __str__ output.

    Unlike NetCDF, a single GRIB file commonly mixes messages that don't
    share one common grid/level/time structure (e.g. surface fields
    alongside multiple pressure levels), so cfgrib.open_datasets splits the
    file into one xarray.Dataset per self-consistent "hypercube" instead of
    the single Dataset xr.open_dataset returns for NetCDF. Each hypercube's
    __str__ is concatenated under a header line so `metadata` stays a
    single string for both formats.
    """
    datasets = cfgrib.open_datasets(path)
    try:
        return "\n\n".join(f"--- hypercube {i + 1}/{len(datasets)} ---\n{ds}" for i, ds in enumerate(datasets))
    finally:
        for ds in datasets:
            ds.close()


@router.get("/metadata")
async def file_metadata(path: str):
    """Extract header/metadata information for a local file.

    Uses the same detection as GET /files/format to identify the file's
    family, then dispatches to a format-specific reader. NetCDF is opened
    with xarray and GRIB with cfgrib; in both cases `metadata` is the
    __str__ of the resulting xarray Dataset(s) (dimensions, coordinates,
    data variables, and attributes). A GRIB file that splits into multiple
    "hypercubes" (see _grib_dataset_str) has each one concatenated into the
    same string. Other recognized families return `metadata: null` for now
    - see the TODOs below.

    TODO: remote URLs aren't supported yet (this currently returns 501 for
    any http(s):// path). Unlike /files/format's 8-byte header sniff, full
    metadata extraction generally needs either the whole file downloaded
    first or an OPeNDAP-style engine that can stream it, and neither is
    wired up here.
    """
    is_url = path.startswith("http://") or path.startswith("https://")
    if is_url:
        # TODO: support remote URLs - either download to a temp file before
        # opening, or (for OPeNDAP-style endpoints) pass the URL straight to
        # xr.open_dataset and let xarray/netCDF4 stream it.
        raise HTTPException(status_code=501, detail="Metadata extraction for remote URLs is not yet supported")

    try:
        family, fmt = await _classify_path(path, is_url)
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    metadata: str | None = None
    if family == "netcdf":
        try:
            metadata = await asyncio.to_thread(_netcdf_dataset_str, path)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to read NetCDF metadata: {e}")
    elif family == "grib":
        try:
            metadata = await asyncio.to_thread(_grib_dataset_str, path)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to read GRIB metadata: {e}")
    elif family == "bufr":
        pass  # TODO: extract BUFR metadata
    elif family == "hdf":
        pass  # TODO: extract HDF4 metadata, e.g. via pyhdf
    elif family == "tiff":
        pass  # TODO: extract TIFF/GeoTIFF metadata, e.g. via rasterio
    elif family == "zarr":
        pass  # TODO: extract Zarr metadata, e.g. via xarray/zarr

    return {"path": path, "family": family, "format": fmt, "metadata": metadata}


def _parents_traversable(path):
    """Check that every parent directory of `path` grants others execute (traverse) access.

    A file can be mode 644 and still be unreachable by other users if any
    directory above it in the tree is not world-traversable, so the file's
    own mode bits alone don't determine real-world global readability.
    """
    for parent in path.resolve().parents:
        try:
            mode = parent.stat().st_mode
        except OSError as e:
            return False, f"cannot stat parent directory {parent}: {e}"
        if not mode & stat.S_IXOTH:
            return False, f"parent directory not traversable by others: {parent}"
    return True, None


def _check_globally_readable(path):
    """Check whether `path` is readable by any user on the system.

    For a directory this requires both the other-read and other-execute
    bits (read to list entries, execute to traverse into it). For a file
    it requires only the other-read bit. In both cases every parent
    directory must also be traversable by others.
    """
    traversable, reason = _parents_traversable(path)
    if not traversable:
        return False, reason

    mode = path.stat().st_mode
    if path.is_dir():
        if mode & stat.S_IROTH and mode & stat.S_IXOTH:
            return True, None
        return False, f"directory not world-readable/listable: {path}"

    if mode & stat.S_IROTH:
        return True, None
    return False, f"file not world-readable: {path}"


def _scan_directory(root, max_results):
    """Recursively scan `root` for files that are not globally readable.

    Symlinks are not followed. Stops early once `max_results` files have
    been scanned, avoiding an unbounded walk over very large or deeply
    nested directory trees (e.g. an NFS-mounted campaign share).
    Returns (files_scanned, non_readable_files, truncated).
    """
    files_scanned = 0
    non_readable: list[dict] = []
    truncated = False

    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for fname in filenames:
            if files_scanned >= max_results:
                truncated = True
                return files_scanned, non_readable, truncated
            fpath = Path(dirpath) / fname
            files_scanned += 1
            ok, reason = _check_globally_readable(fpath)
            if not ok:
                non_readable.append({"path": str(fpath), "reason": reason})

    return files_scanned, non_readable, truncated


async def _check_url_accessible(url: str) -> dict:
    """Send a HEAD request to `url` and report whether it responds successfully."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
        try:
            resp = await client.head(url)
        except httpx.RequestError as e:
            return {"path": url, "accessible": False, "status_code": None, "detail": str(e)}

    return {
        "path": url,
        "accessible": resp.status_code < 400,
        "status_code": resp.status_code,
    }


@router.get("/check-access")
async def check_access(
    path: str,
    recursive: bool = False,
    max_results: int = Query(default=100, ge=1, le=10000),
):
    """Check whether a local path is globally readable, or whether a remote URL is accessible.

    For a local file, checks the world-read permission bit and that every
    parent directory is traversable by others. For a local directory,
    checks the directory itself unless `recursive` is set, in which case
    files under it are checked up to `max_results` files (stopping early
    once that many have been scanned). For an http(s) URL, sends a HEAD
    request and reports whether it responded successfully.
    """
    is_url = path.startswith("http://") or path.startswith("https://")
    if is_url:
        return await _check_url_accessible(path)

    p = Path(path)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {path}")

    if p.is_dir() and recursive:
        files_scanned, non_readable, truncated = await asyncio.to_thread(_scan_directory, p, max_results)
        return {
            "path": path,
            "recursive": True,
            "files_scanned": files_scanned,
            "globally_readable": len(non_readable) == 0,
            "non_readable_files": non_readable,
            "truncated": truncated,
        }

    ok, reason = await asyncio.to_thread(_check_globally_readable, p)
    return {"path": path, "recursive": False, "globally_readable": ok, "reason": reason}
