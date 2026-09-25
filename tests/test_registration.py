"""配准复核测试：校验、联合整数校正求解、三级优化目标、断连视野、
不可行配准与去重重跑，以及 API 层的成功保留/失败不覆盖语义。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.dedup import ValidationError, solve_request
from app.main import app
from app.registration import (
    get_last_review,
    reset_last_review,
    review_request,
    validate_review,
)


@pytest.fixture(autouse=True)
def _reset_last_review():
    reset_last_review()
    yield
    reset_last_review()


def bead(bid, x, y):
    return {"id": bid, "x": x, "y": y}


def fld(name, sx, sy, rng, beads, particles=None):
    return {
        "name": name,
        "shift": {"x": sx, "y": sy},
        "correctionRange": rng,
        "beads": beads,
        "particles": particles or [],
    }


def particle(pid, x, y, cat):
    return {"id": pid, "x": x, "y": y, "category": cat}


def payload(fields, tol=1, limit=1):
    return {"tolerance": tol, "beadResidualLimit": limit, "fields": fields}


def corr_of(res):
    return [(f["correction"]["x"], f["correction"]["y"]) for f in res["fields"]]


# --------------------------------------------------------------------------- #
# 端到端：校正求解 + 校正平移重跑去重
# --------------------------------------------------------------------------- #
def test_review_corrects_drift_and_reruns_dedup():
    # B 区登记平移 (10,0) 漂移了 2 格（真实应为 (8,0)）：
    # 共享珠 B1 在 L=1、R=2 下强制 c_B=(-2,0)，校正后三个 PE 观测重合。
    p = payload(
        [
            fld("A", 0, 0, 2, [bead("B1", 50, 50)],
                [particle("P", 10, 10, "PE")]),
            fld("B", 10, 0, 2, [bead("B1", 43, 50)],
                [particle("Q", 2, 10, "PE")]),
            fld("C", 10, 10, 2, [bead("B1", 40, 40)],
                [particle("R", 0, 0, "PE")]),
        ],
        tol=1, limit=1,
    )
    # 既有去重（原平移）：P-R 重合合并，Q 距离 2 超出容差，共 2 个最终颗粒
    original = solve_request(p)
    assert original["summary"]["finalParticleCount"] == 2

    res = review_request(p)
    # 首视野校正固定为零；B 区 -2，C 区由曼哈顿目标取 0
    assert corr_of(res) == [(0, 0), (-2, 0), (0, 0)]
    fields = {f["fieldName"]: f for f in res["fields"]}
    assert fields["B"]["originalShift"] == {"x": 10, "y": 0}
    assert fields["B"]["correctedShift"] == {"x": 8, "y": 0}
    assert all(f["connectedToFirst"] for f in res["fields"])
    assert res["registration"]["disconnectedFields"] == []

    # 校正后重跑去重：三颗全部合并
    dd = res["deduplication"]
    assert dd["summary"]["finalParticleCount"] == 1
    obs = dd["finalParticles"][0]["observations"]
    assert {o["particleId"] for o in obs} == {"P", "Q", "R"}
    for o in obs:  # 校正后滤膜坐标全部重合于 (10,10)
        assert o["filter"] == {"x": 10, "y": 10}

    # 珠位证据：B1 出现 3 次、参与约束、成对残差均 ≤ 上限
    b1 = next(b for b in res["registration"]["beads"] if b["beadId"] == "B1")
    assert b1["usedInConstraints"] is True
    assert b1["occurrenceCount"] == 3
    assert len(b1["pairResiduals"]) == 3
    assert all(pr["withinLimit"] for pr in b1["pairResiduals"])
    assert (b1["maxDeltaX"], b1["maxDeltaY"]) == (1, 0)
    assert b1["withinLimit"] is True
    # 证据同时给出原登记/校正后滤膜坐标
    b_obs = next(o for o in b1["observations"] if o["fieldName"] == "B")
    assert b_obs["unregisteredFilter"] == {"x": 53, "y": 50}
    assert b_obs["filter"] == {"x": 51, "y": 50}


def test_first_field_correction_is_always_zero():
    # 首视野即使可以用非零校正进一步降残差，也必须固定为零
    p = payload(
        [
            fld("A", 0, 0, 2, [bead("K", 0, 0)]),
            fld("B", 3, 0, 3, [bead("K", 0, 0)]),
            fld("C", 0, 0, 2, []),
        ],
        tol=0, limit=5,
    )
    res = review_request(p)
    assert corr_of(res)[0] == (0, 0)
    # bx = 0-(0+3) = -3，故 cB=(-3,0) 使残差归零
    assert corr_of(res)[1] == (-3, 0)
    assert res["registration"]["objective"]["maxResidual"] == 0


def test_min_max_residual_beats_manhattan_sum():
    # bx=3, L=5：c_B=0（曼哈顿 0）时残差 3；c_B=-3 时残差 0（曼哈顿 3）。
    # 第一目标（最大残差最小）压倒第二目标。
    p = payload(
        [
            fld("A", 0, 0, 5, [bead("K", 0, 0)]),
            fld("B", 3, 0, 5, [bead("K", 0, 0)]),
            fld("C", 0, 0, 5, [bead("Z", 9, 9)]),  # 独占珠，不参与约束
        ],
        tol=0, limit=5,
    )
    res = review_request(p)
    assert corr_of(res)[1] == (-3, 0)
    assert res["registration"]["objective"]["maxResidual"] == 0
    assert res["registration"]["objective"]["totalCorrectionManhattan"] == 3


def test_manhattan_sum_is_second_objective():
    # 两珠 K1/K2：A-B 基础滤膜差分别为 0 与 1，L=1、R=1。
    #   c_B=0：残差 (0, 1)；c_B=1：残差 (1, 0)；c_B=-1：K2 残差 2，不可行。
    # c_B ∈ {0, 1} 都达到最大残差 1（第一目标持平），第二目标取曼哈顿更小的 0。
    p = payload(
        [
            fld("A", 0, 0, 1, [bead("K1", 0, 0), bead("K2", 10, 0)]),
            fld("B", 0, 0, 1, [bead("K1", 0, 0), bead("K2", 9, 0)]),
            fld("C", 0, 0, 1, []),
        ],
        tol=0, limit=1,
    )
    res = review_request(p)
    assert corr_of(res)[1] == (0, 0)
    assert res["registration"]["objective"]["maxResidual"] == 1


def test_third_objective_lexicographic_sequence():
    # 三视野链，L=1、R=1，多个方案残差与曼哈顿相同：
    # B01: f0(0)-f1(0)=0；B02: f0(5)-f2(5)=0；B12: f1(2)-f2(0)=2
    # 可行解 (c1,c2) = (-1,0) 与 (0,1)，最大残差均 1、曼哈顿均 1 →
    # 录入顺序校正序列 ((0,0),(-1,0),(0,0)) 字典序更小。
    p = payload(
        [
            fld("A", 0, 0, 1, [bead("B01", 0, 0)]),
            fld("B", 0, 0, 1, [bead("B01", 0, 0), bead("B12", 2, 0)]),
            fld("C", 0, 0, 1, [bead("B02", 5, 0), bead("B12", 0, 0)]),
        ],
        tol=0, limit=1,
    )
    res = review_request(p)
    seq = [(c["x"], c["y"]) for c in res["registration"]["objective"]["tieBreakSequence"]]
    assert seq == [(0, 0), (-1, 0), (0, 0)]
    assert corr_of(res) == [(0, 0), (-1, 0), (0, 0)]


def test_solution_is_deterministic():
    p = payload(
        [
            fld("A", 0, 0, 2, [bead("K", 0, 0)]),
            fld("B", 2, 1, 2, [bead("K", 0, 0)]),
            fld("C", 2, 2, 2, [bead("K", 0, 0)]),
        ],
        tol=0, limit=3,
    )
    r1 = review_request(p)
    r2 = review_request(p)
    assert corr_of(r1) == corr_of(r2)


def test_single_occurrence_bead_does_not_constrain():
    p = payload(
        [
            fld("A", 0, 0, 0, [bead("only", 100, 100)]),
            fld("B", 5, 5, 0, [bead("other", 0, 0)]),
            fld("C", 0, 0, 0, []),
        ],
        tol=0, limit=0,
    )  # 范围全 0、上限 0，但无共享珠 → 可行，全部校正为零
    res = review_request(p)
    assert corr_of(res) == [(0, 0), (0, 0), (0, 0)]
    single = [b for b in res["registration"]["beads"]]
    assert {b["beadId"]: b["usedInConstraints"] for b in single} == {
        "only": False, "other": False,
    }
    assert res["summary"]["usedBeadCount"] == 0
    assert res["summary"]["singleOccurrenceBeadCount"] == 2


def test_disconnected_fields_are_reported_with_zero_correction():
    # C-D 共享一颗珠，但与首视野 A-B 组完全无共享珠
    p = payload(
        [
            fld("A", 0, 0, 2, [bead("K1", 0, 0)]),
            fld("B", 1, 0, 2, [bead("K1", 0, 0)]),
            fld("C", 8, 8, 2, [bead("K9", 0, 0)]),
            fld("D", 8, 8, 2, [bead("K9", 0, 0)]),
        ],
        tol=0, limit=1,
    )
    res = review_request(p)
    assert res["registration"]["disconnectedFields"] == [2, 3]
    assert corr_of(res)[2:] == [(0, 0), (0, 0)]
    c_info = res["fields"][2]
    assert c_info["connectedToFirst"] is False
    # B 仍由 A-B 组正常求出校正（bx=-1 → cB=(-1,0)）
    assert corr_of(res)[1] == (-1, 0)


# --------------------------------------------------------------------------- #
# 校验：校准珠重复 / 坐标非整数 / 范围不合规
# --------------------------------------------------------------------------- #
def _locs(p):
    with pytest.raises(ValidationError) as ei:
        validate_review(p)
    return [e["loc"] for e in ei.value.errors], ei.value


def test_duplicate_bead_id_in_same_field_is_localizable():
    p = payload([
        fld("A", 0, 0, 2, [bead("K", 0, 0), bead("K", 1, 1)], []),
        fld("B", 0, 0, 2, [bead("K", 0, 0)], []),
        fld("C", 0, 0, 2, [], []),
    ])
    locs, exc = _locs(p)
    assert "fields[0].beads[1].id" in locs
    assert exc.draft is p  # 草稿原样保留


def test_duplicate_bead_coords_in_same_field_is_localizable():
    p = payload([
        fld("A", 0, 0, 2, [bead("K1", 0, 0), bead("K2", 0, 0)], []),
        fld("B", 0, 0, 2, [], []),
        fld("C", 0, 0, 2, [], []),
    ])
    locs, _ = _locs(p)
    assert "fields[0].beads[1]" in locs


def test_non_integer_bead_coords_and_bool_are_rejected():
    p = {
        "tolerance": 1,
        "beadResidualLimit": 1,
        "fields": [
            {"name": "A", "shift": {"x": 0, "y": 0}, "correctionRange": 2,
             "beads": [{"id": "K", "x": 1.5, "y": True}], "particles": []},
            fld("B", 0, 0, 2, [], []),
            fld("C", 0, 0, 2, [], []),
        ],
    }
    locs, _ = _locs(p)
    assert "fields[0].beads[0].x" in locs
    assert "fields[0].beads[0].y" in locs  # bool 不算整数


def test_correction_range_must_be_0_to_5_integer():
    base_fields = lambda r: [
        fld("A", 0, 0, r, [], []),
        fld("B", 0, 0, 2, [], []),
        fld("C", 0, 0, 2, [], []),
    ]
    for bad in (6, -1, 1.5, "2", True, None):
        locs, _ = _locs(payload(base_fields(bad)))
        assert "fields[0].correctionRange" in locs, bad
    # 边界 0 与 5 合规
    assert validate_review(payload(base_fields(0))) is not None
    assert validate_review(payload(base_fields(5))) is not None


def test_bead_residual_limit_must_be_nonnegative_number():
    p = payload([fld("A", 0, 0, 0, []), fld("B", 0, 0, 0, []),
                 fld("C", 0, 0, 0, [])], limit=-1)
    locs, _ = _locs(p)
    assert "beadResidualLimit" in locs
    p["beadResidualLimit"] = "1"
    locs, _ = _locs(p)
    assert "beadResidualLimit" in locs


def test_beads_must_be_list_and_within_size_limit():
    p = payload([
        {"name": "A", "shift": {"x": 0, "y": 0}, "correctionRange": 2,
         "beads": {"id": "K"}, "particles": []},
        fld("B", 0, 0, 2, [], []),
        fld("C", 0, 0, 2, [], []),
    ])
    locs, _ = _locs(p)
    assert "fields[0].beads" in locs

    too_many = [bead(f"K{i}", i, 0) for i in range(31)]
    p2 = payload([
        fld("A", 0, 0, 2, too_many, []),
        fld("B", 0, 0, 2, [], []),
        fld("C", 0, 0, 2, [], []),
    ])
    locs, _ = _locs(p2)
    assert any(loc == "fields[0].beads" for loc in locs)


def test_base_particle_validation_still_applies():
    # 复核请求同时复用既有颗粒校验：视野不足 + 颗粒坐标非整数 + 珠编号缺失
    p = {
        "tolerance": 1,
        "beadResidualLimit": 1,
        "fields": [
            {"name": "A", "shift": {"x": 0, "y": 0}, "correctionRange": 2,
             "beads": [{"id": "", "x": 0, "y": 0}],
             "particles": [{"id": "P", "x": 1.2, "y": 0, "category": "PE"}]},
            fld("B", 0, 0, 2, [], []),
        ],
    }
    locs, _ = _locs(p)
    assert "fields" in locs
    assert "fields[0].beads[0].id" in locs
    assert "fields[0].particles[0].x" in locs


# --------------------------------------------------------------------------- #
# 不存在可行配准
# --------------------------------------------------------------------------- #
def test_infeasible_registration_raises_localizable_error():
    # 范围 0 无法吸收 3 格漂移，上限 1
    p = payload(
        [
            fld("A", 0, 0, 0, [bead("K", 0, 0)]),
            fld("B", 3, 0, 0, [bead("K", 0, 0)]),
            fld("C", 0, 0, 0, []),
        ],
        limit=1,
    )
    with pytest.raises(ValidationError) as ei:
        review_request(p)
    assert all(e["loc"] == "registration" for e in ei.value.errors)
    assert "K" in ei.value.errors[0]["message"]
    assert ei.value.draft is p


def test_joint_infeasibility_is_detected_even_if_each_pair_is_fine():
    # 四视野环，每条边一颗共享珠，L=0（残差必须严格为零）、R=1：
    #   KAB: A(0,0)-B(0,0) 基础差 0 → cB-cA = 0
    #   KBC: B(5,0)-C(5,0) 基础差 0 → cC-cB = 0
    #   KCD: C(0,0)-D(0,0) 基础差 0 → cD-cC = 0
    #   KDA: A(0,5) 滤膜 5；D(1,5) 滤膜 6，基础差 -1 → cD-cA = 1
    # 前三条推出 cA=cB=cC=cD，与第四条矛盾。任意单一珠对都存在可行
    # 整数校正（域宽 ±1 足够），但四环联合无解 → 判为不存在可行配准。
    p = payload(
        [
            fld("A", 0, 0, 1, [bead("KAB", 0, 0), bead("KDA", 0, 5)]),
            fld("B", 0, 0, 1, [bead("KAB", 0, 0), bead("KBC", 5, 0)]),
            fld("C", 0, 0, 1, [bead("KBC", 5, 0), bead("KCD", 0, 0)]),
            fld("D", 0, 0, 1, [bead("KCD", 0, 0), bead("KDA", 1, 5)]),
        ],
        tol=0, limit=0,
    )
    with pytest.raises(ValidationError) as ei:
        review_request(p)
    assert ei.value.errors[0]["loc"] == "registration"
    assert "不存在可行配准" in ei.value.errors[0]["message"]


def test_disconnected_component_with_zero_correction_contradiction_is_infeasible():
    # C、D 互链但与首视野断连（校正只能为零），零校正下残差 3 > L=1
    p = payload(
        [
            fld("A", 0, 0, 2, []),
            fld("B", 0, 0, 2, []),
            fld("C", 3, 0, 2, [bead("K9", 0, 0)]),
            fld("D", 0, 0, 2, [bead("K9", 0, 0)]),
        ],
        limit=1,
    )
    with pytest.raises(ValidationError) as ei:
        review_request(p)
    msgs = " ".join(e["message"] for e in ei.value.errors)
    assert "K9" in msgs and "首视野" in msgs


# --------------------------------------------------------------------------- #
# API：成功保留、失败不覆盖、草稿回显、last 接口
# --------------------------------------------------------------------------- #
client = TestClient(app)


def _good_review_payload():
    return payload(
        [
            fld("A", 0, 0, 2, [bead("B1", 50, 50)],
                [particle("P", 10, 10, "PE")]),
            fld("B", 10, 0, 2, [bead("B1", 43, 50)],
                [particle("Q", 2, 10, "PE")]),
            fld("C", 10, 10, 2, [bead("B1", 40, 40)],
                [particle("R", 0, 0, "PE")]),
        ],
        tol=1, limit=1,
    )


def test_last_review_404_before_any_success():
    r = client.get("/api/registration-reviews/last")
    assert r.status_code == 404


def test_review_api_success_is_retrievable_and_autocheckable():
    body = _good_review_payload()
    r = client.post("/api/registration-reviews", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["registration"]["feasible"] is True
    assert [(f["correction"]["x"], f["correction"]["y"])
            for f in data["fields"]] == [(0, 0), (-2, 0), (0, 0)]
    assert data["deduplication"]["summary"]["finalParticleCount"] == 1
    assert data["summary"]["disconnectedFieldCount"] == 0

    r2 = client.get("/api/registration-reviews/last")
    assert r2.status_code == 200
    assert r2.json() == data  # 成功结果可经 GET 自动核对


def test_review_api_failure_keeps_draft_and_does_not_overwrite_last():
    # 先成功一次
    good = _good_review_payload()
    assert client.post("/api/registration-reviews", json=good).status_code == 200
    last_before = client.get("/api/registration-reviews/last").json()

    # 再发一个不存在可行配准的草稿
    bad = payload(
        [
            fld("A", 0, 0, 0, [bead("K", 0, 0)]),
            fld("B", 9, 0, 0, [bead("K", 0, 0)]),
            fld("C", 0, 0, 0, []),
        ],
        limit=0,
    )
    r = client.post("/api/registration-reviews", json=bad)
    assert r.status_code == 422
    err = r.json()
    assert any(e["loc"] == "registration" for e in err["errors"])
    assert err["draft"] == bad  # 草稿原样回显

    # 上一次成功结果原封不动
    last_after = client.get("/api/registration-reviews/last").json()
    assert last_after == last_before
    assert get_last_review() is last_after or get_last_review() == last_before


def test_review_api_validation_failure_returns_localizable_fields():
    bad = payload([
        fld("A", 0, 0, 2, [bead("K", 0, 0), bead("K", 1, 1)], []),
        fld("B", 0, 0, 9, [], []),  # 范围越界
        fld("C", 0, 0, 2, [], []),
    ])
    r = client.post("/api/registration-reviews", json=bad)
    assert r.status_code == 422
    locs = {e["loc"] for e in r.json()["errors"]}
    assert "fields[0].beads[1].id" in locs
    assert "fields[1].correctionRange" in locs


def test_review_api_malformed_json():
    r = client.post("/api/registration-reviews",
                    content="{oops", headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert r.json()["errors"][0]["loc"] == "$"


# --------------------------------------------------------------------------- #
# 暴力 oracle：穷举所有整数校正方案，按三级目标求全局最优后与求解器核对
# --------------------------------------------------------------------------- #
def test_registration_matches_brute_force_oracle_on_random_instances():
    import itertools
    import random

    from app.registration import solve_registration

    random.seed(20260925)
    for trial in range(120):
        nf = random.randint(3, 4)
        rngs = [random.randint(0, 1) for _ in range(nf)]
        shifts = [(random.randint(-2, 2), random.randint(-2, 2))
                  for _ in range(nf)]
        # 随机生成 2–4 颗共享珠（每颗出现在 2..nf 个视野）
        bead_placements = {}  # bid -> {field: (x, y)}
        for k in range(random.randint(2, 4)):
            bid = f"B{k}"
            occ_fields = random.sample(range(nf), random.randint(2, nf))
            anchor_x, anchor_y = random.randint(0, 4), random.randint(0, 4)
            placements = {}
            for fi in occ_fields:
                # 局部坐标围绕「锚点 - 平移」抖动，制造可行/不可行两种情形
                jitter_x = random.randint(-2, 2)
                jitter_y = random.randint(-2, 2)
                placements[fi] = (anchor_x - shifts[fi][0] + jitter_x,
                                  anchor_y - shifts[fi][1] + jitter_y)
            bead_placements[bid] = placements

        limit = random.choice([0, 1, 2])
        fields = []
        for fi in range(nf):
            beads_ = []
            for bid, placements in bead_placements.items():
                if fi in placements:
                    xy = placements[fi]
                    beads_.append(bead(bid, xy[0], xy[1]))
            fields.append(fld(f"F{fi}", shifts[fi][0], shifts[fi][1],
                              rngs[fi], beads_, []))
        p = payload(fields, tol=0, limit=limit)
        try:
            prepared = validate_review(p)
        except ValidationError:
            continue  # 随机抖动碰巧产生同视野重复坐标等不合规输入，跳过
        corr, disc, evidence, objective, fatal = solve_registration(prepared)

        # 视野图（与实现相同的连通定义）
        adj = {i: set() for i in range(nf)}
        for placements in bead_placements.values():
            fs = sorted(placements)
            for a in range(len(fs)):
                for b in range(a + 1, len(fs)):
                    adj[fs[a]].add(fs[b])
                    adj[fs[b]].add(fs[a])
        seen, main = {0}, [0]
        while main:
            v = main.pop()
            for w in adj[v]:
                if w not in seen:
                    seen.add(w)
                    main.append(w)
        expected_disc = sorted(i for i in range(nf) if i not in seen)

        # 断连分量校正固定为零：零校正下内部珠残差超限即整体不可行
        disconnected_feasible = True
        for bid, placements in bead_placements.items():
            fs = sorted(placements)
            for a in range(len(fs)):
                for b in range(a + 1, len(fs)):
                    fi, fj = fs[a], fs[b]
                    if fi in seen or fj in seen:
                        continue
                    xi, yi = placements[fi]
                    xj, yj = placements[fj]
                    dx = xi + shifts[fi][0] - (xj + shifts[fj][0])
                    dy = yi + shifts[fi][1] - (yj + shifts[fj][1])
                    if abs(dx) > limit or abs(dy) > limit:
                        disconnected_feasible = False

        # 穷举主分量内全部校正方案
        free = [i for i in range(nf) if i in seen and i != 0]
        grids = {i: [(cx, cy) for cx in range(-rngs[i], rngs[i] + 1)
                     for cy in range(-rngs[i], rngs[i] + 1)] for i in free}
        best = None  # (maxres, manhattan, sequence, corr)
        for combo in itertools.product(*(grids[i] for i in free)):
            cand = {0: (0, 0)}
            cand.update(dict(zip(free, combo)))
            for i in range(nf):
                cand.setdefault(i, (0, 0))
            maxres = 0
            ok = True
            for bid, placements in bead_placements.items():
                fs = sorted(placements)
                for a in range(len(fs)):
                    for b in range(a + 1, len(fs)):
                        fi, fj = fs[a], fs[b]
                        if fi not in seen or fj not in seen:
                            continue  # 断连分量校正固定零，单独处理
                        xi, yi = placements[fi]
                        xj, yj = placements[fj]
                        dx = (xi + shifts[fi][0] + cand[fi][0]
                              - (xj + shifts[fj][0] + cand[fj][0]))
                        dy = (yi + shifts[fi][1] + cand[fi][1]
                              - (yj + shifts[fj][1] + cand[fj][1]))
                        maxres = max(maxres, abs(dx), abs(dy))
                        if maxres > limit:
                            ok = False
            if not ok:
                continue
            man = sum(abs(cx) + abs(cy) for cx, cy in cand.values())
            seq = tuple(cand[i] for i in range(nf))
            key = (maxres, man, seq)
            if best is None or key < best[0]:
                best = (key, dict(cand))

        if best is None or not disconnected_feasible:
            assert fatal, f"trial {trial}: 暴力判定不可行，求解器却给解"
            continue
        assert not fatal, f"trial {trial}: 暴力判定可行，求解器报不可行"
        assert sorted(disc) == expected_disc, trial
        got_seq = tuple((objective["tieBreakSequence"][i]["x"],
                         objective["tieBreakSequence"][i]["y"])
                        for i in range(nf))
        assert objective["maxResidual"] == best[0][0], trial
        assert objective["totalCorrectionManhattan"] == best[0][1], trial
        assert got_seq == best[0][2], (trial, got_seq, best[0][2])
        for i in range(nf):
            assert corr[i] == best[1][i], trial

        # 证据中的每颗珠：参与标记与残差上限判定必须自洽
        for ev in evidence or []:
            for pr in ev["pairResiduals"]:
                assert pr["withinLimit"] == (
                    pr["deltaX"] <= limit and pr["deltaY"] <= limit)
