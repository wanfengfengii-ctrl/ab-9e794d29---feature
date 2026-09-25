# 海洋微塑料跨视野去重裁决服务

同一滤膜的相邻显微视野拼接计数时，分析员在网页录入 **3–5 个视野** 的已知平移位置，
以及每个视野中带**唯一编号、整数坐标、聚合物类别**的颗粒候选；服务对重叠带内的同一
颗粒做跨视野去重裁决，返回每个最终颗粒包含的观测、代表坐标、类别和总数。

部分显微视野的登记平移会受**载物台漂移**影响。分析员还可为每个视野补录带
**全局唯一编号**的校准珠局部整数坐标，填写 **0–5 格的单视野校正范围**与
**珠位残差上限**，发起**配准复核**：服务在全部视野校正量上联合选择整数校正方案
（首视野校正固定为零），使同编号校准珠换算到滤膜坐标后的横纵残差均不超过上限，
随后用**校正后的平移重新执行既有颗粒去重**，并展示原平移、校正平移、珠位证据与
更新后的最终颗粒。

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

## 配准复核规则（精确定义）

- 每个视野可补录若干校准珠：`id` 是物理校准珠的**全局唯一身份**，同一编号在
  **不同视野各登记一次**正是跨视野共享证据（同一视野内重复登记编号/坐标则不合规）；
  局部坐标 `x、y` 必须是**整数**。
- 每个非首视野的校正量 `(cx, cy)` 都是整数，且 `|cx|、|cy|` 均不超过该视野填写的
  **校正范围**（0–5 格；首视野校正固定为 `(0,0)`，其范围输入被忽略）。
- 同一编号校准珠在不同视野的出现，换算到滤膜坐标
  （局部坐标 + 视野登记平移 + 校正量）后，任意两视野间的横向残差 `|Δx|` 与
  纵向残差 `|Δy|` 均不超过**珠位残差上限**（边界取等号允许）。
- **只出现一次的校准珠不参与约束**，仅计数并在证据中标注。
- 以「出现不少于两次的同编号校准珠」在视野间连边；**无法经共享校准珠（允许链式
  间接）连到首视野**的视野无法确定校正量，按不可行处理，错误逐项定位到
  `fields[i].calibrationBeads`。
- 在所有可行方案上依次优化：
  1. **最大坐标残差最小**（全部同编号珠成对横纵残差中的 `max(max|Δx|, max|Δy|)`）；
  2. **全部校正量曼哈顿和最小**（`Σ(|cx|+|cy|)`，首视野贡献恒为零）；
  3. 仍相同则按**视野录入顺序的校正坐标序列**
     `((cx0,cy0),(cx1,cy1),…)` 取字典序最小，得到唯一结论。
- 求解器把问题建模为小规模整数 CSP（视野 3–5；每轴整数域至多 11 个值）：
  以共享珠视野对为边预计算整数校正差的横/纵残差表，用 MRV + 双轴前向检查
  搜索完整可行解，并按「最大坐标残差 → 曼哈顿和」下界分支定界；不做贪心近似，
  结论确定且可重复。范围 5 格的最坏随机实例实测在约 15ms 内完成。
- 可行时以校正后平移调用**既有去重求解器**（规则完全不变）；响应并列返回原登记
  平移、校正量、校正后平移、逐珠残差证据与更新后的最终颗粒。
- 校准珠同视野重复（编号或整数坐标）、坐标非整数、校正范围不在 0–5 之间、
  单视野校准珠超过 100 颗，或**不存在可行配准**（含视野无法连到首视野）时返回
  `422`，错误带可定位字段路径并**原样回显草稿**；前端保留草稿，且**不覆盖
  上一次成功的复核结果**（服务端不持久化）。

## 目录结构

```
app/                   FastAPI 应用
  dedup.py             校验 + 精确去重求解 + 响应组装（既有核心算法）
  registration.py      配准复核：校准珠校验 + 整数 CSP 联合校正 + 证据组装
  main.py              POST /api/particle-deduplications、
                       POST /api/registration-reviews、/health、静态页
  static/index.html    单页录入/裁决/配准复核界面
tests/                 单元与 API 测试（含配准复核朴素全枚举 oracle 交叉核对）
scripts/verify.sh      verify 一次性服务入口
scripts/smoke.py       真实 HTTP API 冒烟验收（含配准复核 200/422 场景）
Dockerfile             应用镜像（含 HEALTHCHECK）
docker-compose.yml     web 常驻服务 + verify 一次性服务
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

页面操作：录入/增删 3–5 个视野的名称、登记平移 (x, y)、校正范围与颗粒候选，
并可为每个视野增删校准珠（珠编号、整数局部坐标）；点击「发起去重裁决」得到
既有去重结果；点击「发起配准复核」后，结果区先展示配准证据（原平移、校正量、
校正后平移、逐珠成对残差），再以校正后平移重新去重并展示更新后的最终颗粒。
任何 422 都在页面上定位问题、保留草稿；配准复核失败不清空、不覆盖上一次成功
复核结果。

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
3. `pytest` 全部单元/API 测试（去重校验、精确优化、平局裁决、连通性与彩虹约束；
   配准复核校验、三级目标、珠位证据、朴素全枚举 oracle 交叉核对）；
4. 对运行中的 `web` 服务发真实 HTTP 请求：`GET /health`、`GET /`、
   `POST /api/particle-deduplications` 的 200 与 422 场景，以及
   `POST /api/registration-reviews` 的 200（校正量/证据/校正后去重可自动核对）
   与 422（重复珠、非整数坐标、范围越界、不可连首视野、不可行配准）场景。

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

请求体沿用去重接口全部字段，并新增：

```json
{
  "tolerance": 2,
  "beadResidualLimit": 0,
  "fields": [
    {"name": "A区", "shift": {"x": 0, "y": 0}, "correctionRange": 3,
     "particles": [{"id": "P1", "x": 10, "y": 10, "category": "PE"}],
     "calibrationBeads": [{"id": "B1", "x": 5, "y": 5}]}
  ]
}
```

- 顶层保持既有去重响应形态（`summary`、`finalParticles[]`，坐标按**校正后平移**
  计算），并并列 `registrationReview`：
  - `summary`：共享珠/单次珠数量、最大横残差 `maxResidualX`、最大纵残差
    `maxResidualY`、最大坐标残差、校正量曼哈顿和；
  - `corrections[]`：每视野的原登记平移 `originalShift`、校正量 `correction`
    （首视野恒 `{0,0}`）、校正后平移 `correctedShift`、所用校正范围；
  - `beadEvidence[]`：逐颗校准珠的出现记录（局部坐标、原登记滤膜坐标、校正量、
    校正后滤膜坐标）与逐视野对 `pairwiseResiduals[]`（`deltaX/deltaY/manhattan/
    withinLimit`）；只出现一次的珠子 `participates=false` 且无残差对。
- 不合规（含不可行配准）返回 `422`：`errors[]` 逐项带 `loc`（如
  `fields[1].calibrationBeads[0].x`、`fields[2].calibrationBeads`、
  `beadResidualLimit`），并原样回显 `draft`，附
  `draftPreserved/previousReviewPreserved` 供前端核对。
