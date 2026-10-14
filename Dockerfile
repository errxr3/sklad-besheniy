FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Образу не нужны системные пакеты: проверка живости делается самим Python,
# а бэкапы снимает контейнер postgres. Это ускоряет сборку и убирает зависимость
# от зеркал Debian.

COPY requirements.txt requirements-dev.txt ./
ARG INSTALL_DEV=0
RUN pip install -r requirements.txt \
    && if [ "$INSTALL_DEV" = "1" ]; then pip install -r requirements-dev.txt; fi

COPY . .

EXPOSE 8000
CMD ["/app/scripts/entrypoint.sh"]
