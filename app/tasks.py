from app.celery_app import app

@app.task
def hello_world():
  print("Hello World!")
