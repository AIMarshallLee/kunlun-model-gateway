#!/usr/bin/env bash
set -euo pipefail
compose_file="${COMPOSE_FILE:-docker-compose.production.yml}"
backup_file="${BACKUP_FILE:-}"
if [[ -z "$backup_file" || "$backup_file" == */ || "$backup_file" == *..* ]]; then echo "请设置安全的 BACKUP_FILE" >&2; exit 2; fi
if [[ -e "$backup_file" ]]; then echo "拒绝覆盖已有备份: $backup_file" >&2; exit 2; fi
umask 077
docker compose -f "$compose_file" exec -T postgres pg_dump --format=custom --no-owner --no-acl -U "${POSTGRES_USER:?POSTGRES_USER 未设置}" "${POSTGRES_DB:?POSTGRES_DB 未设置}" > "$backup_file"
echo "备份已创建: $backup_file（请异地加密保存并定期演练恢复）"
