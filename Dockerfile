FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn httpx psycopg2-binary asyncpg pyyaml
COPY config.yaml /app/config.yaml
COPY api /app/api
COPY worker /app/worker
COPY db /app/db
COPY scripts /app/scripts
