"""API 层冒烟测试：健康检查、真实 POST、422 可定位反馈与草稿保留。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_index_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "particle-deduplications" in r.text


def test_post_deduplication_happy_path():
    payload = {
        "tolerance": 2,
        "fields": [
            {"name": "A", "shift": {"x": 0, "y": 0},
             "particles": [{"id": "P1", "x": 10, "y": 10, "category": "PE"}]},
            {"name": "B", "shift": {"x": 10, "y": 0},
             "particles": [{"id": "Q1", "x": 0, "y": 10, "category": "PE"}]},
            {"name": "C", "shift": {"x": 10, "y": 10},
             "particles": [{"id": "R1", "x": 0, "y": 0, "category": "PE"}]},
        ],
    }
    r = client.post("/api/particle-deduplications", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["summary"]["finalParticleCount"] == 1
    fp = data["finalParticles"][0]
    assert {o["particleId"] for o in fp["observations"]} == {"P1", "Q1", "R1"}
    assert fp["representativeCoordinate"]["particleId"] == "P1"
    assert fp["category"] == "PE"
    assert fp["observationCount"] == 3


def test_post_invalid_returns_localizable_errors_and_keeps_draft():
    payload = {
        "tolerance": 2,
        "fields": [
            {"name": "A", "shift": {"x": 0, "y": 0}, "particles": []},
            {"name": "B", "shift": {"x": 0, "y": 0}, "particles": []},
        ],
    }
    r = client.post("/api/particle-deduplications", json=payload)
    assert r.status_code == 422
    data = r.json()
    assert any(e["loc"] == "fields" for e in data["errors"])
    assert data["draft"] == payload  # 草稿原样回显，不丢弃


def test_post_malformed_json():
    r = client.post(
        "/api/particle-deduplications",
        content="{not-json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json()["errors"][0]["loc"] == "$"
