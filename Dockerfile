# 物料管理系统 API 镜像
FROM python:3.11-slim AS base

# 国内服务器统一使用阿里云 Debian 镜像。
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources

FROM base AS api-runtime

WORKDIR /app
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
# 走阿里云 PyPI 镜像(ECS 直连 pypi.org 会卡死)
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ PIP_DEFAULT_TIMEOUT=120

# HEVC 源文件保留原样，同时生成供浏览器播放的 H.264/AAC 副本。
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 只装依赖(不把本项目装成包),从源码运行以保证 frontend 相对路径正确
RUN pip install --no-cache-dir \
    "fastapi>=0.115" "uvicorn[standard]>=0.32" "pydantic>=2.9" "pydantic-settings>=2.6" \
    "sqlalchemy>=2.0" "psycopg[binary]>=3.2" "psycopg-pool>=3.2" "pgvector>=0.3" "alembic>=1.14" \
    "redis>=5.2" "celery>=5.4" "oss2>=2.19" "dashscope>=1.20" \
    "alibabacloud_tea_openapi>=0.4.5" \
    "alibabacloud-green20220302==3.2.4" "httpx>=0.28" "python-multipart>=0.0.12"

COPY app ./app
COPY frontend ./frontend

EXPOSE 8000
# 单 worker:当前用 JSON 文件持久化(AM_DATA_DIR),进程内单一状态,多 worker 会各持一份内存副本导致不一致。
# 迁到共享 RDS(pgvector)后再开多 worker + 多副本承接 500 并发。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
