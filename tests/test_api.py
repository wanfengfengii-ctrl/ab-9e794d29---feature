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


# --------------------------------------------------------------------------- #
# 配准复核接口
# --------------------------------------------------------------------------- #
def review_payload():
    return {
        "tolerance": 0,
        "beadResidualLimit": 0,
        "fields": [
            {"name": "A", "shift": {"x": 0, "y": 0}, "correctionRange": 3,
             "particles": [{"id": "P1", "x": 10, "y": 10, "category": "PE"}],
             "calibrationBeads": [{"id": "B1", "x": 5, "y": 5}]},
            {"name": "B", "shift": {"x": 11, "y": 0}, "correctionRange": 3,
             "particles": [{"id": "Q1", "x": 0, "y": 10, "category": "PE"}],
             "calibrationBeads": [{"id": "B1", "x": -5, "y": 5}]},
            {"name": "C", "shift": {"x": 10, "y": 11}, "correctionRange": 3,
             "particles": [{"id": "R1", "x": 0, "y": 0, "category": "PE"}],
             "calibrationBeads": [{"id": "B1", "x": -5, "y": -5}]},
        ],
    }


def test_post_registration_review_happy_path():
    r = client.post("/api/registration-reviews", json=review_payload())
    assert r.status_code == 200, r.text
    data = r.json()
    rr = data["registrationReview"]
    # 首视野固定为零
    assert rr["corrections"][0]["correction"] == {"x": 0, "y": 0}
    corr = {(c["fieldIndex"]): c["correction"] for c in rr["corrections"]}
    assert corr[1] == {"x": -1, "y": 0}
    assert corr[2] == {"x": 0, "y": -1}
    # 原平移、校正平移并列返回
    assert rr["corrections"][1]["originalShift"] == {"x": 11, "y": 0}
    assert rr["corrections"][1]["correctedShift"] == {"x": 10, "y": 0}
    # 珠位证据
    assert rr["summary"]["sharedBeadCount"] == 1
    bead = rr["beadEvidence"][0]
    assert bead["beadId"] == "B1"
    assert bead["occurrenceCount"] == 3
    # 校正后平移重新去重：容差 0 下三颗观测恰好重合
    assert data["summary"]["finalParticleCount"] == 1
    assert {o["particleId"] for o in data["finalParticles"][0]["observations"]} == {
        "P1", "Q1", "R1"
    }


def test_post_registration_review_unconnected_is_422_and_keeps_draft():
    payload = review_payload()
    # C 的校准珠不再与 A/B 共享 → 无法连到首视野
    payload["fields"][2]["calibrationBeads"][0]["id"] = "OTHER"
    r = client.post("/api/registration-reviews", json=payload)
    assert r.status_code == 422
    data = r.json()
    locs = {e["loc"] for e in data["errors"]}
    assert "fields[2].calibrationBeads" in locs
    assert data["draft"] == payload
    assert data["draftPreserved"] is True
    assert data["previousReviewPreserved"] is True


def test_post_registration_review_bad_bead_is_localizable():
    payload = review_payload()
    payload["fields"][1]["calibrationBeads"].append(
        {"id": "B1", "x": 1.5, "y": 0}
    )  # 同视野重复编号 + 非整数坐标
    payload["fields"][0]["correctionRange"] = 9  # 超出 0–5
    r = client.post("/api/registration-reviews", json=payload)
    assert r.status_code == 422
    locs = {e["loc"] for e in r.json()["errors"]}
    assert "fields[1].calibrationBeads[1].id" in locs
    assert "fields[1].calibrationBeads[1].x" in locs
    assert "fields[0].correctionRange" in locs


def test_post_registration_review_infeasible_is_422():
    payload = review_payload()
    payload["beadResidualLimit"] = 0
    payload["fields"][1]["correctionRange"] = 0
    payload["fields"][2]["correctionRange"] = 0
    r = client.post("/api/registration-reviews", json=payload)
    assert r.status_code == 422
    data = r.json()
    assert any(e["loc"] == "beadResidualLimit" for e in data["errors"])
    assert data["draft"] == payload


def test_post_registration_review_malformed_json():
    r = client.post(
        "/api/registration-reviews",
        content="{not-json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json()["errors"][0]["loc"] == "$"
