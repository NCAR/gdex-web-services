from app.celery_app import celery_app
from pathlib import Path

@celery_app.task
def test_task():
  print("Hello from Celery!")
  return "Yo, Celery is working!"

@celery_app.task
def check_recursive_task(path):
  is_url = path.startswith("http://") or path.startswith("https://")
  if is_url:
      return {
          "error": "unsupported_endpoint",
          "message": "The provided endpoint is a URL, but this function only supports endpoint paths on /glade.",
          "endpoint": path
          }

  p = Path(path)
  return {"path": path, "is_dir": p.is_dir()}


@celery_app.task
def compute_size_task(check_result):
  if "error" in check_result:
      return check_result

  path = check_result["path"]
  p = Path(path)

  if check_result["is_dir"]:
      total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
      return {"path": path, "is_directory": True, "size_bytes": total}

  return {"path": path, "is_directory": False, "size_bytes": p.stat().st_size}