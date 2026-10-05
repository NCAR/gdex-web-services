import logging

from fastapi import FastAPI
from app.routers import datasets, files, generators, compose

# Uvicorn only configures its own loggers; send app loggers (e.g. app.routers.compose)
# to stdout at INFO so they show up in `kubectl logs`.
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

app = FastAPI(
    title="GDEX Web Services",
    description="GDEX backend web services API",
    version="0.1.0",
)

app.include_router(datasets.router)
app.include_router(files.router)
app.include_router(generators.router)
app.include_router(compose.router)


@app.get("/")
async def root():
    return {"status": "ok", "service": "gdex-web-services"}
