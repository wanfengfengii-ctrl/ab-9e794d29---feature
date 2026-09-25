"""微塑料颗粒跨视野去重裁决核心逻辑。

规则概述：
- 仅当两个观测满足「不同视野、类别相同、换算到滤膜坐标后横纵差均不超过容差」时，
  二者之间才允许建立关联（候选边）。
- 一个最终颗粒 = 一个由所选关联连通的观测集合，且同一视野至多一个观测。
- 在所有完整方案（覆盖全部观测的划分）上依次优化：
  1) 最终颗粒数最少；
  2) 所选关联的曼哈顿差总和最小（每个颗粒取其最小生成树）；
  3) 仍相同，则取「按视野录入顺序展开的伙伴编号序列」字典序最小的唯一结论。

注意：本实现枚举所有可行的连通「彩虹」集合（每视野至多一个成员），
用带缓存的精确划分搜索求全局最优，不做局部最近边贪心合并。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import cached_property
from typing import Any

ZERO = Decimal(0)

# 跨视野精确去重裁决是 NP 难的组合优化；为单次请求设置明确规模边界，
# 超限输入按不合规处理并给出可定位反馈。
MAX_FIELDS = 5
MIN_FIELDS = 3
MAX_PARTICLES_PER_FIELD = 30


class ValidationError(Exception):
    """输入不合规：errors 中每一项都可定位到具体字段路径。"""

    def __init__(self, errors: list[dict[str, str]], draft: Any):
        super().__init__("输入校验失败")
        self.errors = errors
        self.draft = draft


@dataclass(frozen=True)
class Obs:
    field: int          # 视野录入序号（0 起）
    pid: str            # 该视野内唯一的伙伴编号
    x: int
    y: int
    category: str
    fx: Decimal         # 换算后的滤膜坐标
    fy: Decimal

    @property
    def key(self) -> tuple[int, str]:
        return (self.field, self.pid)


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #
def _is_int(v: Any) -> bool:
    # bool 是 int 的子类，显式排除
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    # JSON 数字为 int/float；bool 显式排除
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def _err(errors: list[dict[str, str]], loc: str, message: str) -> None:
    errors.append({"loc": loc, "message": message})


def validate(payload: Any) -> dict[str, Any]:
    """校验并清洗草稿；不合规则抛 ValidationError（附带可定位反馈与原草稿）。"""
    errors: list[dict[str, str]] = []

    if not isinstance(payload, dict):
        raise ValidationError(
            [{"loc": "$", "message": "请求体必须是 JSON 对象"}], payload
        )

    # tolerance
    tol_loc = "tolerance"
    tol_raw = payload.get("tolerance")
    if not _is_num(tol_raw):
        _err(errors, tol_loc, "容差必须是非负数字（横纵差共用）")
        tolerance = ZERO
    else:
        tolerance = Decimal(str(tol_raw))
        if tolerance < 0:
            _err(errors, tol_loc, "容差不能为负数")

    # fields
    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, list):
        _err(errors, "fields", "fields 必须是数组，且包含 3 至 5 个视野")
        raw_fields = []
    elif not (MIN_FIELDS <= len(raw_fields) <= MAX_FIELDS):
        _err(errors, "fields",
             f"视野数量必须在 {MIN_FIELDS} 至 {MAX_FIELDS} 之间，当前为 {len(raw_fields)}")

    cleaned_fields: list[dict[str, Any]] = []
    seen_field_names: dict[str, int] = {}

    for i, rf in enumerate(raw_fields):
        floc = f"fields[{i}]"
        if not isinstance(rf, dict):
            _err(errors, floc, "视野必须是对象")
            continue

        # 视野名称（可省略）
        name = rf.get("name")
        if name is None or (isinstance(name, str) and not name.strip()):
            name = f"视野{i + 1}"
        elif not isinstance(name, str):
            _err(errors, f"{floc}.name", "视野名称必须是字符串")
            name = f"视野{i + 1}"
        else:
            name = name.strip()
            if len(name) > 32:
                _err(errors, f"{floc}.name", "视野名称长度不能超过 32")
            if name in seen_field_names:
                _err(errors, f"{floc}.name", f"视野名称与 fields[{seen_field_names[name]}] 重复")
            else:
                seen_field_names[name] = i

        # 平移位置
        shift = rf.get("shift")
        sx = sy = ZERO
        if not isinstance(shift, dict):
            _err(errors, f"{floc}.shift", "shift 必须是包含 x、y 的平移位置对象")
        else:
            if _is_num(shift.get("x")):
                sx = Decimal(str(shift["x"]))
            else:
                _err(errors, f"{floc}.shift.x", "平移位置 x 必须是数字")
            if _is_num(shift.get("y")):
                sy = Decimal(str(shift["y"]))
            else:
                _err(errors, f"{floc}.shift.y", "平移位置 y 必须是数字")

        # 颗粒候选
        rps = rf.get("particles")
        if not isinstance(rps, list):
            _err(errors, f"{floc}.particles", "particles 必须是数组")
            rps = []
        elif len(rps) > MAX_PARTICLES_PER_FIELD:
            _err(errors, f"{floc}.particles",
                 f"单个视野的颗粒候选不能超过 {MAX_PARTICLES_PER_FIELD} 个，"
                 f"当前为 {len(rps)} 个（精确裁决为 NP 难优化，超出规模上限）")
            rps = []  # 超限时跳过逐项解析，错误已定位到该字段

        particles: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_coords: set[tuple[int, int]] = set()
        for j, rp in enumerate(rps):
            ploc = f"{floc}.particles[{j}]"
            if not isinstance(rp, dict):
                _err(errors, ploc, "颗粒候选必须是对象")
                continue

            pid = rp.get("id")
            if not isinstance(pid, str) or not pid.strip():
                _err(errors, f"{ploc}.id", "颗粒编号必须是非空字符串")
                pid = None
            else:
                pid = pid.strip()
                if len(pid) > 64:
                    _err(errors, f"{ploc}.id", "颗粒编号长度不能超过 64")
                if pid in seen_ids:
                    _err(errors, f"{ploc}.id", f"编号 {pid} 在该视野内重复")
                else:
                    seen_ids.add(pid)

            x = rp.get("x")
            y = rp.get("y")
            if not _is_int(x):
                _err(errors, f"{ploc}.x", "局部坐标 x 必须是整数")
            if not _is_int(y):
                _err(errors, f"{ploc}.y", "局部坐标 y 必须是整数")
            if _is_int(x) and _is_int(y):
                if (x, y) in seen_coords:
                    _err(errors, ploc, f"该视野内整数坐标 ({x}, {y}) 与其他颗粒重复")
                else:
                    seen_coords.add((x, y))

            category = rp.get("category")
            if not isinstance(category, str) or not category.strip():
                _err(errors, f"{ploc}.category", "聚合物类别必须是非空字符串")
                category = None
            else:
                category = category.strip()
                if len(category) > 64:
                    _err(errors, f"{ploc}.category", "类别名称长度不能超过 64")

            if pid is not None and _is_int(x) and _is_int(y) and category is not None:
                particles.append(
                    {"id": pid, "x": x, "y": y, "category": category}
                )

        cleaned_fields.append({"name": name, "sx": sx, "sy": sy, "particles": particles})

    if errors:
        raise ValidationError(errors, payload)

    return {"tolerance": tolerance, "fields": cleaned_fields,
            "draftId": payload.get("draftId")}


# --------------------------------------------------------------------------- #
# 求解
# --------------------------------------------------------------------------- #
@dataclass
class _Edge:
    u: int
    v: int
    manhattan: Decimal


class Solver:
    def __init__(self, cleaned: dict[str, Any]):
        self.tolerance: Decimal = cleaned["tolerance"]
        self.field_names = [f["name"] for f in cleaned["fields"]]
        self.obs: list[Obs] = []
        for fi, f in enumerate(cleaned["fields"]):
            for p in f["particles"]:
                self.obs.append(
                    Obs(
                        field=fi,
                        pid=p["id"],
                        x=p["x"],
                        y=p["y"],
                        category=p["category"],
                        fx=Decimal(p["x"]) + f["sx"],
                        fy=Decimal(p["y"]) + f["sy"],
                    )
                )
        self.n = len(self.obs)
        self.adj: list[list[int]] = [[] for _ in range(self.n)]
        self.edge_info: dict[tuple[int, int], _Edge] = {}
        self._build_edges()

        # 可行颗粒（连通且每视野至多一个观测的集合）的代价/MST/键，按需缓存
        self.cluster_cost: dict[frozenset[int], Decimal] = {}
        self.cluster_mst: dict[frozenset[int], tuple[tuple[int, int], ...]] = {}
        self.cluster_key: dict[frozenset[int], tuple[tuple[int, str], ...]] = {}

    def _build_edges(self) -> None:
        obs = self.obs
        tol = self.tolerance
        for i in range(self.n):
            a = obs[i]
            for j in range(i + 1, self.n):
                b = obs[j]
                if a.field == b.field or a.category != b.category:
                    continue
                dx = abs(a.fx - b.fx)
                dy = abs(a.fy - b.fy)
                if dx <= tol and dy <= tol:
                    self.adj[i].append(j)
                    self.adj[j].append(i)
                    self.edge_info[(i, j)] = _Edge(i, j, dx + dy)

    @cached_property
    def _ordered_edges(self) -> list[_Edge]:
        obs = self.obs
        return sorted(
            self.edge_info.values(),
            key=lambda e: (e.manhattan, obs[e.u].key, obs[e.v].key),
        )

    def _mst(self, members: frozenset[int]) -> tuple[Decimal, tuple[tuple[int, int], ...]]:
        """成员集合在候选边图上连通；Kruskal 按 (曼哈顿, 端点键) 定序，结果确定。"""
        parent = {v: v for v in members}

        def find(v: int) -> int:
            while parent[v] != v:
                parent[v] = parent[parent[v]]
                v = parent[v]
            return v

        total = ZERO
        chosen: list[tuple[int, int]] = []
        need = len(members) - 1
        for e in self._ordered_edges:
            if e.u not in members or e.v not in members:
                continue
            ru, rv = find(e.u), find(e.v)
            if ru == rv:
                continue
            parent[ru] = rv
            total += e.manhattan
            chosen.append((e.u, e.v))
            if len(chosen) == need:
                break
        return total, tuple(chosen)

    def _cluster_meta(self, c: frozenset[int]):
        """按需计算并缓存可行簇的 (MST 代价, MST 边, 平局键)。

        平局键 = 该最终颗粒的「伙伴编号序列」（按视野录入序号 → 视野内
        录入位置展开成员伙伴编号）。伙伴编号仅在同一视野内唯一，跨视野
        可能重名，故再以顶点位置序列兜底消歧，保证不同划分的键绝不相等，
        平局裁决得到唯一结论。
        """
        if c in self.cluster_cost:
            return self.cluster_cost[c], self.cluster_mst[c], self.cluster_key[c]
        mst_cost, mst = self._mst(c)
        order = sorted(c)
        key = (
            tuple(self.obs[v].pid for v in order),
            tuple(order),
        )
        self.cluster_cost[c] = mst_cost
        self.cluster_mst[c] = mst
        self.cluster_key[c] = key
        return mst_cost, mst, key

    def _candidate_clusters(self, seed: int, allowed: frozenset[int]):
        """枚举所有满足条件的可行颗粒集合：包含 seed、整体 ⊆ allowed、
        在候选边图上连通、且每个视野至多一个成员（单元素集合包含在内）。

        采用自 seed 出发的集合生长 DFS：只允许加入与当前集合相邻、
        视野未被占用的成员，因此每个中间集合始终连通、始终是彩虹集合。
        返回按 (规模降序, MST 代价, 编号序列键) 排序的列表，
        使划分搜索能尽早触及颗粒数下界。
        """
        found: set[frozenset[int]] = {frozenset([seed])}
        stack: list[frozenset[int]] = [frozenset([seed])]
        while stack:
            s = stack.pop()
            used_fields = {self.obs[v].field for v in s}
            frontier: set[int] = set()
            for v in s:
                for w in self.adj[v]:
                    if w in allowed and w not in s and self.obs[w].field not in used_fields:
                        frontier.add(w)
            for w in frontier:
                ns = frozenset(s | {w})
                if ns not in found:
                    found.add(ns)
                    stack.append(ns)

        options = list(found)
        options.sort(
            key=lambda c: (-len(c), self._cluster_meta(c)[0], self.cluster_key[c])
        )
        return options

    def _components(self) -> list[list[int]]:
        """候选边图的连通分量；分量之间不可能建立关联，可独立求解。"""
        comp_verts: list[list[int]] = []
        comp_id = [-1] * self.n
        for s in range(self.n):
            if comp_id[s] != -1:
                continue
            cid = len(comp_verts)
            stack = [s]
            comp_id[s] = cid
            verts: list[int] = []
            while stack:
                v = stack.pop()
                verts.append(v)
                for w in self.adj[v]:
                    if comp_id[w] == -1:
                        comp_id[w] = cid
                        stack.append(w)
            comp_verts.append(verts)
        return comp_verts

    def solve(self) -> list[frozenset[int]]:
        n_fields = len(self.field_names)
        field_of = [self.obs[v].field for v in range(self.n)]

        from functools import lru_cache

        final_clusters: list[frozenset[int]] = []

        for verts in self._components():
            root_set = frozenset(verts)

            @lru_cache(maxsize=None)
            def go(rem: frozenset[int]):
                """覆盖 rem 的最优方案：(颗粒数, 总曼哈顿, 编号序列键, 有序集合元组)。

                不预设任何合并贪心：每次都枚举 seed 所在的全部可行颗粒集合，
                递归后按三级目标比较，由记忆化搜索保证全局精确最优。
                """
                if not rem:
                    return (0, ZERO, (), ())

                # 每个最终颗粒在同一视野至多一个观测
                # → 颗粒数下界 = 任一视野剩余观测数的最大值
                counts = [0] * n_fields
                for v in rem:
                    counts[field_of[v]] += 1
                lb = max(counts)

                # 分支观测：rem 诱导子图中度数最小者，约束最强
                seed = min(
                    rem,
                    key=lambda v: (
                        sum(1 for w in self.adj[v] if w in rem),
                        self.obs[v].key,
                    ),
                )

                best = None
                for c in self._candidate_clusters(seed, rem):
                    sub_n, sub_cost, sub_key, sub_clusters = go(frozenset(rem - c))
                    total_n = 1 + sub_n
                    if best is not None and total_n > best[0]:
                        continue  # 第一目标（颗粒数）已劣于已知最优
                    c_cost = self.cluster_cost[c]
                    total_cost = c_cost + sub_cost
                    if best is not None and total_n == best[0] and total_cost > best[1]:
                        continue  # 第二目标（曼哈顿总和）已劣于已知最优
                    # 第三目标：按视野录入顺序展开的伙伴编号序列。
                    # 将各颗粒的编号序列键排序后拼接，取字典序最小 → 唯一结论。
                    pkey = tuple(sorted((self.cluster_key[c],) + sub_key))
                    if best is None or (total_n, total_cost, pkey) < (
                        best[0], best[1], best[2]
                    ):
                        best = (total_n, total_cost, pkey, ((c,) + sub_clusters))
                    if total_n == lb and total_cost == ZERO:
                        break  # 同时达到颗粒数下界与零代价下界，无需再试
                return best

            final_clusters.extend(list(go(root_set)[3]))

        return final_clusters


# --------------------------------------------------------------------------- #
# 响应组装
# --------------------------------------------------------------------------- #
def _num(d: Decimal | int):
    if isinstance(d, int):
        return d
    if d == d.to_integral_value():
        return int(d)
    # 非整数：四舍五入到 12 位小数后转 float，避免二进制浮点展示误差
    return float(d.quantize(Decimal("1e-12")))


def build_response(cleaned: dict[str, Any]) -> dict[str, Any]:
    solver = Solver(cleaned)

    if solver.n == 0:
        return {
            "draftId": cleaned.get("draftId"),
            "tolerance": _num(cleaned["tolerance"]),
            "summary": {
                "finalParticleCount": 0,
                "totalObservations": 0,
                "associationCount": 0,
                "totalManhattan": 0,
            },
            "finalParticles": [],
        }

    clusters = solver.solve()
    clusters.sort(key=lambda c: solver.cluster_key[c])

    out_particles: list[dict[str, Any]] = []
    all_associations: list[dict[str, Any]] = []
    total_manhattan = ZERO

    for idx, c in enumerate(clusters, start=1):
        members = sorted(c, key=lambda v: solver.obs[v].key)
        rep = solver.obs[members[0]]  # 录入顺序最早视野中的成员 → 代表坐标
        category = solver.obs[members[0]].category

        observations = []
        for v in members:
            o = solver.obs[v]
            observations.append(
                {
                    "fieldIndex": o.field,
                    "fieldName": solver.field_names[o.field],
                    "particleId": o.pid,
                    "category": o.category,
                    "local": {"x": o.x, "y": o.y},
                    "filter": {"x": _num(o.fx), "y": _num(o.fy)},
                }
            )

        associations = []
        for u, v in solver.cluster_mst[c]:
            ou, ov = solver.obs[u], solver.obs[v]
            if ou.key > ov.key:
                ou, ov = ov, ou
            dx = abs(ou.fx - ov.fx)
            dy = abs(ou.fy - ov.fy)
            assoc = {
                "from": {
                    "fieldIndex": ou.field,
                    "fieldName": solver.field_names[ou.field],
                    "particleId": ou.pid,
                },
                "to": {
                    "fieldIndex": ov.field,
                    "fieldName": solver.field_names[ov.field],
                    "particleId": ov.pid,
                },
                "deltaX": _num(dx),
                "deltaY": _num(dy),
                "manhattan": _num(dx + dy),
            }
            associations.append(assoc)
            all_associations.append(assoc)
            total_manhattan += dx + dy

        out_particles.append(
            {
                "finalParticleId": f"F{idx}",
                "category": category,
                "observationCount": len(members),
                "representativeCoordinate": {
                    "fieldIndex": rep.field,
                    "fieldName": solver.field_names[rep.field],
                    "particleId": rep.pid,
                    "x": _num(rep.fx),
                    "y": _num(rep.fy),
                },
                "observations": observations,
                "associations": associations,
            }
        )

    return {
        "draftId": cleaned.get("draftId"),
        "tolerance": _num(cleaned["tolerance"]),
        "summary": {
            "finalParticleCount": len(clusters),
            "totalObservations": solver.n,
            "associationCount": len(all_associations),
            "totalManhattan": _num(total_manhattan),
        },
        "finalParticles": out_particles,
    }


def solve_request(payload: Any) -> dict[str, Any]:
    """校验入口：不合规抛 ValidationError（由 Web 层转 422 并保留草稿）。"""
    cleaned = validate(payload)
    return build_response(cleaned)
