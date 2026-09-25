#!/usr/bin/env python3
"""verify 一次性服务的 API 冒烟验收：对运行中的 web 服务打真实 HTTP 请求并断言。

覆盖：
1. GET /health 健康检查；
2. GET / 能拿到录入页面；
3. POST /api/particle-deduplications 合法样例 → 200 且裁决结果正确；
4. POST /api/particle-deduplications 不合规样例 → 422、错误可定位、草稿原样回显；
5. POST /api/registration-reviews 合法配准复核 → 200、首视野零校正、
   联合校正量正确、校正平移重跑去重结果正确；
6. GET /api/registration-reviews/last 可核对成功结果；
7. POST /api/registration-reviews 不可行/不合规 → 422 可定位、草稿回显，
   且不覆盖上一次成功结果。

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
    # 配准复核：合法样例（B 区登记平移漂移 2 格，共享校准珠 B1/B2 联合校正）
    # ------------------------------------------------------------------ #
    review_good = {
        "tolerance": 1,
        "beadResidualLimit": 1,
        "fields": [
            {"name": "A区", "shift": {"x": 0, "y": 0}, "correctionRange": 2,
             "beads": [
                 {"id": "B1", "x": 50, "y": 50},
                 {"id": "B2", "x": 80, "y": 50}],
             "particles": [{"id": "P1", "x": 10, "y": 10, "category": "PE"}]},
            {"name": "B区", "shift": {"x": 10, "y": 0}, "correctionRange": 2,
             "beads": [
                 {"id": "B1", "x": 43, "y": 50},
                 {"id": "B2", "x": 73, "y": 50}],
             "particles": [{"id": "Q1", "x": 2, "y": 10, "category": "PE"}]},
            {"name": "C区", "shift": {"x": 10, "y": 10}, "correctionRange": 2,
             "beads": [
                 {"id": "B1", "x": 40, "y": 40},
                 {"id": "B2", "x": 70, "y": 40}],
             "particles": [{"id": "R1", "x": 0, "y": 0, "category": "PE"}]},
        ],
    }
    r = httpx.post(f"{WEB_URL}/api/registration-reviews",
                   json=review_good, timeout=10)
    assert r.status_code == 200, f"合法配准复核应返回 200，实际 {r.status_code}: {r.text}"
    rev = r.json()
    assert rev["registration"]["feasible"] is True
    corrs = [(f["correction"]["x"], f["correction"]["y"]) for f in rev["fields"]]
    assert corrs[0] == (0, 0), "首视野校正必须固定为零"
    assert corrs[1] == (-2, 0), f"B 区漂移校正应为 (-2,0)，实际 {corrs[1]}"
    assert rev["fields"][1]["originalShift"] == {"x": 10, "y": 0}
    assert rev["fields"][1]["correctedShift"] == {"x": 8, "y": 0}
    assert rev["registration"]["disconnectedFields"] == []
    # 三级优化目标的客观值可自动核对
    obj = rev["registration"]["objective"]
    assert obj["maxResidual"] == 1
    assert obj["totalCorrectionManhattan"] == 2
    # 珠位证据
    b1 = next(b for b in rev["registration"]["beads"] if b["beadId"] == "B1")
    assert b1["usedInConstraints"] is True and b1["occurrenceCount"] == 3
    assert all(pr["withinLimit"] for pr in b1["pairResiduals"])
    # 校正后重跑去重：3 个 PE 观测全部重合为 1 个最终颗粒
    dd = rev["deduplication"]
    assert dd["summary"]["finalParticleCount"] == 1
    obs = dd["finalParticles"][0]["observations"]
    assert {o["particleId"] for o in obs} == {"P1", "Q1", "R1"}
    assert all(o["filter"] == {"x": 10, "y": 10} for o in obs)
    print("  [OK] POST 合法配准复核 -> 200，B 区校正 (-2,0)、最大残差 1、"
          "校正后 3 观测合并为 1 颗粒，珠位证据/原平移/校正平移齐备")

    # last 接口：成功结果可经 GET 自动核对
    r = httpx.get(f"{WEB_URL}/api/registration-reviews/last", timeout=5)
    assert r.status_code == 200, "成功复核后应可取回上次结果"
    last = r.json()
    assert last == rev, "GET last 必须返回与本次成功复核完全一致的结果"
    print("  [OK] GET /api/registration-reviews/last -> 200，"
          "结果与成功复核完全一致，可自动核对")

    # 断连视野：D 区只有独占珠，校正固定零并明确标出
    review_disc = json.loads(json.dumps(review_good))
    review_disc["fields"].append({
        "name": "D区", "shift": {"x": 20, "y": 10}, "correctionRange": 2,
        "beads": [{"id": "B8", "x": 200, "y": 200}],
        "particles": [{"id": "S1", "x": 0, "y": 0, "category": "PE"}]})
    r = httpx.post(f"{WEB_URL}/api/registration-reviews",
                   json=review_disc, timeout=10)
    assert r.status_code == 200, r.text
    rev_disc = r.json()
    assert rev_disc["registration"]["disconnectedFields"] == [3]
    assert rev_disc["fields"][3]["connectedToFirst"] is False
    assert rev_disc["fields"][3]["correction"] == {"x": 0, "y": 0}
    b8 = next(b for b in rev_disc["registration"]["beads"] if b["beadId"] == "B8")
    assert b8["usedInConstraints"] is False
    print("  [OK] POST 含断连视野的复核 -> 200，D区校正固定零、disconnectedFields=[3]、"
          "独占珠 B8 不参与约束")

    # 断连样例也成功 → last 已更新；下面验证失败不覆盖
    last_ok = rev_disc

    # 不存在可行配准（范围 0、漂移 3、上限 0）
    review_bad = {
        "tolerance": 1,
        "beadResidualLimit": 0,
        "fields": [
            {"name": "A", "shift": {"x": 0, "y": 0}, "correctionRange": 0,
             "beads": [{"id": "K", "x": 0, "y": 0}], "particles": []},
            {"name": "B", "shift": {"x": 3, "y": 0}, "correctionRange": 0,
             "beads": [{"id": "K", "x": 0, "y": 0}], "particles": []},
            {"name": "C", "shift": {"x": 0, "y": 0}, "correctionRange": 0,
             "beads": [], "particles": []},
        ],
    }
    r = httpx.post(f"{WEB_URL}/api/registration-reviews",
                   json=review_bad, timeout=5)
    assert r.status_code == 422, f"不可行配准应返回 422，实际 {r.status_code}"
    err = r.json()
    assert any(e["loc"] == "registration" and "K" in e["message"]
               for e in err["errors"]), "不可行必须定位到相关校准珠"
    assert err["draft"] == review_bad, "不可行时草稿必须原样回显"
    # 上一次成功结果原封不动
    r = httpx.get(f"{WEB_URL}/api/registration-reviews/last", timeout=5)
    assert r.status_code == 200 and r.json() == last_ok, \
        "复核失败不得覆盖上一次成功复核结果"
    print("  [OK] POST 不存在可行配准 -> 422、定位到校准珠 K、草稿原样回显，"
          "上一次成功结果未被覆盖")

    # 校验类不合规：珠重复 / 坐标非整数 / 范围越界 / 残差上限为负
    review_invalid = {
        "tolerance": 1,
        "beadResidualLimit": -1,
        "fields": [
            {"name": "A", "shift": {"x": 0, "y": 0}, "correctionRange": 9,
             "beads": [{"id": "K", "x": 0, "y": 0},
                       {"id": "K", "x": 1.5, "y": 0}], "particles": []},
            {"name": "B", "shift": {"x": 0, "y": 0}, "correctionRange": 2,
             "beads": [], "particles": []},
            {"name": "C", "shift": {"x": 0, "y": 0}, "correctionRange": 2,
             "beads": [], "particles": []},
        ],
    }
    r = httpx.post(f"{WEB_URL}/api/registration-reviews",
                   json=review_invalid, timeout=5)
    assert r.status_code == 422
    locs = {e["loc"] for e in r.json()["errors"]}
    assert {"beadResidualLimit", "fields[0].correctionRange",
            "fields[0].beads[1].id", "fields[0].beads[1].x"} <= locs
    assert r.json()["draft"] == review_invalid
    r = httpx.get(f"{WEB_URL}/api/registration-reviews/last", timeout=5)
    assert r.json() == last_ok, "校验失败同样不得覆盖上一次成功复核结果"
    print("  [OK] POST 珠重复/非整数坐标/范围越界/负上限 -> 422 逐字段定位、"
          "草稿回显，上一次成功结果仍未覆盖")

    print("== API 冒烟验收全部通过 ==")


if __name__ == "__main__":
    main()
