#!/usr/bin/env bash
# Точка входа фабрики. Все флаги пробрасываются в factory.py.
#
#   ./run.sh                      # взять карточки из «Очереди» и отработать
#   ./run.sh --dry-run            # агент работает, но Kaiten/GitHub не трогаем
#   ./run.sh --card 12345678      # прогнать конкретную карточку
#   ./run.sh --limit 1 --keep-worktree
set -euo pipefail
cd "$(dirname "$0")"

# claude отказывается стартовать внутри другой сессии Claude Code
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT

if [[ ! -f config.json ]]; then
  echo "нет config.json — запусти сначала: python3 setup.py" >&2
  exit 1
fi

# Переменные окружения, без которых в проекте не встанут зависимости (приватный npm-реестр,
# ключ к артефактам и подобное). Имена перечислены в config.json, ключ pass_env — значений
# там нет. Из интерактивного zsh они приходят сами, из launchd и cron — нет, поэтому
# подбираем их из ~/.zshrc.
while read -r name; do
  [[ -z "$name" ]] && continue
  if [[ -z "${!name:-}" && -f "$HOME/.zshrc" ]]; then
    value=$(sed -n "s/^[[:space:]]*export[[:space:]][[:space:]]*${name}=[\"']\{0,1\}\([^\"' ]*\).*/\1/p" \
            "$HOME/.zshrc" | tail -1)
    [[ -n "$value" ]] && export "$name=$value"
  fi
  [[ -z "${!name:-}" ]] && echo "warning: $name не найден — агент может не поставить зависимости" >&2
done < <(python3 -c "
import json
for name in json.load(open('config.json')).get('pass_env') or []:
    print(name)
")

mkdir -p logs state

# Замок от одновременных прогонов. Прогонов теперь два источника — таймер менюбар-
# приложения и ночной LaunchAgent, — а фабрика на параллельную работу не рассчитана:
# два прогона поделят карточки как попало и перетрут друг другу state/status.json.
LOCK="state/run.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  OWNER=$(cat "$LOCK/pid" 2>/dev/null || echo "")
  if [[ -n "$OWNER" ]] && kill -0 "$OWNER" 2>/dev/null; then
    echo "прогон уже идёт (pid $OWNER) — выхожу" >&2
    exit 0
  fi
  # владелец умер, не убрав за собой: замок протух, забираем себе
  echo "снимаю протухший замок от pid ${OWNER:-?}" >&2
  rm -rf "$LOCK" && mkdir "$LOCK"
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

# caffeinate держит ноут бодрствующим ровно пока идёт прогон: -i не даёт заснуть по
# бездействию (работает и от батареи), -s запрещает системный сон от сети. Без этого
# машина засыпает через displaysleep+sleep минут после того, как от неё отошли, —
# и агент, которому отведено 45 минут, до конца не доживает. Сам claude -p ассерта
# не ставит: тот, что видно в pmset, принадлежит десктопному приложению Claude.
# Без exec намеренно: с ним оболочка заменяется процессом, и `trap EXIT` не снимает
# замок. Сейчас это спасает только пайп в tee (он форкает подоболочку), но стоит убрать
# tee — и замок начнёт протухать после каждого прогона.
caffeinate -si python3 factory.py "$@" 2>&1 | tee -a "logs/run.log"
