# Склад зеркал

Веб-система складского учёта зеркал: каждое зеркало получает этикетку со штрихкодом, его путь
от изготовления до отгрузки записывается в журнал, остатки считаются сами. Плюс смены по QR-коду
в цеху и задачи на день для сборщиков. Полное описание — [`SPEC.md`](SPEC.md), принятые решения —
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
3. `start.bat` сам создаст `.env`, соберёт контейнеры, при первом запуске спросит имя и PIN
   администратора (PIN вводится скрытно — цифры не видны) и загрузит справочник из `docs/input/TOP.xlsx`.
4. Откройте http://localhost. Остановить — `stop.bat` (данные сохраняются).

> Камера телефона работает только по HTTPS, поэтому «Начать смену» по QR на телефоне проверяется уже
> на сервере с доменом (ниже). На компьютере по http://localhost всё остальное работает.

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
   сайт по IP), имя и PIN первого администратора. Через 3–5 минут склад работает.
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

1. **Настройки → Сотрудники** — заведите сборщиков, грузчиков, начальников смены (PIN 4–6 цифр).
2. **Настройки → Площадки → «Распечатать код смены»** — лист A4 с QR повесьте в цеху каждой
   площадки. Если лист сфотографировали и унесли — «Перевыпустить код», старый перестанет работать.
3. Маркировка остатка (SPEC §6.11): начальник смены на экране «Сделал зеркало» выбирает сборщика
   «Остаток на запуск» и печатает этикетки на весь пересчитанный остаток. Когда всё наклеено и
   отсканировано — **Настройки → Площадки → «Маркировка остатка завершена: да»**, и плашка
   «Остатки ещё не достоверны» исчезнет.
4. **Настройки → Параметры** — размер этикетки (по умолчанию 58×40 мм), этапы внедрения
   (обязательный скан ШК маркетплейса, сверка с заказом).

---

## Подключение Google Sheets

Система раз в 5 минут перезаписывает лист **«Остатки из системы»** (остальные листы не трогает).

1. В [Google Cloud Console](https://console.cloud.google.com/) создайте проект, включите
   **Google Sheets API** и **Google Drive API**.
2. Создайте **сервисный аккаунт**, скачайте для него ключ в формате JSON.
3. Откройте свою гугл-таблицу → «Настройки доступа» → дайте права **редактора** адресу сервисного
   аккаунта (вида `...@...iam.gserviceaccount.com`).
4. Положите JSON на сервер, например в `secrets/google.json`, и в `.env` укажите:
   ```
   GOOGLE_SHEETS_ID=<ID из адреса таблицы: docs.google.com/spreadsheets/d/<ID>/edit>
   GOOGLE_SERVICE_ACCOUNT_FILE=/app/secrets/google.json
   ```
5. `docker compose up -d` — через 5 минут лист появится. Кнопка «Выгрузить сейчас» — на экране
   «Остатки». Если выгрузка не работает, причина видна на странице **«Состояние»**.

### Формулы для основного листа

Лист «Остатки из системы»: `A` — артикул (как в TOP), `B` — Учительская, `C` — Коминтерна,
`D` — задачи на Учительской, `E` — задачи на Коминтерна, в `G1` — время обновления.
Формулы ищут по имени артикула, поэтому не зависят от порядка строк (артикул в столбце `B`):

```
=ЕСЛИОШИБКА(ВПР($B4;'Остатки из системы'!$A:$E;2;0);"")   — Учительская
=ЕСЛИОШИБКА(ВПР($B4;'Остатки из системы'!$A:$E;3;0);"")   — Коминтерна
=ЕСЛИОШИБКА(ВПР($B4;'Остатки из системы'!$A:$E;4;0);"")   — задачи на Учительской
=ЕСЛИОШИБКА(ВПР($B4;'Остатки из системы'!$A:$E;5;0);"")   — задачи на Коминтерна
```

Можно и без формул: на экране «Остатки» кнопка «Копировать столбец» кладёт в буфер ровно столько
строк, сколько в таблице, — вставьте в `C4`, и каждая цифра встанет напротив своего артикула.

---

## API для BLAGO OTGRUZKI

Ключ создаётся в **Настройки → Ключи API** (показывается один раз) и передаётся в заголовке `X-API-Key`.

```
GET  /api/v1/stock       → [{"article": "...", "site": "...", "qty": 3}, ...]
POST /api/v1/shipments   ← [{"mp_barcode": "...", "article": "...", "date": "2026-10-14"}, ...]
```

Повторная отправка того же списка безопасна (обновление по `mp_barcode`). Полная документация —
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
