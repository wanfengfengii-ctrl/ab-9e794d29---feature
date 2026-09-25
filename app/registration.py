"""配准复核：以全局唯一编号的校准珠联合选择各视野的整数校正量。

背景：部分显微视野的登记平移受载物台漂移影响。分析员为每个视野补录校准珠
（全局唯一编号 + 局部整数坐标），填写 0–5 格的单视野校正范围与珠位残差上限，
发起配准复核。

规则（精确定义）：
- 首个视野的校正量固定为 (0, 0)；其余每个视野 i 的校正量 (cx_i, cy_i) 均为
  整数，且 |cx_i|、|cy_i| 均不超过该视野填写的校正范围。
- 同一编号校准珠在换算到滤膜坐标（局部坐标 + 登记平移 + 校正量）后，
  任意两个出现视野之间的横向残差 |Δx| 与纵向残差 |Δy| 均不得超过残差上限。
- 只出现一次的校准珠不参与任何约束（仅计数、留证）。
- 视野之间以「出现不少于两次的同编号校准珠」连边；无法经共享校准珠
  （允许链式间接）连到首视野的视野无法确定校正量，属不可行配准，
  错误逐项定位到该视野。
- 在所有可行方案上依次优化：
  1) 全部同编号校准珠成对横纵残差中的最大坐标残差最小；
  2) 全部视野校正量的曼哈顿和 Σ(|cx|+|cy|) 最小；
  3) 仍相同则取「按视野录入顺序展开的校正坐标序列」
     ((cx0,cy0),(cx1,cy1),…) 字典序最小，得到唯一结论。
- 可行时用校正后的平移重新执行既有颗粒去重（app.dedup，规则不变）。

规模：视野 3–5、校正范围 0–5（每轴整数域至多 11 个值、单视野候选至多 121 个）。
求解器把问题建模为小规模整数 CSP：以共享珠视野对为边预计算整数校正差的
横/纵残差表，MRV + 双轴前向检查 +（最大残差、曼哈顿和）下界分支定界，
不做贪心近似，结论确定且可重复。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .dedup import (
    ValidationError,
    _is_int,
    _is_num,
    _num,
    build_response,
    validate,
)

ZERO = Decimal(0)

# 单视野校正量（横、纵各自）允许填写的格数范围
MIN_CORRECTION_RANGE = 0
MAX_CORRECTION_RANGE = 5
# 单视野校准珠登记数量上限（共享珠通常稀疏；超限按不合规处理并定位）
MAX_BEADS_PER_FIELD = 100


@dataclass(frozen=True)
class BeadOcc:
    """一颗校准珠在某视野中的一次出现。"""

    field: int
    x: int
    y: int


def _err(errors: list[dict[str, str]], loc: str, message: str) -> None:
    errors.append({"loc": loc, "message": message})


# --------------------------------------------------------------------------- #
# 校准珠 / 校正范围 / 残差上限校验
# --------------------------------------------------------------------------- #
def validate_beads(payload: Any, n_fields: int) -> dict[str, Any]:
    """校验配准复核专属输入，返回清洗结果；不合规抛 ValidationError。

    去重主体（tolerance/fields/shift/particles）的校验由 dedup.validate 负责，
    本函数只处理 correctionRange、calibrationBeads 与 beadResidualLimit。
    """
    errors: list[dict[str, str]] = []

    # 珠位残差上限（横纵共用，非负数字）
    limit_loc = "beadResidualLimit"
    limit_raw = payload.get("beadResidualLimit")
    if not _is_num(limit_raw):
        _err(errors, limit_loc, "珠位残差上限必须是非负数字（横纵残差共用）")
        residual_limit = ZERO
    else:
        residual_limit = Decimal(str(limit_raw))
        if residual_limit < 0:
            _err(errors, limit_loc, "珠位残差上限不能为负数")

    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, list):
        raw_fields = []

    # field_index -> {range: int, beads: [{id,x,y}]}
    per_field: list[dict[str, Any]] = []

    for i in range(n_fields):
        floc = f"fields[{i}]"
        rf = raw_fields[i] if i < len(raw_fields) and isinstance(raw_fields[i], dict) else {}

        # 单视野校正范围
        rng = 5  # 未填写时采用最宽范围，不因此阻塞复核
        rng_raw = rf.get("correctionRange")
        if rng_raw is not None:
            if not _is_int(rng_raw):
                _err(errors, f"{floc}.correctionRange",
                     "单视野校正范围必须是 0 至 5 之间的整数")
            elif not (MIN_CORRECTION_RANGE <= rng_raw <= MAX_CORRECTION_RANGE):
                _err(errors, f"{floc}.correctionRange",
                     f"单视野校正范围必须在 {MIN_CORRECTION_RANGE} 至 "
                     f"{MAX_CORRECTION_RANGE} 格之间，当前为 {rng_raw}")
            else:
                rng = rng_raw

        # 校准珠列表：编号是物理校准珠的全局唯一身份，同一编号在不同视野
        # 各出现一次正是跨视野共享证据；但同一视野内不得重复登记。
        raw_beads = rf.get("calibrationBeads")
        if raw_beads is None:
            raw_beads = []
        if not isinstance(raw_beads, list):
            _err(errors, f"{floc}.calibrationBeads",
                 "calibrationBeads 必须是数组（每项含全局唯一编号 id 与整数坐标 x、y）")
            raw_beads = []
        elif len(raw_beads) > MAX_BEADS_PER_FIELD:
            _err(errors, f"{floc}.calibrationBeads",
                 f"单个视野的校准珠不能超过 {MAX_BEADS_PER_FIELD} 颗，"
                 f"当前为 {len(raw_beads)} 颗")
            raw_beads = []  # 超限时跳过逐项解析，错误已定位到该字段

        beads: list[dict[str, Any]] = []
        local_ids: set[str] = set()
        local_coords: set[tuple[int, int]] = set()
        for j, rb in enumerate(raw_beads):
            bloc = f"{floc}.calibrationBeads[{j}]"
            if not isinstance(rb, dict):
                _err(errors, bloc, "校准珠必须是对象")
                continue

            bid = rb.get("id")
            if not isinstance(bid, str) or not bid.strip():
                _err(errors, f"{bloc}.id", "校准珠编号必须是非空字符串")
                bid = None
            else:
                bid = bid.strip()
                if len(bid) > 64:
                    _err(errors, f"{bloc}.id", "校准珠编号长度不能超过 64")
                if bid in local_ids:
                    _err(errors, f"{bloc}.id",
                         f"校准珠编号 {bid} 在同一视野内重复登记（同一编号在不同"
                         f"视野各出现一次才是合法的共享校准珠）")
                else:
                    local_ids.add(bid)

            bx, by = rb.get("x"), rb.get("y")
            if not _is_int(bx):
                _err(errors, f"{bloc}.x", "校准珠局部坐标 x 必须是整数")
            if not _is_int(by):
                _err(errors, f"{bloc}.y", "校准珠局部坐标 y 必须是整数")

            if bid is not None and _is_int(bx) and _is_int(by):
                if (bx, by) in local_coords:
                    _err(errors, bloc,
                         f"该视野内校准珠整数坐标 ({bx}, {by}) 与其他校准珠重复")
                else:
                    local_coords.add((bx, by))
                beads.append({"id": bid, "x": bx, "y": by})

        per_field.append({"range": rng, "beads": beads})

    if errors:
        raise ValidationError(errors, payload)

    return {"residualLimit": residual_limit, "perField": per_field}


# --------------------------------------------------------------------------- #
# 校正量联合求解
# --------------------------------------------------------------------------- #
@dataclass
class ReviewSolution:
    corrections: list[tuple[int, int]]       # 每视野 (cx, cy)，首视野恒为 (0, 0)
    groups: dict[str, list[BeadOcc]]         # 共享珠（出现 ≥ 2 次）编号 -> 出现列表
    singletons: list[tuple[str, BeadOcc]]    # 只出现一次的校准珠（不参与约束）
    max_dx: Decimal
    max_dy: Decimal
    correction_manhattan: int


def _shared_bead_groups(per_field: list[dict[str, Any]]) -> tuple[
    dict[str, list[BeadOcc]], list[tuple[str, BeadOcc]]
]:
    """按编号汇总校准珠，分为共享珠（≥2 次）与单次珠（不参与约束）。"""
    occurrences: dict[str, list[BeadOcc]] = {}
    for fi, f in enumerate(per_field):
        for b in f["beads"]:
            occurrences.setdefault(b["id"], []).append(
                BeadOcc(field=fi, x=b["x"], y=b["y"])
            )
    groups = {bid: occs for bid, occs in occurrences.items() if len(occs) >= 2}
    singletons = [
        (bid, occs[0]) for bid, occs in occurrences.items() if len(occs) == 1
    ]
    return groups, singletons


def _field_graph(
    groups: dict[str, list[BeadOcc]], n: int
) -> list[set[int]]:
    """以共享校准珠连边：返回视野无向图的邻接表。"""
    adj: list[set[int]] = [set() for _ in range(n)]
    for occs in groups.values():
        fs = sorted({o.field for o in occs})
        for a in range(len(fs)):
            for b in range(a + 1, len(fs)):
                adj[fs[a]].add(fs[b])
                adj[fs[b]].add(fs[a])
    return adj


def _connected_fields(groups: dict[str, list[BeadOcc]], n: int) -> list[bool]:
    """以共享校准珠连边，返回各视野能否（允许链式）连到首视野。"""
    adj = _field_graph(groups, n)
    reachable = [False] * n
    reachable[0] = True
    stack = [0]
    while stack:
        v = stack.pop()
        for w in adj[v]:
            if not reachable[w]:
                reachable[w] = True
                stack.append(w)
    return reachable


def _edge_diff_tables(fields, groups, n):
    """每条共享珠视野边 (i,j)（i<j）登记滤膜坐标差与整数 delta 残差表。

    校正后 i、j 横残差 = |dx0 + cx_i - cx_j|（纵同理）；
    tab_x[(i,j)][d] = 该边上全部共享珠在整数差 d 下的最大横向残差。
    """
    sx = [f["sx"] for f in fields]
    sy = [f["sy"] for f in fields]
    tab_x: dict[tuple[int, int], dict[int, Decimal]] = {}
    tab_y: dict[tuple[int, int], dict[int, Decimal]] = {}
    for i in range(n):
        for j in range(i + 1, n):
            xs, ys = [], []
            for occs in groups.values():
                oi = next((o for o in occs if o.field == i), None)
                oj = next((o for o in occs if o.field == j), None)
                if oi is None or oj is None:
                    continue
                xs.append(Decimal(oi.x) + sx[i] - (Decimal(oj.x) + sx[j]))
                ys.append(Decimal(oi.y) + sy[i] - (Decimal(oj.y) + sy[j]))
            if not xs:
                continue
            # 两个视野校正范围之和的上限为 5+5=10，故校正差 d ∈ [-10, 10]
            span = MAX_CORRECTION_RANGE * 2
            tab_x[(i, j)] = {
                d: max(abs(v + Decimal(d)) for v in xs)
                for d in range(-span, span + 1)
            }
            tab_y[(i, j)] = {
                d: max(abs(v + Decimal(d)) for v in ys)
                for d in range(-span, span + 1)
            }
    return tab_x, tab_y


def _edge_residual(tab, i: int, ci: int, j: int, cj: int) -> Decimal:
    """视野 i、j 校正分别为 ci、cj 时该边在 tab 上的最大残差。"""
    if i < j:
        return tab[(i, j)][ci - cj]
    return tab[(j, i)][cj - ci]


def _axis_feasible(axis_tab, graph, domains, n, limit) -> bool:
    """单轴整数 CSP 可行性：边差 ci-cj 必须使该轴残差不超过 limit。

    变量域每轴至多 11 个整数、视野至多 5 个，MRV + 前向检查微秒级。
    """
    delta_ok: dict[tuple[int, int], set[int]] = {}
    for (i, j), tab in axis_tab.items():
        delta_ok[(i, j)] = {d for d, r in tab.items() if r <= limit}

    dom = [set(d) for d in domains]
    assigned: dict[int, int] = {0: 0}

    def restrict(k: int, v: int):
        """赋值 c_k=v 后收缩未赋值邻居域，返回 (快照, 受影响邻居)。"""
        snapshot = []
        for j in graph[k]:
            if j in assigned:
                continue
            e = (k, j) if k < j else (j, k)
            ok = delta_ok[e]
            if k < j:
                allowed = {v - d for d in ok}   # v - c_j ∈ ok
            else:
                allowed = {v + d for d in ok}   # c_j - v ∈ ok
            snapshot.append((j, set(dom[j])))
            dom[j] &= allowed
            if not dom[j]:
                return snapshot, False
        return snapshot, True

    def restore(snapshot):
        for j, old in snapshot:
            dom[j] = old

    def dfs() -> bool:
        if len(assigned) == n:
            return True
        k = min(
            (v for v in range(n) if v not in assigned),
            key=lambda v: (len(dom[v]),
                           -sum(1 for w in graph[v] if w in assigned), v),
        )
        # 使与已赋值邻居残差更小的值优先（尽快找到可行解即可）
        def val_key(v):
            r = ZERO
            for j in graph[k]:
                if j in assigned:
                    rv = _edge_residual(axis_tab, k, v, j, assigned[j])
                    if rv > r:
                        r = rv
            return (r, abs(v), v)

        for v in sorted(dom[k], key=val_key):
            snapshot, ok = restrict(k, v)
            if ok:
                assigned[k] = v
                if dfs():
                    return True
                del assigned[k]
            restore(snapshot)
        return False

    # 首视野已固定为 0：先对其邻居做一次前向检查
    snapshot, ok = restrict(0, 0)
    if not ok:
        restore(snapshot)
        return False
    result = dfs()
    restore(snapshot)
    return result


def solve_corrections(
    cleaned: dict[str, Any], review: dict[str, Any]
) -> ReviewSolution:
    """联合选择各视野整数校正量，按三级目标取唯一最优；不可行抛 ValidationError。

    建模为小规模整数 CSP（视野 3–5，每轴域至多 11 个整数）：
    - 边约束：共享校准珠的视野对，其校正差使横/纵残差均不超过上限；
    - MRV + 双轴前向检查搜索完整可行解，按（最大坐标残差、曼哈顿和、
      校正坐标序列）维护唯一最优，并用残差/曼哈顿下界分支定界；
    - 不做贪心近似，结论确定且可重复。
    """
    fields = cleaned["fields"]
    n = len(fields)
    per_field = review["perField"]
    limit = review["residualLimit"]

    groups, singletons = _shared_bead_groups(per_field)

    errors: list[dict[str, str]] = []
    reachable = _connected_fields(groups, n)
    for i in range(1, n):
        if not reachable[i]:
            _err(errors, f"fields[{i}].calibrationBeads",
                 f"该视野无法经出现不少于两次的共享校准珠（允许链式间接）"
                 f"连到首视野 {fields[0]['name']}，其校正量无法确定，"
                 f"不存在可行配准")
    if errors:
        raise ValidationError(errors, review["draft"])

    graph = _field_graph(groups, n)
    ranges = [0] + [per_field[i]["range"] for i in range(1, n)]
    domains = [set(range(-r, r + 1)) for r in ranges]
    tab_x, tab_y = _edge_diff_tables(fields, groups, n)
    dx_ok = {e: {d for d, r in tab.items() if r <= limit}
             for e, tab in tab_x.items()}
    dy_ok = {e: {d for d, r in tab.items() if r <= limit}
             for e, tab in tab_y.items()}

    dom_x = [set(d) for d in domains]
    dom_y = [set(d) for d in domains]
    assigned: dict[int, tuple[int, int]] = {0: (0, 0)}
    # 现任最优：(obj1=max(横,纵), 曼哈顿和, 校正序列, 最大横, 最大纵)
    incumbent: tuple[Decimal, int, tuple, Decimal, Decimal] | None = None

    def restrict(k: int, vx: int, vy: int):
        """赋值后收缩未赋值邻居的横纵整数域，返回快照；任一域空则失败。"""
        snapshot = []
        for j in graph[k]:
            if j in assigned:
                continue
            e = (k, j) if k < j else (j, k)
            if k < j:
                okx = {vx - d for d in dx_ok[e]}
                oky = {vy - d for d in dy_ok[e]}
            else:
                okx = {vx + d for d in dx_ok[e]}
                oky = {vy + d for d in dy_ok[e]}
            snapshot.append((j, set(dom_x[j]), set(dom_y[j])))
            dom_x[j] &= okx
            dom_y[j] &= oky
            if not dom_x[j] or not dom_y[j]:
                return snapshot, False
        return snapshot, True

    def restore(snapshot):
        for j, old_x, old_y in snapshot:
            dom_x[j] = old_x
            dom_y[j] = old_y

    def bounds(cur_max: Decimal, cur_manh: int):
        """返回 (最大残差下界, 曼哈顿下界)；某未赋值视野域已空则返回 None。

        对每个未赋值视野只考虑与已赋值邻居的边，取其当前域内可达的最小
        最大残差（忽略未赋值视野之间的边，是合法的乐观下界）；曼哈顿下界
        取各未赋值视野当前域内最小 |x|+|y| 之和。
        """
        lb = cur_max
        lb_manh = cur_manh
        for k in range(n):
            if k in assigned:
                continue
            nbrs = [j for j in graph[k] if j in assigned]
            best_r, best_m = None, None
            for vx in dom_x[k]:
                for vy in dom_y[k]:
                    rmx = rmy = ZERO
                    for j in nbrs:
                        rx = _edge_residual(tab_x, k, vx, j, assigned[j][0])
                        ry = _edge_residual(tab_y, k, vy, j, assigned[j][1])
                        if rx > rmx:
                            rmx = rx
                        if ry > rmy:
                            rmy = ry
                    r = max(rmx, rmy)
                    if best_r is None or r < best_r:
                        best_r = r
                    m = abs(vx) + abs(vy)
                    if best_m is None or m < best_m:
                        best_m = m
            if best_r is None:
                return None  # 域已空（理论上前向检查会提前拦截）
            if best_r > lb:
                lb = best_r
            lb_manh += best_m
        return lb, lb_manh

    def dfs(cur_mdx: Decimal, cur_mdy: Decimal, cur_manh: int):
        nonlocal incumbent
        if len(assigned) == n:
            seq = tuple(assigned[k] for k in range(n))
            cand = (max(cur_mdx, cur_mdy), cur_manh, seq)
            if incumbent is None or cand < incumbent[:3]:
                incumbent = (cand[0], cur_manh, seq, cur_mdx, cur_mdy)
            return

        # MRV：当前双轴域乘积最小的未赋值视野，平局取已赋值邻居更多者
        k = min(
            (v for v in range(n) if v not in assigned),
            key=lambda v: (len(dom_x[v]) * len(dom_y[v]),
                           -sum(1 for w in graph[v] if w in assigned), v),
        )
        nbrs = [j for j in graph[k] if j in assigned]

        def val_score(vx: int, vy: int):
            rmx = rmy = ZERO
            for j in nbrs:
                rx = _edge_residual(tab_x, k, vx, j, assigned[j][0])
                ry = _edge_residual(tab_y, k, vy, j, assigned[j][1])
                if rx > rmx:
                    rmx = rx
                if ry > rmy:
                    rmy = ry
            return (max(rmx, rmy), abs(vx) + abs(vy), (vx, vy))

        values = sorted(
            ((vx, vy) for vx in dom_x[k] for vy in dom_y[k]),
            key=lambda v: val_score(*v),
        )
        for vx, vy in values:
            add_x = add_y = ZERO
            for j in nbrs:
                rx = _edge_residual(tab_x, k, vx, j, assigned[j][0])
                ry = _edge_residual(tab_y, k, vy, j, assigned[j][1])
                if rx > add_x:
                    add_x = rx
                if ry > add_y:
                    add_y = ry
            new_mdx = max(cur_mdx, add_x)
            new_mdy = max(cur_mdy, add_y)
            new_max = max(new_mdx, new_mdy)
            new_manh = cur_manh + abs(vx) + abs(vy)

            # 廉价单调剪枝：目标一随赋值只增不减，已超过现任最优则放弃
            if incumbent is not None and new_max > incumbent[0]:
                continue

            snapshot, ok = restrict(k, vx, vy)
            if not ok:
                restore(snapshot)
                continue

            # 注意：必须先把 k 记入 assigned 再算下界，否则会把 k 当作
            # 未赋值视野，重复计入其曼哈顿贡献（会错误剪枝掉更优解）。
            assigned[k] = (vx, vy)
            prune = False
            if incumbent is not None and new_max == incumbent[0]:
                # 残差已与现任最优持平：只有下界显示能在残差上追平且曼哈顿
                # 不更劣时才继续；new_max < obj1 时仅由可行性决定，必入递归。
                bd = bounds(new_max, new_manh)
                if bd is None:
                    prune = True
                else:
                    lb1, lb2 = bd
                    if lb1 > incumbent[0] or (
                        lb1 == incumbent[0] and lb2 > incumbent[1]
                    ):
                        prune = True
            if not prune:
                dfs(new_mdx, new_mdy, new_manh)
            del assigned[k]
            restore(snapshot)

    snapshot, ok0 = restrict(0, 0, 0)
    if ok0:
        dfs(ZERO, ZERO, 0)
    restore(snapshot)

    if incumbent is None:
        # 横纵两轴各自独立的最小可达残差：用于说明「为何不存在可行配准」。
        # 两轴约束在数学上可分离，分别是一维整数 CSP；对候选残差档位二分。
        def axis_minimum(axis_tab):
            levels = sorted({r for tab in axis_tab.values() for r in tab.values()})
            lo, hi = 0, len(levels) - 1
            best = None
            while lo <= hi:
                mid = (lo + hi) // 2
                if _axis_feasible(axis_tab, graph, domains, n, levels[mid]):
                    best = levels[mid]
                    hi = mid - 1
                else:
                    lo = mid + 1
            return best

        min_x = axis_minimum(tab_x)
        min_y = axis_minimum(tab_y)
        raise ValidationError(
            [{
                "loc": "beadResidualLimit",
                "message": (
                    "不存在可行配准：在各视野校正范围内，同编号校准珠换算到滤膜坐标后"
                    f"横向最小可达最大残差为 {_num(min_x)}、纵向为 {_num(min_y)}"
                    f"（横纵可分别达到），但不存在横纵残差同时不超过上限 "
                    f"{_num(limit)} 的联合整数校正方案"
                ),
            }],
            review["draft"],
        )

    _, best_manh, best_seq, best_dx, best_dy = incumbent
    return ReviewSolution(
        corrections=list(best_seq),
        groups=dict(sorted(groups.items())),
        singletons=sorted(singletons, key=lambda t: (t[1].field, t[0])),
        max_dx=best_dx,
        max_dy=best_dy,
        correction_manhattan=best_manh,
    )


# --------------------------------------------------------------------------- #
# 响应组装
# --------------------------------------------------------------------------- #
def _shift(x: Decimal, y: Decimal) -> dict[str, Any]:
    return {"x": _num(x), "y": _num(y)}


def build_review_response(payload: Any) -> dict[str, Any]:
    """配准复核完整入口：校验 → 联合求解校正量 → 校正后平移重跑去重 → 组装证据。"""
    # 既有去重输入规则全部沿用；与配准复核专属校验合并反馈，
    # 使页面一次定位全部问题（不合规同样 422、原样保留草稿）。
    errors: list[dict[str, str]] = []
    cleaned = None
    try:
        cleaned = validate(payload)
    except ValidationError as exc:
        errors.extend(exc.errors)

    if isinstance(payload, dict) and isinstance(payload.get("fields"), list):
        n_fields = len(cleaned["fields"]) if cleaned is not None else len(payload["fields"])
    else:
        n_fields = 0

    review = None
    if isinstance(payload, dict):
        try:
            review = validate_beads(payload, n_fields)
        except ValidationError as exc:
            errors.extend(exc.errors)

    if errors:
        raise ValidationError(errors, payload)

    review["draft"] = payload
    solution = solve_corrections(cleaned, review)

    fields = cleaned["fields"]
    n = len(fields)
    corrections = solution.corrections

    # 以校正后的平移重新执行既有颗粒去重（dedup 规则与接口形态保持不变）
    corrected = {
        "tolerance": cleaned["tolerance"],
        "draftId": cleaned.get("draftId"),
        "fields": [
            {
                "name": fields[i]["name"],
                "sx": fields[i]["sx"] + Decimal(corrections[i][0]),
                "sy": fields[i]["sy"] + Decimal(corrections[i][1]),
                "particles": fields[i]["particles"],
            }
            for i in range(n)
        ],
    }
    dedup_result = build_response(corrected)

    # 逐视野校正方案
    corr_entries: list[dict[str, Any]] = []
    for i, f in enumerate(fields):
        cx, cy = corrections[i]
        corr_entries.append({
            "fieldIndex": i,
            "fieldName": f["name"],
            "correctionRange": review["perField"][i]["range"],
            "originalShift": _shift(f["sx"], f["sy"]),
            "correction": {"x": cx, "y": cy},
            "correctedShift": _shift(
                f["sx"] + Decimal(cx), f["sy"] + Decimal(cy)
            ),
        })

    # 珠位证据：共享珠逐出现、逐对残差；单次珠标注不参与约束。
    # 所有出现记录（含单次珠）字段形态一致，便于接口与前端统一核对。
    def occurrence_record(o: BeadOcc) -> tuple[dict[str, Any], Decimal, Decimal]:
        cx, cy = corrections[o.field]
        fx_reg = Decimal(o.x) + fields[o.field]["sx"]
        fy_reg = Decimal(o.y) + fields[o.field]["sy"]
        fx_new = fx_reg + Decimal(cx)
        fy_new = fy_reg + Decimal(cy)
        return {
            "fieldIndex": o.field,
            "fieldName": fields[o.field]["name"],
            "local": {"x": o.x, "y": o.y},
            "registeredShift": _shift(
                fields[o.field]["sx"], fields[o.field]["sy"]
            ),
            "registeredFilter": {"x": _num(fx_reg), "y": _num(fy_reg)},
            "correction": {"x": cx, "y": cy},
            "correctedShift": _shift(
                fields[o.field]["sx"] + Decimal(cx),
                fields[o.field]["sy"] + Decimal(cy),
            ),
            "filter": {"x": _num(fx_new), "y": _num(fy_new)},
        }, fx_new, fy_new

    bead_evidence: list[dict[str, Any]] = []
    for bid, occs in solution.groups.items():
        occ_sorted = sorted(occs, key=lambda o: o.field)
        occurrences = []
        coords = {}
        for o in occ_sorted:
            rec, fx_new, fy_new = occurrence_record(o)
            coords[o.field] = (fx_new, fy_new)
            occurrences.append(rec)
        pairwise = []
        for a in range(len(occ_sorted)):
            for b in range(a + 1, len(occ_sorted)):
                oa, ob = occ_sorted[a], occ_sorted[b]
                dx = abs(coords[oa.field][0] - coords[ob.field][0])
                dy = abs(coords[oa.field][1] - coords[ob.field][1])
                pairwise.append({
                    "from": {"fieldIndex": oa.field,
                             "fieldName": fields[oa.field]["name"]},
                    "to": {"fieldIndex": ob.field,
                           "fieldName": fields[ob.field]["name"]},
                    "deltaX": _num(dx),
                    "deltaY": _num(dy),
                    "manhattan": _num(dx + dy),
                    "withinLimit": dx <= review["residualLimit"]
                                   and dy <= review["residualLimit"],
                })
        bead_evidence.append({
            "beadId": bid,
            "occurrenceCount": len(occ_sorted),
            "participates": True,
            "occurrences": occurrences,
            "pairwiseResiduals": pairwise,
        })

    for bid, o in solution.singletons:
        bead_evidence.append({
            "beadId": bid,
            "occurrenceCount": 1,
            "participates": False,
            "occurrences": [occurrence_record(o)[0]],
            "pairwiseResiduals": [],
        })

    total_beads = sum(len(f["beads"]) for f in review["perField"])
    registration_review = {
        "beadResidualLimit": _num(review["residualLimit"]),
        "summary": {
            "fieldCount": n,
            "calibrationBeadCount": total_beads,
            "sharedBeadCount": len(solution.groups),
            "singletonBeadCount": len(solution.singletons),
            "unconnectedFieldCount": 0,
            "maxResidualX": _num(solution.max_dx),
            "maxResidualY": _num(solution.max_dy),
            "maxCoordinateResidual": _num(max(solution.max_dx, solution.max_dy)),
            "totalCorrectionManhattan": solution.correction_manhattan,
        },
        "unconnectedFields": [],
        "corrections": corr_entries,
        "beadEvidence": bead_evidence,
    }

    # 去重结论保持既有顶层形态，配准复核块并列返回，便于接口自动核对
    result = dict(dedup_result)
    result["registrationReview"] = registration_review
    return result


def solve_review_request(payload: Any) -> dict[str, Any]:
    """Web 层入口：任何不合规都抛 ValidationError（422 + 可定位 + 草稿保留）。"""
    return build_review_response(payload)
