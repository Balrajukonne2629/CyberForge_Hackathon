FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py config.yaml /app/
COPY api /app/api
COPY worker /app/worker
COPY db /app/db
COPY scripts /app/scripts
