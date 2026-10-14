#!/bin/sh
# Запуск тестов с отдельной тестовой базой PostgreSQL.
# Локально:  TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5433/sklad_test ./scripts/test.sh
# В докере:  docker compose run --rm -e INSTALL_DEV=1 app pytest -q
set -e
cd "$(dirname "$0")/.."
exec python -m pytest "$@"
