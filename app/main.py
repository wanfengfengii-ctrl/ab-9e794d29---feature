"""FastAPI Web 层：静态页面、健康检查与去重裁决 POST 接口。"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .dedup import ValidationError, solve_request

logger = logging.getLogger("particle-dedup")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="海洋微塑料跨视野去重裁决服务",
    version="1.0.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/particle-deduplications")
async def particle_deduplications(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        # 草稿内容无法解析：返回可定位反馈，前端保留本地草稿
        return JSONResponse(
            status_code=422,
            content={
                "message": "请求体不是合法的 JSON",
                "errors": [{"loc": "$", "message": "请求体必须是合法 JSON 对象"}],
                "draft": None,
            },
        )

    try:
        result = solve_request(payload)
    except ValidationError as exc:
        # 不合规：逐项可定位反馈，并原样回显草稿（服务端不持久化、不丢弃）
        logger.info("裁决请求校验失败：%d 处问题", len(exc.errors))
        return JSONResponse(
            status_code=422,
            content={
                "message": "输入不合规，已保留草稿，请按下列提示修改",
                "errors": exc.errors,
                "draft": exc.draft,
            },
        )

    return JSONResponse(status_code=200, content=result)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
