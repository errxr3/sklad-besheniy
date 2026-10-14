#!/bin/sh
# Точка входа контейнера приложения: ждём базу, применяем миграции, запускаем сервер.
set -e

echo "Ожидание базы данных..."
python -m app.cli wait-for-db

echo "Применение миграций..."
alembic upgrade head

echo "Запуск сервера..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'
