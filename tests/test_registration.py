"""配准复核测试：校准珠校验、联合校正量求解、三级目标、珠位证据与去重重跑。"""

from __future__ import annotations

import pytest

from app.dedup import ValidationError
from app.registration import solve_review_request


def bead(bid, x, y):
    return {"id": bid, "x": x, "y": y}


def fld(name, shift, particles, beads=None, rng=5):
    return {
        "name": name,
        "shift": {"x": shift[0], "y": shift[1]},
        "correctionRange": rng,
        "particles": particles,
        "calibrationBeads": list(beads or []),
    }


def p(pid, x, y, cat):
    return {"id": pid, "x": x, "y": y, "category": cat}


def payload(fields, tol=2, limit=0):
    return {
        "tolerance": tol,
        "beadResidualLimit": limit,
        "fields": fields,
    }


def corrections(res):
    return [(c["fieldIndex"], c["correction"]["x"], c["correction"]["y"])
            for c in res["registrationReview"]["corrections"]]


# --------------------------------------------------------------------------- #
# 基本求解
# --------------------------------------------------------------------------- #
def test_first_field_correction_is_fixed_zero():
    pl = payload(
        [
            fld("A", (0, 0), [], [bead("B1", 5, 5)], rng=3),
            fld("B", (10, 0), [], [bead("B1", -4, 5)], rng=3),
            fld("C", (10, 10), [], [bead("B1", -5, -4)], rng=3),
        ],
        limit=0,
    )
    res = solve_review_request(pl)
    corr = corrections(res)
    assert corr[0] == (0, 0, 0)            # 首视野恒为零
    assert corr[1] == (1, -1, 0)
    assert corr[2] == (2, 0, -1)
    s = res["registrationReview"]["summary"]
    assert s["maxCoordinateResidual"] == 0
    assert s["maxResidualX"] == 0
    assert s["maxResidualY"] == 0
    assert s["totalCorrectionManhattan"] == 2
    assert s["sharedBeadCount"] == 1
    assert s["singletonBeadCount"] == 0
    assert s["unconnectedFieldCount"] == 0


def test_first_field_never_moves_even_if_alignment_requires_it():
    # f1 校正范围为 0 无法补偿 2 格漂移；首视野又固定为零 → 不存在可行配准
    pl = payload(
        [
            fld("A", (0, 0), [], [bead("B1", 0, 0)], rng=5),
            fld("B", (2, 0), [], [bead("B1", 0, 0)], rng=0),
            fld("C", (0, 0), [], [bead("B1", 0, 0)], rng=0),
        ],
        limit=0,
    )
    with pytest.raises(ValidationError) as ei:
        solve_review_request(pl)
    assert any(e["loc"] == "beadResidualLimit" for e in ei.value.errors)
    assert ei.value.draft is pl


def test_corrected_shift_re_runs_dedup_and_merges_particles():
    # 容差 0：按登记平移 Q1 在 (11,10)，与 P1(10,10) 差 1 格不合并；
    # 校准珠证据要求 B 视野校正 (-1,0)，校正后重新去重才合并为同一颗粒。
    pl = payload(
        [
            fld("A", (0, 0), [p("P1", 10, 10, "PE")], [bead("B1", 5, 5)]),
            fld("B", (11, 0), [p("Q1", 0, 10, "PE")], [bead("B1", -5, 5)]),
            fld("C", (10, 11), [p("R1", 0, 0, "PE")], [bead("B1", -5, -5)]),
        ],
        tol=0, limit=0,
    )
    res = solve_review_request(pl)
    assert corrections(res) == [(0, 0, 0), (1, -1, 0), (2, 0, -1)]
    assert res["summary"]["finalParticleCount"] == 1
    fp = res["finalParticles"][0]
    assert {o["particleId"] for o in fp["observations"]} == {"P1", "Q1", "R1"}
    # 校正后平移与原平移同时给出，便于自动核对
    c1 = res["registrationReview"]["corrections"][1]
    assert c1["originalShift"] == {"x": 11, "y": 0}
    assert c1["correction"] == {"x": -1, "y": 0}
    assert c1["correctedShift"] == {"x": 10, "y": 0}
    # 观测滤膜坐标按校正后平移计算
    q1 = next(o for o in fp["observations"] if o["particleId"] == "Q1")
    assert q1["filter"] == {"x": 10, "y": 10}


def test_singleton_beads_do_not_constrain():
    pl = payload(
        [
            fld("A", (0, 0), [], [bead("B1", 0, 0), bead("ONLY-A", 9, 9)]),
            fld("B", (0, 0), [], [bead("B1", 0, 0), bead("ONLY-B", 3, 3)]),
            fld("C", (0, 0), [], [bead("B1", 0, 0)]),
        ],
        limit=0,
    )
    res = solve_review_request(pl)
    s = res["registrationReview"]["summary"]
    assert s["sharedBeadCount"] == 1
    assert s["singletonBeadCount"] == 2
    single = [b for b in res["registrationReview"]["beadEvidence"]
              if not b["participates"]]
    assert {b["beadId"] for b in single} == {"ONLY-A", "ONLY-B"}
    for b in single:
        assert b["occurrenceCount"] == 1
        assert b["pairwiseResiduals"] == []
        # 单次珠出现记录与共享珠字段形态一致，只是不参与约束
        occ = b["occurrences"][0]
        assert set(occ) >= {
            "fieldIndex", "fieldName", "local", "registeredShift",
            "registeredFilter", "correction", "correctedShift", "filter",
        }


def test_shared_beads_may_chain_indirectly_to_first_field():
    # B1 连接 A-B；B2 只连接 B-C（没有任何一颗珠同时出现在 A、C）。
    # C 经 B 链式连到首视野，校正量仍可联合确定。
    pl = payload(
        [
            fld("A", (0, 0), [], [bead("B1", 0, 0)]),
            fld("B", (0, 0), [], [bead("B1", 1, 0), bead("B2", 0, 0)]),
            fld("C", (0, 0), [], [bead("B2", 0, 1)]),
        ],
        limit=0,
    )
    res = solve_review_request(pl)
    assert corrections(res) == [(0, 0, 0), (1, -1, 0), (2, -1, -1)]


def test_unconnected_fields_are_reported_individually():
    pl = payload(
        [
            fld("A", (0, 0), [], [bead("B1", 0, 0)]),
            fld("B", (0, 0), [], [bead("B2", 0, 0)]),
            fld("C", (0, 0), [], [bead("B3", 0, 0), bead("B4", 1, 1)]),
        ],
        limit=1,
    )
    with pytest.raises(ValidationError) as ei:
        solve_review_request(pl)
    locs = {e["loc"] for e in ei.value.errors}
    assert locs == {"fields[1].calibrationBeads", "fields[2].calibrationBeads"}
    # 错误信息须明确指出无法连到首视野
    assert all("首视野" in e["message"] for e in ei.value.errors)
    assert ei.value.draft is pl


def test_no_beads_at_all_is_infeasible():
    pl = payload(
        [fld("A", (0, 0), []), fld("B", (0, 0), []), fld("C", (0, 0), [])],
        limit=1,
    )
    with pytest.raises(ValidationError) as ei:
        solve_review_request(pl)
    assert {e["loc"] for e in ei.value.errors} == {
        "fields[1].calibrationBeads", "fields[2].calibrationBeads"
    }


# --------------------------------------------------------------------------- #
# 三级优化目标
# --------------------------------------------------------------------------- #
def test_objective_one_min_max_residual_beats_zero_manhattan():
    # 不校正时残差 2（曼哈顿和 0）；校正 2 格后残差 0（曼哈顿和 2）。
    # 第一目标是最大坐标残差最小，故必须付出校正量。
    pl = payload(
        [
            fld("A", (0, 0), [], [bead("B1", 0, 0)]),
            fld("B", (0, 0), [], [bead("B1", -2, 0)]),
            fld("C", (0, 0), [], [bead("B1", -2, 0)]),
        ],
        limit=5,
    )
    res = solve_review_request(pl)
    assert corrections(res)[1] == (1, 2, 0)
    assert res["registrationReview"]["summary"]["maxCoordinateResidual"] == 0


def test_objective_two_min_manhattan_within_min_residual():
    # f0 珠位 (0,0)、(2,0)；f1/f2 珠位 (0,0)、(1,0)（局部坐标互不相同）。
    # 校正 cx=0 时残差为 0、1（最大 1，曼哈顿 0）；cx=1 时为 1、0
    # （最大 1，曼哈顿 1）。最大残差相同 → 第二目标取曼哈顿更小的 cx=0。
    pl = payload(
        [
            fld("A", (0, 0), [], [bead("B1", 0, 0), bead("B2", 2, 0)]),
            fld("B", (0, 0), [], [bead("B1", 0, 0), bead("B2", 1, 0)]),
            fld("C", (0, 0), [], [bead("B1", 0, 0), bead("B2", 1, 0)]),
        ],
        limit=5,
    )
    res = solve_review_request(pl)
    s = res["registrationReview"]["summary"]
    assert s["maxCoordinateResidual"] == 1
    assert s["totalCorrectionManhattan"] == 0
    assert corrections(res) == [(0, 0, 0), (1, 0, 0), (2, 0, 0)]


def test_objective_three_lexicographic_correction_sequence():
    # 联合约束才会出现的真实平局：
    #   B1 出现于三个视野（登记位置重合）；B2 仅出现于 B、C，登记 x 相差 2。
    # 方案 (cB,cC) = (0,-1) 与 (1,0) 时全部成对残差最大值都是 1、
    # 校正量曼哈顿和都是 1；第三目标按视野录入顺序比较校正坐标序列，
    # 首视野同为 (0,0)，第二视野 (0,0) 字典序小于 (1,0) → 取 (0,-1)。
    pl = payload(
        [
            fld("A", (0, 0), [], [bead("B1", 0, 0)]),
            fld("B", (0, 0), [], [bead("B1", 0, 0), bead("B2", 0, 1)]),
            fld("C", (0, 0), [], [bead("B1", 0, 0), bead("B2", 2, 1)]),
        ],
        limit=5,
    )
    res = solve_review_request(pl)
    s = res["registrationReview"]["summary"]
    assert s["maxCoordinateResidual"] == 1
    assert s["totalCorrectionManhattan"] == 1
    assert corrections(res) == [(0, 0, 0), (1, 0, 0), (2, -1, 0)]


def test_result_is_deterministic():
    pl = payload(
        [
            fld("A", (0, 0), [], [bead("B1", 0, 0), bead("B2", 2, 0)]),
            fld("B", (0, 0), [], [bead("B1", 0, 0), bead("B2", 1, 0)]),
            fld("C", (0, 0), [], [bead("B1", 0, 0), bead("B2", 1, 0)]),
        ],
        limit=5,
    )
    r1 = solve_review_request(pl)
    r2 = solve_review_request(pl)
    assert corrections(r1) == corrections(r2)


def test_residual_limit_boundary_is_inclusive():
    # 残差恰好等于上限（横纵各 1）允许配准
    pl = payload(
        [
            fld("A", (0, 0), [], [bead("B1", 0, 0)]),
            fld("B", (0, 0), [], [bead("B1", 0, 0)], rng=0),
            fld("C", (0, 0), [], [bead("B1", 1, 1)], rng=0),
        ],
        limit=1,
    )
    res = solve_review_request(pl)
    ev = next(b for b in res["registrationReview"]["beadEvidence"]
              if b["beadId"] == "B1")
    pair = next(r for r in ev["pairwiseResiduals"]
                if {r["from"]["fieldIndex"], r["to"]["fieldIndex"]} == {0, 2})
    assert pair["deltaX"] == 1
    assert pair["deltaY"] == 1
    assert pair["withinLimit"] is True


# --------------------------------------------------------------------------- #
# 珠位证据
# --------------------------------------------------------------------------- #
def test_bead_evidence_records_registered_and_corrected_positions():
    pl = payload(
        [
            fld("A", (0, 0), [], [bead("B1", 5, 5)]),
            fld("B", (10, 0), [], [bead("B1", -4, 5)]),
            fld("C", (10, 10), [], [bead("B1", -5, -4)]),
        ],
        limit=0,
    )
    res = solve_review_request(pl)
    ev = res["registrationReview"]["beadEvidence"][0]
    assert ev["beadId"] == "B1"
    assert ev["occurrenceCount"] == 3
    by_field = {o["fieldIndex"]: o for o in ev["occurrences"]}
    # 原登记滤膜坐标暴露漂移，校正后全部重合
    assert by_field[1]["registeredFilter"] == {"x": 6, "y": 5}
    assert by_field[1]["correction"] == {"x": -1, "y": 0}
    assert by_field[1]["filter"] == {"x": 5, "y": 5}
    assert by_field[2]["registeredFilter"] == {"x": 5, "y": 6}
    assert by_field[2]["filter"] == {"x": 5, "y": 5}
    assert len(ev["pairwiseResiduals"]) == 3
    for pair in ev["pairwiseResiduals"]:
        assert pair["deltaX"] == 0
        assert pair["deltaY"] == 0
        assert pair["withinLimit"] is True


# --------------------------------------------------------------------------- #
# 不合规输入：定位 + 草稿保留
# --------------------------------------------------------------------------- #
def test_duplicate_bead_within_same_field_is_localizable():
    pl = payload(
        [
            fld("A", (0, 0), [], [bead("B1", 0, 0), bead("B1", 1, 1)]),
            fld("B", (0, 0), [], [bead("B1", 0, 0)]),
            fld("C", (0, 0), [], [bead("B1", 0, 0)]),
        ],
        limit=0,
    )
    with pytest.raises(ValidationError) as ei:
        solve_review_request(pl)
    locs = [e["loc"] for e in ei.value.errors]
    assert "fields[0].calibrationBeads[1].id" in locs
    assert ei.value.draft is pl


def test_same_bead_id_across_fields_is_the_shared_link():
    # 同一编号在不同视野各出现一次是合法且必要的共享证据，不得报错
    pl = payload(
        [
            fld("A", (0, 0), [], [bead("B1", 0, 0), bead("B2", 7, 7)]),
            fld("B", (0, 0), [], [bead("B1", 0, 0), bead("B2", 7, 7)]),
            fld("C", (0, 0), [], [bead("B1", 0, 0), bead("B2", 7, 7)]),
        ],
        limit=0,
    )
    res = solve_review_request(pl)
    assert res["registrationReview"]["summary"]["sharedBeadCount"] == 2


def test_non_integer_bead_coords_and_bad_range_are_localizable():
    pl = payload(
        [
            {"name": "A", "shift": {"x": 0, "y": 0}, "correctionRange": 6,
             "particles": [], "calibrationBeads": [{"id": "B1", "x": 1.5, "y": 0}]},
            {"name": "B", "shift": {"x": 0, "y": 0}, "correctionRange": -1,
             "particles": [], "calibrationBeads": [{"id": "B1", "x": 0, "y": "z"}]},
            fld("C", (0, 0), [], [bead("B1", 0, 0)]),
        ],
        limit=0,
    )
    with pytest.raises(ValidationError) as ei:
        solve_review_request(pl)
    locs = {e["loc"] for e in ei.value.errors}
    assert "fields[0].correctionRange" in locs
    assert "fields[0].calibrationBeads[0].x" in locs
    assert "fields[1].correctionRange" in locs
    assert "fields[1].calibrationBeads[0].y" in locs
    assert ei.value.draft is pl


def test_bad_residual_limit_is_localizable():
    pl = payload(
        [
            fld("A", (0, 0), [], [bead("B1", 0, 0)]),
            fld("B", (0, 0), [], [bead("B1", 0, 0)]),
            fld("C", (0, 0), [], [bead("B1", 0, 0)]),
        ],
    )
    pl["beadResidualLimit"] = -1
    with pytest.raises(ValidationError) as ei:
        solve_review_request(pl)
    assert any(e["loc"] == "beadResidualLimit" for e in ei.value.errors)


def test_bead_coord_duplicate_within_field_is_localizable():
    pl = payload(
        [
            fld("A", (0, 0), [], [bead("B1", 0, 0), bead("B2", 0, 0)]),
            fld("B", (0, 0), [], [bead("B1", 0, 0), bead("B2", 0, 0)]),
            fld("C", (0, 0), [], [bead("B1", 0, 0)]),
        ],
        limit=0,
    )
    with pytest.raises(ValidationError) as ei:
        solve_review_request(pl)
    assert "fields[0].calibrationBeads[1]" in [e["loc"] for e in ei.value.errors]


def test_dedup_input_rules_still_apply_through_review():
    pl = payload(
        [
            fld("A", (0, 0), [p("P1", 1.5, 0, "PE")], [bead("B1", 0, 0)]),
            fld("B", (0, 0), [], [bead("B1", 0, 0)]),
            fld("C", (0, 0), [], [bead("B1", 0, 0)]),
        ],
        limit=0,
    )
    with pytest.raises(ValidationError) as ei:
        solve_review_request(pl)
    assert "fields[0].particles[0].x" in [e["loc"] for e in ei.value.errors]


def test_non_dict_payload_raises_validation_error():
    with pytest.raises(ValidationError):
        solve_review_request(["not", "an", "object"])


def test_bead_count_limit_is_localizable():
    too_many = [bead(f"B{j}", j, 0) for j in range(101)]
    pl = payload(
        [
            fld("A", (0, 0), [], too_many),
            fld("B", (0, 0), [], [bead("B0", 0, 0)]),
            fld("C", (0, 0), [], [bead("B0", 0, 0)]),
        ],
        limit=0,
    )
    with pytest.raises(ValidationError) as ei:
        solve_review_request(pl)
    assert any(
        e["loc"] == "fields[0].calibrationBeads" and "100" in e["message"]
        for e in ei.value.errors
    )


# --------------------------------------------------------------------------- #
# 朴素全枚举 oracle：校正范围限制在 0–2 使笛卡尔枚举可承受，
# 在随机实例上与 CSP 求解器的三级最优结论逐一核对。
# --------------------------------------------------------------------------- #
def test_matches_brute_force_oracle_on_random_instances():
    import random
    from decimal import Decimal
    from itertools import product

    from app.dedup import validate
    from app.registration import (
        _field_graph,
        _shared_bead_groups,
        solve_corrections,
        validate_beads,
    )

    def brute(cleaned, review):
        fields = cleaned["fields"]
        nn = len(fields)
        per_field = review["perField"]
        lim = review["residualLimit"]
        grps, _ = _shared_bead_groups(per_field)
        sxx = [f["sx"] for f in fields]
        syy = [f["sy"] for f in fields]
        opts = [[(0, 0)]] + [
            list(product(range(-per_field[i]["range"], per_field[i]["range"] + 1),
                         repeat=2))
            for i in range(1, nn)
        ]
        best = None
        feasible_any = False
        for combo in product(*opts):
            mdx = mdy = Decimal(0)
            ok = True
            for occs in grps.values():
                coords = [
                    (
                        Decimal(o.x) + sxx[o.field] + Decimal(combo[o.field][0]),
                        Decimal(o.y) + syy[o.field] + Decimal(combo[o.field][1]),
                    )
                    for o in occs
                ]
                for a in range(len(coords)):
                    for b in range(a + 1, len(coords)):
                        dx = abs(coords[a][0] - coords[b][0])
                        dy = abs(coords[a][1] - coords[b][1])
                        mdx = max(mdx, dx)
                        mdy = max(mdy, dy)
                        if dx > lim or dy > lim:
                            ok = False
            key = (max(mdx, mdy),
                   sum(abs(cx) + abs(cy) for cx, cy in combo),
                   combo)
            if ok:
                feasible_any = True
                if best is None or key < best:
                    best = key
        return best, feasible_any

    def connected(grps, nn):
        adj = _field_graph(grps, nn)
        seen = [False] * nn
        seen[0] = True
        stack = [0]
        while stack:
            v = stack.pop()
            for w in adj[v]:
                if not seen[w]:
                    seen[w] = True
                    stack.append(w)
        return all(seen)

    random.seed(2026)
    checked = 0
    for t in range(60):
        nf = random.choices([3, 4, 5], weights=[70, 20, 10])[0]
        nbeads = random.randint(1, 4)
        bids = [f"B{k}" for k in range(nbeads)]
        owner = {b: {0} for b in bids}
        for i in range(1, nf):
            owner[random.choice(bids)].add(i)
        for b in bids:
            for i in range(nf):
                if random.random() < 0.35:
                    owner[b].add(i)
        bead_pos = {}
        for b in bids:
            base = (random.randint(-2, 2), random.randint(-2, 2))
            bead_pos[b] = {
                fi: (base[0] - random.randint(-2, 2),
                     base[1] - random.randint(-2, 2))
                for fi in sorted(owner[b])
            }
        raw_fields = []
        for fi in range(nf):
            used, beads = set(), []
            for b in bids:
                if fi in bead_pos[b]:
                    x, y = bead_pos[b][fi]
                    if (x, y) in used:
                        continue
                    used.add((x, y))
                    beads.append(bead(b, x, y))
            raw_fields.append({
                "name": f"F{t}-{fi}",
                "shift": {"x": random.randint(-2, 2),
                          "y": random.randint(-2, 2)},
                "correctionRange": random.choice([0, 1, 1, 2]),
                "particles": [],
                "calibrationBeads": beads,
            })
        pl = payload(raw_fields, tol=1,
                     limit=random.choice([0, 0, 1, 2]))
        cleaned = validate(pl)
        review = validate_beads(pl, nf)
        review["draft"] = pl
        grps, _ = _shared_bead_groups(review["perField"])

        if not connected(grps, nf):
            with pytest.raises(ValidationError):
                solve_corrections(cleaned, review)
            checked += 1
            continue

        best, feasible_any = brute(cleaned, review)
        if not feasible_any:
            with pytest.raises(ValidationError):
                solve_corrections(cleaned, review)
        else:
            sol = solve_corrections(cleaned, review)
            got = (
                max(sol.max_dx, sol.max_dy),
                sol.correction_manhattan,
                tuple(sol.corrections),
            )
            assert got == best, (t, got, best, pl)
        checked += 1
    assert checked == 60
