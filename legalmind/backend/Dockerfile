FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

COPY pyproject.toml ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY tests ./tests

# Development image only.
RUN pip install --no-cache-dir ".[dev]"

RUN useradd --create-home --uid 10001 appuser
# 命名卷首次挂载时继承镜像中该目录的属主，appuser 才能写入原始文件
RUN mkdir -p /data/artifacts && chown appuser:appuser /data/artifacts
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
