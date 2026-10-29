# syntax=docker/dockerfile:1.7
# GaugeLens: the API and the built React UI in one image (docs/11_deployment_azure.md §2).

# Stage 1: build the React UI -> /web/dist
FROM node:20-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# Stage 2: API runtime (FastAPI serves web/dist at /)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

COPY backend/ /app/backend
COPY --from=web /web/dist /app/web/dist

RUN useradd --create-home --uid 10001 gaugelens && mkdir -p /app/cache /app/evidence \
    && chown -R gaugelens /app/cache /app/evidence
USER gaugelens

EXPOSE 8000
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
