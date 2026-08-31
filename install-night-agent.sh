#!/usr/bin/env bash
# Ночные прогоны: LaunchAgent, который будит фабрику каждый час в ночном окне.
#
#   ./install-night-agent.sh            # поставить, окно берётся из config.json
#   ./install-night-agent.sh --remove   # убрать
#
# Сам по себе он ноутбук НЕ будит — launchd умеет только запускать задачи, а не
# поднимать спящую машину. Разбудить может лишь pmset, и он требует root:
#
#   sudo pmset repeat wakeorpoweron MTWRFSU 21:55:00
#
# Порядок такой: pmset будит ноут в 21:55, launchd в 22:05 запускает прогон,
# caffeinate внутри run.sh не даёт заснуть, пока агент работает.
set -euo pipefail
cd "$(dirname "$0")"

ROOT="$(pwd)"
LABEL="local.kaiten-fabrica-night"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "${1:-}" == "--remove" ]]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "ночной агент убран"
    exit 0
fi

[[ -f config.json ]] || { echo "нет config.json — сначала python3 setup.py" >&2; exit 1; }

# Часы окна берём из конфига, чтобы расписание и фильтр по тегу не разъезжались:
# бессмысленно будить машину в 22:00, если фабрика считает ночью промежуток с полуночи.
read -r FROM TO < <(python3 -c "
import json
night = json.load(open('config.json')).get('night') or {}
print(night.get('from_hour', 22), night.get('to_hour', 5))
")

# Часы окна: от FROM до TO, через полночь. Прогон в час — чаще не нужно, один
# заход успевает взять max_cards_per_run карточек и занимает десятки минут.
HOURS=()
hour=$FROM
while :; do
    HOURS+=("$hour")
    hour=$(( (hour + 1) % 24 ))
    [[ "$hour" == "$TO" ]] && break
    [[ ${#HOURS[@]} -gt 24 ]] && break
done

INTERVALS=""
for h in "${HOURS[@]}"; do
    INTERVALS+="
        <dict>
            <key>Hour</key><integer>$h</integer>
            <key>Minute</key><integer>5</integer>
        </dict>"
done

mkdir -p "$HOME/Library/LaunchAgents" logs
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>

    <!-- zsh -ilc, а не run.sh напрямую: агенту нужны nvm-версия node и переменные
         из .zshrc, которых у launchd в окружении нет -->
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>-ilc</string>
        <!-- &amp;&amp;, а не &&: это XML, и plutil на голом амперсанде ругается -->
        <string>cd '$ROOT' &amp;&amp; ./run.sh</string>
    </array>

    <!-- Если в это время ноут спал, launchd запустит задачу сразу после пробуждения.
         Разбудить сам он не может — это делает pmset. -->
    <key>StartCalendarInterval</key>
    <array>$INTERVALS
    </array>

    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key><string>$ROOT/logs/night.log</string>
    <key>StandardErrorPath</key><string>$ROOT/logs/night.log</string>
</dict>
</plist>
PLISTEOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "ночной агент поставлен: $PLIST"
echo "прогоны в часы: ${HOURS[*]} (в :05)"
echo
echo "Осталось разрешить ноуту просыпаться — это требует root, запусти сам:"
echo
echo "    sudo pmset repeat wakeorpoweron MTWRFSU $(printf '%02d' $(( (FROM + 24 - 1) % 24 ))):55:00"
echo
echo "Проверить:  pmset -g sched"
echo "Отменить:   sudo pmset repeat cancel"
