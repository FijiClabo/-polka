# ---------- 1. сборка мини-приложения
FROM node:22-alpine AS webapp
WORKDIR /webapp
COPY webapp/package.json webapp/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY webapp/ ./
RUN npm run build

# ---------- 2. бэкенд: бот + API + планировщик
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata curl \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY . .
COPY --from=webapp /webapp/dist ./webapp/dist
RUN rm -rf webapp/node_modules tests/books/files && mkdir -p /app/data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 CMD curl -fsS http://localhost:8000/health || exit 1
CMD ["sh", "-c", "alembic upgrade head && python main.py"]
