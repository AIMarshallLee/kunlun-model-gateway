#!/usr/bin/env bash
set -euo pipefail
echo "Retired: production uses external Supabase, not a Compose postgres service. See docs/RESTORE-ACCEPTANCE.md. No backup was created." >&2
exit 2
