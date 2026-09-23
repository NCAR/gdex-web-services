from celery import Celery

celery_app = Celery(
  "gdex-services",
  broker = "redis://redis:6379/0",
  backend = "redis://redis:6379/1", 
)
celery_app.conf.result_expires = 86400  # seconds

# import files that contain celery related tasks
import app.tasks