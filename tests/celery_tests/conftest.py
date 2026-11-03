"""Pytest setup for the Celery-related tests only (this folder).

Runs Celery tasks eagerly (in-process) with an in-memory result backend so
no Redis is needed. Lives here, not in tests/conftest.py, so tests outside this
folder (e.g. the gdexws tests in CI, where Celery is not installed) never import it.
"""
import pytest

from app.celery_app import celery_app
from app.utils import callback

celery_app.conf.update(
    broker_url="memory://",
    result_backend="cache+memory://",
    task_always_eager=True,
    task_store_eager_result=True,
)


@pytest.fixture(autouse=True)
def allow_test_callback_url(monkeypatch):
    """The tests post to a fake portal URL; callback_to_portal only sends to allowlisted ones."""
    monkeypatch.setattr(callback, "ALLOWED_CALLBACK_URLS", (*callback.ALLOWED_CALLBACK_URLS, "https://portal/cb"))
