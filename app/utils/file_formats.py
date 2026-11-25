"""Magic-byte file format detection shared by the files router and the Celery tasks.

Lives in utils (not in app/routers/files.py) so the worker-side tasks can use it
without importing a FastAPI router, which would be a circular import.
"""

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


def read_local_header(path: str) -> bytes:
    """Read the first 8 bytes of a local file (enough for every magic number above)."""
    with open(path, "rb") as f:
        return f.read(8)
