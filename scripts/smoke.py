#!/usr/bin/env python3
"""verify 一次性服务的 API 冒烟验收：对运行中的 web 服务打真实 HTTP 请求并断言。

覆盖：
1. GET /health 健康检查；
2. GET / 能拿到录入页面；
3. POST /api/particle-deduplications 合法样例 → 200 且裁决结果正确；
4. POST /api/particle-deduplications 不合规样例 → 422、错误可定位、草稿原样回显。

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

    print("== API 冒烟验收全部通过 ==")


if __name__ == "__main__":
    main()
