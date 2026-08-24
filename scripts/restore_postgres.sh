#!/usr/bin/env bash
set -euo pipefail
compose_file="${COMPOSE_FILE:-docker-compose.production.yml}"
backup_file="${BACKUP_FILE:-}"
if [[ -z "$backup_file" || ! -f "$backup_file" ]]; then echo "请设置存在的 BACKUP_FILE" >&2; exit 2; fi
if [[ "${CONFIRM_RESTORE:-}" != "YES_RESTORE_PRODUCTION" ]]; then echo "恢复会覆盖目标数据库；设置 CONFIRM_RESTORE=YES_RESTORE_PRODUCTION 后重试" >&2; exit 2; fi
echo "开始恢复 $backup_file；请确认 API 已停止或进入维护模式。"
docker compose -f "$compose_file" exec -T postgres pg_restore --clean --if-exists --no-owner --no-acl -U "${POSTGRES_USER:?POSTGRES_USER 未设置}" -d "${POSTGRES_DB:?POSTGRES_DB 未设置}" < "$backup_file"
echo "恢复完成；请执行 alembic current --check-heads 和业务冒烟测试。"
