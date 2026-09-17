from app.celery_app import app

@app.task
def test_task():
  print("Hello from Celery!")
  return "Yo, Celery is working!"
