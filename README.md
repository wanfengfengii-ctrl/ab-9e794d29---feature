# 海洋微塑料跨视野去重裁决服务

同一滤膜的相邻显微视野拼接计数时，分析员在网页录入 **3–5 个视野** 的已知平移位置，
以及每个视野中带**唯一编号、整数坐标、聚合物类别**的颗粒候选；服务对重叠带内的同一
颗粒做跨视野去重裁决，返回每个最终颗粒包含的观测、代表坐标、类别和总数。

部分显微视野的登记平移会受**载物台漂移**影响，服务另提供**配准复核**：分析员为每个
视野补录带**全局唯一编号的校准珠局部整数坐标**，并填写 **0–5 格单视野校正范围**与
**珠位残差上限**；系统固定首视野校正为零，在所有视野校正量中联合选择整数方案，再用
**校正后的平移重新执行既有颗粒去重**。

## 裁决规则（精确定义）

- **允许的关联**仅可在满足全部条件的两个观测间建立：
  1. 来自**不同视野**；
  2. **聚合物类别相同**；
  3. 各自换算到滤膜坐标（局部整数坐标 + 视野平移位置）后，
     横向差 `|Δx|` 与纵向差 `|Δy|` **均不超过容差**（边界取等号允许）。
- 每个**最终颗粒**是一组由所选关联**连通**的观测，且**同一视野最多一个观测**。
- 解的优化目标在**所有完整方案**（覆盖全部观测的划分）上依次为：
  1. **最终颗粒数最少**；
  2. 所选关联的**曼哈顿差总和最小**（每个颗粒取其最小生成树）；
  3. 仍相同则按**视野录入顺序展开的伙伴编号序列**取字典序最小，得到唯一结论：
     每个颗粒的序列键 = 其成员按「视野录入序号 → 视野内录入位置」展开的编号元组，
     方案键 = 各颗粒序列键排序后的拼接，比较取字典序最小。
- 求解器枚举所有合法的连通「彩虹」集合并做带记忆化的精确划分搜索，
  **不使用局部最近边贪心合并**（单元测试中含贪心反例
  `test_local_nearest_greedy_is_rejected_by_global_optimum`）。
- 输入不合规时返回 `422`，错误项带可定位的字段路径
  （如 `fields[0].particles[2].x`），并**原样回显草稿**；前端草稿同时保存在
  浏览器 `localStorage`，任何失败都不丢失。

**规模边界**：视野 3–5 个；单视野颗粒候选至多 30 个（超限按不合规处理，错误定位到
`fields[i].particles`）。精确裁决是 NP 难组合优化，典型稀疏显微数据（小容差、
重叠带窄）在该上限内为毫秒级响应。

## 配准复核（校准珠驱动的联合整数校正）

去重前若发现登记平移受载物台漂移影响，分析员可为每个视野补录校准珠：

- 每颗校准珠带**全局唯一编号**与该视野下的**局部整数坐标**（相对原登记平移）；
- 每个视野填写 **0–5 格单视野校正范围**（校正量 `(cx, cy)` 均为
  `[-R, R]` 内的整数格点）；
- 填写全局共用的**珠位残差上限** `beadResidualLimit`。

规则（精确定义）：

- **首视野校正固定为 `(0, 0)`**。
- 同一编号校准珠出现在视野 i、j 时，换算到滤膜坐标（局部坐标 + 登记平移 + 校正量）
  后的横向差 `|Δx|`、纵向差 `|Δy|` **均不得超过残差上限**。
- **只出现一次的校准珠不参与约束**（证据中标注 `usedInConstraints=false`）。
- 以视野为节点、「共享出现 ≥2 次的校准珠」为边构图；**无法经共享校准珠连到首视野的
  视野**校正固定为零，在 `disconnectedFields` 中明确指出，重跑去重沿用原平移。
- 在所有可行整数方案上依次优化：
  1. **最大坐标残差最小**（所有珠对横/纵残差的最大值）；
  2. **全部校正量曼哈顿和最小**（Σ(|cx|+|cy|)）；
  3. 仍相同则按**视野录入顺序的校正坐标序列**取字典序最小，得到唯一结论。
- 成功后用**校正后平移重新执行既有颗粒去重**（同一精确求解器），并返回原平移、
  校正量、校正平移、逐珠证据（原/校正滤膜坐标、成对残差）与更新后的最终颗粒。

**不合规处理**：校准珠在同一视野重复、坐标非整数、校正范围不是 0–5 的整数、残差上限
非法，或**不存在可行配准**时，接口返回 `422`，错误项可定位（珠级路径如
`fields[0].beads[1].x`，配准级矛盾定位到 `registration` 并指出相关校准珠与视野），
**原样回显草稿且不覆盖上一次成功复核结果**。上一次成功结果可经
`GET /api/registration-reviews/last` 自动核对（尚无记录时 404）。

求解器以「视野对」为单位把该对上的全部校准珠合并为一条矩形整数区间约束（支持检查与
珠数无关），再做弧相容剪枝 + MRV 枚举；规模边界（≤5 视野、每视野 ≤30 珠、R≤5）下
典型为毫秒至百毫秒级。单元测试含对随机小实例穷举全部整数校正方案的暴力 oracle，
逐例核对三级优化目标与求解器完全一致。

## 目录结构

```
app/                 FastAPI 应用
  dedup.py           颗粒去重：校验 + 精确求解 + 响应组装（核心算法）
  registration.py    配准复核：校准珠校验 + 联合整数校正求解 + 校正后重跑去重
  main.py            去重/配准复核 POST、上次成功复核 GET、/health、静态页
  static/index.html  单页录入/裁决/复核界面
tests/               单元与 API 测试（含两个暴力枚举 oracle）
scripts/verify.sh    verify 一次性服务入口
scripts/smoke.py     真实 HTTP API 冒烟验收（去重 + 配准复核）
Dockerfile           应用镜像（含 HEALTHCHECK）
docker-compose.yml   web 常驻服务 + verify 一次性服务
```

## 运行

需要 Docker（含 Compose v2）。宿主机端口通过 `.env` 中的 `HOST_PORT` 配置（默认 8080）：

```bash
# 可选：修改宿主机端口
echo "HOST_PORT=9090" > .env

docker compose up --build -d
# 打开 http://localhost:8080
curl -s http://localhost:8080/health
```

页面操作：录入/增删 3–5 个视野的名称、登记平移 (x, y)、**0–5 格校正范围**、
校准珠与颗粒候选；点击「发起去重裁决」查看原平移下的结果，或点击「发起配准复核」
联合求整数校正量并查看原平移、校正平移、珠位证据与校正后重跑的最终颗粒；
「查看上一次成功复核结果」可随时取回服务端保留的上次成功结果。结果区展示最终颗粒
总数、每个颗粒包含的观测、代表坐标、类别、所用关联及其曼哈顿差。

## 一次性验收服务 verify

自动执行**代码检查、构建产物检查与 API 冒烟验收**，以退出码结束：

```bash
docker compose build
docker compose run --rm verify
# 通过时退出码 0；任一步失败退出码非 0
```

`verify` 依次执行：

1. `python -m compileall` 代码编译检查；
2. `pip check` + 依赖导入（构建产物完整性）；
3. `pytest` 全部单元/API 测试（去重校验、精确优化、平局裁决、连通性、彩虹约束；
   配准复核校验、三级目标、断连视野、不可行配准及两个暴力枚举 oracle）；
4. 对运行中的 `web` 服务发真实 HTTP 请求：`GET /health`、`GET /`、
   `POST /api/particle-deduplications` 的 200 与 422 场景，以及
   `POST /api/registration-reviews` 的 200/422 场景（含断连、不可行定位、
   草稿回显）与 `GET /api/registration-reviews/last` 的可核对性、失败不覆盖语义。

也可在宿主机本地开发运行：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload
pytest -q
```

## 接口

`POST /api/particle-deduplications`

```json
{
  "tolerance": 2,
  "fields": [
    {"name": "A区", "shift": {"x": 0, "y": 0},
     "particles": [{"id": "P1", "x": 10, "y": 10, "category": "PE"}]}
  ]
}
```

响应含 `summary.finalParticleCount` 与 `finalParticles[]`；每项给出
`category`、`observationCount`、`representativeCoordinate`（滤膜坐标，取录入顺序最早
视野中的成员）、`observations[]`（含局部/滤膜坐标）以及 `associations[]`
（MST 所选关联的横纵差与曼哈顿差）。

`POST /api/registration-reviews`

请求体在去重字段基础上，为每个视野增加 `correctionRange`（0–5 整数）与 `beads[]`
（`{id, x, y}`，局部整数坐标），并在顶层增加 `beadResidualLimit`：

```json
{
  "tolerance": 2,
  "beadResidualLimit": 1,
  "fields": [
    {"name": "A区", "shift": {"x": 0, "y": 0}, "correctionRange": 2,
     "beads": [{"id": "B1", "x": 50, "y": 50}],
     "particles": [{"id": "P1", "x": 10, "y": 10, "category": "PE"}]},
    {"name": "B区", "shift": {"x": 10, "y": 0}, "correctionRange": 2,
     "beads": [{"id": "B1", "x": 43, "y": 50}],
     "particles": [{"id": "Q1", "x": 2, "y": 10, "category": "PE"}]}
  ]
}
```

成功响应（200）含：

- `fields[]`：每视野的 `originalShift`、`correction`、`correctedShift`、
  `correctionRange` 与 `connectedToFirst`；
- `registration.objective`：`maxResidual`、`totalCorrectionManhattan`、
  `tieBreakSequence`（按录入顺序的校正坐标序列）；
- `registration.disconnectedFields`：无法经共享校准珠连到首视野的视野序号；
- `registration.beads[]`：每颗珠的出现点、原登记/校正后滤膜坐标、校正量、
  `usedInConstraints`、成对 `deltaX/deltaY/withinLimit` 与最大残差；
- `deduplication`：以校正后平移重跑既有去重的完整结果（结构同去重接口）。

不合规或不存在可行配准时返回 `422`，`errors[].loc` 可定位（珠级或
`registration`），`draft` 原样回显，且**不更新**上次成功结果。

`GET /api/registration-reviews/last`：返回最近一次成功复核的完整结果（200），
尚无成功记录时返回 404；可供外部自动核对。
