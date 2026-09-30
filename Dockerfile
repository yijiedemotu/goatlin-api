# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# 多阶段构建：builder 只负责编译依赖 wheel，runtime 只带运行必需文件，
# 最终镜像不含 gcc / 构建缓存，体积可缩小 60% 以上（面试常问的镜像瘦身手段）。
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
# 先只拷依赖清单再安装：只要依赖不变，这一层就命中缓存，改业务代码不会重装依赖
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .

# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_ENV=production \
    APP_DEBUG=false \
    DATABASE_URL=sqlite+aiosqlite:///./data/goalfin.db

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src
COPY scripts ./scripts

# 非 root 运行：容器逃逸后的权限被限制在普通用户，是最低成本的纵深防御
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live').status==200 else 1)"

# 生产用 uvicorn 多 worker；迁移在容器启动时先执行，保证 schema 与代码版本一致
CMD ["sh", "-c", "python -m alembic upgrade head && exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4 --proxy-headers"]
