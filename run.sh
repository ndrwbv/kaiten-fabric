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
# Ни один прогон столько не длится: агенту отведено 45 минут, плюс запас на ревью.
# Замок старше этого — след прогона, который умер, не убрав за собой.
LOCK="state/run.lock"
LOCK_MAX_MINUTES=75
if ! mkdir "$LOCK" 2>/dev/null; then
  OWNER=$(cat "$LOCK/pid" 2>/dev/null || echo "")
  AGE=$(( ($(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || date +%s)) / 60 ))

  # Живость владельца одна ничего не доказывает: обёртка может осиротеть и висеть
  # часами, пока её внук держит открытым pipe. Поймали вживую — два часа простоя,
  # все запуски молча выходили. Поэтому решает ещё и возраст замка.
  if [[ -n "$OWNER" ]] && kill -0 "$OWNER" 2>/dev/null && (( AGE < LOCK_MAX_MINUTES )); then
    echo "прогон уже идёт (pid $OWNER, ${AGE} мин) — выхожу" >&2
    exit 75
  fi
  if [[ -n "$OWNER" ]] && kill -0 "$OWNER" 2>/dev/null; then
    # Гасим только если это точно наш зависший прогон. Pid переиспользуются, и
    # убивать группу по одному номеру — верный способ прибить что-то чужое:
    # при отладке этой самой ветки я так погасил собственную оболочку.
    OWNER_CMD=$(ps -o command= -p "$OWNER" 2>/dev/null || echo "")
    OWNER_PGID=$(ps -o pgid= -p "$OWNER" 2>/dev/null | tr -d ' ')
    if [[ "$OWNER_CMD" == *run.sh* || "$OWNER_CMD" == *factory.py* ]] \
       && [[ -n "$OWNER_PGID" && "$OWNER_PGID" != "$(ps -o pgid= -p $$ | tr -d ' ')" ]]; then
      echo "замку ${AGE} мин, зависший прогон $OWNER — гашу его группу" >&2
      kill -TERM -- "-$OWNER_PGID" 2>/dev/null || true
      sleep 2
      kill -KILL -- "-$OWNER_PGID" 2>/dev/null || true
    else
      echo "замку ${AGE} мин, но pid $OWNER занят чем-то другим — просто забираю замок" >&2
    fi
  else
    echo "снимаю протухший замок от pid ${OWNER:-?} (${AGE} мин)" >&2
  fi
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
