#!/bin/bash
# weblogin-block.sh add|del <ip> -- maintain the web login blocklist file.
# Called by fail2ban action weblogin-app.
set -u
FILE="/etc/weblogin/blocklist.txt"
ACTION="${1:-}"
IP="${2:-}"
[ -z "$ACTION" ] || [ -z "$IP" ] && exit 1
mkdir -p "$(dirname "$FILE")"
touch "$FILE"
case "$ACTION" in
  add)
    grep -qxF "$IP" "$FILE" 2>/dev/null || echo "$IP" >> "$FILE"
    ;;
  del)
    tmp="$(mktemp)"
    grep -vxF "$IP" "$FILE" > "$tmp" 2>/dev/null || true
    mv "$tmp" "$FILE"
    ;;
  *)
    exit 1
    ;;
esac
exit 0
