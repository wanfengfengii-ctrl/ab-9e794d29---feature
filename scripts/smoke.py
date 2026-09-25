#!/usr/bin/env python3
"""verify 一次性服务的 API 冒烟验收：对运行中的 web 服务打真实 HTTP 请求并断言。

覆盖：
1. GET /health 健康检查；
2. GET / 能拿到录入页面；
3. POST /api/particle-deduplications 合法样例 → 200 且裁决结果正确；
4. POST /api/particle-deduplications 不合规样例 → 422、错误可定位、草稿原样回显；
5. POST /api/registration-reviews 合法样例 → 200，校正量/珠位证据/校正后去重可核对；
6. POST /api/registration-reviews 不合规样例（重复珠、非整数坐标、范围越界、
   不可连首视野、不存在可行配准）→ 422、错误逐项可定位、草稿原样回显。

任一断言失败即以非零退出码结束。
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx

WEB_URL = os.environ.get("WEB_URL", "http://web:8000").rstrip("/")


def wait_for_health(timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"{WEB_URL}/health", timeout=3)
            if r.status_code == 200 and r.json().get("status") == "ok":
                print(f"  [OK] GET /health -> 200 {r.text.strip()}")
                return
        except Exception as exc:  # 服务可能尚未就绪
            last_err = exc
        time.sleep(1.5)
    print(f"  [FAIL] 健康检查在 {timeout:.0f}s 内未通过：{last_err}")
    sys.exit(1)


def main() -> None:
    print(f"== API 冒烟验收（目标 {WEB_URL}） ==")
    wait_for_health()

    # 页面
    r = httpx.get(f"{WEB_URL}/", timeout=5)
    assert r.status_code == 200, "首页应返回 200"
    assert "/api/particle-deduplications" in r.text, "页面必须调用真实 POST 接口"
    print("  [OK] GET / -> 200，页面包含去重接口调用")

    # 合法请求：三个视野中的同一颗 PE 颗粒，容差边界恰好成立
    good_payload = {
        "tolerance": 2,
        "fields": [
            {"name": "A区", "shift": {"x": 0, "y": 0},
             "particles": [{"id": "P1", "x": 10, "y": 10, "category": "PE"}]},
            {"name": "B区", "shift": {"x": 10, "y": 0},
             "particles": [{"id": "Q1", "x": 0, "y": 10, "category": "PE"}]},
            {"name": "C区", "shift": {"x": 10, "y": 10},
             "particles": [{"id": "R1", "x": 0, "y": 0, "category": "PE"}]},
        ],
    }
    r = httpx.post(f"{WEB_URL}/api/particle-deduplications",
                   json=good_payload, timeout=10)
    assert r.status_code == 200, f"合法请求应返回 200，实际 {r.status_code}: {r.text}"
    data = r.json()
    assert data["summary"]["finalParticleCount"] == 1, json.dumps(data, ensure_ascii=False)
    fp = data["finalParticles"][0]
    obs_ids = sorted(o["particleId"] for o in fp["observations"])
    assert obs_ids == ["P1", "Q1", "R1"]
    assert fp["category"] == "PE"
    assert fp["representativeCoordinate"]["particleId"] == "P1"
    assert fp["representativeCoordinate"] == {
        "fieldIndex": 0, "fieldName": "A区", "particleId": "P1", "x": 10, "y": 10
    }
    print("  [OK] POST 合法样例 -> 200，3 个观测正确合并为 1 个最终颗粒，"
          "代表坐标/类别/总数符合预期")

    # 不合规请求：视野数量不足 3
    bad_payload = {
        "tolerance": 2,
        "fields": good_payload["fields"][:2],
    }
    r = httpx.post(f"{WEB_URL}/api/particle-deduplications",
                   json=bad_payload, timeout=5)
    assert r.status_code == 422, f"不合规请求应返回 422，实际 {r.status_code}"
    err = r.json()
    assert any(e["loc"] == "fields" for e in err["errors"]), "错误必须可定位"
    assert err["draft"] == bad_payload, "不合规时必须原样保留草稿"
    print("  [OK] POST 不合规样例 -> 422，反馈可定位到 fields，草稿原样回显保留")

    # 颗粒级精确定位
    bad2 = {
        "tolerance": -1,
        "fields": [
            {"name": "A", "shift": {"x": 0, "y": 0},
             "particles": [{"id": "P1", "x": 1.5, "y": 0, "category": "PE"}]},
            {"name": "B", "shift": {"x": 0, "y": 0}, "particles": []},
            {"name": "C", "shift": {"x": 0, "y": 0}, "particles": []},
        ],
    }
    r = httpx.post(f"{WEB_URL}/api/particle-deduplications",
                   json=bad2, timeout=5)
    assert r.status_code == 422
    locs = {e["loc"] for e in r.json()["errors"]}
    assert {"tolerance", "fields[0].particles[0].x"} <= locs
    print("  [OK] POST 多处不合规 -> 422，逐字段定位 tolerance 与 "
          "fields[0].particles[0].x")

    # ------------------------------------------------------------------ #
    # 配准复核：合法样例
    # ------------------------------------------------------------------ #
    review_ok = {
        "tolerance": 0,
        "beadResidualLimit": 0,
        "fields": [
            {"name": "A区", "shift": {"x": 0, "y": 0}, "correctionRange": 3,
             "particles": [{"id": "P1", "x": 10, "y": 10, "category": "PE"}],
             "calibrationBeads": [{"id": "B1", "x": 5, "y": 5}]},
            {"name": "B区", "shift": {"x": 11, "y": 0}, "correctionRange": 3,
             "particles": [{"id": "Q1", "x": 0, "y": 10, "category": "PE"}],
             "calibrationBeads": [{"id": "B1", "x": -5, "y": 5}]},
            {"name": "C区", "shift": {"x": 10, "y": 11}, "correctionRange": 3,
             "particles": [{"id": "R1", "x": 0, "y": 0, "category": "PE"}],
             "calibrationBeads": [{"id": "B1", "x": -5, "y": -5}]},
        ],
    }
    r = httpx.post(f"{WEB_URL}/api/registration-reviews",
                   json=review_ok, timeout=10)
    assert r.status_code == 200, f"配准复核合法请求应返回 200，实际 {r.status_code}: {r.text}"
    data = r.json()
    rr = data["registrationReview"]
    assert rr["corrections"][0]["correction"] == {"x": 0, "y": 0}, "首视野校正必须固定为零"
    corr = {c["fieldIndex"]: c["correction"] for c in rr["corrections"]}
    assert corr[1] == {"x": -1, "y": 0}
    assert corr[2] == {"x": 0, "y": -1}
    assert rr["summary"]["maxCoordinateResidual"] == 0
    assert rr["summary"]["totalCorrectionManhattan"] == 2
    # 原平移 / 校正平移 / 校正后平移
    assert rr["corrections"][1]["originalShift"] == {"x": 11, "y": 0}
    assert rr["corrections"][1]["correctedShift"] == {"x": 10, "y": 0}
    # 珠位证据：B1 出现三次、三对横纵残差均为 0
    bead = next(b for b in rr["beadEvidence"] if b["beadId"] == "B1")
    assert bead["occurrenceCount"] == 3
    assert len(bead["pairwiseResiduals"]) == 3
    for pair in bead["pairwiseResiduals"]:
        assert pair["deltaX"] == 0 and pair["deltaY"] == 0
        assert pair["withinLimit"] is True
    # 校正后重新去重：容差 0 下三颗 PE 观测合并
    assert data["summary"]["finalParticleCount"] == 1
    assert {o["particleId"] for o in data["finalParticles"][0]["observations"]} == {
        "P1", "Q1", "R1"
    }
    print("  [OK] POST 配准复核合法样例 -> 200，首视野固定零、联合校正 (-1,0)/(0,-1)、"
          "珠位证据完整、校正后去重合并为 1 颗粒")

    # ------------------------------------------------------------------ #
    # 配准复核：422 场景
    # ------------------------------------------------------------------ #
    # 校准珠同视野重复 + 非整数坐标 + 校正范围越界
    bad_review = json.loads(json.dumps(review_ok))
    bad_review["fields"][1]["calibrationBeads"].append(
        {"id": "B1", "x": 1.5, "y": 0}
    )
    bad_review["fields"][0]["correctionRange"] = 6
    r = httpx.post(f"{WEB_URL}/api/registration-reviews",
                   json=bad_review, timeout=5)
    assert r.status_code == 422
    body = r.json()
    locs = {e["loc"] for e in body["errors"]}
    assert {"fields[1].calibrationBeads[1].id",
            "fields[1].calibrationBeads[1].x",
            "fields[0].correctionRange"} <= locs
    assert body["draft"] == bad_review, "不合规时必须原样保留草稿"
    assert body["draftPreserved"] is True
    assert body["previousReviewPreserved"] is True
    print("  [OK] POST 配准复核珠重复/坐标非整数/范围越界 -> 422，逐项定位且草稿原样回显")

    # 无法连到首视野
    disconnected = json.loads(json.dumps(review_ok))
    disconnected["fields"][2]["calibrationBeads"][0]["id"] = "ORPHAN"
    r = httpx.post(f"{WEB_URL}/api/registration-reviews",
                   json=disconnected, timeout=5)
    assert r.status_code == 422
    locs = {e["loc"] for e in r.json()["errors"]}
    assert "fields[2].calibrationBeads" in locs
    assert r.json()["draft"] == disconnected
    print("  [OK] POST 配准复核视野无法连到首视野 -> 422，定位到该视野且草稿保留")

    # 不存在可行配准（范围不足以补偿漂移，残差上限 0）
    infeasible = json.loads(json.dumps(review_ok))
    infeasible["fields"][1]["correctionRange"] = 0
    infeasible["fields"][2]["correctionRange"] = 0
    r = httpx.post(f"{WEB_URL}/api/registration-reviews",
                   json=infeasible, timeout=10)
    assert r.status_code == 422
    assert any(e["loc"] == "beadResidualLimit" for e in r.json()["errors"])
    assert r.json()["draft"] == infeasible
    print("  [OK] POST 配准复核不存在可行配准 -> 422，明确反馈且草稿保留，"
          "不覆盖上一次成功复核结果")

    print("== API 冒烟验收全部通过 ==")


if __name__ == "__main__":
    main()
