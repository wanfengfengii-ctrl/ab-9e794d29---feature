#!/usr/bin/env bash
# verify 一次性服务入口：代码检查 → 依赖检查 → 单元测试 → 真实 API 冒烟验收。
# 任一步失败立即以非零退出码结束（set -e）。
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== [1/4] 代码：Python 编译检查 =="
python -m compileall -q app tests scripts
echo "   编译通过"

echo "== [2/4] 构建产物：依赖完整性检查 =="
pip check
python -c "import fastapi, uvicorn, httpx; print('   运行时与验收依赖均可导入')"

echo "== [3/4] 代码：单元测试（pytest）=="
python -m pytest -q

echo "== [4/4] API 冒烟验收（对 compose 中的 web 服务发真实 HTTP 请求）=="
python scripts/smoke.py

echo ""
echo "✔ verify 全部验收通过：代码检查、构建产物与 API 冒烟均成功。"
