from fastapi import FastAPI
from app.routers import datasets

app = FastAPI(
    title="GDEX Web Services",
    description="GDEX backend web services API",
    version="0.1.0",
)

app.include_router(datasets.router)


@app.get("/")
async def root():
    return {"status": "ok", "service": "gdex-web-services"}
