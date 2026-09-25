# 海洋微塑料跨视野去重裁决服务

同一滤膜的相邻显微视野拼接计数时，分析员在网页录入 **3–5 个视野** 的已知平移位置，
以及每个视野中带**唯一编号、整数坐标、聚合物类别**的颗粒候选；服务对重叠带内的同一
颗粒做跨视野去重裁决，返回每个最终颗粒包含的观测、代表坐标、类别和总数。

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

## 目录结构

```
app/                 FastAPI 应用
  dedup.py           校验 + 精确求解 + 响应组装（核心算法）
  main.py            POST /api/particle-deduplications、/health、静态页
  static/index.html  单页录入/裁决界面
tests/               20 项单元与 API 测试
scripts/verify.sh    verify 一次性服务入口
scripts/smoke.py     真实 HTTP API 冒烟验收
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

页面操作：录入/增删 3–5 个视野的名称、平移 (x, y) 与颗粒候选（编号、整数坐标、类别），
点击「发起去重裁决」；结果区展示最终颗粒总数、每个颗粒包含的观测、代表坐标、类别、
所用关联及其曼哈顿差。

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
3. `pytest` 全部单元/API 测试（校验、精确优化、平局裁决、连通性与彩虹约束）；
4. 对运行中的 `web` 服务发真实 HTTP 请求：`GET /health`、`GET /`、
   `POST /api/particle-deduplications` 的 200 与 422 场景。

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
