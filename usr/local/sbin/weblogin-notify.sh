#!/bin/bash
# weblogin-notify.sh "<message>" -- hantar notifikasi ke Telegram.
# Token dibaca dari /etc/weblogin/env (600). Tiada secret dalam fail ini.
set -u
[ "$#" -ge 1 ] || exit 0
E="/etc/weblogin/env"
T=$(sed -n 's/^WEBLOGIN_TG_TOKEN=//p' "$E" 2>/dev/null | head -1)
C=$(sed -n 's/^WEBLOGIN_TG_CHAT=//p'  "$E" 2>/dev/null | head -1)
[ -n "${T:-}" ] && [ -n "${C:-}" ] || exit 0
curl -sS --retry 2 --retry-connrefused --max-time 10 \
  -X POST "https://api.telegram.org/bot${T}/sendMessage" \
  -d chat_id="${C}" -d parse_mode="HTML" \
  --data-urlencode "text=$1" >/dev/null 2>&1
exit 0
