"""去重核心算法的单元测试（含非贪心反例、平局裁决、边界与校验）。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.dedup import (
    ValidationError,
    build_response,
    solve_request,
    validate,
)

ZERO = Decimal(0)


def field(name, sx, sy, particles):
    return {"name": name, "shift": {"x": sx, "y": sy}, "particles": particles}


def p(pid, x, y, cat):
    return {"id": pid, "x": x, "y": y, "category": cat}


def base(particles_by_field, tol=2, shifts=None):
    shifts = shifts or [(0, 0)] * len(particles_by_field)
    return {
        "tolerance": tol,
        "fields": [
            field(f"F{i}", shifts[i][0], shifts[i][1], ps)
            for i, ps in enumerate(particles_by_field)
        ],
    }


def ids_of(res):
    """最终颗粒 -> 各视野伙伴编号列表（按视野顺序）。"""
    out = []
    for fp in res["finalParticles"]:
        out.append([
            (o["fieldIndex"], o["particleId"])
            for o in sorted(fp["observations"], key=lambda o: o["fieldIndex"])
        ])
    return out


# --------------------------------------------------------------------------- #
def test_chain_across_three_fields_merges_into_one():
    payload = base(
        [
            [p("A", 0, 0, "PE")],
            [p("B", 1, 0, "PE")],
            [p("C", 1, 1, "PE")],
        ],
        tol=2,
    )
    res = solve_request(payload)
    assert res["summary"]["finalParticleCount"] == 1
    assert res["summary"]["totalObservations"] == 3
    assert res["summary"]["associationCount"] == 2
    fp = res["finalParticles"][0]
    assert fp["category"] == "PE"
    assert fp["representativeCoordinate"]["particleId"] == "A"
    assert fp["representativeCoordinate"] == {
        "fieldIndex": 0, "fieldName": "F0", "particleId": "A", "x": 0, "y": 0
    }


def test_different_category_never_associates():
    payload = base(
        [
            [p("A", 0, 0, "PE")],
            [p("B", 1, 0, "PP")],
            [p("C", 2, 0, "PE")],
        ],
        tol=5,
    )
    res = solve_request(payload)
    # B 类别不同永不关联；A、C 同类且滤膜距离 2 ≤ 5，合并为一个颗粒
    assert res["summary"]["finalParticleCount"] == 2
    groups = [sorted(pid for _, pid in g) for g in ids_of(res)]
    assert groups == [["A", "C"], ["B"]]


def test_same_field_observations_never_merge_even_if_identical():
    payload = base(
        [
            [p("A1", 0, 0, "PE"), p("A2", 1, 0, "PE")],
            [p("B", 1, 0, "PE")],
            [p("C", 2, 0, "PE")],
        ],
        tol=5,
    )
    res = solve_request(payload)
    # A1、A2 同视野，必属两个不同最终颗粒；其余观测在其间分配
    counts = [len(x) for x in ids_of(res)]
    assert res["summary"]["finalParticleCount"] == 2
    assert sorted(counts) == [1, 3] or sorted(counts) == [2, 2]


def test_tolerance_boundary_is_inclusive_with_shift():
    payload = base(
        [[p("A", 0, 0, "PE")], [p("B", 0, 0, "PE")], [p("C", 0, 0, "PE")]],
        tol=2,
        shifts=[(0, 0), (2, 0), (0, 2)],
    )
    res = solve_request(payload)
    assert res["summary"]["finalParticleCount"] == 1
    # 恰好等于容差允许关联；曼哈顿总和 = 2 + 2 = 4
    assert res["summary"]["totalManhattan"] == 4


def test_shift_breaks_overlap():
    payload = base(
        [[p("A", 0, 0, "PE")], [p("B", 0, 0, "PE")], [p("C", 0, 0, "PE")]],
        tol=2,
        shifts=[(0, 0), (3, 0), (0, 3)],
    )
    res = solve_request(payload)
    assert res["summary"]["finalParticleCount"] == 3


def test_non_greedy_global_optimum():
    # 1D 坐标；tol=1。候选边构成路径（每条边曼哈顿差均为 1）：
    #   A(0) — B(1) — Cp(2) — Bp(3) — C(4)
    # B、Bp 同视野，C、Cp 同视野。最少颗粒数下界 = 2。
    # 朴素的「沿最近边不断并堆」贪心会尝试把路径两端的 C 也并入
    # {A,B,Cp}，或把 Bp 并入其中，二者都造成同一颗粒含同视野两个观测，
    # 必须被拒绝。精确枚举在全部合法彩虹连通划分上求全局最优，
    # 唯一达到下界 2 且总代价 3 的方案是 {A,B,Cp} + {Bp,C}。
    payload = base(
        [
            [p("A", 0, 0, "PE")],
            [p("B", 1, 0, "PE"), p("Bp", 3, 0, "PE")],
            [p("C", 4, 0, "PE"), p("Cp", 2, 0, "PE")],
        ],
        tol=1,
    )
    res = solve_request(payload)
    assert res["summary"]["finalParticleCount"] == 2
    assert res["summary"]["totalManhattan"] == 3
    groups = [sorted(pid for _, pid in g) for g in ids_of(res)]
    # 另一看似自然的方案 {A,B}+{Bp,C,Cp} 因 C、Cp 同视野而非法，被精确排除
    assert groups == [["A", "B", "Cp"], ["Bp", "C"]]


def test_local_nearest_greedy_is_rejected_by_global_optimum():
    # 二维布局（所有点同类 PE，tol=1；边旁为曼哈顿差）：
    #   B(0,1) 距 A(0,0)=1、A2(0,2)=1、C(1,1)=1
    #   B2(1,0) 距 A=1、C(1,1)=1
    # 「从最近边开始贪心并堆」会先合并 A-B-C（或 A-B2-C），形成一个 3 观颗粒，
    # 之后 A2-B、B2-C 都因同视野冲突无法再用 → 只剩 2 个孤立观测，
    # 贪心总共得到 3 个最终颗粒、代价 2。
    # 全局精确枚举找到 2 个颗粒：{A,B2}(1) + {A2,B,C}(1+1=2)，总代价 3：
    # 以稍高的关联代价换取更少的最终颗粒数（第一优化目标），贪心无法得到。
    payload = base(
        [
            [p("A", 0, 0, "PE"), p("A2", 0, 2, "PE")],
            [p("B", 0, 1, "PE"), p("B2", 1, 0, "PE")],
            [p("C", 1, 1, "PE")],
        ],
        tol=1,
    )
    res = solve_request(payload)
    assert res["summary"]["finalParticleCount"] == 2
    assert res["summary"]["totalManhattan"] == 3
    groups = [sorted(pid for _, pid in g) for g in ids_of(res)]
    assert groups == [["A", "B2"], ["A2", "B", "C"]]


def test_tie_break_by_partner_id_sequence():
    # f1/f2 各两个观测，在 2D 上构成等代价完全二分图（曼哈顿差均为 100，
    # 横纵差均不超过容差 100）。两种配对方案颗粒数与总曼哈顿完全相同：
    #   方案一 {B,C}+{B2,C2}；方案二 {B,C2}+{B2,C}
    # 按视野录入顺序展开的伙伴编号序列取字典序最小 → 方案一。
    # 另有一个类别不同的孤立观测 D。
    payload = base(
        [
            [p("D", 0, 0, "PS")],
            [p("B", 0, 0, "PE"), p("B2", 100, 0, "PE")],
            [p("C", 0, 100, "PE"), p("C2", 100, 100, "PE")],
        ],
        tol=100,
    )
    res = solve_request(payload)
    assert res["summary"]["finalParticleCount"] == 3
    assert res["summary"]["totalManhattan"] == 200
    groups = sorted(
        sorted(pid for _, pid in g) for g in ids_of(res)
    )
    assert groups == [["B", "C"], ["B2", "C2"], ["D"]]


def test_tie_break_unique_even_with_cross_field_duplicate_ids():
    # 伙伴编号只在「视野内」唯一：三个视野各有编号 X、Y 的观测，完全合法。
    # 几何（tol=10，二维对称）：
    #   f0: X(0,0), Y(10,10)；f1: X(0,10), Y(10,0)；f2: X(100,0), Y(101,0)
    # f0/f1 的四个观测构成曼哈顿距离全等的 K2,2（每条边曼哈顿差均为 10）：
    #   配对一 {f0.X,f1.X} + {f0.Y,f1.Y}：总代价 20
    #   配对二 {f0.X,f1.Y} + {f0.Y,f1.X}：总代价 20
    # 颗粒数与曼哈顿总和完全相同 → 由伙伴编号序列裁决：
    #   配对一的方案键 (("X"),("X","X"),("Y"),("Y","Y"))
    #   字典序小于配对二的 (("X"),("X","Y"),("X","Y"),("Y")) → 配对一。
    # f2 的两个观测同视野且互相不可关联，各自单独成颗粒。
    payload = base(
        [
            [p("X", 0, 0, "PE"), p("Y", 10, 10, "PE")],
            [p("X", 0, 10, "PE"), p("Y", 10, 0, "PE")],
            [p("X", 100, 0, "PE"), p("Y", 101, 0, "PE")],
        ],
        tol=10,
    )
    res1 = solve_request(payload)
    res2 = solve_request(payload)  # 重复裁决必须得到同一结论
    sig1 = [
        sorted((o["fieldIndex"], o["particleId"]) for o in fp["observations"])
        for fp in res1["finalParticles"]
    ]
    sig2 = [
        sorted((o["fieldIndex"], o["particleId"]) for o in fp["observations"])
        for fp in res2["finalParticles"]
    ]
    assert sig1 == sig2
    assert res1["summary"]["finalParticleCount"] == 4
    assert res1["summary"]["totalManhattan"] == 20
    # 配对一：同编号跨视野合并；f2 的两个观测各自单独
    assert sorted(sorted(x) for x in sig1) == [
        [(0, "X"), (1, "X")],
        [(0, "Y"), (1, "Y")],
        [(2, "X")],
        [(2, "Y")],
    ]


def test_cost_tie_prefers_smaller_manhattan():
    # 两种单颗粒合并：A-B-C 走边 1+1=2；布局中不应选到更长的 A-C(2) 替代。
    payload = base(
        [
            [p("A", 0, 0, "PE")],
            [p("B", 1, 0, "PE")],
            [p("C", 2, 0, "PE")],
        ],
        tol=2,
    )
    res = solve_request(payload)
    assert res["summary"]["finalParticleCount"] == 1
    assert res["summary"]["totalManhattan"] == 2


def test_decimal_shifts_and_coordinates():
    payload = base(
        [
            [p("A", 1, 0, "PE")],
            [p("B", 0, 0, "PE")],
            [p("C", 0, 0, "PE")],
        ],
        tol=1,
        shifts=[(0, 0), (0.5, 0), (1.5, 0)],
    )
    res = solve_request(payload)
    # 滤膜坐标：A=1.0、B=0.5、C=1.5；|A-B|=.5、|B-C|=1、|A-C|=.5
    # 合成一个颗粒，MST 取两条 .5 的边 → 曼哈顿总和 1
    assert res["summary"]["finalParticleCount"] == 1
    assert res["summary"]["totalManhattan"] == 1


def test_each_final_particle_is_connected_and_rainbow():
    import random

    random.seed(7)
    cats = ["PE", "PP"]
    for trial in range(60):
        nf = random.randint(3, 5)
        fields_ = []
        for fi in range(nf):
            k = random.randint(0, 3)
            # 同视野内坐标互不相同，避免触发“整数坐标重复”校验
            cells = random.sample(
                [(x, y) for x in range(7) for y in range(7)], k
            )
            ps = [
                p(f"id{fi}-{t}", x, y, random.choice(cats))
                for t, (x, y) in enumerate(cells)
            ]
            fields_.append(field(f"F{fi}", 0, 0, ps))
        payload = {"tolerance": random.randint(1, 3), "fields": fields_}
        res = solve_request(payload)

        # 覆盖全部观测且无重复
        seen = []
        for fp in res["finalParticles"]:
            fidx = [o["fieldIndex"] for o in fp["observations"]]
            assert len(fidx) == len(set(fidx)), "同一视野出现两个观测"
            seen.extend((o["fieldIndex"], o["particleId"]) for o in fp["observations"])
        n_obs = sum(len(f["particles"]) for f in fields_)
        assert len(seen) == n_obs
        assert len(set(seen)) == n_obs

        # MST 边数 = 观测数 - 1（连通）
        for fp in res["finalParticles"]:
            assert len(fp["associations"]) == fp["observationCount"] - 1


# --------------------------------------------------------------------------- #
def test_requires_three_to_five_fields():
    with pytest.raises(ValidationError) as ei:
        validate({"tolerance": 1, "fields": [
            field("a", 0, 0, [p("x", 0, 0, "PE")]),
            field("b", 0, 0, [p("x", 0, 0, "PE")]),
        ]})
    locs = [e["loc"] for e in ei.value.errors]
    assert "fields" in locs
    assert ei.value.draft is not None  # 草稿随异常保留


def test_duplicate_id_and_coord_are_localizable():
    payload = base(
        [
            [p("A", 0, 0, "PE"), p("A", 0, 0, "PE")],
            [p("B", 0, 0, "PE")],
            [p("C", 0, 0, "PE")],
        ],
        tol=1,
    )
    with pytest.raises(ValidationError) as ei:
        validate(payload)
    locs = [e["loc"] for e in ei.value.errors]
    assert "fields[0].particles[1].id" in locs
    assert "fields[0].particles[1]" in locs  # 坐标重复定位到整条候选
    # 原草稿原样保留
    assert ei.value.draft is payload


def test_non_integer_coords_and_bad_types():
    payload = {
        "tolerance": -1,
        "fields": [
            {"name": "a", "shift": {"x": "0", "y": 0},
             "particles": [{"id": "A", "x": 1.5, "y": 0, "category": "PE"}]},
            "not-an-object",
            field("c", 0, 0, [p("C", 0, 0, "")]),
        ],
    }
    with pytest.raises(ValidationError) as ei:
        validate(payload)
    locs = {e["loc"] for e in ei.value.errors}
    assert "tolerance" in locs
    assert "fields[0].shift.x" in locs
    assert "fields[0].particles[0].x" in locs
    assert "fields[1]" in locs
    assert "fields[2].particles[0].category" in locs


def test_empty_particle_list_is_valid():
    res = build_response(validate(base([[], [], []], tol=1)))
    assert res["summary"]["finalParticleCount"] == 0
    assert res["finalParticles"] == []


def test_particle_count_limit_is_localizable():
    too_many = [p(f"P{j}", j % 50, j // 50, "PE") for j in range(31)]
    payload = base([too_many, [], []], tol=1)
    with pytest.raises(ValidationError) as ei:
        validate(payload)
    assert any(
        e["loc"] == "fields[0].particles" and "30" in e["message"]
        for e in ei.value.errors
    )


# --------------------------------------------------------------------------- #
# 独立暴力枚举 oracle：对全部观测穷举所有集合划分（不做连通分量分解，
# 跨分量块会因 MST 不连通被拒），按三级目标求全局最优后与求解器核对。
# --------------------------------------------------------------------------- #
def _brute_oracle(solver):
    def mst_cost(verts, edges):
        m = {v: v for v in verts}

        def find(x):
            while m[x] != x:
                m[x] = m[m[x]]
                x = m[x]
            return x

        total, used = ZERO, 0
        for w, u, v in edges:
            if u not in verts or v not in verts:
                continue
            if find(u) != find(v):
                m[m[u]] = m[v]
                total, used = total + w, used + 1
        return total if used == len(verts) - 1 else None

    def partitions(s):
        if not s:
            yield []
            return
        s = list(s)
        first = s[0]
        for rest in partitions(frozenset(s[1:])):
            for i in range(len(rest)):
                yield rest[:i] + [rest[i] | {first}] + rest[i + 1:]
            yield [frozenset([first])] + rest

    edges = sorted((e.manhattan, e.u, e.v) for e in solver.edge_info.values())
    best = None
    for part in partitions(frozenset(range(solver.n))):
        ok, costs, keys = True, ZERO, ()
        for block in part:
            if len({solver.obs[v].field for v in block}) != len(block):
                ok = False  # 同一视野两个观测 → 非法
                break
            c = mst_cost(set(block), edges)
            if c is None:
                ok = False  # 块不连通 → 非法
                break
            costs += c
            keys = tuple(sorted(keys + (
                (
                    tuple(solver.obs[v].pid for v in sorted(block)),
                    tuple(sorted(block)),
                ),
            )))
        if ok:
            cand = (len(part), costs, keys)
            if best is None or cand < best:
                best = cand

    clusters = solver.solve()
    got = (
        len(clusters),
        sum(solver.cluster_cost[c] for c in clusters),
        tuple(sorted(
            (
                tuple(solver.obs[v].pid for v in sorted(c)),
                tuple(sorted(c)),
            )
            for c in clusters
        )),
    )
    assert got == best, f"求解器 {got} != 暴力全局最优 {best}"


def test_matches_brute_force_oracle_on_random_instances():
    import random

    from app.dedup import Solver

    random.seed(2026)
    for trial in range(40):
        nf = random.randint(3, 5)
        fields_ = []
        for fi in range(nf):
            k = random.randint(1, 2)
            cells = random.sample(
                [(x, y) for x in range(4) for y in range(4)], k
            )
            fields_.append({
                "name": f"F{fi}", "shift": {"x": 0, "y": 0},
                "particles": [
                    p(f"{chr(65 + fi)}{t}", x, y, random.choice(["PE", "PP", "PS"]))
                    for t, (x, y) in enumerate(cells)
                ],
            })
        cleaned = validate({"tolerance": random.randint(1, 2), "fields": fields_})
        solver = Solver(cleaned)
        assert solver.n <= 10  # Bell(10) 以内，暴力可承受
        _brute_oracle(solver)
