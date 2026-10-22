#!/bin/sh
# Точка входа контейнера приложения: ждём базу, применяем миграции, запускаем сервер.
set -e

echo "Ожидание базы данных..."
python -m app.cli wait-for-db

echo "Применение миграций..."
alembic upgrade head

echo "Запуск сервера..."
# Несколько процессов: 100 человек одновременно сканируют без задержек (нагрузка 2026-10-22)
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' \
    --workers "${WEB_WORKERS:-3}"
