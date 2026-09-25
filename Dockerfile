FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖以利用构建缓存；验收依赖一并装入镜像，verify 服务无需联网取包
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements.txt -r requirements-dev.txt

COPY app ./app
COPY tests ./tests
COPY scripts ./scripts
RUN chmod +x scripts/verify.sh

EXPOSE 8000

# 应用级容器健康检查（无外部依赖，使用标准库）
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2); sys.exit(0 if r.status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
