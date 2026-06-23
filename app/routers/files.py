import asyncio

import httpx
from fastapi import APIRouter, HTTPException

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
