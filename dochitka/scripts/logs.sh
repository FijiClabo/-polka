#!/usr/bin/env bash
# Логи приложения в реальном времени (Ctrl+C — выйти)
cd "$(dirname "$0")/.."
docker compose logs -f --tail 200 app
