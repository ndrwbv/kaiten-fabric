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

mkdir -p logs
exec python3 factory.py "$@" 2>&1 | tee -a "logs/run.log"
