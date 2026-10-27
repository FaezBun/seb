#!/bin/bash
# axl-notify.sh "<message>" -- hantar ke Telegram bot seb.
# Token dibaca dari /etc/axl-login/env (600). Tiada secret dalam fail ini.
set -u
[ "$#" -ge 1 ] || exit 0
MSG="$1"
E="/etc/axl-login/env"
T=$(sed -n 's/^AXL_TG_TOKEN=//p' "$E" 2>/dev/null | head -1)
C=$(sed -n 's/^AXL_TG_CHAT=//p'  "$E" 2>/dev/null | head -1)
[ -n "${T:-}" ] && [ -n "${C:-}" ] || exit 0

# Tambah geolokasi untuk IP awam (jika wujud dalam mesej)
ip=$(echo "$MSG" | grep -oP '(\d+\.\d+\.\d+\.\d+|([0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4})' | head -1)
if [ -n "${ip:-}" ]; then
  # Skip IP private/loopback/link-local/CGNAT etc
  if ! echo "$ip" | grep -Eq '^(127\.|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[0-1])\.|169\.254\.|100\.(6[4-9]|[7-9][0-9]|1[0-1][0-9]|12[0-7])\.|192\.0\.0\.|192\.0\.2\.|198\.18\.|198\.19\.|198\.51\.100\.|203\.0\.113\.|224\.|240\.|255\.255\.255\.255$)' && \
     ! echo "$ip" | grep -Eq '^(::1$|fe[89ab][0-9a-fA-F]:|f[cd][0-9a-fA-F][0-9a-fA-F]:|64:ff9b::|2001:db8:)'; then
    geo=$(curl -sS --connect-timeout 2 --max-time 3 "http://ip-api.com/json/${ip}?fields=status,country,regionName,city,isp,org" 2>/dev/null)
    if [ -n "${geo:-}" ] && echo "$geo" | jq -e '.status == "success"' >/dev/null 2>&1; then
      loc=$(echo "$geo" | jq -r '"\(.city // "-"), \(.regionName // "-"), \(.country // "-") | ISP: \(.isp // "-")"' 2>/dev/null)
      [ -n "${loc:-}" ] && [ "$loc" != " -, -, - | ISP: -" ] && MSG="$MSG
\n\n📍 Lokasi: $loc"
    fi
  fi
fi

curl -sS --retry 2 --retry-connrefused --max-time 10 \
  -X POST "https://api.telegram.org/bot${T}/sendMessage" \
  -d chat_id="${C}" -d parse_mode="HTML" \
  --data-urlencode "text=$MSG" >/dev/null 2>&1
exit 0
