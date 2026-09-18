from app.celery_app import celery_app

@celery_app.task
def test_task():
  print("Hello from Celery!")
  return "Yo, Celery is working!"
