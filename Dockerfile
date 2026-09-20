FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/data/names.db \
    PORT=8000

WORKDIR /srv/app

# 先装依赖，利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 非 root 运行；/data 存放 SQLite 数据库（可挂卷持久化）
RUN useradd --system --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data
USER appuser

EXPOSE 8000

# 应用启动时自动完成数据库初始化（建库建表，幂等）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
