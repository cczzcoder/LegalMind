import os

os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = (
    "postgresql+asyncpg://unused:unused@localhost/unused"
)
os.environ["DEV_API_KEY"] = "a" * 64
os.environ["DEV_ORG_ID"] = "11111111-1111-4111-8111-111111111111"
os.environ["DEV_USER_ID"] = "22222222-2222-4222-8222-222222222222"

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.modules.wiki.schemas import CreatePage


def test_liveness():
    with TestClient(app) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json()["service"] == "LegalMind"


def test_wiki_requires_credentials():
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/wiki/pages",
            json={"title": "测试", "body": "草稿正文"},
        )

    assert response.status_code == 401


def test_blank_title_is_rejected():
    with pytest.raises(ValidationError):
        CreatePage(title="   ", body="正文")
