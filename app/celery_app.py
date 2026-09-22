from celery import Celery

celery_app = Celery(
  "gdex-services",
  broker = "redis://redis:6379/0",
  backend = "redis://redis:6379/1", 
)

# import files that contain celery related tasks
import app.tasks