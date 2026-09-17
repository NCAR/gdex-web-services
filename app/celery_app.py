from celery import Celery

app = Celery(
  "gdex-services",
  broker="redis://redis:6379/0"
)

# import files that contain celery tasks
import app.tasks
