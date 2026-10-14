#!/usr/bin/env bash
# Create every host in hosts.txt in Checkmk via the REST API, then activate changes.
# Usage: CMK_URL=http://192.168.1.249:30500/homelab CMK_USER=automation CMK_SECRET=... ./add-hosts.sh
set -euo pipefail
: "${CMK_SECRET:?create an automation user in Setup > Users and export its secret}"
CMK_URL=${CMK_URL:-http://192.168.1.249:30500/homelab}
CMK_USER=${CMK_USER:-automation}
API="$CMK_URL/check_mk/api/1.0"
AUTH="Authorization: Bearer $CMK_USER $CMK_SECRET"
cd "$(dirname "$0")"

grep -vE '^\s*(#|$)' hosts.txt | while read -r name ip agent; do
  tag=cmk-agent; [ "$agent" = no ] && tag=ping
  echo "adding $name ($ip, $tag)"
  curl -fsS -X POST "$API/domain-types/host_config/collections/all" \
    -H "$AUTH" -H 'Content-Type: application/json' \
    -d "{\"folder\":\"/\",\"host_name\":\"$name\",\"attributes\":{\"ipaddress\":\"$ip\",\"tag_agent\":\"$tag\"}}" \
    >/dev/null || echo "  (skipped: may already exist)"
done

echo "activating changes"
curl -fsS -X POST "$API/domain-types/activation_run/actions/activate-changes/invoke" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"redirect":false,"force_foreign_changes":true}' >/dev/null
echo done
