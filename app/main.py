"""FastAPI Web 层：静态页面、健康检查、去重裁决与配准复核接口。"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .dedup import ValidationError, solve_request
from .registration import get_last_review, review_request, save_last_review

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


@app.post("/api/registration-reviews")
async def registration_reviews(request: Request) -> JSONResponse:
    """配准复核：求联合整数校正量并用校正平移重跑既有颗粒去重。

    成功（200）时结果同时成为新的「上一次成功复核结果」；
    失败（422）返回可定位错误并原样回显草稿，绝不覆盖上次成功结果。
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=422,
            content={
                "message": "请求体不是合法的 JSON",
                "errors": [{"loc": "$", "message": "请求体必须是合法 JSON 对象"}],
                "draft": None,
            },
        )

    try:
        result = review_request(payload)
    except ValidationError as exc:
        logger.info("配准复核请求校验失败：%d 处问题", len(exc.errors))
        return JSONResponse(
            status_code=422,
            content={
                "message": "输入不合规或不存在可行配准，已保留草稿，"
                           "上一次成功复核结果保持不变",
                "errors": exc.errors,
                "draft": exc.draft,
            },
        )

    save_last_review(result)
    return JSONResponse(status_code=200, content=result)


@app.get("/api/registration-reviews/last")
def registration_review_last() -> JSONResponse:
    """上一次成功的配准复核结果（可自动核对）；尚无成功记录时返回 404。"""
    last = get_last_review()
    if last is None:
        return JSONResponse(
            status_code=404,
            content={"message": "尚无上一次成功的配准复核结果"},
        )
    return JSONResponse(status_code=200, content=last)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
