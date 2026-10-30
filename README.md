# Склад зеркал

Веб-система складского учёта зеркал: каждое зеркало получает этикетку со штрихкодом, его путь
от изготовления до отгрузки записывается в журнал, остатки считаются сами. Плюс смены (кнопка «Начать смену»)
и задачи на день для сборщиков. Полное описание — [`SPEC.md`](SPEC.md), принятые решения —
[`DECISIONS.md`](DECISIONS.md), инструкция для сотрудников — [`docs/инструкция.md`](docs/инструкция.md).

## Что внутри

Python 3.12, FastAPI, PostgreSQL 16, Caddy (HTTPS), всё в `docker compose`:

| Контейнер | Зачем |
|---|---|
| `postgres` | база данных |
| `app` | приложение; при старте само применяет миграции |
| `caddy` | вход снаружи, автоматический HTTPS-сертификат |
| `backup` | ночной `pg_dump` в папку `backups/`, хранение 30 дней |

---

## Запуск на своём компьютере (Windows)

1. Установите [Docker Desktop](https://www.docker.com/products/docker-desktop/), запустите его и дождитесь
   надписи «Docker Desktop is running».
2. Скачайте проект и запустите:
   ```
   git clone -b claude/new-session-hm25m6 https://github.com/errxr3/sklad-besheniy.git
   cd sklad-besheniy
   start.bat
   ```
3. `start.bat` сам создаст `.env`, соберёт контейнеры, при первом запуске спросит имя и пароль
   администратора (пароль вводится скрытно — символы не видны, логин для входа: admin) и загрузит справочник из `docs/input/TOP.xlsx`.
4. Откройте http://localhost. Остановить — `stop.bat` (данные сохраняются).

> Камера телефона (скан этикеток камерой) работает только по HTTPS — проверяется уже на сервере
> с доменом (ниже). На компьютере по http://localhost всё остальное работает.

Обновить версию: `git pull`, затем `start.bat` (или `docker compose up -d --build`).

---

## Сервер и автовыкладка через GitHub

Код выкладывается на сервер самим GitHub: при каждом обновлении ветки `claude/new-session-hm25m6`
(или `main`) он прогоняет тесты и, если всё зелёное, обновляет сервер. База, `.env` и бэкапы
на сервере при этом не трогаются.

**Один раз:**
1. Купите VPS: Ubuntu 24.04, 2 ядра, 2 ГБ памяти, 30 ГБ диска (Timeweb Cloud, Selectel и т.п.).
2. В репозитории на GitHub: **Settings → Secrets and variables → Actions → New repository secret**:
   - `SERVER_HOST` — IP сервера;
   - `SERVER_PASSWORD` — пароль root от хостинга;
   - `SERVER_USER` — только если входите не под root.
3. **Actions → «Первая установка на сервер» → Run workflow**: впишите домен (можно пусто — тогда
   сайт по IP), имя и пароль первого администратора (логин для входа: admin). Через 3–5 минут склад работает.
4. Если указали домен — у регистратора сделайте A-запись домена на IP сервера. Caddy сам получит
   HTTPS-сертификат.

**Дальше** каждое изменение кода в ветке уезжает на сервер само — ход видно во вкладке **Actions**
(«Проверка и выкладка»). Если тесты упали, на сервер ничего не попадёт. Откат — вернуть прошлый
коммит, он выложится так же.

Повторный запуск «Первой установки» безопасен: пароли и база сохраняются, можно поменять домен.

---

## Установка на сервер вручную (без GitHub Actions)

1. **Домен.** У регистратора создайте A-запись, например `sklad.example.ru` → IP сервера.
2. **Docker.**
   ```
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker $USER   # затем перелогиньтесь
   ```
3. **Проект.**
   ```
   git clone -b claude/new-session-hm25m6 https://github.com/errxr3/sklad-besheniy.git
   cd sklad-besheniy
   cp .env.example .env
   nano .env
   ```
   Обязательно поменяйте в `.env`:
   - `SECRET_KEY` — длинная случайная строка: `python3 -c "import secrets;print(secrets.token_urlsafe(48))"`;
   - `POSTGRES_PASSWORD` и тот же пароль внутри `DATABASE_URL`;
   - `SITE_ADDRESS=sklad.example.ru` — ваш домен (Caddy сам получит сертификат Let's Encrypt);
   - `SESSION_COOKIE_SECURE=1`.
4. **Запуск.**
   ```
   docker compose up -d --build
   docker compose ps          # все контейнеры Up, app — healthy
   ```
   Порты 80 и 443 на сервере должны быть открыты.
5. **Администратор и справочник.**
   ```
   docker compose run --rm app python -m app.cli create-admin
   docker compose run --rm app python -m app.cli import-top docs/input/TOP.xlsx
   ```
6. Откройте `https://sklad.example.ru` и войдите администратором.

### Первые шаги в системе

1. **Настройки → Сотрудники** — заведите сборщиков, грузчиков, начальников смены: имя, фамилия, логин и пароль.
2. **Настройки → Площадки** — какие артикулы делают на площадке. Смена начинается кнопкой
   «Начать смену» на телефоне, QR-кодов нет.
3. Ввод остатков: кладовщица на экране «Ввод остатков» печатает этикетки на то, что уже лежит на
   полках, — зеркала сразу записываются на склад; лишние этикетки убирает сканом. Когда всё
   наклеено — **Настройки → Площадки → «Ввод остатков закончен: да»**.
4. **Настройки → Параметры** — размер этикетки (по умолчанию 58×40 мм), этапы внедрения
   (обязательный скан ШК маркетплейса, сверка с заказом).

---

## API для BLAGO OTGRUZKI

Ключ создаётся в **Администратор → Интеграции → Ключи API** (показывается один раз) и передаётся
в заголовке `X-API-Key`. Его вписывают в BLAGO OTGRUZKI: «Настройки → Склад BLAGO».

```
GET  /api/v1/ping      → проверка связи и ключа
POST /api/v1/demand    ← заказы дня: {day, replace, lines: [{market, channel, truck, posting, article, qty, production, ordered_at}]}
                       → по каждому отправлению decision: go / later / ask / no и причина
POST /api/v1/batches   ← партия после скачивания этикеток: {day, market, channel, truck, source,
                         items: [{posting, origin, article, production, codes, ordered_at}]}
```

Повторная отправка той же партии безопасна: отправление одно, по номеру. Полная документация —
`/docs` (открыта только администратору).

---

## Бэкапы и восстановление

Контейнер `backup` каждую ночь в 03:17 делает `pg_dump` в папку `backups/` и удаляет файлы старше
30 дней (`BACKUP_KEEP_DAYS`). Время последнего бэкапа видно на странице «Состояние».

Сделать бэкап прямо сейчас:
```
docker compose exec backup sh /tmp/backup.sh once
```

Восстановить из бэкапа (**все текущие данные будут заменены**):
```
docker compose stop app
docker compose exec postgres psql -U sklad -d postgres -c "DROP DATABASE sklad"
docker compose exec postgres psql -U sklad -d postgres -c "CREATE DATABASE sklad"
gunzip -c backups/sklad_2026-10-14_03-17.sql.gz | docker compose exec -T postgres psql -U sklad -d sklad
docker compose start app
```
На Windows вместо `gunzip -c ... |` распакуйте файл архиватором и выполните
`docker compose exec -T postgres psql -U sklad -d sklad < sklad_....sql`.

Копируйте папку `backups/` куда-нибудь за пределы сервера — бэкап на том же диске не спасёт от
поломки диска.

---

## Команды обслуживания

```
docker compose run --rm app python -m app.cli create-admin      # новый администратор
docker compose run --rm app python -m app.cli import-top FILE   # обновить справочник из TOP.xlsx
docker compose run --rm app python -m app.cli verify-stock      # сверить остатки с журналом
docker compose run --rm app python -m app.cli close-shifts      # закрыть забытые смены
docker compose run --rm app python -m app.cli list-users        # список сотрудников
docker compose logs -f app                                      # логи приложения
```

## Для разработчика

Тесты идут против настоящего PostgreSQL (тестовая база создаётся сама):
```
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql+psycopg://user@localhost:5432/sklad_test pytest -q
ruff check . && mypy app
```
Сквозной тест браузером (`tests/test_e2e_browser.py`) нужен Chromium для Playwright.
