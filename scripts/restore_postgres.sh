#!/usr/bin/env bash
set -euo pipefail
echo "Retired: production Supabase restore requires a separately approved target and Vault recovery plan. See docs/RESTORE-ACCEPTANCE.md. No restore was attempted." >&2
exit 2
