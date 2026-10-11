FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# 依赖单独一层：改代码不用重装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY web/ ./web/
COPY data/metadata.json ./data/metadata.json

# 非 root 运行；logs 必须可写，data 挂卷后由宿主机决定
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/logs \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8200

# 单 worker 是硬约束：会话在进程内存里，多 worker 会把同一会话分叉
CMD ["uvicorn", "analytics_copilot.service:app", "--host", "0.0.0.0", "--port", "8200", "--workers", "1"]
