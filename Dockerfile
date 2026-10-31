FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir fastapi uvicorn httpx psycopg2-binary asyncpg pyyaml
COPY config.py config.yaml /app/
COPY api /app/api
COPY worker /app/worker
COPY db /app/db
COPY scripts /app/scripts
