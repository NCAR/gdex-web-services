import asyncio
import os
import uuid
from io import BytesIO

import boto3
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import xarray as xr
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/generators", tags=["generators"])

_OBJECT_STORE_ENDPOINT = "https://boreas.ucar.edu"
_BUCKET = "gdex-data"
_OBJECT_PREFIX = "services_tmp"
_ALLOWED_ROOT = "/glade/"


def _resolve_local_path(path):
    """Resolve `path` and confirm it falls under the /glade/ mount, rejecting escapes via symlinks or `..`."""
    resolved = os.path.realpath(path)
    if resolved != _ALLOWED_ROOT.rstrip("/") and not resolved.startswith(_ALLOWED_ROOT):
        raise HTTPException(status_code=403, detail=f"Path must be under {_ALLOWED_ROOT}")
    return resolved


def _s3_client():
    """Build a boto3 S3 client for the boreas object store using credentials from the environment."""
    return boto3.client(
        "s3",
        endpoint_url=_OBJECT_STORE_ENDPOINT,
        aws_access_key_id=os.environ["OBJECT_STORE_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["OBJECT_STORE_SECRET_KEY"],
    )


def _render_netcdf_variable(path, variable):
    """Open a local NetCDF file with xarray and render one variable to PNG bytes."""
    with xr.open_dataset(path) as ds:
        if variable is not None:
            if variable not in ds.data_vars:
                raise HTTPException(status_code=400, detail=f"Variable not found: {variable}")
            var_name = variable
        else:
            try:
                var_name = next(iter(ds.data_vars))
            except StopIteration:
                raise HTTPException(status_code=400, detail="File has no data variables to visualize")

        fig, ax = plt.subplots()
        try:
            ds[var_name].plot(ax=ax)
            buf = BytesIO()
            fig.savefig(buf, format="png")
        finally:
            plt.close(fig)

    return var_name, buf.getvalue()


def _upload_image(image_bytes):
    """Upload PNG bytes to the boreas object store under the services_tmp/ prefix, publicly readable, and return the object key."""
    key = f"{_OBJECT_PREFIX}/{uuid.uuid4()}.png"
    _s3_client().put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=image_bytes,
        ContentType="image/png",
        ACL="public-read",
    )
    return key


def _generate_and_upload(path, variable):
    """Render the requested variable and upload it, returning (variable_name, object_key)."""
    var_name, image_bytes = _render_netcdf_variable(path, variable)
    key = _upload_image(image_bytes)
    return var_name, key


@router.post("/visualize")
async def visualize(path: str, variable: str | None = Query(default=None)):
    """Render a variable from a local NetCDF file with matplotlib and upload it to the object store.

    Uses xarray to open the file and plot either the given `variable` or,
    if omitted, the first data variable found, via xarray's built-in
    `.plot()` (which picks a line/pcolormesh/etc. based on dimensionality).
    The rendered PNG is uploaded to the boreas object store under
    services_tmp/ and the resulting public location is returned.

    `path` must resolve under /glade/. Note: no size limit is enforced on
    the input file, so a very large NetCDF file could be loaded fully into
    memory here and risk exceeding the container's memory limit — worth
    adding a size guard if that becomes a problem in practice.
    """
    resolved_path = _resolve_local_path(path)
    if not os.path.exists(resolved_path):
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    try:
        var_name, key = await asyncio.to_thread(_generate_and_upload, resolved_path, variable)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "path": path,
        "variable": var_name,
        "bucket": _BUCKET,
        "key": key,
        "location": f"{_OBJECT_STORE_ENDPOINT}/{_BUCKET}/{key}",
    }
