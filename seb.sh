#!/bin/bash

TOKEN="BOT_API"
CHAT_ID="TELE_ID"

NOTIFIED_ONBATT=0
NOTIFIED_50=0
NOTIFIED_20=0
UPDATE_ID=0

# ==============================================================
# FUNGSI UTAMA HANTAR MESEJ & MENU KEKAL DI BAWAH CHAT
# ==============================================================
send_telegram() {
    local TEXT="$1"
    
    # Butang bawah: "1. UPS STATE" dan "2. BANLIST"
    local KEYBOARD='{
        "keyboard": [
            [{"text": "1. UPS STATE"}, {"text": "2. BANLIST"}]
        ],
        "resize_keyboard": true,
        "is_persistent": true
    }'

    curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    -d parse_mode="HTML" \
    --data-urlencode text="${TEXT}" \
    -d "reply_markup=${KEYBOARD}" > /dev/null
}

# ==============================================================
# FUNGSI KHAS: PAPAR SENARAI BAN FAIL2BAN (DENGAN BUTANG KEKAL)
# ==============================================================
send_banlist_text() {
    local JAILS=$(sudo fail2ban-client status 2>/dev/null | grep "Jail list" | sed 's/.*:[\t ]*//' | tr ',' ' ')
    local ALL_IPS=()
    
    for jail in $JAILS; do
        local ips=$(sudo fail2ban-client status "$jail" 2>/dev/null | grep "Banned IP list" | sed 's/.*:[\t ]*//')
        for ip in $ips; do
            [[ -n "$ip" ]] && ALL_IPS+=("$ip")
        done
    done

    # Butang bawah kekal untuk banlist juga
    local KEYBOARD='{
        "keyboard": [
            [{"text": "1. UPS STATE"}, {"text": "2. BANLIST"}]
        ],
        "resize_keyboard": true,
        "is_persistent": true
    }'

    # Jika tiada IP yang kena ban
    if [ ${#ALL_IPS[@]} -eq 0 ]; then
        curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        -d chat_id="${CHAT_ID}" \
        -d parse_mode="HTML" \
        --data-urlencode text="🛡️ <b>FAIL2BAN STATUS:</b> Tiada IP yang sedang disekat (Clean)." \
        -d "reply_markup=${KEYBOARD}" > /dev/null
        return
    fi

    # Gunakan printf supaya \n diterjemahkan dengan betul dan turun baris
    local MSG
    MSG=$(printf "🛡️ <b>SENARAI IP YANG DI-BAN:</b>\n")
    
    local COUNTER=1
    for ip in "${ALL_IPS[@]}"; do
        MSG+=$(printf "<b>%d.</b> <code>%s</code>\n" "$COUNTER" "$ip")
        COUNTER=$((COUNTER + 1))
    done

    # Hantar senarai berserta keyboard supaya butang bawah sentiasa betul (2. BANLIST)
    curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    -d parse_mode="HTML" \
    --data-urlencode text="$MSG" \
    -d "reply_markup=${KEYBOARD}" > /dev/null
}

# ==============================================================
# GELUNG UTAMA (WHILE TRUE)
# ==============================================================
while true; do
    
    # 1. BAHAGIAN PEMANTAUAN UPS (BATERI & LETRIK)
    STATE=$(sudo pwrstat -status 2>/dev/null | grep -i "State" | awk -F'\.\.\.+' '{print $2}' | xargs)
    SUPPLY=$(sudo pwrstat -status 2>/dev/null | grep -i "Power Supply by" | awk -F'\.\.\.+' '{print $2}' | xargs)
    BATT=$(sudo pwrstat -status 2>/dev/null | grep -i "Battery Capacity" | awk -F'\.\.\.+' '{print $2}' | awk '{print $1}')

    if [[ -n "$STATE" ]]; then
        if [[ "$STATE" != "Normal" ]] || [[ "$SUPPLY" == *"Battery"* ]]; then
            
            if [[ $NOTIFIED_ONBATT -eq 0 ]]; then
                send_telegram "⚠️ ELEKTRIK TERPUTUS! Server beroperasi guna bateri UPS. Status: $STATE | Bateri: $BATT%"
                NOTIFIED_ONBATT=1
            fi
            
            if [[ $BATT -le 50 && $NOTIFIED_50 -eq 0 ]]; then
                send_telegram "⚠️ PERHATIAN! Bateri UPS tinggal $BATT%. Status: $STATE"
                NOTIFIED_50=1
            fi
            
            if [[ $BATT -le 20 && $NOTIFIED_20 -eq 0 ]]; then
                send_telegram "🚨 KRITIKAL! Bateri UPS pada tahap $BATT%. Server akan dimatikan tak lama lagi."
                NOTIFIED_20=1
            fi

        elif [[ "$STATE" == "Normal" ]] && [[ "$SUPPLY" == *"Utility"* ]]; then
            if [[ $NOTIFIED_ONBATT -eq 1 ]]; then
                send_telegram "✅ ELEKTRIK KEMBALI PULIH! Server beroperasi macam biasa. Bateri dicas: $BATT%"
                NOTIFIED_ONBATT=0
                NOTIFIED_50=0
                NOTIFIED_20=0
            fi
        fi
    fi

    # 2. BAHAGIAN TERIMA ARAHAN BUTTON & TELEGRAM (LONG POLLING 5 SAAT)
    UPDATES=$(curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates?offset=$((UPDATE_ID + 1))&timeout=5")

    if [[ -n "$UPDATES" ]] && [[ "$UPDATES" != '{"ok":true,"result":[]}' ]]; then
        NEW_UPDATE_ID=$(echo "$UPDATES" | jq -r '.result[-1]?.update_id? // empty')
        
        if [[ -n "$NEW_UPDATE_ID" ]]; then
            UPDATE_ID=$NEW_UPDATE_ID
            
            # Semak teks mesej masuk
            MSG_TEXT=$(echo "$UPDATES" | jq -r --arg chat_id "$CHAT_ID" '.result[]? | select(.message?.chat?.id?|tostring == $chat_id) | .message?.text? // empty' | tail -n 1)
            
            if [[ "$MSG_TEXT" == "1. UPS STATE" ]] || [[ "$MSG_TEXT" == "/upstate" ]]; then
                FULL_STATUS=$(sudo pwrstat -status)
                send_telegram "📊 <b>STATUS SEMASA UPS:</b>
<pre><code>$FULL_STATUS</code></pre>"
            elif [[ "$MSG_TEXT" == "2. BANLIST" ]] || [[ "$MSG_TEXT" == "/banlist" ]] || [[ "$MSG_TEXT" == "/start" ]]; then
                send_banlist_text
            fi
        fi
    fi

done
