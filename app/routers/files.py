import asyncio
import os
import stat
from pathlib import Path

import httpx
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
