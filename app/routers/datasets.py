from fastapi import APIRouter

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.get("/")
async def list_datasets():
    """Return a list of available datasets."""
    return {
        "datasets": [
            {"id": "ds084.1", "title": "NCEP GFS 0.25 Degree Global Forecast Grids"},
            {"id": "ds083.3", "title": "NCEP GDAS/FNL 0.25 Degree Global Tropospheric Analyses"},
        ]
    }


@router.get("/{dataset_id}")
async def get_dataset(dataset_id: str):
    """Return metadata for a specific dataset."""
    return {
        "id": dataset_id,
        "title": f"Dataset {dataset_id}",
        "status": "active",
    }
