"""配准复核：校准珠证据驱动的单视野整数校正量联合求解。

背景：去重前部分显微视野的登记平移受载物台漂移影响。分析员可为每个视野
补录带全局唯一编号的校准珠局部整数坐标，并填写 0–5 格的单视野校正范围
（校正量 cx、cy 均须落在 [-R, R] 的整数格点上）与珠位残差上限。

规则（精确定义）：
- 首个视野的校正量固定为 (0, 0)。
- 同一编号校准珠出现在视野 i、j，局部整数坐标 (xi, yi)、(xj, yj)，
  原登记平移 (sxi, syi)、(sxj, syj)，校正量 (cix, ciy)、(cjx, cjy)：
  换算到滤膜坐标后
      Δx = (xi + sxi + cix) - (xj + sxj + cjx)
      Δy = (yi + syi + ciy) - (yj + syj + cjy)
  方案可行必须对每颗珠的每对出现视野都满足 |Δx| ≤ L、|Δy| ≤ L，
  L 为珠位残差上限。
- 全局只出现一次的校准珠不参与任何约束（证据中标注 usedInConstraints=false）。
- 视野为节点、「共享出现 ≥2 次的校准珠」为边构图；无法经共享校准珠
  连到首视野的视野无法锚定校正量，校正固定为零并在 disconnectedFields
  中明确指出，重跑去重时沿用原平移。
- 在全部可行方案上依次优化：
  1) 最大坐标残差（所有珠对的横/纵残差的最大值）最小；
  2) 全部校正量曼哈顿和 Σ(|cx| + |cy|) 最小；
  3) 仍相同则按视野录入顺序的校正坐标序列 ((cx0, cy0), …) 取字典序最小，
     得到唯一结论。
- 校正后平移 = 原平移 + 校正量，用其重新执行既有颗粒去重（dedup.build_response）。

输入不合规（珠在同一视野重复、坐标非整数、校正范围不合规、不存在可行
配准等）抛 ValidationError，错误项可定位，且不覆盖上一次成功复核结果。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .dedup import (
    MAX_PARTICLES_PER_FIELD,
    ValidationError,
    _is_int,
    _is_num,
    _num,
    build_response,
    validate,
)

ZERO = Decimal(0)

# 单视野校正范围边界（0–5 格）
MIN_CORRECTION_RANGE = 0
MAX_CORRECTION_RANGE = 5
# 单视野补录校准珠数量上限（精确枚举的规模边界）
MAX_BEADS_PER_FIELD = MAX_PARTICLES_PER_FIELD
BEAD_ID_MAX_LEN = 64

# 最近一次成功的配准复核结果（服务端单例；失败永不覆盖，重启清空）
_LAST_REVIEW: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def _err(errors: list[dict[str, str]], loc: str, message: str) -> None:
    errors.append({"loc": loc, "message": message})


def validate_review(payload: Any) -> dict[str, Any]:
    """校验复核草稿：复用颗粒去重校验，再校验校正范围、残差上限与校准珠。"""
    errors: list[dict[str, str]] = []

    # 颗粒 / 视野 / 平移等基础结构完全复用既有去重校验（correctionRange、
    # beads 为其额外字段，既有 validate 原样忽略）。
    try:
        cleaned = validate(payload)
    except ValidationError as exc:
        errors.extend(exc.errors)
        cleaned = None

    if not isinstance(payload, dict):
        raise ValidationError(
            [{"loc": "$", "message": "请求体必须是 JSON 对象"}], payload
        )

    # 珠位残差上限
    limit_loc = "beadResidualLimit"
    raw_limit = payload.get("beadResidualLimit")
    bead_limit: Decimal = ZERO
    if not _is_num(raw_limit):
        _err(errors, limit_loc, "珠位残差上限必须是非负数字")
    else:
        bead_limit = Decimal(str(raw_limit))
        if bead_limit < 0:
            _err(errors, limit_loc, "珠位残差上限不能为负数")

    raw_fields = payload.get("fields")
    fields_obj = raw_fields if isinstance(raw_fields, list) else []
    n_fields = len(fields_obj)

    # 每视野：校正范围 + 校准珠
    ranges: list[int | None] = []
    beads_by_field: list[list[dict[str, Any]]] = []
    for i, rf in enumerate(fields_obj):
        floc = f"fields[{i}]"
        r: int | None = None
        field_beads: list[dict[str, Any]] = []

        if not isinstance(rf, dict):
            beads_by_field.append(field_beads)
            ranges.append(None)
            continue

        # 单视野校正范围：必填的 0–5 整数（bool 显式排除）
        raw_r = rf.get("correctionRange")
        if not _is_int(raw_r):
            _err(errors, f"{floc}.correctionRange",
                 "单视野校正范围必须是 0 至 5 的整数")
        elif not (MIN_CORRECTION_RANGE <= raw_r <= MAX_CORRECTION_RANGE):
            _err(errors, f"{floc}.correctionRange",
                 f"单视野校正范围必须在 {MIN_CORRECTION_RANGE} 至 "
                 f"{MAX_CORRECTION_RANGE} 格之间，当前为 {raw_r}")
        else:
            r = int(raw_r)

        # 校准珠（可为空；缺失视为空数组）
        raw_beads = rf.get("beads", [])
        if not isinstance(raw_beads, list):
            _err(errors, f"{floc}.beads", "beads 必须是数组")
            raw_beads = []
        elif len(raw_beads) > MAX_BEADS_PER_FIELD:
            _err(errors, f"{floc}.beads",
                 f"单个视野的校准珠不能超过 {MAX_BEADS_PER_FIELD} 颗，"
                 f"当前为 {len(raw_beads)} 颗")
            raw_beads = []

        seen_bead_ids: set[str] = set()
        seen_bead_coords: set[tuple[int, int]] = set()
        for k, rb in enumerate(raw_beads):
            bloc = f"{floc}.beads[{k}]"
            if not isinstance(rb, dict):
                _err(errors, bloc, "校准珠必须是对象")
                continue

            bid = rb.get("id")
            if not isinstance(bid, str) or not bid.strip():
                _err(errors, f"{bloc}.id", "校准珠编号必须是非空字符串")
                bid = None
            else:
                bid = bid.strip()
                if len(bid) > BEAD_ID_MAX_LEN:
                    _err(errors, f"{bloc}.id",
                         f"校准珠编号长度不能超过 {BEAD_ID_MAX_LEN}")
                if bid in seen_bead_ids:
                    # 同一视野重复编号：无法区分，明确拒绝并定位
                    _err(errors, f"{bloc}.id",
                         f"校准珠编号 {bid} 在该视野内重复")
                else:
                    seen_bead_ids.add(bid)

            bx, by = rb.get("x"), rb.get("y")
            if not _is_int(bx):
                _err(errors, f"{bloc}.x", "校准珠局部坐标 x 必须是整数")
            if not _is_int(by):
                _err(errors, f"{bloc}.y", "校准珠局部坐标 y 必须是整数")
            if _is_int(bx) and _is_int(by):
                if (bx, by) in seen_bead_coords:
                    _err(errors, bloc,
                         f"该视野内校准珠整数坐标 ({bx}, {by}) 与其他校准珠重复")
                else:
                    seen_bead_coords.add((bx, by))

            if bid is not None and _is_int(bx) and _is_int(by):
                field_beads.append({"id": bid, "x": int(bx), "y": int(by)})

        ranges.append(r)
        beads_by_field.append(field_beads)

    if errors or cleaned is None:
        raise ValidationError(errors or [{"loc": "$", "message": "请求不合规"}], payload)

    return {
        "cleaned": cleaned,
        "bead_limit": bead_limit,
        "ranges": ranges,
        "beads_by_field": beads_by_field,
        "n_fields": n_fields,
        "draft_id": cleaned.get("draftId"),
    }


# --------------------------------------------------------------------------- #
# 配准 CSP：视野为节点，(cx, cy) ∈ [-R, R]² 为域，共享珠给二元矩形约束
# --------------------------------------------------------------------------- #
_DELTA_CACHE: dict[tuple[int, int, bool], frozenset[int]] = {}


def _delta_values(i: int, j: int, ranges: list[int],
                  fixed: int | None = None) -> frozenset[int]:
    """ci - cj 的可能整数差集合（fixed 视野的校正固定为零）。"""
    ri, rj = ranges[i], ranges[j]
    i_fixed = fixed == i
    j_fixed = fixed == j
    key = (ri, rj, i_fixed, j_fixed)
    got = _DELTA_CACHE.get(key)
    if got is None:
        if i_fixed and j_fixed:
            got = frozenset({0})
        elif i_fixed:
            got = frozenset(range(-rj, rj + 1))
        elif j_fixed:
            got = frozenset(range(-ri, ri + 1))
        else:
            got = frozenset(range(-ri - rj, ri + rj + 1))
        _DELTA_CACHE[key] = got
    return got


class _RegistrationCSP:
    """对单个视野连通分量枚举可行整数校正方案。

    同一视野对 (i, j) 之间可能有多颗共享珠。对每颗珠有约束
    |bx + cix - cjx| ≤ T、纵轴同理。对同一视野对，全部珠给出的允许
    校正差 d = ci - cj 是各整数区间的交集；该交集只需该方向上各珠
    基础差的最小/最大值即可确定：
        d ∈ [-T - min_bx, T - max_bx]（纵轴同理）
    因而「该视野对的全部校准珠」可合并为一条矩形区间约束，
    支持检查与珠数无关；区间为空即该视野对在 T 下不可行。
    """

    def __init__(self, members: list[int], ranges: list[int],
                 pair_bounds: list[tuple[int, int, Decimal, Decimal,
                                          Decimal, Decimal]],
                 fix_zero_field: int | None):
        self.members = members
        self._ranges = ranges
        self._fix_zero_field = fix_zero_field
        # 无向边（i < j）及其 i→j 方向基础差端点
        self.edges: list[tuple[int, int, Decimal, Decimal, Decimal, Decimal]] = []
        # 相邻表：field -> [(other, minx, maxx, miny, maxy)]，方向已归正
        self.neighbors: dict[int, list[tuple[int, Decimal, Decimal,
                                              Decimal, Decimal]]] = {
            v: [] for v in members
        }
        for i, j, minx, maxx, miny, maxy in pair_bounds:
            self.edges.append((i, j, minx, maxx, miny, maxy))
            self.neighbors[i].append((j, minx, maxx, miny, maxy))
            # j→i 方向：基础差取负，端点交换
            self.neighbors[j].append((i, -maxx, -minx, -maxy, -miny))

        self.domains_init: dict[int, set[tuple[int, int]]] = {}
        for v in members:
            r = ranges[v]
            grid = {(cx, cy) for cx in range(-r, r + 1) for cy in range(-r, r + 1)}
            if v == fix_zero_field:
                grid &= {(0, 0)}  # 首视野校正固定为零
            self.domains_init[v] = grid

    def _bounds(self, rel: tuple[int, Decimal, Decimal, Decimal, Decimal],
                cutoff: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        _, minx, maxx, miny, maxy = rel
        # d = ci - cj 的允许整数区间
        return (-cutoff - minx, cutoff - maxx,
                -cutoff - miny, cutoff - maxy)

    def _supports(self, vi: tuple[int, int],
                  rel: tuple[int, Decimal, Decimal, Decimal, Decimal],
                  dom_other: set[tuple[int, int]],
                  cutoff: Decimal) -> bool:
        """dom_other 中是否存在取值，与 vi 同时满足该视野对上的全部珠。"""
        cix, ciy = vi
        lox, hix, loy, hiy = self._bounds(rel, cutoff)
        # cj ∈ [ci - hi, ci - lo]（两轴）
        cjx_lo, cjx_hi = cix - hix, cix - lox
        cjy_lo, cjy_hi = ciy - hiy, ciy - loy
        for cjx, cjy in dom_other:
            if cjx_lo <= cjx <= cjx_hi and cjy_lo <= cjy <= cjy_hi:
                return True
        return False

    def _arc_consistent(self, domains: dict[int, set[tuple[int, int]]],
                        cutoff: Decimal,
                        queue: list[tuple[int, int]] | None = None) -> bool:
        """二元弧相容收域到不动点；任一域清空返回 False。"""
        if queue is None:
            queue = []
            for i, j, *_ in self.edges:
                queue.extend([(i, j), (j, i)])
        rel_of = {}
        for i, nbrs in self.neighbors.items():
            for (j, *rest) in nbrs:
                rel_of[(i, j)] = (j, *rest)
        while queue:
            i, j = queue.pop()
            rel = rel_of.get((i, j))
            if rel is None:
                continue
            dom_j = domains[j]
            dom_i = domains[i]
            removed = False
            for vi in list(dom_i):
                if not self._supports(vi, rel, dom_j, cutoff):
                    dom_i.discard(vi)
                    removed = True
            if not dom_i:
                return False
            if removed:
                for k, *_ in self.neighbors[i]:
                    if k != j:
                        queue.append((k, i))
        return True

    def _assignment_residual(self, values: dict[int, tuple[int, int]]
                             ) -> Decimal:
        """完整赋值下所有珠对的横纵残差最大值（用每视野对端点精确计算）。"""
        m = ZERO
        for i, j, minx, maxx, miny, maxy in self.edges:
            dx = values[i][0] - values[j][0]
            dy = values[i][1] - values[j][1]
            m = max(m, abs(minx + dx), abs(maxx + dx),
                    abs(miny + dy), abs(maxy + dy))
        return m

    def feasible(self, cutoff: Decimal) -> bool:
        """是否存在残差全部 ≤ cutoff 的完整赋值。"""
        domains = {v: set(d) for v, d in self.domains_init.items()}
        if not self._arc_consistent(domains, cutoff):
            return False
        return self._dfs_feasible(domains, cutoff)

    def _dfs_feasible(self, domains, cutoff) -> bool:
        pending = [v for v in self.members if len(domains[v]) > 1]
        if not pending:
            values = {v: next(iter(domains[v])) for v in self.members}
            # 弧相容已保证，这里再以聚合区间做一次 O(边数) 的完整兜底核验
            for i, j, minx, maxx, miny, maxy in self.edges:
                dx = values[i][0] - values[j][0]
                dy = values[i][1] - values[j][1]
                if (abs(minx + dx) > cutoff or abs(maxx + dx) > cutoff
                        or abs(miny + dy) > cutoff or abs(maxy + dy) > cutoff):
                    return False
            return True
        v = min(pending, key=lambda x: (len(domains[x]), x))  # MRV
        for val in sorted(domains[v]):
            nd = {k: set(s) for k, s in domains.items()}
            nd[v] = {val}
            queue = [(o, v) for o, *_ in self.neighbors[v]]
            if self._arc_consistent(nd, cutoff, queue) and self._dfs_feasible(nd, cutoff):
                return True
        return False

    def best_solution(self, cutoff: Decimal):
        """在残差 ≤ cutoff 下按（曼哈顿和, 录入顺序校正序列）求唯一最优。

        返回 (manhattan_sum, corrections_dict, max_residual)；无解返回 None。
        """
        domains = {v: set(d) for v, d in self.domains_init.items()}
        if not self._arc_consistent(domains, cutoff):
            return None

        best: tuple[int, tuple, dict[int, tuple[int, int]], Decimal] | None = None
        assignment: dict[int, tuple[int, int]] = {}

        def dfs() -> None:
            nonlocal best
            pending = [v for v in self.members if v not in assignment]
            if not pending:
                man = sum(abs(cx) + abs(cy) for cx, cy in assignment.values())
                seq = tuple(assignment[v] for v in self.members)
                if best is None or (man, seq) < (best[0], best[1]):
                    best = (man, seq, dict(assignment),
                            self._assignment_residual(assignment))
                return
            # MRV 选变量；值按 (曼哈顿, cx, cy) 排序，尽早触达优解
            v = min(pending, key=lambda x: (len(domains[x]), x))
            for val in sorted(domains[v],
                              key=lambda c: (abs(c[0]) + abs(c[1]), c[0], c[1])):
                cur_man = sum(
                    abs(cx) + abs(cy) for (cx, cy) in assignment.values()
                ) + abs(val[0]) + abs(val[1])
                if best is not None and cur_man > best[0]:
                    continue  # 部分曼哈顿和已不可能优于当前最优
                saved = {k: set(s) for k, s in domains.items()}
                assignment[v] = val
                domains[v] = {val}
                queue = [(o, v) for o, *_ in self.neighbors[v]]
                if self._arc_consistent(domains, cutoff, queue):
                    dfs()
                del assignment[v]
                for k in self.members:
                    domains[k] = saved[k]

        dfs()
        if best is None:
            return None
        return best[0], best[2], best[3]

    def residual_threshold_candidates(self, limit: Decimal) -> list[Decimal]:
        """最小瓶颈的候选残差值集合（≤ limit，有序）。

        每视野对每轴的残差由该方向基础差的两个端点决定；校正差 ci-cj
        只依赖双方校正范围（连续整数区间），故只需枚举端点×差集。
        """
        vals: set[Decimal] = {ZERO}
        for i, j, minx, maxx, miny, maxy in self.edges:
            deltas = _delta_values(i, j, self._ranges, self._fix_zero_field)
            for lo, hi in ((minx, maxx), (miny, maxy)):
                for endpoint in (lo, hi):
                    for delta in deltas:
                        v = abs(endpoint + delta)
                        if v <= limit:
                            vals.add(v)
        return sorted(vals)


# --------------------------------------------------------------------------- #
# 配准求解
# --------------------------------------------------------------------------- #
def _build_bead_index(beads_by_field: list[list[dict[str, Any]]]):
    """bead_id -> 出现的 [(field_index, x, y)]（按视野录入顺序）。"""
    index: dict[str, list[tuple[int, int, int]]] = {}
    for fi, beads in enumerate(beads_by_field):
        for b in beads:
            index.setdefault(b["id"], []).append((fi, b["x"], b["y"]))
    return index


def solve_registration(prepared: dict[str, Any]):
    """返回 (corrections, disconnected, bead_evidence 原料, objective, fatal_errors)。

    约束层面的不可行作为 fatal_errors 返回（loc=registration 或具体珠），
    调用方据此抛 ValidationError。
    """
    cleaned = prepared["cleaned"]
    ranges: list[int] = prepared["ranges"]
    beads_by_field: list[list[dict[str, Any]]] = prepared["beads_by_field"]
    limit: Decimal = prepared["bead_limit"]
    n_fields: int = prepared["n_fields"]
    shifts = [(Decimal(0) + f["sx"], Decimal(0) + f["sy"]) for f in cleaned["fields"]]

    bead_index = _build_bead_index(beads_by_field)

    # 视野图：共享出现 ≥2 次的珠则连边
    adj: dict[int, set[int]] = {i: set() for i in range(n_fields)}
    # 每条二元约束：(bead_id, i, j, base_x, base_y)
    pair_constraints: list[tuple[str, int, int, Decimal, Decimal]] = []
    for bid, occ in bead_index.items():
        if len(occ) < 2:
            continue
        for a in range(len(occ)):
            for b in range(a + 1, len(occ)):
                fi, xi, yi = occ[a]
                fj, xj, yj = occ[b]
                adj[fi].add(fj)
                adj[fj].add(fi)
                bx = (Decimal(xi) + shifts[fi][0]) - (Decimal(xj) + shifts[fj][0])
                by = (Decimal(yi) + shifts[fi][1]) - (Decimal(yj) + shifts[fj][1])
                pair_constraints.append((bid, fi, fj, bx, by))

    # 连通分量（无向 BFS），含首视野者为主分量
    seen = [False] * n_fields
    components: list[list[int]] = []
    for start in range(n_fields):
        if seen[start]:
            continue
        comp = []
        stack = [start]
        seen[start] = True
        while stack:
            v = stack.pop()
            comp.append(v)
            for w in adj[v]:
                if not seen[w]:
                    seen[w] = True
                    stack.append(w)
        components.append(sorted(comp))
    main_comp = next(c for c in components if 0 in c)
    main_set = set(main_comp)
    disconnected = sorted(i for i in range(n_fields) if i not in main_set)

    def pairs_of(members: set[int]):
        return [(bid, i, j, bx, by)
                for (bid, i, j, bx, by) in pair_constraints
                if i in members and j in members]

    def pair_bounds_of(members: set[int]):
        """同一视野对上的全部校准珠合并为基础差端点 (minx,maxx,miny,maxy)。"""
        agg: dict[tuple[int, int], dict[str, Decimal]] = {}
        for _, fi, fj, bx, by in pairs_of(members):
            key = (fi, fj)
            cur = agg.get(key)
            if cur is None:
                agg[key] = {"minx": bx, "maxx": bx, "miny": by, "maxy": by}
            else:
                cur["minx"] = min(cur["minx"], bx)
                cur["maxx"] = max(cur["maxx"], bx)
                cur["miny"] = min(cur["miny"], by)
                cur["maxy"] = max(cur["maxy"], by)
        return [(i, j, b["minx"], b["maxx"], b["miny"], b["maxy"])
                for (i, j), b in agg.items()]

    fatal: list[dict[str, str]] = []
    corrections: dict[int, tuple[int, int]] = {i: (0, 0) for i in range(n_fields)}
    objective_max_res = ZERO

    for comp in components:
        comp_set = set(comp)
        if 0 not in comp:
            # 无法经共享校准珠连到首视野：无全局锚点，校正固定为零，
            # 但仍须验证零校正下该孤立分量内部的珠位残差不超过上限；
            # 否则属于「不存在可行配准」并定位到相关校准珠。
            for bid, fi, fj, bx, by in pairs_of(comp_set):
                if not (abs(bx) <= limit and abs(by) <= limit):
                    fatal.append({
                        "loc": "registration",
                        "message": (
                            f"校准珠 {bid} 所在视野（"
                            f"{cleaned['fields'][fi]['name']}、"
                            f"{cleaned['fields'][fj]['name']}）无法经共享校准珠连到"
                            f"首视野，校正只能固定为零，但零校正下残差 "
                            f"({_num(abs(bx))}, {_num(abs(by))}) 超过上限 "
                            f"{_num(limit)}，不存在可行配准"
                        ),
                    })
            for _, _, minx, maxx, miny, maxy in pair_bounds_of(comp_set):
                objective_max_res = max(
                    objective_max_res, abs(minx), abs(maxx), abs(miny), abs(maxy))
            continue

        # 逐珠对二元预检：单独都无解的珠对可精确定位。
        # 校正量差 ci-cj 为连续整数区间 [dlo, dhi]，每个坐标轴存在可行
        # 整数差 iff 区间与 [-L-base, L-base] 相交；两轴须同时可行。
        def pair_feasible(fi: int, fj: int,
                          bx: Decimal, by: Decimal) -> bool:
            deltas = _delta_values(fi, fj, ranges, 0)
            dlo, dhi = min(deltas), max(deltas)
            for base in (bx, by):
                if dhi < -limit - base or dlo > limit - base:
                    return False
            return True

        for bid, fi, fj, bx, by in pairs_of(main_set):
            if not pair_feasible(fi, fj, bx, by):
                fatal.append({
                    "loc": "registration",
                    "message": (
                        f"校准珠 {bid} 在视野 {cleaned['fields'][fi]['name']} 与 "
                        f"{cleaned['fields'][fj]['name']} 间不存在满足残差上限 "
                        f"{_num(limit)} 的整数校正（原平移滤膜坐标差 "
                        f"({_num(bx)}, {_num(by)})，"
                        f"超出两视野校正范围可达区间）"
                    ),
                })
        if fatal:
            return None, None, None, None, fatal

        # 同视野对的多颗珠合并为矩形区间约束后再构造 CSP
        csp = _RegistrationCSP(
            comp, ranges, pair_bounds_of(main_set), fix_zero_field=0)

        if not csp.feasible(limit):
            bead_ids = sorted({bid for (bid, *_rest) in pairs_of(main_set)})
            fatal.append({
                "loc": "registration",
                "message": (
                    "不存在可行配准：与首视野连通的视野（"
                    + "、".join(cleaned["fields"][v]["name"] for v in comp)
                    + "）的共享校准珠（" + "、".join(bead_ids)
                    + "）约束在各视野校正范围内无联合整数解"
                ),
            })
            break

        # 第一目标：最小最大残差 —— 在候选瓶颈值上二分
        candidates = csp.residual_threshold_candidates(limit)
        lo, hi = 0, len(candidates) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if csp.feasible(candidates[mid]):
                hi = mid
            else:
                lo = mid + 1
        best_cutoff = candidates[lo]
        # 第二、三目标：曼哈顿和最小，再录入顺序校正序列字典序最小
        _, sol, actual_max = csp.best_solution(best_cutoff)
        corrections.update(sol)
        objective_max_res = max(objective_max_res, actual_max)

    if fatal:
        return None, None, None, None, fatal

    # 珠位证据（校正后）
    evidence = _bead_evidence(
        bead_index, cleaned, shifts, corrections, limit, main_set
    )

    total_man = sum(abs(cx) + abs(cy) for cx, cy in corrections.values())
    objective = {
        "maxResidual": _num(objective_max_res),
        "totalCorrectionManhattan": total_man,
        "tieBreakSequence": [
            {"x": corrections[i][0], "y": corrections[i][1]}
            for i in range(n_fields)
        ],
    }
    return corrections, sorted(disconnected), evidence, objective, []


def _bead_evidence(bead_index, cleaned, shifts, corrections, limit, main_set):
    """组装每颗校准珠的证据：出现点、原/校正滤膜坐标、成对残差。"""
    field_names = [f["name"] for f in cleaned["fields"]]
    evidence = []
    for bid in sorted(bead_index):
        occ = bead_index[bid]
        used = len(occ) >= 2
        observations = []
        for fi, x, y in occ:
            sx, sy = shifts[fi]
            cx, cy = corrections[fi]
            observations.append({
                "fieldIndex": fi,
                "fieldName": field_names[fi],
                "local": {"x": x, "y": y},
                "unregisteredFilter": {"x": _num(Decimal(x) + sx),
                                        "y": _num(Decimal(y) + sy)},
                "filter": {"x": _num(Decimal(x) + sx + cx),
                           "y": _num(Decimal(y) + sy + cy)},
                "correction": {"x": cx, "y": cy},
                "connectedToFirst": fi in main_set,
            })
        pair_residuals = []
        max_dx = max_dy = ZERO
        within = True
        for a in range(len(occ)):
            for b in range(a + 1, len(occ)):
                fi, xi, yi = occ[a]
                fj, xj, yj = occ[b]
                dx = ((Decimal(xi) + shifts[fi][0] + corrections[fi][0])
                      - (Decimal(xj) + shifts[fj][0] + corrections[fj][0]))
                dy = ((Decimal(yi) + shifts[fi][1] + corrections[fi][1])
                      - (Decimal(yj) + shifts[fj][1] + corrections[fj][1]))
                adx, ady = abs(dx), abs(dy)
                max_dx = max(max_dx, adx)
                max_dy = max(max_dy, ady)
                ok = adx <= limit and ady <= limit
                within = within and ok
                pair_residuals.append({
                    "fromFieldIndex": fi,
                    "fromFieldName": field_names[fi],
                    "toFieldIndex": fj,
                    "toFieldName": field_names[fj],
                    "deltaX": _num(adx),
                    "deltaY": _num(ady),
                    "withinLimit": ok,
                })
        evidence.append({
            "beadId": bid,
            "occurrenceCount": len(occ),
            "usedInConstraints": used,
            "observations": observations,
            "pairResiduals": pair_residuals,
            "maxDeltaX": _num(max_dx),
            "maxDeltaY": _num(max_dy),
            "withinLimit": within if used else True,
        })
    return evidence


# --------------------------------------------------------------------------- #
# 响应组装与成功结果保留
# --------------------------------------------------------------------------- #
def review_request(payload: Any) -> dict[str, Any]:
    """配准复核入口：校验 → 求解 → 校正平移重跑去重；失败抛 ValidationError。"""
    prepared = validate_review(payload)
    corrections, disconnected, evidence, objective, fatal = solve_registration(prepared)
    if fatal:
        raise ValidationError(fatal, payload)

    cleaned = prepared["cleaned"]
    limit = prepared["bead_limit"]

    # 用校正后的平移构造去重输入（复用既有精确求解，不重复实现）
    corrected_cleaned = {
        "tolerance": cleaned["tolerance"],
        "draftId": cleaned.get("draftId"),
        "fields": [
            {
                "name": f["name"],
                "sx": f["sx"] + corrections[i][0],
                "sy": f["sy"] + corrections[i][1],
                "particles": f["particles"],
            }
            for i, f in enumerate(cleaned["fields"])
        ],
    }
    dedup_result = build_response(corrected_cleaned)

    fields_out = []
    for i, f in enumerate(cleaned["fields"]):
        cx, cy = corrections[i]
        fields_out.append({
            "fieldIndex": i,
            "fieldName": f["name"],
            "originalShift": {"x": _num(f["sx"]), "y": _num(f["sy"])},
            "correctionRange": prepared["ranges"][i],
            "correction": {"x": cx, "y": cy},
            "correctedShift": {"x": _num(f["sx"] + cx), "y": _num(f["sy"] + cy)},
            "connectedToFirst": i not in disconnected,
        })

    used_beads = [e for e in evidence if e["usedInConstraints"]]
    result = {
        "draftId": prepared["draft_id"],
        "tolerance": _num(cleaned["tolerance"]),
        "beadResidualLimit": _num(limit),
        "summary": {
            "fieldCount": len(cleaned["fields"]),
            "connectedFieldCount": len(cleaned["fields"]) - len(disconnected),
            "disconnectedFieldCount": len(disconnected),
            "usedBeadCount": len(used_beads),
            "singleOccurrenceBeadCount": len(evidence) - len(used_beads),
            "maxResidual": objective["maxResidual"],
            "totalCorrectionManhattan": objective["totalCorrectionManhattan"],
            "finalParticleCount": dedup_result["summary"]["finalParticleCount"],
        },
        "fields": fields_out,
        "registration": {
            "feasible": True,
            "firstFieldFixed": {"x": 0, "y": 0},
            "objective": objective,
            "disconnectedFields": disconnected,
            "beads": evidence,
        },
        "deduplication": dedup_result,
    }
    return result


def save_last_review(review: dict[str, Any]) -> None:
    global _LAST_REVIEW
    _LAST_REVIEW = review


def get_last_review() -> dict[str, Any] | None:
    return _LAST_REVIEW


def reset_last_review() -> None:
    """仅供测试重置服务端保留的上次成功结果。"""
    global _LAST_REVIEW
    _LAST_REVIEW = None
