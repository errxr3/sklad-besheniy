#!/bin/sh
# Первая установка склада на чистый сервер Ubuntu (запускает GitHub Actions, см. .github/workflows/setup.yml).
# Код к этому моменту уже лежит в /opt/sklad. Скрипт можно запускать повторно: он ничего не ломает.
#
# Переменные окружения:
#   SITE_DOMAIN  — домен (sklad-zerkal.ru); пусто — сайт по IP без HTTPS
#   ADMIN_NAME   — имя первого администратора (пусто — не создавать)
#   ADMIN_PIN    — его PIN, 4–6 цифр
set -eu

APP_DIR=/opt/sklad
cd "$APP_DIR"

echo "== 1. Docker"
if ! command -v docker >/dev/null 2>&1; then
	curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker --version
docker compose version

echo "== 2. Брандмауэр"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
	ufw allow 22/tcp >/dev/null
	ufw allow 80/tcp >/dev/null
	ufw allow 443/tcp >/dev/null
	echo "порты 22, 80, 443 открыты"
else
	echo "ufw не включён — ничего не меняю"
fi

echo "== 3. Настройки (.env)"
random() { head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "$1"; }
if [ ! -f .env ]; then
	SECRET=$(random 48)
	DB_PASS=$(random 24)
	cp .env.example .env
	sed -i \
		-e "s|^SECRET_KEY=.*|SECRET_KEY=$SECRET|" \
		-e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$DB_PASS|" \
		-e "s|^DATABASE_URL=.*|DATABASE_URL=postgresql+psycopg://sklad:$DB_PASS@postgres:5432/sklad|" \
		.env
	echo ".env создан, пароли сгенерированы"
else
	echo ".env уже есть — пароли не трогаю"
fi
# Домен можно поменять повторным запуском установки
if [ -n "${SITE_DOMAIN:-}" ]; then
	sed -i -e "s|^SITE_ADDRESS=.*|SITE_ADDRESS=$SITE_DOMAIN|" -e "s|^SESSION_COOKIE_SECURE=.*|SESSION_COOKIE_SECURE=1|" .env
	echo "сайт: https://$SITE_DOMAIN"
else
	sed -i -e "s|^SITE_ADDRESS=.*|SITE_ADDRESS=:80|" -e "s|^SESSION_COOKIE_SECURE=.*|SESSION_COOKIE_SECURE=0|" .env
	echo "домен не задан — сайт будет по http://IP-сервера (камера без HTTPS не работает)"
fi
chmod 600 .env

echo "== 4. Сборка и запуск"
docker compose up -d --build

echo "== 5. Ожидание приложения"
i=0
until docker compose exec -T app python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" >/dev/null 2>&1; do
	i=$((i + 1))
	if [ "$i" -gt 60 ]; then
		echo "Приложение не ответило за 3 минуты:"
		docker compose logs --tail 80 app
		exit 1
	fi
	sleep 3
done
echo "приложение работает"

echo "== 6. Справочник и администратор"
ARTICLES=$(docker compose exec -T postgres psql -U sklad -d sklad -tAc "SELECT count(*) FROM articles" 2>/dev/null || echo 0)
if [ "${ARTICLES:-0}" = "0" ]; then
	docker compose exec -T app python -m app.cli import-top docs/input/TOP.xlsx
else
	echo "справочник уже загружен ($ARTICLES строк)"
fi
if [ -n "${ADMIN_NAME:-}" ] && [ -n "${ADMIN_PIN:-}" ]; then
	docker compose exec -T app python -m app.cli create-admin --name "$ADMIN_NAME" --pin "$ADMIN_PIN" || \
		echo "администратор не создан (возможно, уже есть с таким именем)"
fi

docker image prune -f >/dev/null 2>&1 || true
echo "== Готово"
docker compose ps
