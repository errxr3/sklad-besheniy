#!/bin/sh
# Ночной бэкап базы: pg_dump в /backups, хранение BACKUP_KEEP_DAYS дней.
# Запуск: /backup.sh once   — один дамп
#         /backup.sh loop   — ждать 03:17 каждую ночь и делать дамп
set -e

KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"
DIR=/backups

make_dump() {
	mkdir -p "$DIR"
	NAME="$DIR/sklad_$(date +%Y-%m-%d_%H-%M).sql.gz"
	echo "Бэкап: $NAME"
	pg_dump --no-owner --no-privileges | gzip -9 > "$NAME.part"
	mv "$NAME.part" "$NAME"
	find "$DIR" -name 'sklad_*.sql.gz' -type f -mtime "+$KEEP_DAYS" -delete
	echo "Готово. Хранится файлов: $(find "$DIR" -name 'sklad_*.sql.gz' | wc -l)"
}

case "$1" in
	once)
		make_dump
		;;
	loop)
		while true; do
			NOW=$(date +%H%M)
			if [ "$NOW" = "0317" ]; then
				make_dump || echo "Бэкап не удался"
				sleep 3600
			fi
			sleep 50
		done
		;;
	*)
		echo "Использование: $0 once|loop" >&2
		exit 2
		;;
esac
