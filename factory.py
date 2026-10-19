#!/usr/bin/env python3
"""
Фабрика агентов: Kaiten -> Claude Code (headless) -> git branch + PR -> Kaiten.

Берёт карточки из колонки "Очередь", гоняет по каждой headless-агента в отдельном
git-worktree, и в зависимости от исхода двигает карточку и пишет комментарий:

    Очередь -> В работе -> На ревью        (агент сделал коммит, создан PR)
                        -> Вопрос от агента (агенту не хватило информации)
                        -> Упало            (агент/обёртка сломались)

Отдельная дешёвая фаза — разведка «Инбокса»: карточки из сырого потока команды
агент читает, ищет описанное в коде и пишет в карточку, хватает ли данных, чтобы
задачу можно было брать в работу. Там он ничего не двигает и не правит, только
комментирует.

Запуск: ./run.sh [--dry-run] [--card ID] [--limit N] [--keep-worktree] [--no-pr]

Токен берётся из .env рядом со скриптом, иначе из ~/.claude/.env (там уже лежат
KAITEN_TOKEN и KAITEN_DOMAIN), иначе из переменных окружения.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
TEMPLATE_PATH = ROOT / "prompt_template.md"
REVIEW_TEMPLATE_PATH = ROOT / "review_prompt_template.md"
TRIAGE_TEMPLATE_PATH = ROOT / "triage_prompt_template.md"
ACCEPTANCE_TEMPLATE_PATH = ROOT / "epic_acceptance_prompt_template.md"
SPEC_TEMPLATE_PATH = ROOT / "epic_spec_prompt_template.md"
SPEC_REVIEW_TEMPLATE_PATH = ROOT / "epic_spec_review_prompt_template.md"
DECOMPOSE_TEMPLATE_PATH = ROOT / "epic_decompose_prompt_template.md"
# Всё, что специфично для конкретного проекта, лежит здесь и подставляется в промпты.
# Промпты от этого остаются общими, а команда правит только свои правила.
PROJECT_DIR = ROOT / "project"
PROJECT_DOCS_PATH = PROJECT_DIR / "docs.md"
PROJECT_CHECKLIST_PATH = PROJECT_DIR / "checklist.md"
WORKTREES = ROOT / "worktrees"
# Всё, что Стёпа заводит в чужом репозитории, живёт под `stepa/`: так его ветки
# видно в списке сразу и не спутать с ветками людей и других агентов.
BRANCH_PREFIX = "stepa/card-"
# Префиксы до переезда на `stepa/`. По ним ещё живут открытые PR, и фабрика должна
# их находить — для правок, мержа и витрины, — пока последний не закроется. Новые
# ветки под ними не заводятся.
LEGACY_BRANCH_PREFIXES = ("ai/card-",)
LOGS = ROOT / "logs"
STATE = ROOT / "state"
STATUS_FILE = STATE / "status.json"
# журнал исходов: строка на каждую законченную работу. status.json помнит только
# последнюю, а витрине нужна история — что сделано, что упало и когда
EVENTS_FILE = LOGS / "events.jsonl"
# счётчик неудач разведки по карточкам: не долбить одну и ту же карточку каждые 10 минут
TRIAGE_STATE_FILE = STATE / "triage.json"
# треды в Time: по какой карточке в каком канале лежит корневое сообщение и до какого
# ответа в нём мы уже дочитали
TIME_STATE_FILE = STATE / "time.json"

# Сухой прогон. Флаг лежит в модуле, а не ходит аргументом: состояние пишется из
# десятка мест, и протаскивать args в каждое — верный способ однажды забыть.
# Ставится один раз, в main, из `--dry-run`.
DRY_RUN = False

ENV_CANDIDATES = [ROOT / ".env", Path.home() / ".claude" / ".env"]

# Комментарии агента помечаем, чтобы отличать их от человеческих: и агент, и человек
# пишут под одним API-токеном, по автору их не различить.
AGENT_MARK = "🤖"
# Ревьювер пишет своей меткой: по ней считаем круги ревью
REVIEWER_MARK = "🔍"
# Разведчик инбокса — своей. Без вариационного селектора: эмодзи ездит через два API,
# и лишний невидимый символ ломал бы сравнение startswith
TRIAGE_MARK = "🧭"
# Метка эпик-агента. Объявлена здесь, а не в разделе эпиков, потому что входит
# в AGENT_MARKS: без неё фабрика принимала свои же комментарии по эпику за реплику
# человека — и «последнее слово за человеком» ломалось на каждом эпике.
EPIC_MARK = "🧩"

# всё, что написано роботами. Комментарий не с этой метки — реплика человека
AGENT_MARKS = (AGENT_MARK, REVIEWER_MARK, TRIAGE_MARK, EPIC_MARK)

# Метка комментария, перенесённого из треда в Time. В AGENT_MARKS её намеренно нет:
# это слова человека, просто сказанные в другом месте, и вся машина — «ответили ли
# на вопрос», «чей сейчас ход», стоп-фраза — должна считать их человеческими.
FROM_TIME_MARK = "💬"

# Строка в описании рабочей карточки: из какой карточки инбокса она выросла. По ней же
# ловится дубль, если разведка почему-то зайдёт на ту карточку второй раз.
INBOX_ORIGIN = "Из инбокса:"
INBOX_ORIGIN_RE = re.compile(r"Из инбокса:\s*#(\d+)")

# Строка в комментарии инбокса: задача уже поставлена. Разведка к такой карточке
# больше не возвращается, даже если в ней продолжают переписку.
#
# Доску в строке больше не называем: задача теперь заводится там, где работает
# команда. Старую формулировку держим для узнавания — по ней помечены карточки,
# разобранные до переезда, и забыть их значит разобрать их заново, за деньги.
HANDOFF_LINE = "Поставил задачу:"
HANDOFF_LINES = (HANDOFF_LINE, "Поставил задачу на «Доску для клода»:")

# Как зовут разработчика и по какому тегу он берёт задачи. Имя — не украшение:
# фабрика работает на общих досках наравне с людьми, и команде удобнее говорить
# «Стёпа возьмёт», а не «фабрика обработает карточку». Меняется в config.json,
# секция `bot`.
BOT_NAME = "Stepa Tugarev"
# Тег «это моё». На общей доске он — единственный способ сказать «Стёпа, возьми»,
# и он же висит на всём, что Стёпа ведёт: на эпиках, на сабтасках, на задачах
# из инбокса. Ночной тег растёт из него же: `<тег>:night`.
BOT_TAG = "stepa"
NIGHT_SUFFIX = ":night"

# Ручной стоп-кран: фраза в карточке выключает по ней всех агентов. Список можно
# дополнить в config.json, ключ stop_phrases.
STOP_PHRASES = [
    "стёпа не трогай", "степа не трогай", "не трогай стёпу", "не трогай степу",
    "стёпа не бери", "степа не бери", "не для стёпы", "не для степы",
    "клод не трогай", "не трогай клод", "клод не бери", "не для клода",
    "claude не трогай", "не трогай claude",
]

# Kaiten отвечает 400 на комментарий длиннее 4096 символов. Считает он, судя по
# поведению, в UTF-16 (эмодзи = 2), поэтому и мы меряем так же и оставляем запас
# под шапку «продолжение i/n».
COMMENT_LIMIT = 4096
COMMENT_RESERVE = 48

ACCEPTANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ready", "unclear"]},
        "summary": {"type": "string"},
        "criteria": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
        "backend_needed": {"type": "boolean"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary"],
    "additionalProperties": False,
}

SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "unclear"]},
        "summary": {"type": "string"},
        "spec_path": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary"],
    "additionalProperties": False,
}

SPEC_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "needs_changes"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                    "where": {"type": "string"},
                    "what": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["severity", "what", "fix"],
                "additionalProperties": False,
            },
        },
        # То, что автор закрыть не может: нужен макет, продуктовое решение, доступ.
        # Без этого поля круг правок упирался в одну и ту же стену, пока не кончались
        # круги, — по $4 за стену.
        "needs_human": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "summary"],
    "additionalProperties": False,
}

DECOMPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ready", "unclear"]},
        "summary": {"type": "string"},
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["frontend", "backend"]},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    # фронт можно делать сразу, на моке: бек блокирует не разработку,
                    # а раскатку. Флаг оседает в карточке и в PR предупреждением
                    "mocks_backend": {"type": "boolean"},
                },
                "required": ["kind", "title", "description"],
                "additionalProperties": False,
            },
        },
        "questions": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary"],
    "additionalProperties": False,
}


VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "unclear", "blocked"]},
        "summary": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "string"},
        # checks в карточку не пишем, но спрашиваем: агент, которого просят отчитаться
        # о проверках, чаще их и запускает. Оседает в logs/card-*.json
        "checks": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        # ответы на замечания людей из PR, по номеру замечания: уезжают в сам PR
        "pr_replies": {"type": "array", "items": {
            "type": "object",
            "properties": {"ref": {"type": "string"}, "text": {"type": "string"}},
            "required": ["ref", "text"],
            "additionalProperties": False,
        }},
    },
    "required": ["status", "summary"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "needs_changes"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                    "where": {"type": "string"},
                    "what": {"type": "string"},
                    "why": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["severity", "where", "what", "why", "fix"],
                "additionalProperties": False,
            },
        },
        # см. SPEC_REVIEW_SCHEMA: то, что автор закрыть не может без человека
        "needs_human": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "summary"],
    "additionalProperties": False,
}

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ready", "needs_info", "not_a_task"]},
        "problem": {"type": "string"},
        "where": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
        "plan": {"type": "array", "items": {"type": "string"}},
        "effort": {"type": "string", "enum": ["s", "m", "l"]},
        "risk": {"type": "string"},
    },
    "required": ["status", "problem"],
    "additionalProperties": False,
}



def with_products(schema: dict, cfg: dict, nested: str | None = None,
                  required: bool = True) -> dict:
    """
    Схема ответа с полями `product` и `repo`, перечисленными из конфига.

    Список — enum, а не свободная строка: агенту проще выбрать из готового, а нам не
    надо гадать, что значит «сайт-фронт». Без продуктов схема остаётся как была.
    `nested` — поле-список, в элементах которого живёт выбор (сабтаски эпика).
    """
    choices = destinations(cfg)
    if not choices:
        return schema
    repos = list(dict.fromkeys(repo for key in choices for repo in product_repos(cfg, key)))
    extra = {"product": {"type": "string", "enum": list(choices)},
             "repo": {"type": "string", "enum": repos}}
    result = json.loads(json.dumps(schema))
    target = result["properties"][nested]["items"] if nested else result
    target["properties"].update(extra)
    if required:
        target["required"] = list(dict.fromkeys(target.get("required", []) + list(extra)))
    return result


SEVERITY_ICON = {"blocker": "🛑", "major": "⚠️", "minor": "💬"}

# что уходит в шапку комментария и в строку «последняя» в меню-баре
TRIAGE_HEAD = {
    "ready": "данных хватает, можно брать в работу",
    "needs_info": "данных не хватает",
    "not_a_task": "правка кода не нужна",
}

EFFORT_LABEL = {"s": "оценка S (пара часов)", "m": "оценка M (день)",
                "l": "оценка L (больше дня)"}

# Транслит для имён веток: кириллица в git легальна, но неудобна во всём остальном.
TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slugify(text: str, limit: int = 48) -> str:
    """«Обязательный ввод номера» -> «obyazatelnyy-vvod-nomera»."""
    latin = "".join(TRANSLIT.get(char, char) for char in (text or "").lower())
    slug = re.sub(r"[^a-z0-9]+", "-", latin).strip("-")
    return slug[:limit].rstrip("-")


# --------------------------------------------------------------------------- #
# инфраструктура
# --------------------------------------------------------------------------- #

# Команды, которые печатают JSON для машины: у них stdout обязан быть чистым, иначе
# «токен из ~/.claude/.env» первой строкой ломает разбор. Логи в этом режиме уходят
# в stderr — человеку они по-прежнему видны, а `run.sh` подхватывает и их (2>&1).
MACHINE_OUTPUT = False


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True,
          file=sys.stderr if MACHINE_OUTPUT else sys.stdout)


class ScoutSetupError(RuntimeError):
    """Разведка не смогла подготовить рабочую копию: это беда окружения, а не карточки."""


class FactoryError(RuntimeError):
    """Ошибка обёртки — карточка уедет в 'Упало'."""


class NetworkError(FactoryError):
    """
    curl не дошёл до сервера: нет сети, не разрезолвился домен, вышло время. Сюда же
    502/503/504 — за ними стоит шлюз, а не ответ Kaiten.

    Наследуется от FactoryError намеренно: все существующие ловушки продолжают
    работать. Но от ответа сервера это отличается принципиально — из молчания сети
    не следует ничего о том, что на той стороне. Семнадцатого сентября мак проснулся
    раньше вайфая, `GET /cards/{id}` упал по таймауту, это прочиталось как «карточку
    удалили», и треды в Time выкинулись из состояния безвозвратно.
    """


class NotFoundError(FactoryError):
    """Сервер ответил 404: объекта правда нет — вот на это опираться уже можно."""


class AgentAuthError(FactoryError):
    """
    claude не смог авторизоваться: беда окружения, как и ScoutSetupError, и точно
    так же не вина карточки.

    Наследуется от FactoryError намеренно: все существующие ловушки продолжают
    работать, и прогон не разваливается трейсбеком. Разведка ловит этот класс
    отдельно — ей важно не записать протухший токен в счёт карточке.
    """


def bot_name(cfg: dict) -> str:
    """Как зовут разработчика."""
    return ((cfg.get("bot") or {}).get("name") or BOT_NAME).strip()


def bot_tag(cfg: dict) -> str:
    """
    Тег «это моё».

    Один тег на всё: на общей доске он говорит «Стёпа, возьми», на эпике — «этот
    эпик ведёт Стёпа», на сабтаске — «эту написал он». Разные теги под каждый случай
    были бы честнее по смыслу, но человеку пришлось бы помнить три слова вместо
    одного, а он помнит имя.
    """
    return ((cfg.get("bot") or {}).get("tag") or BOT_TAG).strip()


def utf16_len(text: str) -> int:
    """Длина так, как её считает Kaiten: эмодзи и прочий не-BMP — по два символа."""
    return len(text.encode("utf-16-le")) // 2


def split_comment(text: str, limit: int = COMMENT_LIMIT) -> list[str]:
    """
    Режет длинный комментарий на части, которые Kaiten проглотит.

    Рвём по границам абзацев, если абзац сам великоват — по строкам, и только
    в самом безнадёжном случае — посимвольно. Каждая часть начинается с AGENT_MARK,
    иначе pick_cards примет последнюю за ответ человека и утащит карточку в работу.
    """
    if utf16_len(text) <= limit:
        return [text]

    budget = limit - COMMENT_RESERVE
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.rstrip())
        current = ""

    def add(piece: str, separator: str) -> None:
        nonlocal current
        candidate = current + separator + piece if current else piece
        if utf16_len(candidate) <= budget:
            current = candidate
            return
        flush()
        if utf16_len(piece) <= budget:
            current = piece
            return
        # кусок не влезает целиком — разбираем его помельче
        if separator == "\n\n":
            for line in piece.split("\n"):
                add(line, "\n")
            return
        # последний рубеж: посимвольно, считая ширину в UTF-16, а не в кодовых точках,
        # иначе строка из эмодзи проскакивает мимо лимита
        buffer, width = "", 0
        for char in piece:
            char_width = 2 if ord(char) > 0xFFFF else 1
            if width + char_width > budget:
                chunks.append(buffer)
                buffer, width = char, char_width
            else:
                buffer += char
                width += char_width
        if buffer:
            chunks.append(buffer)

    for paragraph in text.split("\n\n"):
        add(paragraph, "\n\n")
    flush()

    total = len(chunks)
    # метку продолжения берём ту же, что у первой части: иначе последняя часть ревью
    # выглядела бы как реплика исполнителя, и круги ревью посчитались бы неверно
    lead = text.lstrip()
    mark = next((m for m in (REVIEWER_MARK, TRIAGE_MARK) if lead.startswith(m)), AGENT_MARK)
    result = [chunks[0]]
    for index, chunk in enumerate(chunks[1:], start=2):
        result.append(f"{mark} _(продолжение {index}/{total})_\n\n{chunk}")
    if not result[0].startswith(mark):
        result[0] = f"{mark} {result[0]}"
    return result


def write_status(**fields) -> None:
    """
    Состояние для менюбар-приложения. Смена фазы отмечается временем: по нему видно,
    сколько шаг уже длится, а значит и что он завис.
    """
    """
    Состояние для менюбар-приложения: что фабрика делает прямо сейчас.
    Пишем через временный файл — читатель не должен поймать половину JSON.
    """
    STATE.mkdir(exist_ok=True)
    data = {}
    if STATUS_FILE.is_file():
        try:
            data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if "phase" in fields and fields.get("phase") != data.get("phase"):
        data["phase_since"] = now if fields.get("phase") else None
    data.update(fields)
    data["updated"] = now
    tmp = STATUS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATUS_FILE)


def note_phase(args, text: str) -> None:
    """
    Отметить шаг фабрики. Тонкая обёртка над write_status: без неё каждый вызов
    приходится оборачивать в `if not args.prompt_only`, и о нём легко забыть —
    в фазе эпиков так и вышло, там весь шаг проходил молча.
    """
    if not getattr(args, "prompt_only", False):
        write_status(phase=text)


def load_env() -> dict:
    """Первый .env, в котором есть KAITEN_TOKEN, выигрывает. Иначе — окружение."""
    for path in ENV_CANDIDATES:
        if not path.is_file():
            continue
        values = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            line = line.removeprefix("export ")
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
        if values.get("KAITEN_TOKEN"):
            log(f"токен из {path}")
            return values
    if os.environ.get("KAITEN_TOKEN"):
        log("токен из переменных окружения")
        return dict(os.environ)
    raise FactoryError(
        "KAITEN_TOKEN не найден. Положи его в " + " или ".join(str(p) for p in ENV_CANDIDATES)
    )

# Имя записи в Keychain, куда меню-бар кладёт секреты из окна настроек.
KEYCHAIN_SERVICE = "kaiten-fabrica"


def keychain_secret(name: str) -> str:
    """
    Секрет из Keychain. Не на маке — пусто, и это не ошибка.

    Окно настроек пишет сюда, а не в файл: токен в открытом виде на диске — то, чего
    в публичном репозитории не должно появиться даже случайно.
    """
    if sys.platform != "darwin":
        return ""
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def env_file_values(path: Path) -> dict:
    """Пары ключ-значение из .env. Файла нет или он битый — пустой словарь."""
    values = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_secret(name: str, env: dict | None = None) -> str:
    """
    Секрет ищется в одном порядке на всё: окружение, `.env`, Keychain.

    Первым окружение — под будущий сервер: там нет ни Keychain, ни профиля, зато есть
    переменные из systemd, и они должны выигрывать, не требуя правки конфига.

    Дальше все `.env` из ENV_CANDIDATES, а не только тот словарь, что передали. Иначе
    была бы ловушка: `load_env` выбирает первый файл, в котором есть KAITEN_TOKEN, и
    секрет, положенный в другой файл, оказался бы невидим без единого сообщения.

    Keychain последним. Смысла в нём меньше, чем кажется: чтобы фабрика читала оттуда
    без диалога подтверждения, запись надо открыть всем программам пользователя — а тогда
    защита ровно та же, что у файла с правами 0600. Оставлен на случай, когда кому-то так
    удобнее, но окно настроек пишет в `.env`.
    """
    from_env = (os.environ.get(name) or "").strip()
    if from_env:
        return from_env
    passed = ((env or {}).get(name) or "").strip()
    if passed:
        return passed
    for path in ENV_CANDIDATES:
        found = (env_file_values(path).get(name) or "").strip()
        if found:
            return found
    return keychain_secret(name)


# --------------------------------------------------------------------------- #
# Kaiten
# --------------------------------------------------------------------------- #

# Чтение из Kaiten повторяем: одиночный обрыв — штатное событие в корпоративной сети,
# и целого прогона он стоить не должен. Восемнадцатого сентября /cards трижды за день
# приезжал наполовину и упирался в --max-time 30, а минутой позже тот же запрос
# отдавался за полторы секунды — каждый раз это отменяло прогон целиком. Паузы растут,
# чтобы покрыть и главный случай: мак проснулся раньше вайфая.
KAITEN_READ_RETRIES = 3
KAITEN_RETRY_PAUSE = 6

# Ответы шлюза, за которыми ответа Kaiten нет вовсе. Для нас это то же молчание сети:
# о карточке из них не следует ничего, поэтому и повторяем их так же.
KAITEN_TRANSIENT_HTTP = (502, 503, 504)


class Kaiten:
    def __init__(self, domain: str, token: str, space_id: int, dry_run: bool = False):
        self.api = f"https://{domain}/api/latest"
        self.domain = domain
        self.token = token
        self.space_id = space_id
        self.dry_run = dry_run

    def _request(self, method: str, path: str, body=None, params=None):
        """
        Ходим через curl, а не через urllib: на рабочих маках стоит корпоративный
        MITM-прокси, его корневой сертификат лежит в системном хранилище. curl туда
        смотрит, python — в свой bundled certifi, и падает на CERTIFICATE_VERIFY_FAILED.
        Токен уходит в curl через stdin (-K -), чтобы не светиться в `ps`.

        Чтение повторяем, запись — никогда. Из оборванной записи не следует, что
        Kaiten её не принял: соединение могло умереть уже после того, как комментарий
        лёг в карточку, и второй заход положил бы туда второй такой же.
        """
        url = self.api + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        cmd = ["curl", "-sS", "-K", "-", "--max-time", "30",
               "-X", method, "-H", "Content-Type: application/json",
               "-w", "\n%{http_code}", url]
        if body is not None:
            cmd += ["--data-raw", json.dumps(body, ensure_ascii=False)]

        attempts = KAITEN_READ_RETRIES if method == "GET" else 1
        for attempt in range(1, attempts + 1):
            try:
                return self._curl(cmd, method, path)
            except NetworkError as e:
                if attempt >= attempts:
                    raise
                pause = KAITEN_RETRY_PAUSE * attempt
                log(f"  сеть подвела на {method} {path} ({str(e).split(' -> ')[-1][:90]}), "
                    f"повтор {attempt + 1} из {attempts} через {pause}с")
                time.sleep(pause)

    def _curl(self, cmd: list[str], method: str, path: str):
        """Один заход: разобранный ответ, либо ошибка того или иного рода."""
        try:
            proc = subprocess.run(cmd, input=f'header = "Authorization: Bearer {self.token}"\n',
                                  capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise NetworkError(f"Kaiten {method} {path} -> curl не уложился в минуту") from None
        if proc.returncode != 0:
            # Сюда же приходит curl 28 после сна: пока мак спит, не тикает ни таймер
            # curl, ни наш, и просыпаются оба сразу — отсюда в логе «timed out after
            # 972323 milliseconds» при --max-time 30. Сервер тут ни при чём.
            raise NetworkError(f"Kaiten {method} {path} -> curl {proc.returncode}: "
                               f"{proc.stderr.strip()[:300]}")

        raw, _, code = proc.stdout.rpartition("\n")
        if code.strip() and int(code) >= 400:
            if int(code) in KAITEN_TRANSIENT_HTTP:
                # За таким ответом нет ответа самого Kaiten — это молчание шлюза,
                # и знать по нему про карточку нечего, ровно как при обрыве.
                raise NetworkError(f"Kaiten {method} {path} -> HTTP {code}")
            kind = NotFoundError if int(code) == 404 else FactoryError
            raise kind(f"Kaiten {method} {path} -> HTTP {code}: {raw[:500]}")
        return json.loads(raw) if raw.strip() else None

    def _write(self, method: str, path: str, body=None):
        if self.dry_run:
            log(f"  [dry-run] {method} {path} {json.dumps(body, ensure_ascii=False)[:160]}")
            return None
        return self._request(method, path, body)

    # -- чтение ------------------------------------------------------------- #

    def board(self, board_id: int) -> dict:
        """
        Доска целиком: колонки и дорожки приезжают прямо в ответе.

        Отдельного запроса за колонками нет — `GET /boards/{id}/columns` не показывает
        подколонки, а `GET /boards/{id}/columns/{id}` отвечает 405. Разбирает ответ
        flat_columns, там же и подробности.
        """
        return self._request("GET", f"/boards/{board_id}")

    def cards_on_board(self, board_id: int, with_description: bool = False,
                       lane_id: int | None = None) -> list:
        """
        Все живые карточки доски, а с `lane_id` — одной её дорожки: доска эпиков
        общая на всю компанию, и тянуть её целиком ради своей дорожки незачем.
        API отдаёт максимум 100 за раз.
        """
        found, offset = [], 0
        while True:
            params = {"board_id": board_id, "archived": "false", "condition": 1,
                      "limit": 100, "offset": offset}
            if lane_id:
                params["lane_id"] = lane_id
            if with_description:
                params["additional_card_fields"] = "description"
            page = self._request("GET", "/cards", params=params) or []
            found += page
            if len(page) < 100:
                return found
            offset += 100

    def children(self, card_id: int) -> list:
        return self._request("GET", f"/cards/{card_id}/children") or []

    def cards_in_column(self, board_id: int, column_id: int) -> list:
        return self._request("GET", "/cards", params={
            "board_id": board_id,
            "column_id": column_id,
            "archived": "false",
            "additional_card_fields": "description",
            "limit": 100,
        }) or []

    def card(self, card_id: int) -> dict:
        return self._request("GET", f"/cards/{card_id}")

    def comments(self, card_id: int) -> list:
        """
        Комментарии людей и фабрики, по времени. Интеграции отсюда вычищены.

        У системных аккаунтов Kaiten отрицательный id: у GitHub-бота Octocat это -3.
        Его комментарий про коммит прилетает через доли секунды после нашего — и
        оказывается последним. Фаза эпика читается из комментариев, «последним
        высказался не агент» значит «человек что-то ответил», и эпик после каждой
        правки спеки заново уходил в правку вместо ревью: два круга по $4 подряд
        первого сентября. Заодно из промпта уходит шум, который агенту не нужен.
        """
        data = self._request("GET", f"/cards/{card_id}/comments") or []
        human = [c for c in data if (c.get("author") or {}).get("id", 0) >= 0]
        return sorted(human, key=lambda c: c.get("created") or "")

    # -- запись ------------------------------------------------------------- #

    def comment(self, card_id: int, text: str) -> None:
        parts = split_comment(text)
        if len(parts) > 1:
            log(f"  комментарий на {utf16_len(text)} символов — режу на {len(parts)} части")
        for part in parts:
            if part.strip():  # пустой комментарий Kaiten тоже не примет
                self._write("POST", f"/cards/{card_id}/comments", {"text": part})

    def move(self, card_id: int, column_id: int) -> None:
        self._write("PATCH", f"/cards/{card_id}", {"column_id": column_id})

    def create_card(self, body: dict) -> dict | None:
        return self._write("POST", "/cards", body)

    def patch_card(self, card_id: int, body: dict) -> None:
        self._write("PATCH", f"/cards/{card_id}", body)

    def add_child(self, parent_id: int, child_id: int) -> None:
        self._write("POST", f"/cards/{parent_id}/children", {"card_id": child_id})

    def add_tag(self, card_id: int, name: str) -> None:
        """Тег добавляется по имени; с tag_id Kaiten отвечает 400."""
        self._write("POST", f"/cards/{card_id}/tags", {"name": name})

    def remove_tag(self, card_id: int, tag_id: int) -> None:
        """
        Отвязать тег от карточки. В отличие от блокера, тег снимается по-настоящему
        и мусора в общем списке тегов компании не оставляет — проверено на живой
        карточке: после удаления `GET /tags` его больше не показывает.
        """
        self._write("DELETE", f"/cards/{card_id}/tags/{tag_id}")

    def blockers(self, card_id: int) -> list:
        """
        Только действующие блокеры.

        Снятый блокер Kaiten не удаляет, а помечает `released: true`, и он остаётся
        в выдаче навсегда. Проверка на непустой список заблокировала бы карточку
        насовсем после первого же снятия.
        """
        found = self._request("GET", f"/cards/{card_id}/blockers") or []
        return [b for b in found if not b.get("released")]

    def block(self, card_id: int, reason: str) -> None:
        self._write("POST", f"/cards/{card_id}/blockers", {"reason": reason})

    def unblock(self, card_id: int, blocker_id: int) -> None:
        self._write("DELETE", f"/cards/{card_id}/blockers/{blocker_id}")

    def add_checklist(self, card_id: int, name: str, items: list[str]) -> dict | None:
        """Чек-лист целиком: сам список и пункты по одному, отдельными запросами."""
        created = self._write("POST", f"/cards/{card_id}/checklists", {"name": name})
        if not created:
            return None
        for text in items:
            self._write("POST", f"/cards/{card_id}/checklists/{created['id']}/items",
                        {"text": text, "checked": False})
        return created

    def card_url(self, card: dict) -> str:
        # в ответе /cards/{id} space_id не приходит, поэтому берём его из конфига
        space = card.get("space_id") or (card.get("board") or {}).get("space_id") or self.space_id
        return f"https://{self.domain}/space/{space}/boards/card/{card['id']}"


def flat_columns(board: dict) -> list[dict]:
    """
    Все колонки доски одним списком, включая подколонки.

    Подколонки Kaiten не отдаёт отдельным запросом: `GET /boards/{id}/columns` их не
    показывает, а `GET /boards/{id}/columns/{id}` вообще 405. Единственное место, где
    они лежат, — ключ `subcolumns` внутри колонок самой доски.

    Карточки живут только в листьях: у колонки с подколонками своих карточек не бывает
    (проверено на доске эпиков — в родительских колонках ноль). Поэтому родителей
    в список не кладём, иначе в мастере можно выбрать колонку, в которую не попасть.
    """
    result = []
    for column in sorted(board.get("columns") or [], key=lambda c: c.get("sort_order") or 0):
        subs = sorted(column.get("subcolumns") or [], key=lambda c: c.get("sort_order") or 0)
        if not subs:
            result.append({**column, "path": column.get("title") or ""})
            continue
        for sub in subs:
            result.append({**sub, "path": f"{column.get('title')} / {sub.get('title')}"})
    return result


def column_label(column: dict) -> str:
    return f"{column.get('path') or column.get('title')}  (id {column['id']})"


def tag_id(card: dict, name: str) -> int | None:
    """
    id тега на карточке — чтобы его снять. None, если тега нет.

    Берём `tag_id`, а не `id`: у Kaiten это одно и то же число, но `tag_id` — это
    именно то, чего ждёт `DELETE /cards/{id}/tags/{tag_id}`, и полагаться лучше
    на него.
    """
    wanted = normalize_phrase(name)
    for tag in (card.get("tags") or []):
        if normalize_phrase(tag.get("name")) == wanted:
            found = tag.get("tag_id") or tag.get("id")
            return int(found) if found else None
    return None


def has_tag(card: dict, name: str) -> bool:
    """
    Есть ли на карточке такой тег.

    В выдаче списком ключ `tags` появляется только у карточек, у которых теги реально
    стоят: у остальных его нет вовсе. Поэтому `or []` здесь обязателен.
    """
    wanted = normalize_phrase(name)
    return any(normalize_phrase(tag.get("name")) == wanted for tag in (card.get("tags") or []))


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #

# Какие события уезжают в канал, если в конфиге не сказано иное.
TIME_EVENTS = ("pr_opened", "pr_updated", "fix_taken", "status", "asked")

# Последняя строка сообщения — не название колонки, а фраза человеку в канал: там
# сидят коллеги, а не фабрика, и им важно одно — смотреть уже можно или ещё нет.
# Пока возимся — «не смотрите». Нужен человек — «позырьте». Закончили — строка просто
# исчезает, и сообщение снова становится ссылкой на готовый PR.
# Роли могут делить колонку, поэтому побеждает первая подходящая.
TIME_STATUSES = {
    "queue": "пацаны, пока не смотрите, сыровато",
    "in_progress": "пацаны, пока не смотрите, сыровато",
    "agent_review": "пацаны, пока не смотрите, сыровато",
    "fixes": "пацаны, пока не смотрите, сыровато",
    "review": "пацанчики, позырьте плз",
    "question": "пацанчики, позырьте плз, есть вопросики",
    "failed": "у меня тут упало, нужна помощь",
    "done": "",
}

# Когда у агента есть вопросы, статус говорит и об этом: сами вопросы уезжают в тред,
# а строка в канале объясняет, зачем туда идти.
TIME_STATUS_ASKED = "пацанчики, позырьте плз, есть вопросики"

# Все статусы разом — чтобы узнать свою же строку в сообщении, прочитанном из Time.
TIME_STATUS_LINES = frozenset(s for s in [*TIME_STATUSES.values(), TIME_STATUS_ASKED] if s)

# То же самое, но обычными словами: статус-фраза хороша в конце сообщения и плохо
# читается внутри предложения, а на вопрос «как дела» отвечать надо предложением.
# Всё от первого лица: в треде с человеком говорит разработчик, а не пульт управления
# фабрикой. Под капотом исполнитель и ревьювер — разные агенты, но в канале это
# подробности устройства, и «ждёт ревьювера» из разговора выпадает.
TIME_PLAIN_STATUSES = {
    "queue": "ещё не брался, задача в очереди",
    "in_progress": "в работе, пишу код",
    "agent_review": "дописал, перечитываю за собой",
    "fixes": "правлю по замечаниям",
    "review": "жду вашего ревью",
    "question": "жду ответа на вопросы",
    "failed": "сломался на этой задаче, нужна помощь",
    "done": "готово",
}

# По этим словам короткое сообщение в треде считается вопросом «что там с задачей»:
# на такое отвечаем из карточки и агента не зовём. Список намеренно грубый — ошибка
# здесь стоит дорого: спросят «как дела», а мы потратим на это настоящий прогон.
TIME_STATUS_WORDS = ("как дела", "что там", "как продвигается", "как успехи", "статус",
                     "на каком этапе", "что по задаче", "есть новости", "ну как",
                     "как оно", "что нового", "долго ещё", "когда будет")

# Сообщение длиннее этого вопросом о статусе не считается, даже если начинается
# с «как дела»: в «как дела? и поправь ещё заголовок» главное — вторая половина.
TIME_STATUS_LIMIT = 80

# Слова, по которым сообщение считается постановкой, а не вопросом. Не весь русский
# императив, а то, чем в треде правда просят. Сравниваем по целым словам: в «а ты
# проверял на большом экране?» есть «провер», но нет «проверь», и это разные вещи.
TIME_TASK_WORDS = frozenset("""
    поправь поправьте правь исправь почини сделай сделайте переделай перепиши
    добавь допиши дополни убери удали верни замени перенеси подвинь опусти подними
    уменьши увеличь поменяй смени обнови откати закрой разбери разберись выровняй
    проверь попробуй посмотри посмотрите глянь гляньте уточни оставь
    надо нужно давай
""".split())


def times(count: int) -> str:
    """«один раз», «2 раза», «5 раз» — чтобы ответ в треде не косноязычил."""
    if count == 1:
        return "один раз"
    tail = "раза" if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14) else "раз"
    return f"{count} {tail}"


def is_status_line(line: str) -> bool:
    """
    Последняя строка сообщения — это статус, который мы сами же и дописали?

    Нужно там, где сообщение читается из Time, а не из своего состояния: снять старый
    статус, чтобы не приписать к нему новый. Курсив учитываем ради сообщений, висящих
    в канале с тех пор, когда статус писался курсивом.
    """
    line = line.strip()
    if len(line) > 2 and line.startswith("_") and line.endswith("_"):
        line = line[1:-1].strip()
    return line in TIME_STATUS_LINES


def strip_mention(text: str, handle: str) -> str:
    """Убрать обращение к боту: в карточку и в разбор оно уже ни к чему."""
    if not handle:
        return text.strip()
    return re.sub(rf"(?i){re.escape(handle)}[:,]?\s*", "", text).strip()


def thread_intent(text: str) -> str:
    """
    Чего от нас хотят сообщением в треде: `status`, `question` или `task`.

    `status` — «как дела»: отвечаем из карточки и агента не зовём. Спросить в треде
    «что там» стоит человеку одну строку, а прогон агента — минуты и деньги.

    `question` — вопрос по самой задаче: «а на каком экране смотрел?». Ответить
    на такое можно только заглянув в код, поэтому круг мы всё-таки заводим, но
    просим ответить словами, а не искать, что бы поправить.

    `task` — всё остальное, и сюда же уходит спорное: правка, принятая за вопрос,
    потерялась бы молча, а лишний вопрос агент просто разберёт вместе с задачей.
    """
    body = text.strip()
    low = normalize_phrase(body)
    if set(low.split()) & TIME_TASK_WORDS:
        return "task"
    if len(body) <= TIME_STATUS_LIMIT and any(w in low for w in TIME_STATUS_WORDS):
        return "status"
    return "question" if "?" in body else "task"


def time_status_answer(profile: dict | None, card: dict, comments: list,
                       asks: list, blocker: str = "") -> str:
    """
    Ответ на «как дела» — из карточки, без запуска агента.

    Спросить в треде «что там» стоит человеку одну строку, а прогон агента — минуты
    и деньги. Всё, что нужно для ответа, и так лежит в Kaiten: колонка, сколько раз
    переделывали и заголовок последнего отчёта.
    """
    role, _ = time_status(profile, card, asks, blocker)
    where = TIME_PLAIN_STATUSES.get(role, "не пойму, где карточка")
    lines = [f"Сейчас: {where}."]

    rounds = sum(1 for comment in comments
                 if strip_html(comment.get("text", "")).startswith(REVIEWER_MARK))
    if rounds:
        lines.append(f"Переделывал по замечаниям {times(rounds)}.")

    # заголовок последнего отчёта — первая строка агентского комментария без разметки
    headline = ""
    for comment in comments:
        text = strip_html(comment.get("text", ""))
        if text.startswith(AGENT_MARKS):
            headline = text.splitlines()[0].lstrip("".join(AGENT_MARKS)).strip(" *")
    if headline:
        lines.append(f"Последнее: {headline}")
    if asks:
        lines.append("Вопросы — выше в треде, жду ответа.")
    return " ".join(lines)


class Time:
    """
    Клиент Time — это Mattermost, обычный `/api/v4`.

    Ходим curl'ом по той же причине, что и в Kaiten: на рабочих маках корпоративный
    MITM-прокси, и python падает на проверке сертификата.

    Два транспорта. С токеном бота доступно всё: и писать, и читать тред. С incoming
    webhook — только писать: тред он создать не умеет, id своего сообщения не отдаёт,
    а читать канал им нельзя вообще. Поэтому `can_read` — это ещё и «умеем ли мы
    вторую половину работы».
    """

    def __init__(self, base_url: str, token: str = "", webhook_url: str = "",
                 dry_run: bool = False):
        self.api = (base_url or "").rstrip("/") + "/api/v4"
        self.token = token
        self.webhook_url = webhook_url
        self.dry_run = dry_run
        self._my_id: str | None = None

    @property
    def can_read(self) -> bool:
        return bool(self.token)

    @property
    def my_id(self) -> str:
        """Свой id — он нужен реакциям. Спрашиваем один раз на клиента."""
        if self._my_id is None:
            self._my_id = (self.me() or {}).get("id") or ""
        return self._my_id

    def _request(self, method: str, url: str, body=None, config: str = ""):
        """
        Секреты уходят в curl через stdin (-K -), а не в argv: и токен, и URL вебхука
        видны были бы в `ps` любому пользователю машины. У вебхука URL и есть секрет,
        поэтому в его случае через stdin едет сам адрес.
        """
        cmd = ["curl", "-sS", "-K", "-", "--max-time", "30", "-X", method,
               "-H", "Content-Type: application/json", "-w", "\n%{http_code}"]
        if url:
            cmd.append(url)
        if body is not None:
            cmd += ["--data-raw", json.dumps(body, ensure_ascii=False)]
        proc = subprocess.run(cmd, input=config or "\n", capture_output=True,
                              text=True, timeout=60)
        if proc.returncode != 0:
            raise FactoryError(f"Time {method} -> curl {proc.returncode}: "
                               f"{proc.stderr.strip()[:200]}")
        raw, _, code = proc.stdout.rpartition("\n")
        if code.strip() and int(code) == 404:
            raise NotFoundError(f"Time {method} -> HTTP 404: {raw[:300]}")
        if code.strip() and int(code) >= 400:
            raise FactoryError(f"Time {method} -> HTTP {code}: {raw[:300]}")
        try:
            return json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError:
            # вебхук на успех отвечает не JSON, а словом ok — это не ошибка
            return None

    def _auth(self) -> str:
        return f'header = "Authorization: Bearer {self.token}"\n'

    def post(self, channel_id: str, text: str, root_id: str = "") -> dict | None:
        """Сообщение в канал или ответ в тред. Возвращает пост — у вебхука его нет."""
        if self.dry_run:
            where = f"тред {root_id}" if root_id else f"канал {channel_id}"
            log(f"  [dry-run] Time -> {where}: {text[:120]}")
            return None
        if self.webhook_url:
            if root_id:
                log("  вебхук не умеет отвечать в тред — сообщение уйдёт в канал")
            return self._request("POST", "", {"text": text},
                                 config=f'url = "{self.webhook_url}"\n')
        body = {"channel_id": channel_id, "message": text}
        if root_id:
            body["root_id"] = root_id
        return self._request("POST", f"{self.api}/posts", body, config=self._auth())

    def edit(self, post_id: str, text: str) -> None:
        """
        Переписать своё сообщение. У Mattermost это `PUT /posts/{id}/patch`, а не PATCH:
        на PATCH он отвечает 404 «нет такой ручки».
        """
        if self.dry_run:
            log(f"  [dry-run] Time -> правка {post_id}: {text[:120]}")
            return
        self._request("PUT", f"{self.api}/posts/{post_id}/patch", {"message": text},
                      config=self._auth())

    def react(self, post_id: str, user_id: str, emoji: str = "eyes") -> None:
        """
        Поставить эмодзи на чужое сообщение.

        Нужно, потому что работа по сообщению из треда занимает круг: карточка уезжает
        в «Правки», агент идёт смотреть код, человек всё это время не знает, услышали
        его или нет. Глазок закрывает эту паузу сразу и ничего не обещает сверх «взял».

        Повторную реакцию Mattermost не считает ошибкой — она и так уже стоит.
        """
        if self.dry_run:
            log(f"  [dry-run] Time -> :{emoji}: на {post_id}")
            return
        self._request("POST", f"{self.api}/reactions",
                      {"user_id": user_id, "post_id": post_id, "emoji_name": emoji},
                      config=self._auth())

    def unreact(self, post_id: str, user_id: str, emoji: str = "eyes") -> None:
        """Снять своё эмодзи. Глазок живёт ровно пока идёт работа, а не вечно."""
        if self.dry_run:
            log(f"  [dry-run] Time -> снять :{emoji}: с {post_id}")
            return
        self._request("DELETE",
                      f"{self.api}/users/{user_id}/posts/{post_id}/reactions/{emoji}",
                      config=self._auth())

    def thread(self, root_id: str) -> list:
        """Сообщения треда по времени. Ответ Mattermost — словарь постов плюс порядок."""
        data = self._request("GET", f"{self.api}/posts/{root_id}/thread",
                             config=self._auth()) or {}
        posts = list((data.get("posts") or {}).values())
        return sorted(posts, key=lambda post: post.get("create_at") or 0)

    def channel_posts(self, channel_id: str, since_ms: int) -> list:
        """
        Сообщения канала новее `since_ms`, по времени. Mattermost отдаёт по `since`
        и правленные старые — их отсекает сам разбор, по `create_at`.
        """
        data = self._request("GET", f"{self.api}/channels/{channel_id}/posts?since={since_ms}",
                             config=self._auth()) or {}
        posts = list((data.get("posts") or {}).values())
        return sorted(posts, key=lambda post: post.get("create_at") or 0)

    def post_by_id(self, post_id: str) -> dict:
        return self._request("GET", f"{self.api}/posts/{post_id}",
                             config=self._auth()) or {}

    def me(self) -> dict:
        data = self._request("GET", f"{self.api}/users/me", config=self._auth()) or {}
        self._my_id = data.get("id") or ""
        return data

    def my_channels(self) -> list[dict]:
        """
        Каналы, в которых бот уже состоит.

        Бота в нужные каналы обычно добавляют сразу при создании, так что спрашивать
        у человека id канала незачем — он и так есть в этом списке. Личные переписки
        отбрасываем: оповещения про PR им не место.
        """
        found = []
        teams = self._request("GET", f"{self.api}/users/me/teams",
                              config=self._auth()) or []
        for team in teams:
            channels = self._request(
                "GET", f"{self.api}/users/me/teams/{team.get('id')}/channels",
                config=self._auth()) or []
            for channel in channels:
                if channel.get("type") in ("D", "G"):
                    continue
                found.append({
                    "id": channel.get("id"),
                    "name": channel.get("display_name") or channel.get("name") or "?",
                    "team": team.get("display_name") or team.get("name") or "",
                    "private": channel.get("type") == "P",
                })
        return sorted(found, key=lambda c: (c["team"], c["name"].lower()))

    def users(self, ids: list[str]) -> dict:
        """id -> пользователь. Одним запросом на всех, а не по одному на автора."""
        if not ids:
            return {}
        found = self._request("POST", f"{self.api}/users/ids", sorted(set(ids)),
                              config=self._auth()) or []
        return {user.get("id"): user for user in found}

    def user_by_email(self, email: str) -> dict:
        """
        Пользователь по почте; пусто, если такого нет. Почта — единственное, что
        у Kaiten и Time общее: логины разные (`a_boev1` против `a.boev`).
        """
        try:
            return self._request("GET", f"{self.api}/users/email/{urllib.parse.quote(email)}",
                                 config=self._auth()) or {}
        except NotFoundError:
            return {}


def time_config(cfg: dict) -> dict:
    return ((cfg.get("notify") or {}).get("time") or {})


def time_client(cfg: dict, env: dict, dry_run: bool = False) -> Time | None:
    """
    Клиент или None, если отправка не настроена.

    Нет секции `notify.time` — молчим: фабрика должна вести себя ровно так, как до
    появления бота. Секция есть, а секрета нет — говорим, это уже опечатка в настройке.
    """
    conf = time_config(cfg)
    if not conf:
        return None
    if (conf.get("transport") or "bot") == "webhook":
        url = resolve_secret("TIME_WEBHOOK_URL", env)
        if not url:
            log("Time настроен на вебхук, но TIME_WEBHOOK_URL не найден — не пишу")
            return None
        return Time(conf.get("base_url", ""), webhook_url=url, dry_run=dry_run)
    token = resolve_secret("TIME_BOT_TOKEN", env)
    if not token:
        log("Time настроен на бота, но TIME_BOT_TOKEN не найден — не пишу")
        return None
    if not conf.get("base_url"):
        log("Time: не задан notify.time.base_url — не пишу")
        return None
    return Time(conf["base_url"], token=token, dry_run=dry_run)


def time_channel(conf: dict, repo_key: str | None) -> str:
    """Канал под репозиторий, иначе общий: два проекта в один канал сваливать незачем."""
    channels = conf.get("channels") or {}
    return channels.get(repo_key or "") or channels.get("default") or ""


def time_wants(conf: dict, event: str) -> bool:
    return event in (conf.get("events") or TIME_EVENTS)


def load_time_state() -> dict:
    if TIME_STATE_FILE.is_file():
        try:
            return json.loads(TIME_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"threads": {}}


def save_time_state(state: dict) -> None:
    """
    Запомнить треды. В сухом прогоне — ничего не пишем.

    Сухой прогон не отправляет сообщений, а состояние тредов — это ровно «что уже
    сказано и что уже прочитано»: `asked` и `last_post_id`. Запиши их — и настоящий
    прогон промолчит, считая работу сделанной: вопросы в тред не уедут, а сообщения
    людей будут помечены прочитанными, хотя в карточку их никто не переносил.
    Внутри самого прогона состояние живёт в памяти как обычно.
    """
    if DRY_RUN:
        return
    STATE.mkdir(exist_ok=True)
    tmp = TIME_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(TIME_STATE_FILE)


def time_announce(kaiten: Kaiten, cfg: dict, args, card_id: int) -> int:
    """
    Объявить в канал PR карточки, у которой он уже открыт.

    Обычно сообщение уходит само, из `open_pr`. Но бывает, что PR старше самой отправки:
    бота настроили позже, чем сделали задачу; или отправка сорвалась на сети — ошибки там
    только в лог, потому что открытый PR важнее отчёта в мессенджер. Тогда объявление
    некому послать второй раз, и корня треда не появится — а без него не работают
    и правки из треда. Эта команда его и создаёт.
    """
    conf = time_config(cfg)
    if not conf:
        print("Секции notify.time в конфиге нет — фабрика в Time не пишет.")
        return 1
    known = (load_time_state().get("threads") or {}).get(str(card_id))
    if known and known.get("root_id"):
        print(f"По #{card_id} тред уже есть: сообщение в канале не дублируем.")
        return 0

    card = kaiten.card(card_id)
    # репозиторий — по доске карточки: у продукта он может быть не тот, что по умолчанию
    profile = profile_for_board(board_profiles(cfg), card.get("board_id")) or {}
    repo_key, repo_cfg = resolve_repo(card, cfg, profile.get("repo"))
    repo = Path(repo_cfg["path"]).expanduser()
    branch = remote_branch_for_card(repo, repo_cfg["remote"], branch_prefixes(cfg), card_id)
    if not branch:
        print(f"Ветки по #{card_id} в {repo_cfg['remote']} нет — объявлять нечего.")
        return 1
    pr_url = run_bounded(
        ["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "url",
         "--jq", ".[0].url"], repo, 120).stdout.strip()
    if not pr_url.startswith("http"):
        print(f"PR для ветки {branch} не нашёлся.")
        return 1

    # summary берём из отчёта агента в карточке: verdict давно отработал, а фраза
    # «что сделано» осталась там же, где её читает человек. Нужен ровно первый абзац
    # после строки с веткой: дальше в комментарии идут «Риски» и предупреждения,
    # и без этого в канал уехала бы стена текста
    summary = ""
    for comment in kaiten.comments(card_id):
        text = strip_html(comment.get("text", ""))
        if text.startswith(AGENT_MARK) and "PR:" in text:
            blocks = [block.strip() for block in text.split("\n\n")[1:]
                      if block.strip() and not block.strip().startswith(("PR:", "Ветка:"))]
            summary = blocks[0] if blocks else ''
    notify_pr(cfg, repo_key, card, kaiten.card_url(card), pr_url,
              {"summary": summary}, args.dry_run)
    print(f"Объявил #{card_id}: {pr_url}")
    return 0


def time_channels(cfg: dict, args) -> int:
    """
    Список каналов бота машинным JSON. Этим пользуется окно настроек: человеку остаётся
    выбрать канал из списка, а не искать его id в интерфейсе Time.
    """
    client = time_client(cfg, load_env(), args.dry_run)
    if not client or not client.can_read:
        # причина зависит от транспорта, и в окне настроек эта строка становится
        # единственным пунктом списка — она должна объяснять, что делать
        webhook = (time_config(cfg).get("transport") or "bot") == "webhook"
        why = ("у вебхука канал зашит в URL — выбирать нечего" if webhook
               else "сначала сохрани токен бота")
        print(json.dumps({"error": why}, ensure_ascii=False))
        return 1
    try:
        print(json.dumps(client.my_channels(), ensure_ascii=False, indent=2))
    except FactoryError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 1
    return 0


def time_test(cfg: dict, args) -> int:
    """
    Отправить в канал тестовое сообщение и рассказать, что вышло.

    Проверяет ровно тот путь, которым потом пойдут настоящие сообщения: те же секреты,
    тот же транспорт, тот же curl. Этим же занимается кнопка «Проверить» в окне настроек —
    она просто зовёт эту команду, чтобы проверка и работа не разъезжались.
    """
    conf = time_config(cfg)
    if not conf:
        print("Секции notify.time в конфиге нет — фабрика в Time не пишет.")
        return 1
    client = time_client(cfg, load_env(), args.dry_run)
    if not client:
        print("Секрет не найден. Положи TIME_BOT_TOKEN (или TIME_WEBHOOK_URL) "
              "в .env, окружение или Keychain.")
        return 1

    transport = "вебхук" if client.webhook_url else "бот"
    channel = time_channel(conf, None)
    print(f"Транспорт: {transport}")
    if client.can_read:
        try:
            who = client.me()
            print(f"Бот: {who.get('username') or '?'} (id {who.get('id') or '?'})")
        except FactoryError as e:
            print(f"Токен не подошёл: {e}")
            return 1
        if not channel:
            print("Канал не задан (notify.time.channels.default) — писать некуда.")
            return 1
    try:
        post = client.post(channel, "Проверка связи из фабрики.")
    except FactoryError as e:
        print(f"Не отправилось: {e}")
        return 1
    if post and post.get("id"):
        print(f"Отправлено, id сообщения {post['id']}. Посмотри в канале, "
              f"от чьего имени оно пришло.")
    else:
        print("Отправлено. Ответа с id нет — так и должно быть у вебхука; "
              "посмотри в канале, от чьего имени оно пришло.")
    return 0


def time_pr_message(card_url: str, pr_url: str, summary: str) -> str:
    """
    Две строки, и всё: ссылка на PR с одной фразой о том, что он делает, и ссылка
    на карточку. Заголовок карточки не нужен — фраза агента говорит больше, а номер
    и так виден по ссылке. Приглашения «ответь в треде» тоже нет: учить этому в каждом
    сообщении дорого, а знают об этом и так.
    """
    head = f"[PR]({pr_url})"
    if summary:
        head += f" {summary}"
    return f"{head}\n[Карточка]({card_url})"


# Чем фабрика отвечает на сообщение в треде вместо слов: глазок — «взял и делаю»,
# крест — «не могу, меня блочит». Крест без слов был бы загадкой, поэтому рядом
# с ним всё-таки уходит причина; глазку слова не нужны.
EYES = "eyes"
CROSS = "x"


def author_mention(client: Time, card: dict, info: dict) -> str:
    """
    `@логин` автора карточки в Time — или пусто, если его там не нашли.

    Находку запоминаем в треде, чтобы не спрашивать Time второй раз; сбой
    сети — не находка, его не запоминаем.
    """
    if "author" in info:
        return info["author"]
    email = ((card.get("owner") or {}).get("email") or "").strip()
    if not email or not client.can_read:
        return ""
    try:
        user = client.user_by_email(email)
    except FactoryError as e:
        log(f"  !! не нашёл в Time автора карточки: {e}")
        return ""
    name = user.get("username") or ""
    info["author"] = f"@{name}" if name and not user.get("delete_at") else ""
    return info["author"]


def tag_author(client: Time, card: dict, info: dict, channel: str, root_id: str) -> None:
    """
    Позвать автора карточки в тред с PR.

    Mattermost зовёт в тред только тех, кого в нём упомянули: без тега автор
    узнаёт про PR, только если сам наткнётся на него в канале, а вопросы
    и «поправил» потом висят там, куда он не заглядывает. Отдельным ответом,
    а не в самом сообщении про PR: оно — две строки ссылок, и его потом
    переписывает статус. Не вышло — PR от этого хуже не стал, только в лог.
    """
    mention = author_mention(client, card, info)
    if not mention or not root_id:
        return
    try:
        client.post(channel, f"{mention} это по твоей карточке", root_id=root_id)
        log(f"  позвал в тред автора карточки ({mention})")
    except FactoryError as e:
        log(f"  !! не позвал автора карточки в тред: {e}")


def thread_marks(info: dict) -> dict:
    """Что фабрика наставила в треде: id сообщения -> эмодзи."""
    marks = info.get("reacted")
    if marks is None:
        # состояние, записанное до появления креста: там лежал список одних глазков
        return {post_id: EYES for post_id in (info.get("eyed") or [])}
    return dict(marks)


def mark_thread(client: Time, info: dict, user_id: str,
                post_ids: list[str], emoji: str) -> None:
    """
    Поставить реакцию на сообщения, по которым фабрика ответила делом.

    Что и куда поставили — помним в состоянии: снимать это потом, а искать свои
    реакции по чужому треду дороже, чем записать пару id.
    """
    marks = thread_marks(info)
    for post_id in post_ids:
        try:
            client.react(post_id, user_id, emoji)
        except FactoryError as e:
            log(f"  !! реакция :{emoji}: на {post_id} не встала: {e}")
            continue
        marks[post_id] = emoji
    info["reacted"] = marks
    info.pop("eyed", None)


def drop_marks(client: Time, info: dict) -> None:
    """
    Снять реакции с сообщений, по которым фабрика работала.

    Они значат «сейчас мой ход»; круг кончился — дальше за фабрику говорят её же
    слова, и висеть им незачем. Не снялось — работа от этого не отменяется,
    поэтому только в лог.
    """
    marks = thread_marks(info)
    if not marks:
        return
    try:
        who = client.my_id
    except FactoryError as e:
        log(f"  !! не узнал себя — реакции не снял: {e}")
        return
    for post_id, emoji in marks.items():
        try:
            client.unreact(post_id, who, emoji)
        except FactoryError as e:
            log(f"  !! реакция :{emoji}: с {post_id} не снялась: {e}")
    info["reacted"] = {}
    info.pop("eyed", None)


def notify_pr(cfg: dict, repo_key: str | None, card: dict, card_url: str,
              pr_url: str, verdict: dict, dry_run: bool,
              updated: bool = False, env: dict | None = None) -> None:
    """
    Сообщение про PR в канал Time. Ошибки только в лог: PR уже открыт, и отчёт
    в мессенджер не важнее того, что работа сделана.

    Новый PR — корневое сообщение, и его id запоминается: он же корень треда, в который
    человек напишет правки. Обновление PR (круг «Правки») — ответ в тот же тред, чтобы
    канал не зарастал одной и той же задачей.
    """
    conf = time_config(cfg)
    event = "pr_updated" if updated else "pr_opened"
    if not conf or not time_wants(conf, event) or not pr_url.startswith("http"):
        return
    client = time_client(cfg, env or load_env(), dry_run)
    if not client:
        return
    state = load_time_state()
    known = (state.get("threads") or {}).get(str(card["id"])) or {}
    try:
        if updated:
            if not known.get("root_id"):
                return  # корня нет — отвечать некуда, а новым сообщением шуметь незачем
            summary = brief(verdict.get("summary") or "")
            if echoes_title(summary, card.get("title") or ""):
                # Агенту сказано отчитываться о правке, но если он всё же пересказал
                # постановку — в канал это не тащим: лучше честное «подробности в PR»,
                # чем фраза, из которой человек не поймёт, что с задачей стало.
                log("  фраза агента повторяет заголовок карточки — в тред её не пишу")
                summary = ""
            if known.get("answering"):
                # Круг завели вопросом из треда — там ждут ответа, а не отчёта
                # о правке. «Поправил: смотрел на iPad» было бы ответом не на то.
                text = summary or "Посмотрел, ответ — в карточке."
            else:
                text = (f"Чекай, поправил: {summary}" if summary
                        else "Чекай, поправил по замечаниям, подробности в PR.")
            client.post(known.get("channel_id", ""), text, root_id=known["root_id"])
            # Отчёт ушёл — значит реакциям конец: они висели вместо слов, пока шла
            # работа, а теперь за неё говорит сам отчёт
            drop_marks(client, known)
            # Круг закончился — вопрос отвечен, а следующая пачка вопросов по этому
            # треду будет уже «после правки», и сказать об этом придётся вслух.
            known["answering"] = False
            known["fixed"] = True
            state.setdefault("threads", {})[str(card["id"])] = known
            save_time_state(state)
            log("  написал в тред Time про правку")
            return
        base = time_pr_message(card_url, pr_url, brief(verdict.get("summary") or ""))
        if known.get("own_root") is False and known.get("root_id"):
            # Карточку дали в канале — PR и есть ответ на ту просьбу, туда же он и
            # уходит. Это сообщение несёт и статус: корень треда чужой.
            post = client.post(known.get("channel_id", ""), base, root_id=known["root_id"])
            drop_marks(client, known)
            tag_author(client, card, known, known.get("channel_id", ""), known["root_id"])
            known.update(base=base, status="",
                         status_post=(post or {}).get("id") or known.get("status_post", ""))
            state.setdefault("threads", {})[str(card["id"])] = known
            save_time_state(state)
            log("  ответил в тред Time ссылкой на PR")
            return
        channel = time_channel(conf, repo_key)
        if not channel and not client.webhook_url:
            log("Time: не задан канал (notify.time.channels) — не пишу")
            return
        post = client.post(channel, base)
        if post and post.get("id"):
            info = {
                "channel_id": post.get("channel_id") or channel,
                "root_id": post["id"],
                "last_post_id": post["id"],
                "base": base,
                "status": "",
                "posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            tag_author(client, card, info, info["channel_id"], post["id"])
            state.setdefault("threads", {})[str(card["id"])] = info
            save_time_state(state)
        log("  написал про PR в Time")
    except Exception as e:  # noqa: BLE001 — PR уже открыт, отправка не важнее
        log(f"  !! в Time не написал: {e}")


def time_status(profile: dict | None, card: dict, asks: list,
                blocker: str = "") -> tuple[str, str]:
    """
    Роль колонки и фраза для канала. Роль нужна логике (конец потока, «Вопрос»),
    фраза — человеку. Обе пустые, если колонка не из потока фабрики.

    На доске команды колонок меньше, чем ролей, и «Ревью» — это и вход ревьювера,
    и «дальше человек». По одной колонке их не различить, а разница для канала
    решающая: в первом случае туда идти рано, во втором — ждут именно человека.
    Различает их свой блокер: фабрика вешает его ровно тогда, когда отдаёт ход.
    """
    if not profile:
        return "", ""
    roles = [role for role in TIME_STATUSES
             if card.get("column_id") == role_column(profile, role)]
    if not roles:
        return "", ""
    if len(roles) > 1 and blocker.startswith(BLOCK_MARK):
        handed = next((role for role in roles
                       if role in HANDOFF_BLOCKERS
                       and blocker.startswith(block_reason(HANDOFF_BLOCKERS[role]))), "")
        if handed:
            roles = [handed]
    role = roles[0]
    label = TIME_STATUSES[role]
    if asks and role in ("review", "question"):
        label = TIME_STATUS_ASKED
    return role, label


def with_status(base: str, status: str) -> str:
    """
    Статус — последняя строка сообщения, обычным текстом.

    Курсивом он был раньше и читался как сноска от машины, а это такая же реплика
    человека в канал, как и всё остальное в сообщении.
    """
    return f"{base}\n{status}" if status else base


def thread_asks(asks: list[str], again: bool = False) -> str:
    """
    Вопросы для треда — так, как их написал бы человек.

    В карточке у каждой строки свой значок важности, и там он к месту. В треде это
    короткое сообщение живому человеку, и «❓ ⚠️» в начале строки только сбивает:
    непонятно, вопрос это или предупреждение. Один вопрос — одна строка, несколько —
    список: перечисление из одной строки в мессенджере не читается.

    `again` — вопросы уже второй раз за тот же тред, то есть это круг после правки.
    Без этой оговорки вторая пачка выглядит дико: час назад отчитались «поправил»,
    а теперь снова «есть вопросы», и человек не понимает, откуда они взялись.
    """
    clean = [ask for ask in (ASK_MARK_RE.sub("", a).strip() for a in asks) if ask]
    if not clean:
        return ""
    # «и вот что вылезло» читалось как список замечаний, а не как вопрос: на такое
    # человеку нечего ответить, и он справедливо переспрашивал, чего от него хотят
    after_fix = "Перечитал ещё раз после правки, и появился вопрос" if again else ""
    if again and len(clean) > 1:
        after_fix = "Перечитал ещё раз после правки, и появились вопросы"
    if len(clean) == 1:
        return f"{after_fix or 'Есть вопрос'}: {clean[0]}"
    return (f"{after_fix or 'Есть следующие вопросы'}:\n"
            + "\n".join(f"- {ask}" for ask in clean))


def status_post(info: dict) -> str:
    """
    Какое сообщение несёт строку статуса. В своём треде — корень. В чужом (Стёпу
    позвали в канале) корень чужой, и статус живёт в первом своём ответе — том,
    что с PR. Пока его нет, статусу жить негде: пусто.
    """
    if info.get("own_root") is False:
        return info.get("status_post") or ""
    return info.get("root_id") or ""


def thread_base(client: Time, info: dict) -> str:
    """
    Текст сообщения без строки статуса.

    Обычно он запомнен при отправке. Для тредов, созданных до появления статуса, читаем
    сообщение из Time и отрезаем последнюю строку, если она и есть статус. Курсив тут
    же и проверяется: статус писался им до того, как перестал, и такие сообщения в
    канале ещё висят.
    """
    if info.get("base"):
        return info["base"]
    post = client.post_by_id(status_post(info))
    lines = (post.get("message") or "").splitlines()
    if len(lines) > 1 and is_status_line(lines[-1]):
        lines = lines[:-1]
    return "\n".join(lines)


def from_thread_comment(author: str, text: str, question: bool = False) -> str:
    """
    Комментарий в карточку из треда.

    Метка FROM_TIME_MARK, а не AGENT_MARK: это слова человека, и вся машина — «ответили
    ли на вопрос», «чей ход», стоп-фраза — должна считать их человеческими. Иначе ответ
    в треде выглядел бы для фабрики её собственной репликой и ничего не двигал.

    `question` — в треде спросили, а не поставили задачу. Без этой пометки агент идёт
    искать, что бы поправить, и правит на всякий случай то, о чём его просто спросили.
    """
    head = "Вопрос из треда в Time" if question else "Из треда в Time"
    text = f"{FROM_TIME_MARK} **{head}** ({author}):\n\n{text}"
    if question:
        text += ("\n\nЭто вопрос, а не постановка: ответь на него своими словами "
                 "в отчёте. Код меняй, только если из ответа прямо следует правка.")
    return text


def profile_for_board(profiles: list[dict], board_id: int) -> dict | None:
    return next((p for p in profiles if p["board_id"] == board_id), None)


def sync_time_status(kaiten: Kaiten, cfg: dict, args, profiles: list[dict],
                     client: Time | None = None) -> tuple[dict, dict]:
    """
    Привести строку статуса в канале к тому, что сейчас в карточке.

    Статус берём из колонки, а не из событий фабрики: карточку двигают в десятке мест,
    и хук в каждом однажды забыли бы поставить. А так источник правды один, и статус
    верен даже после прогона, который убили на середине.

    Отдельной функцией — потому что смотреть на колонку надо дважды за прогон. Прогон
    сам же карточки и двигает: ревью уводит их человеку или в «Правки». Пока это
    делалось только в начале чтения тредов, между двумя прогонами в канале час висело
    «пока не смотрите, сыровато» по работе, которую час назад доделали, — и канал
    расходился с витриной, где всё это время было честное «на ревью у человека».

    Возвращает состояние тредов и разбор карточек: чтение тредов идёт сразу следом,
    и спрашивать Kaiten про те же карточки второй раз незачем.
    """
    conf = time_config(cfg)
    if not conf:
        return {}, {}
    client = client or time_client(cfg, load_env(), args.dry_run)
    # На вебхуке ни сообщение поправить, ни в тред написать нечем: статуса там не будет
    if not client or not client.can_read:
        return {}, {}

    state = load_time_state()
    threads = state.get("threads") or {}
    if not threads:
        return state, {}

    known = {}
    for card_key, info in list(threads.items()):
        root_id = info.get("root_id")
        if not root_id:
            continue

        try:
            card = kaiten.card(int(card_key))
        except NotFoundError:
            log(f"  #{card_key} больше нет — убираю тред из состояния")
            threads.pop(card_key, None)
            save_time_state(state)
            continue
        except FactoryError as e:
            # Kaiten не ответил — это не «карточки нет». Тред остаётся на месте:
            # потерять его насовсем значит навсегда заморозить строку статуса в
            # канале и оборвать ответы людей, а пропустить один прогон — ничего.
            log(f"  #{card_key}: Kaiten не ответил, тред оставляю как есть ({e})")
            continue

        profile = profile_for_board(profiles, card.get("board_id"))
        comments = kaiten.comments(card["id"])
        # В канал — только вопросы: замечания ревьювера читает тот, кто открыл PR,
        # а человеку в треде от них нечего ответить
        asks = human_questions(comments)
        # Блокер тут не про «трогать нельзя», а про «чей ход»: на доске команды
        # только по нему и видно, что колонка «Ревью» — это уже ревью человека
        stuck = blocked_by(kaiten, card["id"])
        role, status = time_status(profile, card, asks, stuck or "")

        # Вопросы агента — в тред: человеку сказали «позырьте», и он должен увидеть,
        # о чём речь, не открывая карточку. Второй раз одно и то же не пишем.
        if asks and time_wants(conf, "asked") and role in ("review", "question"):
            asked = "\n".join(asks)
            # Вторая пачка вопросов по тому же треду — это круг после правки, и без
            # оговорки она выглядит дико: час назад тут отчитались «поправил».
            text = thread_asks(asks, again=bool(info.get("fixed") or info.get("asked")))
            if text and info.get("asked") != asked:
                try:
                    client.post(info.get("channel_id", ""), text, root_id=root_id)
                    # Круг кончился вопросами — ход опять человека, и глазок на его
                    # сообщении врал бы, что фабрика всё ещё что-то делает
                    drop_marks(client, info)
                    info["asked"] = asked
                    save_time_state(state)
                    log(f"  #{card_key}: вопросы в тред ({len(asks)})")
                except FactoryError as e:
                    log(f"  !! не смог написать вопросы в тред по #{card_key}: {e}")

        if (time_wants(conf, "status") and role and status != info.get("status")
                and status_post(info)):
            try:
                info["base"] = thread_base(client, info)
                client.edit(status_post(info), with_status(info["base"], status))
                info["status"] = status
                save_time_state(state)
                log(f"  #{card_key}: статус в канале -> {status or 'снят'}")
            except FactoryError as e:
                log(f"  !! не смог поправить статус по #{card_key}: {e}")

        # «Готово» — конец: строку статуса сняли, дальше по этой карточке ничего
        # не случится, и держать её в состоянии значит спрашивать Kaiten про неё
        # каждый прогон до конца времён
        if role == "done":
            threads.pop(card_key, None)
            save_time_state(state)
            continue

        known[card_key] = {"card": card, "profile": profile,
                           "comments": comments, "asks": asks, "stuck": stuck}
    return state, known


def refresh_time_status(kaiten: Kaiten, cfg: dict, args, profiles: list[dict]) -> None:
    """То же самое в конце прогона: карточки переехали — пусть и в канале переедут."""
    try:
        sync_time_status(kaiten, cfg, args, profiles)
    except Exception as e:  # noqa: BLE001 — мессенджер не важнее сделанной работы
        log(f"статус в Time не обновился: {e}")


def follow_time_threads(kaiten: Kaiten, cfg: dict, args, profiles: list[dict]) -> int:
    """
    Фаза чтения тредов: что человек написал в треде — то и станет правкой.

    Тред здесь — второй вход в ту же машину, а не своя. Сообщение переносится
    комментарием в карточку, карточка уезжает в «Правки», и дальше всё идёт обычным
    порядком: исполнитель, ревьювер, тот же PR в той же ветке. Прямой путь
    «сообщение → агент» пришлось бы снабдить своим счётчиком кругов, стоп-фразой,
    бюджетом и ночным тегом — и через месяц он разъехался бы с оригиналом.

    Опрос, а не входящий вебхук: фабрика — пакетный процесс по расписанию, принимать
    входящий HTTP ей некуда. На сервере можно будет реагировать сразу, но опрос
    останется — он же и восстановление после простоя.
    """
    conf = time_config(cfg)
    if not conf:
        return 0
    env = load_env()
    client = time_client(cfg, env, args.dry_run)
    if not client:
        return 0
    if not client.can_read:
        log("Time на вебхуке: тред читать нечем, правки из треда не заработают")
        return 0

    state, known = sync_time_status(kaiten, cfg, args, profiles, client=client)
    if not known:
        return 0
    if not conf.get("follow_threads", True):
        return 0
    threads = state.get("threads") or {}

    try:
        me = client.me()
    except FactoryError as e:
        log(f"Time: не смог узнать себя ({e}) — треды не читаю")
        return 0
    bot_id = me.get("id") or ""
    # По этому обращению фабрика понимает, что говорят с ней. Берём из токена, а не
    # из конфига: имя бота знает он сам, и лишней настройке тут делать нечего
    handle = f"@{me.get('username')}".lower() if me.get("username") else ""

    allowed = [name.lstrip("@").lower() for name in (conf.get("allow_from") or [])]
    note_phase(args, "читаю треды в Time")
    moved = 0

    for card_key, seen in known.items():
        info = threads.get(card_key)
        if not info:
            continue
        root_id = info["root_id"]
        card, profile = seen["card"], seen["profile"]
        comments, asks = seen["comments"], seen["asks"]
        # блокер уже спрошен снимком статуса — второй раз за тот же прогон не ходим
        stuck = seen.get("stuck")

        try:
            posts = client.thread(root_id)
        except FactoryError as e:
            log(f"  !! тред по #{card_key} не прочитался: {e}")
            continue

        seen = info.get("last_post_id")
        fresh, passed = [], not seen
        for post in posts:
            if passed:
                fresh.append(post)
            elif post.get("id") == seen:
                passed = True
        if not passed:
            # запомненного сообщения в треде уже нет (удалили) — считаем прочитанным всё,
            # иначе на следующем прогоне перенесём весь тред заново. Записать это надо
            # сразу: без записи поправка теряется и повторяется каждый прогон
            fresh = []
            if posts:
                info["last_post_id"] = posts[-1].get("id")
                save_time_state(state)
        if not fresh:
            continue

        # системные сообщения («такой-то присоединился») у Mattermost со своим type
        human = [post for post in fresh
                 if post.get("user_id") != bot_id
                 and not (post.get("type") or "")
                 and (post.get("message") or "").strip()]
        info["last_post_id"] = fresh[-1].get("id")

        if not human:
            save_time_state(state)
            continue

        names = client.users([post.get("user_id") for post in human])
        if allowed:
            human = [post for post in human
                     if (names.get(post.get("user_id"), {}).get("username") or "").lower()
                     in allowed]
            if not human:
                log(f"  #{card_key}: в треде писали не те, кому можно (allow_from)")
                save_time_state(state)
                continue

        # В треде люди говорят и между собой: тегают друг друга, обсуждают релиз,
        # просят апрув. Фабрика вмешивается только когда обратились к ней — иначе она
        # уносила бы в карточку чужой разговор и заводила по нему правки.
        addressed = [(post, strip_mention(post.get("message") or "", handle))
                     for post in human
                     if handle and handle in (post.get("message") or "").lower()]
        if not addressed:
            if human:
                log(f"  #{card_key}: в треде говорят не со мной — не трогаю")
            save_time_state(state)
            continue

        # «Как дела» — это вопрос, а не задача: отвечаем из карточки и ничего
        # не двигаем. Иначе такой вопрос уезжал бы правкой, и следующий прогон тратил
        # бы на него настоящего агента.
        kinds = [(post, text, thread_intent(text)) for post, text in addressed]
        if any(kind == "status" for _, _, kind in kinds):
            try:
                client.post(info.get("channel_id", ""),
                            time_status_answer(profile, card, comments, asks,
                                               stuck or ""),
                            root_id=root_id)
                log(f"  #{card_key}: ответил в тред, что с задачей")
            except FactoryError as e:
                log(f"  !! не смог ответить в тред по #{card_key}: {e}")

        # Вопрос по самой задаче круг всё-таки заводит: ответить на «а на каком экране
        # смотрел» можно только заглянув в код. Но в карточке он помечается вопросом,
        # иначе агент пойдёт искать, что бы поправить, и поправит на всякий случай.
        work = [triple for triple in kinds if triple[2] != "status" and triple[1]]
        if not work:
            save_time_state(state)
            continue
        only_asked = all(kind == "question" for _, _, kind in work)

        moved += 1
        target = role_column(profile, "fixes") if profile else None
        # карточку в «Вопросе» двигать не надо: она и так ждёт ответа человека, и
        # перенесённый комментарий делает её пригодной к работе следующим же прогоном
        stay = ([role_column(profile, role) for role in ACTIVE_ROLES]
                + [role_column(profile, "question")]) if profile else []
        # блокер (кроме своего блокера передачи) и «увели из потока» — единственные
        # два случая, когда сообщение из треда в работу не уходит
        astray = outside_flow(profile, card)

        # По итогам круга отчитаться надо ответом, а не словом «поправил». Ставим
        # до развилки: круг случится и у заблокированной карточки — когда человек
        # снимет блокер, — и у той, что уже стоит в рабочей колонке.
        info["answering"] = only_asked

        # Чем отвечаем на сообщение. Глазок — «взял и делаю»: словами тут говорить
        # нечего, человек и так написал, чтобы это сделали, а тред, в котором дальше
        # пойдёт разговор по делу, от «записал в карточку» зарастает вдвое быстрее.
        # Крест — «не могу»: молчание с ним человек прочитал бы как «делает», поэтому
        # рядом уходит и причина.
        # Свой блокер передачи — «сейчас ход человека», и человек его сделал: сообщение
        # в треде снимает такой блокер. Иначе ему отвечали «меня блочит: нужен человек».
        release = (handed_over(kaiten.blockers(card["id"]))
                   if stuck and stuck.startswith(BLOCK_MARK) and not astray else [])
        if release:
            mark, reply = EYES, ""
        elif stuck:
            log(f"  #{card_key} заблокирована ({stuck}) — карточку не двигаю")
            mark = CROSS
            reply = ("Меня блочит блокер на карточке — сними, и поеду."
                     if stuck == NO_REASON else f"Меня блочит: {stuck}")
        elif astray:
            # карточку увели в «Тестинг» или «Ролаут» — тащить её оттуда назад
            # в работу нельзя, даже если в треде попросили правку
            log(f"  #{card_key} стоит вне колонок фабрики — карточку не двигаю")
            mark = CROSS
            reply = ("Не поеду: карточка уже не в моих колонках — верни её в работу, "
                     "если правку надо сделать.")
        else:
            mark, reply = EYES, ""

        # Ставим до Kaiten: и комментарий, и перенос ещё могут не получиться, а знать,
        # чем кончилось его сообщение, человеку надо в любом случае. Снимется реакция
        # тогда же, когда придёт настоящий ответ, — кругом позже.
        if time_wants(conf, "fix_taken"):
            mark_thread(client, info, bot_id,
                        [post.get("id") or "" for post, _, _ in work], mark)

        # Комментарий пишем и в отказ: сообщение не должно потеряться из-за того,
        # что кто-то повесил блокер, — человек снимет его, и правка поедет сама
        for post, text, kind in work:
            user = names.get(post.get("user_id"), {})
            author = (user.get("nickname") or user.get("username")
                      or user.get("first_name") or "кто-то в Time")
            kaiten.comment(card["id"], from_thread_comment(author, text,
                                                           question=kind == "question"))
        log(f"  #{card_key}: перенёс из треда {len(work)} "
            f"{'сообщение' if len(work) == 1 else 'сообщений'}"
            + (" (это вопрос)" if only_asked else ""))

        # Снимаем после комментария: без него следующий прогон взял бы карточку
        # и не узнал, о чём просили
        for blocker in release:
            kaiten.unblock(card["id"], blocker["id"])
        if release:
            log(f"  #{card_key}: в треде ответили — снял свой блокер ({stuck})")
        # На доске команды «Ревью» — та же колонка, что вход ревьювера, и «стоять на
        # месте» после снятия блокера значило бы отдать правку человека ревьюверу.
        # Поэтому снятый блокер везёт карточку в «Правки» из любой своей колонки.
        if (mark == EYES and target and card.get("column_id") != target
                and (release or card.get("column_id") not in stay)):
            kaiten.move(card["id"], target)

        if reply and time_wants(conf, "fix_taken"):
            try:
                client.post(info.get("channel_id", ""), reply, root_id=root_id)
            except FactoryError as e:
                log(f"  !! не смог ответить в тред: {e}")
        save_time_state(state)

    if moved:
        log(f"из тредов Time пришло правок по {moved} карточкам")
    return 0


# Ссылка на карточку в сообщении: и полная («…/boards/card/<id>»), и короткая
# («…kaiten.ru/<id>»), какую Kaiten даёт кнопкой «скопировать ссылку».
CARD_LINK_RE = re.compile(r"kaiten\.ru/(?:[\w/-]*?/)?card/(\d+)|kaiten\.ru/(\d+)\b")


def card_from_text(text: str) -> int | None:
    found = CARD_LINK_RE.search(text or "")
    return int(found.group(1) or found.group(2)) if found else None


def without_card_links(text: str) -> str:
    """Текст просьбы без ссылок на Kaiten: карточка и так та самая, ссылка в ней — шум."""
    text = re.sub(r"\[([^\]]*)\]\(\S*kaiten\.ru/[^)]*\)", r"\1", text or "")
    return re.sub(r"<?\S*kaiten\.ru/\S*", "", text).strip()


def take_from_channel(kaiten: Kaiten, cfg: dict, profiles: list[dict],
                      card_id: int) -> tuple[str, dict | None, dict | None, bool]:
    """
    Берёт ли Стёпа карточку, которую ему дали в канале. Возвращает отказ одной фразой
    (пусто — берёт), саму карточку, профиль её доски и надо ли ставить её в «Очередь».

    Беру только задачи с досок продуктов: то, что лежит в очереди или левее неё.
    Правее — в работе у человека, на ревью, в раскатке — карточка уже чья-то, и
    утаскивать её назад значит отменить чужое решение. Отказ — фразой от первого
    лица, коротко: человек прочтёт её в канале между делом.
    """
    try:
        card = kaiten.card(card_id)
    except NotFoundError:
        return "Не вижу такую карточку.", None, None, False
    profile = profile_for_board(profiles, card.get("board_id"))
    if not profile:
        return "Это не моя доска — беру задачи с досок продуктов.", card, None, False
    if hands_off(card, kaiten.comments(card_id), cfg):
        return "В карточке просили её не трогать.", card, profile, False
    stuck = blocked_by(kaiten, card_id)
    if stuck and not stuck.startswith(BLOCK_MARK):
        return (f"Меня блочит: {stuck}" if stuck != NO_REASON
                else "На карточке блокер — сними, и возьму."), card, profile, False
    column = card.get("column_id")
    queue = role_column(profile, "queue")
    order = [c["id"] for c in flat_columns(kaiten.board(profile["board_id"]))]
    waiting = column in order and queue in order and order.index(column) <= order.index(queue)
    if mine(profile, card):
        if column == role_column(profile, "done") or outside_flow(profile, card) and not waiting:
            return "Она уже дальше разработки — верни в беклог, если надо переделать.", \
                card, profile, False
        # уже моя и в потоке — сообщение станет просьбой по ней, двигать некуда
        return "", card, profile, waiting and column != queue
    if waiting:
        return "", card, profile, column != queue
    if column in [role_column(profile, role) for role in ("in_progress", "agent_review")]:
        return "Её уже кто-то делает — сними с себя и тегни меня снова.", card, profile, False
    return ("Она уже дальше разработки — верни в беклог, если надо переделать.",
            card, profile, False)


def follow_time_mentions(kaiten: Kaiten, cfg: dict, args, profiles: list[dict]) -> int:
    """
    Фаза каналов: Стёпу тегнули со ссылкой на карточку — он её берёт.

    Вход тот же, что у тредов: сообщение не запускает агента напрямую, а ставит
    карточку в поток — тег «моё», «Очередь», текст просьбы комментарием, — и дальше
    её ведёт обычный прогон с кругами, ревью и бюджетом. Ответ — в тред того
    сообщения: 👀 «взял», потом ссылка на PR; ❌ и одна фраза — почему не взял.

    Каналы — те, куда фабрика пишет про PR (`notify.time.channels`): в чужие она
    не пишет и читать их незачем. Курсор — время последнего прочитанного сообщения
    по каналу; первый проход только ставит курсор, иначе Стёпа ответил бы на всю
    историю канала разом. Сообщения из уже известных тредов здесь мимо — их читает
    `follow_time_threads`.
    """
    conf = time_config(cfg)
    if not conf or not conf.get("follow_mentions", True):
        return 0
    client = time_client(cfg, load_env(), args.dry_run)
    if not client or not client.can_read:
        return 0
    channels = sorted({c for c in (conf.get("channels") or {}).values() if c})
    if not channels:
        return 0
    try:
        me = client.me()
    except FactoryError as e:
        log(f"Time: не смог узнать себя ({e}) — каналы не читаю")
        return 0
    bot_id = me.get("id") or ""
    handle = f"@{me.get('username')}".lower() if me.get("username") else ""
    if not handle:
        return 0
    allowed = [name.lstrip("@").lower() for name in (conf.get("allow_from") or [])]

    state = load_time_state()
    cursors = state.setdefault("channels", {})
    threads = state.setdefault("threads", {})
    taken = 0
    now_ms = int(time.time() * 1000)
    for channel in channels:
        since = (cursors.get(channel) or {}).get("since")
        if not since:
            cursors[channel] = {"since": now_ms}
            save_time_state(state)
            log(f"Time: начинаю слушать канал {channel} с этого момента")
            continue
        try:
            posts = client.channel_posts(channel, since)
        except FactoryError as e:
            log(f"  !! канал {channel} не прочитался: {e}")
            continue
        fresh = [post for post in posts if (post.get("create_at") or 0) > since]
        if not fresh:
            continue
        known_roots = {info.get("root_id") for info in threads.values()}
        asks = [post for post in fresh
                if post.get("user_id") != bot_id and not (post.get("type") or "")
                and handle in (post.get("message") or "").lower()
                and (post.get("root_id") or post.get("id")) not in known_roots]
        names = client.users([post.get("user_id") for post in asks]) if asks else {}
        for post in asks:
            user = names.get(post.get("user_id"), {})
            if allowed and (user.get("username") or "").lower() not in allowed:
                log(f"  в канале звал не тот, кому можно (allow_from)")
                continue
            root = post.get("root_id") or post.get("id")
            card_id = card_from_text(post.get("message") or "")
            if not card_id:
                mark_thread(client, {}, bot_id, [post["id"]], CROSS)
                client.post(channel, "Кинь ссылку на карточку — возьму.", root_id=root)
                continue
            if str(card_id) in threads:
                # по карточке уже есть тред — разговор о ней живёт там
                mark_thread(client, {}, bot_id, [post["id"]], EYES)
                client.post(channel, "Она уже у меня — пишу в её тред.", root_id=root)
                continue
            refusal, card, profile, to_queue = take_from_channel(kaiten, cfg, profiles,
                                                                 card_id)
            if refusal:
                log(f"  #{card_id} из канала не беру: {refusal}")
                mark_thread(client, {}, bot_id, [post["id"]], CROSS)
                client.post(channel, refusal, root_id=root)
                continue

            info = {"channel_id": channel, "root_id": root, "own_root": False,
                    "last_post_id": post["id"], "base": "", "status": "",
                    "posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            mark_thread(client, info, bot_id, [post["id"]], EYES)
            author = (user.get("nickname") or user.get("username")
                      or user.get("first_name") or "кто-то в Time")
            text = strip_mention(without_card_links(post.get("message") or ""), handle)
            kaiten.comment(card_id, from_thread_comment(author, text or "возьми в работу"))
            if not mine(profile, card):
                kaiten.add_tag(card_id, profile.get("tag") or BOT_TAG)
            if to_queue:
                kaiten.move(card_id, role_column(profile, "queue"))
            threads[str(card_id)] = info
            save_time_state(state)
            taken += 1
            log(f"  #{card_id} взял из канала: тег, «Очередь»")
        cursors[channel] = {"since": max(post.get("create_at") or 0 for post in fresh)}
        save_time_state(state)
    if taken:
        log(f"из каналов Time взял карточек: {taken}")
    return taken


# --------------------------------------------------------------------------- #
# ночные задачи
# --------------------------------------------------------------------------- #

# Карточку с этим тегом фабрика берёт только ночью. Смысл — тяжёлые и шумные задачи:
# долгие прогоны тестов, массовые правки, всё, что мешает работать днём.
NIGHT_FROM = 22
NIGHT_TO = 5


def night_config(cfg: dict) -> tuple[str, int, int]:
    """
    Ночной тег и окно. Тег по умолчанию — `<тег бота>:night`: переименовали бота,
    и ночной тег переехал за ним, а не остался напоминанием о прошлой жизни.
    """
    night = cfg.get("night") or {}
    return (night.get("tag") or (bot_tag(cfg) + NIGHT_SUFFIX),
            int(night.get("from_hour", NIGHT_FROM)),
            int(night.get("to_hour", NIGHT_TO)))


def is_night(cfg: dict, now: datetime | None = None) -> bool:
    """
    Ночь ли сейчас по местному времени машины.

    Окно переходит через полночь, поэтому обычное `from <= hour < to` не годится:
    при 22–5 условие распадается на «после 22» ИЛИ «до 5».
    """
    _, start, end = night_config(cfg)
    hour = (now or datetime.now()).hour
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def off_hours(card: dict, cfg: dict, now: datetime | None = None) -> str | None:
    """
    Причина, по которой ночную карточку сейчас брать нельзя. None — можно.

    Это мягкий стоп, в отличие от стоп-фразы: она значит «никогда», а тег — «не сейчас».
    Карточка просто ждёт своего часа и ничем не помечается: вешать на неё блокер было бы
    вредно, человеку пришлось бы снимать его каждое утро.
    """
    tag, start, end = night_config(cfg)
    if not has_tag(card, tag):
        return None
    if is_night(cfg, now):
        return None
    return f"ночная задача, беру её с {start}:00 до {end}:00"


def skip_off_hours(cards: list, cfg: dict) -> list:
    """Убирает ночные карточки, до которых ещё не дошло время. Возвращает (оставшиеся, сколько отложено)."""
    kept, waiting = [], 0
    for card in cards:
        if off_hours(card, cfg):
            waiting += 1
            continue
        kept.append(card)
    if waiting:
        _, start, end = night_config(cfg)
        log(f"{waiting} ночных карточек ждут окна {start}:00–{end}:00")
    NIGHT_WAITING["count"] += waiting
    return kept


# Ночные карточки, отложенные за прогон — для человечка в меню-баре
NIGHT_WAITING = {"count": 0}



# --------------------------------------------------------------------------- #
# блокеры: как фабрика говорит «дальше ход человека»
# --------------------------------------------------------------------------- #

# Свои блокеры узнаём по префиксу: на карточке может висеть и человеческий блокер,
# и его снимать нельзя. Свои фабрика тоже не снимает — их снимает человек, и именно
# это снятие означает «продолжай». В Kaiten другого признака апрува у нас нет: и агент,
# и человек пишут под одним токеном, по автору их не различить. Единственное
# исключение — просьба в треде Time, см. `handed_over`: там автор виден.
BLOCK_MARK = "🤖"

BLOCK_QUESTION = "не хватило данных, ответь в комментариях"
BLOCK_FAILED = "прогон сломался, нужен человек"
BLOCK_HUMAN_REVIEW = "ревью агента пройдено, нужен человек"
BLOCK_ACCEPTANCE = "жду апрува приёмочных критериев"
BLOCK_ROLLOUT = "нельзя раскатывать до релиза бека"


def block_reason(kind: str, detail: str = "") -> str:
    text = f"{BLOCK_MARK} {kind}"
    return f"{text}: {detail}" if detail else text


def ours(blocker: dict) -> bool:
    return str(blocker.get("reason") or "").startswith(BLOCK_MARK)


# Блокер без текста: причину человек не написал, а сказать что-то надо
NO_REASON = "заблокирована"


def blocked_by(kaiten: Kaiten, card_id: int) -> str | None:
    """Причина, по которой карточку сейчас трогать нельзя. None — путь свободен."""
    for blocker in kaiten.blockers(card_id):
        return str(blocker.get("reason") or NO_REASON)
    return None


# Сколько эпик ждёт ответа, прежде чем фабрика пойдёт дальше сама. 0 — ждать вечно,
# как было раньше. Смысл не в том, чтобы обойтись без человека, а в том, чтобы задача
# не стояла сутки из-за вопроса вроде «какой текст у подписи»: агент примет решение,
# явно перечислит, что додумал, и человек поправит это на ревью — если захочет.
DEFAULT_ANSWER_WAIT_HOURS = 4


def parse_stamp(value) -> datetime | None:
    """Дата из Kaiten ('2026-09-01T05:01:02.600Z') в aware datetime. None — не разобрали."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def stale_holds(kaiten: Kaiten, flow: dict, card_id: int) -> tuple[list, list]:
    """
    Наши блокеры на карточке и те из них, что уже просрочены.

    Просроченными считаем, только если просрочены **все**: пока висит хоть один
    свежий вопрос, ответ ещё может прийти, и начинать додумывать рано.
    """
    held = [b for b in kaiten.blockers(card_id) if ours(b)]
    hours = flow.get("answer_wait_hours", DEFAULT_ANSWER_WAIT_HOURS)
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        hours = DEFAULT_ANSWER_WAIT_HOURS
    if not held or hours <= 0:
        return held, []
    deadline = datetime.now(timezone.utc) - timedelta(hours=hours)
    stale = [b for b in held
             if (parse_stamp(b.get("created")) or datetime.now(timezone.utc)) < deadline]
    return held, (stale if len(stale) == len(held) else [])


def hold(kaiten: Kaiten, card_id: int, kind: str, detail: str = "") -> None:
    """
    Вешает блокер, если такого ещё нет.

    Идемпотентность здесь обязательна: прогон повторяется каждый час, и без проверки
    на карточке за сутки выросла бы стопка одинаковых блокеров.
    """
    reason = block_reason(kind, detail)
    if any(str(b.get("reason") or "") == reason for b in kaiten.blockers(card_id)):
        log(f"  блокер уже висит: {kind}")
        return
    log(f"  вешаю блокер: {kind}")
    kaiten.block(card_id, reason)


# --------------------------------------------------------------------------- #
# профиль доски
# --------------------------------------------------------------------------- #

# Роли колонок, без которых поток некуда вести.
REQUIRED_ROLES = ("queue", "in_progress", "agent_review")

# Колонки, из которых фабрика забирает карточки сама. Если роль «дальше человек»
# указывает в одну из них, одного движения мало: без блокера следующий прогон
# подберёт карточку снова и цикл пойдёт по кругу.
ACTIVE_ROLES = ("queue", "in_progress", "agent_review")

# Роли, означающие «дальше человек». На доске со своей колонкой под каждую роль всё
# видно и так, и блокер не нужен. На чужой доске колонок меньше, роли делят их между
# собой — там блокер и есть единственный способ сказать, чей сейчас ход.
HANDOFF_BLOCKERS = {
    "question": BLOCK_QUESTION,
    "failed": BLOCK_FAILED,
    "review": BLOCK_HUMAN_REVIEW,
}


def handed_over(blockers: list) -> list:
    """
    Блокеры, которые снимает просьба человека в треде Time. Пусто — снимать нечего.

    Блокер передачи значит «ход человека», и сообщение `@бот поправь` в треде — это
    и есть его ход: автор там виден, а не спрятан за общим токеном, как в Kaiten.
    Без этого фабрика отвечала «Меня блочит: ревью агента пройдено, нужен человек»
    тому самому человеку, которого ждала.

    Только если **все** блокеры на карточке — наши блокеры передачи. Чужой блокер
    («ждём макет») не наш, апрув приёмочных критериев комментарием не даётся
    по договорённости, а «нельзя раскатывать до бека» правкой не лечится.
    """
    kinds = tuple(block_reason(kind) for kind in HANDOFF_BLOCKERS.values())
    if blockers and all(str(b.get("reason") or "").startswith(kinds) for b in blockers):
        return blockers
    return []


def make_profile(key: str, board: dict, repo_key: str | None = None,
                 attach_to_debt: bool = True, own_only: bool = False,
                 tag: str = BOT_TAG) -> dict:
    columns = {role: value for role, value in (board.get("columns") or {}).items() if value}
    missing = [role for role in REQUIRED_ROLES if not columns.get(role)]
    if missing:
        raise FactoryError(f"профиль «{key}»: нет обязательных колонок {', '.join(missing)}")
    return {
        "key": key,
        "board_id": board["board_id"],
        "columns": columns,
        "lane_id": board.get("lane_id"),
        "card_type_id": board.get("card_type_id"),
        "repo": repo_key,
        "attach_to_debt": attach_to_debt,
        # На своей доске фабрика хозяйка и берёт всё, что лежит в «Очереди». На доске
        # команды так нельзя: там свой бэклог, и трогать можно только помеченное —
        # тег и есть эта пометка.
        "own_only": own_only,
        "tag": tag,
    }


# --------------------------------------------------------------------------- #
# продукты: доска + репозитории
# --------------------------------------------------------------------------- #

def products(cfg: dict) -> dict:
    """
    Продукты команды: у каждого своя спринт-доска и свои репозитории.

    Нет секции — режим старый, одна рабочая доска и одна доска сабтасок. Секция
    есть — доски берутся отсюда, а `kaiten.board_id` и `epic_flow.subtasks` больше
    не читаются: команда, которая ведёт магазин, сайт и B2B-заказы, раскладывает эпик
    на три доски, и доска сабтасок «одна на всё» перестаёт что-либо значить.
    """
    return cfg.get("products") or {}


def destinations(cfg: dict) -> dict:
    """
    Продукты, куда можно ставить новую работу.

    `retired` — доска, с которой команда уехала, но на ней ещё досиживают свои
    карточки: их PR надо закрыть по мержу, а треды в Time — довести. Новой работы
    туда не ставим, и агенту её в списке не показываем.
    """
    return {key: product for key, product in products(cfg).items()
            if not product.get("retired")}


def default_product(cfg: dict) -> str:
    choices = destinations(cfg)
    key = cfg.get("default_product")
    return key if key in choices else next(iter(choices), "")


def product_repos(cfg: dict, key: str) -> list[str]:
    """Репозитории продукта по порядку; первый — тот, куда идёт работа по умолчанию."""
    known = cfg.get("repos") or {}
    return [repo for repo in (products(cfg).get(key) or {}).get("repos") or []
            if repo in known]


def product_for_lane(cfg: dict, lane_id: int | None) -> str:
    """
    Главный продукт эпика — по дорожке на доске эпиков.

    Доска эпиков общая на всю компанию, и продукт команды там выражен дорожкой.
    Эпик в чужой дорожке тоже может прийти с тегом —
    тогда главным считаем продукт по умолчанию, а не отказываемся работать.
    """
    for key, product in destinations(cfg).items():
        if lane_id and lane_id in (product.get("epic_lanes") or []):
            return key
    return default_product(cfg)


def product_profile(cfg: dict, key: str) -> dict:
    product = products(cfg)[key]
    repos = product_repos(cfg, key)
    profile = make_profile(product.get("title") or key, product,
                           repos[0] if repos else None,
                           attach_to_debt=False, own_only=True, tag=bot_tag(cfg))
    return {**profile, "product": key, "repos": repos,
            "retired": bool(product.get("retired"))}


def profile_for_product(cfg: dict, key: str | None) -> dict:
    """Профиль продукта; неизвестный продукт — продукт по умолчанию, а не падение."""
    if key not in destinations(cfg):
        key = default_product(cfg)
    return product_profile(cfg, key)


def pick_product_repo(cfg: dict, product: str, repo: str | None) -> tuple[str, str]:
    """
    Продукт и репозиторий, которые агент назвал, — после проверки, что они существуют.

    Агент выбирает из списка, но список ему показан текстом: бывает, что он называет
    репозиторий соседнего продукта или выдумывает ключ. Чужой репозиторий берём
    вместе с его продуктом — задача должна лечь на ту доску, где живёт её код.
    """
    choices = destinations(cfg)
    if product not in choices:
        owner = next((key for key in choices if repo in product_repos(cfg, key)), "")
        product = owner or default_product(cfg)
    repos = product_repos(cfg, product)
    if repo not in repos:
        owner = next((key for key in choices if repo in product_repos(cfg, key)), "")
        if owner:
            product, repos = owner, product_repos(cfg, owner)
        else:
            repo = repos[0] if repos else ""
    return product, repo


def repo_line(cfg: dict, product: str, repo: str) -> str:
    """
    Строка `repo: <ключ>` для описания карточки, если репозиторий не главный у продукта.

    Исполнитель видит только карточку, а доска говорит лишь о продукте: у B2B-заказов
    три репозитория, и без этой строки бек ушёл бы в микрофронты.
    """
    repos = product_repos(cfg, product)
    return f"repo: {repo}" if repo and repos and repo != repos[0] else ""


def scout_dirs(cfg: dict, keep: str | None = None) -> list[tuple[str, Path]]:
    """
    Копии всех репозиторий продуктов на чтение — для разведки и фаз эпика.

    Копия та же, что у разведчика: постоянная, подтягивается к базовой ветке.
    `keep` — репозиторий, в котором агент и так стоит, второй раз его не открываем.
    Репозиторий, которого нет на диске, пропускаем с записью в лог: без него агент
    обеднеет, но работать сможет, а падать из-за этого всей фазе незачем.
    """
    seen, found = set(), []
    for key in destinations(cfg):
        for repo_key in product_repos(cfg, key):
            if repo_key in seen or repo_key == keep:
                continue
            seen.add(repo_key)
            repo_cfg = cfg["repos"][repo_key]
            repo = Path(repo_cfg["path"]).expanduser()
            if not (repo / ".git").exists():
                log(f"  репозитория {repo_key} нет на диске ({repo}) — агент его не увидит")
                continue
            try:
                found.append((repo_key, ensure_scout_worktree(repo, repo_cfg, repo_key)))
            except FactoryError as e:
                log(f"  копия {repo_key} не подтянулась ({e}) — агент её не увидит")
    return found


def repos_note(cfg: dict, here: str, dirs: list[tuple[str, Path]]) -> str:
    """
    Карта продуктов и репозиториев для промпта. Без продуктов — пусто.

    Агенту нужно знать не только пути, но и что где живёт: по названию
    `site` не догадаться, фронт это или бек, а `b2b-frontend` может оказаться
    внутренней админкой, тогда как клиентская часть живёт внутри сайта.
    """
    choices = destinations(cfg)
    if not choices:
        return ""
    paths = dict(dirs)
    lines = ["## Продукты и репозитории", "",
             "Команда ведёт несколько продуктов, у каждого своя доска и свои репозитории. "
             f"Ты стоишь в рабочей копии `{here}`. Остальные открыты тебе на чтение "
             "по абсолютным путям — ищи и читай в них так же, как в своей, но ничего "
             "в них не меняй.", ""]
    for key, product in choices.items():
        lines.append(f"- **{product.get('title') or key}** (`{key}`)")
        for repo_key in product_repos(cfg, key):
            about = (cfg["repos"][repo_key].get("about") or "").strip()
            where = ("ты здесь" if repo_key == here
                     else f"`{paths[repo_key]}`" if repo_key in paths
                     else "копии нет — не смотри")
            lines.append(f"  - `{repo_key}` — {about + ' — ' if about else ''}{where}")
    return "\n".join(lines)


def board_profiles(cfg: dict) -> list[dict]:
    """
    Доски, по которым ходят исполнитель и ревьювер.

    Рабочая доска есть всегда. Доска сабтасок появляется, когда включён режим эпиков:
    у неё и колонки другие, и репозиторий может быть другой.

    А может оказаться, что это одна и та же доска, — и это нормальный конец истории.
    Своя «доска для клода» нужна, пока Стёпа только заводится: там он никому не мешает.
    Когда он работает как разработчик в команде, у него и доска должна быть командная,
    иначе рядом с доской команды живёт вторая вселенная, и по доске уже не видно,
    кто чем занят.

    Тогда профиль остаётся один и по правилам чужой доски: берём только карточки
    с тегом. Двух профилей на одну доску быть не должно — доску обошли бы дважды,
    и в первый же обход, по правилам своей, утащили бы в работу командный бэклог.
    """
    if products(cfg):
        # По доске на продукт, все командные: берём только карточки с тегом
        return [product_profile(cfg, key) for key in products(cfg)]
    tag = bot_tag(cfg)
    subtasks = ((cfg.get("epic_flow") or {}).get("subtasks") or {})
    # сабтаска уже висит дочерней на эпике: второй родитель в виде долга спринта
    # только раздует описание долга, поэтому туда её не привязываем
    if subtasks.get("board_id") == cfg["kaiten"]["board_id"]:
        return [make_profile("работа", subtasks, subtasks.get("repo"),
                             attach_to_debt=False, own_only=True, tag=tag)]
    profiles = [make_profile("работа", cfg["kaiten"], tag=tag)]
    if subtasks.get("board_id"):
        profiles.append(make_profile("сабтаски", subtasks, subtasks.get("repo"),
                                     attach_to_debt=False, own_only=True, tag=tag))
    return profiles


def role_column(profile: dict, role: str) -> int | None:
    return profile["columns"].get(role)


def outside_flow(profile: dict | None, card: dict) -> bool:
    """
    Карточка стоит в колонке, у которой нет ни одной роли фабрики?

    Значит её увёл человек — в «Тестинг», в «Ролаут», в свою колонку, — и дальше
    он ведёт её сам. Вытащить её оттуда назад в работу фабрика не имеет права
    никогда: это откат чужого решения, а не её круг.

    Внутри своих колонок она ходит как угодно, в том числе влево: «Правки» →
    «В работе» и «Вопрос» → «В работе» — обычные переходы её потока, и запрещать
    движение назад вообще нельзя, иначе круг правок встанет.

    Эпикам это правило выражено по-другому, окном колонок: у них своя доска,
    ролей на ней нет, и «своё» там — `ready_column_id` и `development_column_id`.
    """
    columns = {column for column in (profile or {}).get("columns", {}).values() if column}
    return bool(columns) and card.get("column_id") not in columns


def needs_blocker(profile: dict, role: str) -> bool:
    """
    Нужно ли к движению добавить блокер.

    Нужно, если колонки под роль нет вовсе или она совпадает с колонкой, откуда
    фабрика сама берёт карточки: иначе «отдал человеку» ничем не отличается от
    «готово к работе», и карточка вернётся в цикл следующим же прогоном.
    """
    target = role_column(profile, role)
    if not target:
        return True
    return any(target == role_column(profile, active) for active in ACTIVE_ROLES)


def hand_over(kaiten: Kaiten, profile: dict, card_id: int, role: str,
              detail: str = "") -> None:
    """Отдаёт карточку человеку: двигает, если есть куда, и вешает блокер, если надо."""
    target = role_column(profile, role)
    if target:
        kaiten.move(card_id, target)
    kind = HANDOFF_BLOCKERS.get(role)
    if kind and needs_blocker(profile, role):
        hold(kaiten, card_id, kind, detail)



# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #

def run_bounded(cmd: list[str], cwd: Path | str, timeout: int,
                env: dict | None = None) -> subprocess.CompletedProcess:
    """
    Внешняя команда с таймаутом, которую действительно можно оборвать.

    Обычный `subprocess.run(timeout=…)` тут не спасает: он убивает только прямого
    ребёнка, а внуки живут дальше и держат открытым pipe, который питон читает.
    Ловили вживую — убил `git push`, его `ssh` осиротел и продолжал держать stderr,
    и питон навсегда завис в select() без единого процесса-ребёнка.

    Поэтому команда запускается в своей группе процессов (start_new_session), а по
    таймауту гасится вся группа целиком.
    """
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, text=True, env=env, start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(proc)
        # добираем то, что успело выйти: после гибели группы pipe закрыт и чтение
        # уже не может заблокироваться
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err) from None
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def kill_group(proc: subprocess.Popen) -> None:
    """Гасит всю группу процессов, а не только прямого ребёнка."""
    try:
        group = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, signal_number)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


# Таймаут на git. Без него один зависший ssh морозит прогон навсегда: agent.timeout_sec
# покрывает только вызов claude. Ловили вживую — `git push` провисел 27 минут на ssh,
# фабрика всё это время держала замок и показывала в трее работу, которой не было.
GIT_TIMEOUT = 120
GIT_NETWORK_TIMEOUT = 300
GIT_NETWORK_OPS = ("push", "fetch", "clone", "pull", "ls-remote")

# Сетевую операцию повторяем: обрыв соединения — штатное событие в корпоративной сети,
# и из-за него карточка не должна уезжать в «Упало». Все перечисленные операции
# идемпотентны, повтор ничего не портит: push идёт с --force-with-lease, и если ветку
# успел изменить кто-то другой, лease не даст перезаписать.
GIT_NETWORK_RETRIES = 3
GIT_RETRY_PAUSE = 8

# По этим признакам ошибка считается сетевой и достойной повтора. Всё остальное —
# отказ по существу (нет прав, конфликт, битая ветка), и повторять его бессмысленно.
NETWORK_HINTS = (
    "не ответил", "Connection closed", "Connection reset", "Connection refused",
    "Could not read from remote", "Operation timed out", "timed out",
    "Broken pipe", "reset by peer", "kex_exchange", "Temporary failure",
    "Network is unreachable", "early EOF", "RPC failed", "TLS packet",
)


def looks_like_network(message: str) -> bool:
    return any(hint.lower() in message.lower() for hint in NETWORK_HINTS)


# Признаки того, что claude не смог авторизоваться. Токен подписки живёт считанные дни
# и протухает молча: сам claude отвечает честным 401, но делает это на каждом запросе
# внутри агента. Агент перебирает попытки, упирается в свой таймаут — и наружу это
# выглядит как «агент не уложился», хотя ни агент, ни карточка ни при чём. Ровно так
# фабрика неделю складывала карточки в «Упало» и вычёркивала их из разведки.
# Это claude говорит от своего имени, и только в stderr: в stdout у него поток
# событий, и туда те же слова может занести сам агент.
AUTH_HINTS = (
    "OAuth access token has expired", "Failed to authenticate",
    "Please run /login", "Invalid API key",
)

AUTH_BROKEN = ("claude не авторизован — авторизация протухла. Почини её "
               "(`claude auth login`) и запусти прогон заново")


def auth_failed_event(line: str) -> bool:
    """
    Строка потока, в которой claude сознаётся, что его не пустили.

    Смотрим на поле `error` самого события, а не на текст: про 401 агент способен
    написать и сам — читая чужой лог или правя код авторизации, — а цена ошибки тут
    высокая, мы по этому признаку убиваем работающего агента. Вывод инструментов
    лежит вложенно, в `message.content`, и наверх не всплывает.
    """
    if "authentication" not in line:
        return False
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(event, dict) and event.get("error") == "authentication_failed"


def looks_like_auth(out: str | None, err: str | None) -> bool:
    """Похоже ли, что агент не начался, потому что claude не пустили."""
    if any(auth_failed_event(line) for line in (out or "").splitlines()):
        return True
    text = (err or "").lower()
    return any(hint.lower() in text for hint in AUTH_HINTS)


def agent_session_expires() -> datetime | None:
    """
    Когда протухает авторизация claude. None — узнать не удалось.

    Читаем только срок: сам токен нам не нужен и в лог попасть не должен. На маке
    claude держит авторизацию в связке ключей, иначе — файлом в ~/.claude.

    Ничего не блокирует и ни на что не влияет: claude обычно продлевает токен сам,
    и «протух по часам» ещё не значит «не работает». Это для витрины — чтобы в
    меню-баре была кнопка входа, а не полдня догадок, почему фабрика молчит.
    Приговор выносит AgentAuthError на живом запросе, а не эта функция.
    """
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return None  # с долгоживущим токеном сессия из связки ключей не при делах
    raw = ""
    if sys.platform == "darwin":
        try:
            found = subprocess.run(
                ["security", "find-generic-password",
                 "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=10)
            raw = found.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            raw = ""
    if not raw:
        stored = Path.home() / ".claude" / ".credentials.json"
        raw = stored.read_text(encoding="utf-8") if stored.is_file() else ""
    try:
        stamp = (json.loads(raw or "{}").get("claudeAiOauth") or {}).get("expiresAt")
        return datetime.fromtimestamp(stamp / 1000, timezone.utc) if stamp else None
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def note_auth(ok: bool) -> None:
    """
    Записать в статус, пускает ли claude агента. Меню-бар по этому флагу рисует
    кнопку входа: сам он про авторизацию ничего не знает и знать не должен.
    """
    expires = agent_session_expires()
    write_status(auth={
        "ok": ok,
        "expires": expires.isoformat(timespec="seconds") if expires else None,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


def auth_broken() -> AgentAuthError:
    """Одной строкой: и в статус записали, и готовое исключение вернули."""
    note_auth(False)
    return AgentAuthError(AUTH_BROKEN)


def forget_auth_note() -> None:
    """
    Снять отметку «не авторизован», если причина явно ушла.

    Нужно ради одного случая: человек нажал в меню-баре «войти», вошёл — а работы на
    доске нет, агент не запускается, и красная строка висела бы до первой карточки.
    Свежая сессия (или заведённый долгоживущий токен) — достаточный повод её снять;
    если авторизация всё-таки не работает, ближайший же агент вернёт отметку назад.
    """
    if not STATUS_FILE.is_file():
        return
    try:
        current = json.loads(STATUS_FILE.read_text(encoding="utf-8")).get("auth") or {}
    except (json.JSONDecodeError, OSError):
        return
    if current.get("ok", True):
        return
    expires = agent_session_expires()
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or (
            expires and expires > datetime.now(timezone.utc)):
        note_auth(True)

# ssh не должен ни ждать пароля, ни висеть на мёртвом соединении. BatchMode запрещает
# любые запросы к терминалу, ServerAlive рвёт зависшую сессию за полминуты.
GIT_SSH_COMMAND = ("ssh -o BatchMode=yes -o ConnectTimeout=15 "
                   "-o ServerAliveInterval=10 -o ServerAliveCountMax=3")


def git(cwd: Path, *args: str, check: bool = True) -> str:
    network = bool(args) and args[0] in GIT_NETWORK_OPS
    timeout = GIT_NETWORK_TIMEOUT if network else GIT_TIMEOUT
    attempts = GIT_NETWORK_RETRIES if network else 1
    env = {**os.environ, "GIT_SSH_COMMAND": GIT_SSH_COMMAND,
           "GIT_TERMINAL_PROMPT": "0"}

    for attempt in range(1, attempts + 1):
        failure = ""
        try:
            proc = run_bounded(["git", *args], cwd, timeout, env)
            if proc.returncode == 0:
                return proc.stdout.strip()
            failure = f"{proc.returncode}: {proc.stderr.strip()[:800]}"
        except subprocess.TimeoutExpired:
            failure = f"не ответил за {timeout}с — оборвал"

        last = attempt >= attempts
        if last or not looks_like_network(failure):
            if check:
                raise FactoryError(f"git {' '.join(args)} -> {failure}")
            return ""
        pause = GIT_RETRY_PAUSE * attempt
        log(f"  сеть подвела на git {args[0]} ({failure.splitlines()[0][:90]}), "
            f"повтор {attempt + 1} из {attempts} через {pause}с")
        time.sleep(pause)
    return ""


def make_worktree(repo: Path, branch: str, base: str, remote: str, path: Path,
                  continue_from: str | None = None) -> None:
    """
    Отдельная рабочая копия на карточку: параллельные запуски не дерутся.

    `continue_from` — имя удалённой ветки, с которой продолжаем (круг правок после ревью).
    Без него ветка отводится заново от базовой, и прошлые коммиты теряются, — для правок
    это означало бы переписывание уже открытого PR с нуля.
    """
    if path.exists():
        git(repo, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
    git(repo, "worktree", "prune", check=False)
    git(repo, "fetch", remote, base)
    start = f"{remote}/{base}"
    if continue_from:
        git(repo, "fetch", remote, continue_from)
        start = f"{remote}/{continue_from}"
    git(repo, "worktree", "add", "-B", branch, str(path), start)


def epic_of_card(card: dict) -> int | None:
    """Номер эпика, из которого выросла сабтаска. None — карточка сама по себе."""
    match = EPIC_ORIGIN_RE.search(strip_html(card.get("description")) or "")
    return int(match.group(1)) if match else None


def branch_prefix(cfg: dict) -> str:
    """Префикс, под которым заводятся новые ветки карточек."""
    return (cfg.get("pr") or {}).get("branch_prefix") or BRANCH_PREFIX


def branch_prefixes(cfg: dict) -> tuple[str, ...]:
    """Все префиксы, под которыми может найтись ветка карточки: текущий первым."""
    prefix = branch_prefix(cfg)
    return (prefix, *(old for old in LEGACY_BRANCH_PREFIXES if old != prefix))


def remote_branch_for_card(repo: Path, remote: str, prefixes: tuple[str, ...],
                           card_id: int) -> str | None:
    """Ищет уже запушенную ветку карточки: имя могло смениться вместе с заголовком."""
    git(repo, "fetch", remote, "--prune", check=False)
    patterns = [name for prefix in prefixes
                for name in (f"{prefix}{card_id}", f"{prefix}{card_id}-*")]
    output = git(repo, "ls-remote", "--heads", remote, *patterns, check=False)
    names = [line.split("refs/heads/", 1)[1].strip()
             for line in output.splitlines() if "refs/heads/" in line]
    if not names:
        return None
    # если веток почему-то несколько — берём самую длинную: она с заголовком, а не голый номер
    return max(names, key=len)


def drop_worktree(repo: Path, path: Path) -> None:
    git(repo, "worktree", "remove", "--force", str(path), check=False)
    shutil.rmtree(path, ignore_errors=True)
    git(repo, "worktree", "prune", check=False)


# --------------------------------------------------------------------------- #
# промпт
# --------------------------------------------------------------------------- #

def strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def normalize_phrase(text: str) -> str:
    """«Клод, НЕ ТРОГАЙ!!» -> «клод не трогай»: сравниваем без регистра и знаков."""
    return re.sub(r"[^\w]+", " ", (text or "").lower()).strip()


def hands_off(card: dict, comments: list, cfg: dict) -> str | None:
    """
    Просили не трогать? Тогда возвращаем найденную фразу, и агент к карточке не подходит.

    Смотрим заголовок, описание и человеческие комментарии. Свои не смотрим: иначе агент,
    процитировавший запрет, заблокировал бы карточку сам себе.
    """
    human = [strip_html(c.get("text", "")) for c in comments or []]
    haystack = normalize_phrase(" ".join([
        card.get("title") or "",
        strip_html(card.get("description")),
        *(text for text in human if not text.startswith(AGENT_MARKS)),
    ]))
    for phrase in cfg.get("stop_phrases") or STOP_PHRASES:
        if normalize_phrase(phrase) in haystack:
            return phrase
    return None


RETURN_NOTE = """
> ⚠️ **Ты уже брал эту карточку и вернул её с вопросами.** Человек ответил — его ответ
> в комментариях выше, последним. Прочитай его внимательно и продолжи с учётом ответа.
> Если ответ по-прежнему не снимает неопределённость — верни `unclear` ещё раз с более
> точными вопросами. Повторный вопрос лучше, чем догадка.
"""

FIX_NOTE = """
> 🔍 **Это круг правок после ревью.** Твои прошлые коммиты уже в ветке и в открытом PR —
> ветка отведена от них, а не от `main`. Не переделывай задачу заново.
>
> Замечания ревьювера — коротко в комментарии 🔍 выше, подробно (с файлом и причиной) ниже.
> Закрой их все: `🛑` и `⚠️` обязательно, `💬` — по возможности. Правь ровно то, на что
> указано, попутных изменений не вноси.
>
> Если с замечанием ты не согласен — не игнорируй его молча: сделай остальное, а по этому
> напиши в `risks`, почему считаешь текущий код верным. Это уйдёт человеку.
>
> В `summary` — **что ты исправил в этом круге**, а не о чём была задача: строка уйдёт
> в рабочий чат как «Поправил: …», и заголовок карточки на этом месте человеку ничего
> не говорит — он его и писал.
>
> Коммить обычным порядком, одним коммитом поверх прошлых.
"""

HUMAN_FIX_NOTE = """
> 💬 **Это круг правок по просьбе человека.** Твои прошлые коммиты уже в ветке и в открытом
> PR — ветка отведена от них, а не от `main`. Не переделывай задачу заново.
>
> Что сделать — в последнем комментарии выше, помеченном 💬: человек написал это в рабочем
> чате. Если он ссылается на замечания в PR — они ниже, в «Замечаниях людей в PR».
> Замечания ревьювера с 🔍 выше разобраны прошлыми кругами, к ним не возвращайся.
> Правь ровно то, о чём просят, попутных изменений не вноси.
>
> Если с замечанием ты не согласен — не игнорируй его молча: сделай остальное, а по этому
> напиши в `risks`, почему считаешь текущий код верным. Это уйдёт человеку.
>
> В `summary` — **что ты исправил в этом круге**, а не о чём была задача: строка уйдёт
> в рабочий чат как «Поправил: …».
>
> Коммить обычным порядком, одним коммитом поверх прошлых.
"""


PR_REPLIES_NOTE = """
На каждое замечание из этого списка ответь в `pr_replies`: `ref` — номер из квадратных
скобок, `text` — одна короткая фраза от первого лица, как ответил бы автор PR: что
поправил («Переименовал в `initialShouldSkipCustomerPhone`») или почему оставил как есть.
Ответ уйдёт в PR рядом с замечанием — без «агента», «круга» и пересказа замечания.
"""


def fix_note(comments: list, feedback: list[dict], details: str = "") -> str:
    """Чей это круг правок — ревьювера или человека из треда — и что люди сказали в PR."""
    last = strip_html(comments[-1].get("text", "")) if comments else ""
    human = last.startswith(FROM_TIME_MARK)
    note = HUMAN_FIX_NOTE if human else FIX_NOTE
    if details and not human:
        note += "\n**Замечания ревьювера подробно:**\n\n" + details + "\n"
    if feedback:
        note += ("\n**Замечания людей в PR** (новее последнего коммита ветки):\n\n"
                 + "\n".join(item["text"] for item in feedback) + "\n"
                 + PR_REPLIES_NOTE)
    return note


def format_comments(comments: list) -> str:
    lines = []
    for c in comments:
        author = (c.get("author") or {}).get("full_name", "?")
        body = strip_html(c.get("text", ""))
        if body:
            lines.append(f"- **{author}:** {body}")
    return "\n".join(lines) or "(нет)"


def format_checklists(card: dict) -> str:
    lines = []
    for cl in card.get("checklists") or []:
        lines.append(f"- {cl.get('name', 'Чек-лист')}:")
        for item in cl.get("items") or []:
            mark = "x" if item.get("checked") else " "
            lines.append(f"  - [{mark}] {item.get('text', '')}")
    return "\n".join(lines) or "(нет)"


def read_project_file(path: Path, fallback: str) -> str:
    """
    Правила проекта: какие файлы читать и на что смотреть на ревью. Файла может не быть
    или он может быть пустым — это рабочий вариант, агент тогда идёт по общим соображениям.
    Молчать в этом случае нельзя: пустая строка на месте списка читается как «правил нет»,
    а не как «правила не заполнили».
    """
    if not path.is_file():
        return fallback
    # комментарии для человека в начале файла агенту не нужны
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    return text.strip() or fallback


def project_file(repo_key: str | None, name: str) -> Path:
    """
    Файл правил под репозиторий: `project/<ключ>/<имя>`, а нет его — общий `project/<имя>`.

    Пока репозиторий был один, правила лежали прямо в `project/`. Когда их стало шесть,
    общий файл перестал подходить: правила фронта в промпте агента, который пишет
    бек на C#, — это не правила, а шум, и ревьювер начал бы требовать `*.spec.ts`
    от проекта на xUnit. Общий файл остаётся для тех, у кого репозиторий один.
    """
    own = PROJECT_DIR / repo_key / name if repo_key else None
    return own if own and own.is_file() else PROJECT_DIR / name


def project_replacements(repo_key: str | None = None) -> dict:
    return {
        "{{PROJECT_DOCS}}": read_project_file(
            project_file(repo_key, PROJECT_DOCS_PATH.name),
            "- `CLAUDE.md` в корне и ближайший `AGENTS.md` к пакету, который трогаешь.\n"
            "  (Список правил проекта не заполнен — смотри, что есть в репозитории.)"),
        "{{PROJECT_CHECKLIST}}": read_project_file(
            project_file(repo_key, PROJECT_CHECKLIST_PATH.name),
            "Отдельного списка правил у проекта нет. Опирайся на то, что написано в файлах\n"
            "правил выше, и на общие требования: тесты на изменённую логику, отсутствие\n"
            "заглушенных линтеров и отладочных логов, объём правки по задаче."),
    }


def indent_block(text: str, spaces: int) -> str:
    """Плейсхолдер может стоять внутри нумерованного списка — тогда блок надо сдвинуть."""
    if not spaces:
        return text
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line
                     for line in text.splitlines()).lstrip()


def apply_template(template: str, replacements: dict) -> str:
    """Подставляет {{ПЛЕЙСХОЛДЕРЫ}}, сохраняя отступ, с которым стоит плейсхолдер."""
    for key, value in replacements.items():
        # многострочные значения выравниваем по отступу самого плейсхолдера
        for match in list(re.finditer(rf"^([ \t]*)" + re.escape(key), template, re.MULTILINE)):
            template = template.replace(match.group(0),
                                        match.group(1) + indent_block(value, len(match.group(1))))
        template = template.replace(key, value)
    return template


def build_prompt(card: dict, comments: list, repo_cfg: dict, branch: str, card_url: str,
                 returning: bool = False, fixing: bool = False,
                 feedback: list[dict] | None = None) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "{{CARD_ID}}": str(card["id"]),
        "{{CARD_URL}}": card_url,
        "{{TITLE}}": card.get("title") or "(без заголовка)",
        "{{DESCRIPTION}}": strip_html(card.get("description")) or "(описания нет)",
        "{{COMMENTS}}": format_comments(comments),
        "{{CHECKLISTS}}": format_checklists(card),
        "{{BRANCH}}": branch,
        "{{BASE_BRANCH}}": repo_cfg["base_branch"],
        "{{REMOTE}}": repo_cfg["remote"],
        "{{RETURN_NOTE}}": (fix_note(comments, feedback or [], review_details(card["id"]))
                            if fixing else RETURN_NOTE if returning else "")
                           + scout_hints(card),
        **project_replacements(repo_cfg.get("key")),
    }
    return apply_template(template, replacements)


# --------------------------------------------------------------------------- #
# агент
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Пульс агента
# --------------------------------------------------------------------------- #

# Как назвать по-русски то, что агент делает прямо сейчас: глагол и поле ввода,
# из которого берётся уточнение. Инструмент, которого тут нет, покажется своим именем —
# это честнее, чем молчать.
AGENT_VERBS = {
    "Read": ("читает", "file_path"),
    "Edit": ("правит", "file_path"),
    "MultiEdit": ("правит", "file_path"),
    "Write": ("пишет", "file_path"),
    "NotebookEdit": ("правит", "notebook_path"),
    "Grep": ("ищет", "pattern"),
    "Glob": ("ищет", "pattern"),
    "Bash": ("запускает", "command"),
    "Task": ("поднимает субагента", None),
    "Skill": ("берёт скилл", "skill"),
    "TodoWrite": ("раскладывает план", None),
    "WebFetch": ("читает страницу", "url"),
    "WebSearch": ("ищет в интернете", "query"),
    # служебный инструмент схемы: им агент оформляет вердикт в самом конце
    "StructuredOutput": ("оформляет ответ", None),
}


def short_value(value, field: str) -> str:
    """Путь ужимаем до хвоста, всё остальное просто подрезаем: в меню-бар влезает мало."""
    text = str(value).strip()
    if "path" in field:
        parts = [p for p in text.split("/") if p]
        text = "/".join(parts[-2:]) if len(parts) > 1 else text
    return text[:44]


class AgentPulse:
    """
    Живой след работы агента: что он делает прямо сейчас.

    Без него `claude -p` — чёрный ящик: наружу не выходит ничего, пока агент не
    закончит, а это по нашим логам шесть минут в среднем и двадцать шесть в худшем
    случае. Всё это время в меню-баре висела одна и та же строка, и отличить работу
    от зависшей сети было нечем.
    """

    # Статус пишется на диск, поэтому не на каждое событие: в разгоне их несколько
    # в секунду, а человек всё равно читает глазами.
    THROTTLE_SEC = 2.0

    def __init__(self, publish: bool = True):
        self.result: dict | None = None
        self.steps = 0
        self.action: str | None = None
        self.publish = publish
        self._last_write = 0.0
        self._written_action: str | None = None

    def feed(self, line: str) -> None:
        """Строка NDJSON из claude. Ошибки глотаем: витрина не важнее сделанной работы."""
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        # Итог приходит последним событием — в нём и вердикт, и стоимость.
        if event.get("type") == "result":
            self.result = event
            return
        action = self.describe(event)
        if action:
            self.steps += 1
            self.action = action
        # Бьёмся на любом событии, не только на понятном: важно само «он жив».
        self.beat()

    def describe(self, event: dict) -> str | None:
        """Из события — строчка на русском. None, если показывать нечего."""
        if event.get("type") != "assistant":
            return None
        blocks = (event.get("message") or {}).get("content")
        if not isinstance(blocks, list):
            return None
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name") or "")
            verb, field = AGENT_VERBS.get(name, (None, None))
            if verb is None:
                # в том числе MCP-инструменты: mcp__figma__get_screenshot -> get_screenshot
                return name.split("__")[-1] or None
            value = (block.get("input") or {}).get(field) if field else None
            return f"{verb} {short_value(value, field)}".strip() if value else verb
        return None

    def beat(self, force: bool = False) -> None:
        now = time.time()
        # Смену действия показываем почти сразу — ради неё всё и затевалось.
        # Повторные события просто подтверждают «агент жив», им хватает и пары секунд.
        wait = 0.5 if self.action != self._written_action else self.THROTTLE_SEC
        if not force and now - self._last_write < wait:
            return
        self._last_write = now
        self._written_action = self.action
        if not self.publish:
            return
        write_status(agent={
            "action": self.action,
            "steps": self.steps,
            "beat": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })


def clear_pulse() -> None:
    """Агент отработал — убираем его след, иначе он переживёт прогон."""
    try:
        write_status(agent=None)
    except Exception:  # noqa: BLE001 — на выходе падать уже незачем
        pass


def run_streaming(cmd: list[str], cwd: Path | str, timeout: int,
                  env: dict | None, on_line, stop_when=None) -> subprocess.CompletedProcess:
    """
    То же, что run_bounded, но stdout отдаётся построчно по мере поступления.

    Читают отдельные потоки: главный ждёт сам процесс и по таймауту гасит всю группу.
    Читатели демонические и join-ятся с таймаутом намеренно — внук, переживший
    родителя, держит pipe открытым, и без этого мы бы снова навсегда зависли в чтении
    (ровно тот случай, ради которого появился run_bounded).

    `stop_when` — предикат на строку потока: True значит «ждать больше нечего, гаси».
    Нужен потому, что не всякая безнадёга кончается ошибкой процесса: на протухшей
    авторизации claude не падает, а молча ходит по кругу ретраев до самого таймаута.
    """
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, text=True, env=env, start_new_session=True,
        bufsize=1,
    )
    out: list[str] = []
    err: list[str] = []

    def pump_out() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            out.append(line)
            try:
                on_line(line)
            except Exception:  # noqa: BLE001 — витрина не должна ронять прогон
                pass
            if stop_when and stop_when(line):
                # Гасим прямо отсюда и без wait: ждать процесс из потока-читателя,
                # пока его ждёт главный, — лишний способ поймать себя за хвост.
                # Главному хватит того, что группа умерла: его wait вернётся сам.
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                return

    def pump_err() -> None:
        err.append(proc.stderr.read())  # type: ignore[union-attr]

    readers = [threading.Thread(target=pump_out, daemon=True),
               threading.Thread(target=pump_err, daemon=True)]
    for reader in readers:
        reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(proc)
        for reader in readers:
            reader.join(timeout=15)
        raise subprocess.TimeoutExpired(cmd, timeout, output="".join(out),
                                        stderr="".join(err)) from None
    for reader in readers:
        reader.join(timeout=15)
    return subprocess.CompletedProcess(cmd, proc.returncode, "".join(out), "".join(err))


def run_agent(prompt: str, cwd: Path, agent_cfg: dict,
              schema: dict | None = None, verdict_key: str = "status",
              add_dirs: list[Path] | None = None) -> tuple[dict, dict]:
    """
    Возвращает (verdict, meta). Кидает FactoryError, если claude не отработал.

    `add_dirs` — соседние репозитории, открытые агенту на чтение. Разведке и фазам
    эпика мало одного: задача про B2B-заказ живёт и в его микрофронтах, и в сайте,
    и в беке, и решить, куда её ставить, можно только посмотрев во все.
    """
    schema = schema or VERDICT_SCHEMA
    cmd = [
        "claude", "-p", prompt,
        # Потоком, а не одним куском в конце: только так видно, что агент жив.
        # Разбирает поток AgentPulse, он же пишет «что делает сейчас» в статус.
        # --verbose обязателен: без него claude не отдаёт stream-json в режиме -p.
        "--output-format", "stream-json", "--verbose",
        "--json-schema", json.dumps(schema),
        "--permission-mode", agent_cfg["permission_mode"],
        "--allowedTools", ",".join(agent_cfg["allowed_tools"]),
    ]
    if agent_cfg.get("model"):
        cmd += ["--model", agent_cfg["model"]]
    if agent_cfg.get("effort"):
        # подписка claude.ai принимает только low/medium/high, "max" она отвергает
        cmd += ["--effort", agent_cfg["effort"]]
    if agent_cfg.get("max_budget_usd"):
        cmd += ["--max-budget-usd", str(agent_cfg["max_budget_usd"])]
    for extra in add_dirs or []:
        cmd += ["--add-dir", str(extra)]

    env = os.environ.copy()
    # claude отказывается стартовать внутри другой сессии Claude Code
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    pulse = AgentPulse()
    try:
        # через run_streaming: claude поднимает свои подпроцессы, и по таймауту надо
        # гасить всю группу — иначе внук переживёт родителя и повесит чтение pipe
        proc = run_streaming(cmd, cwd, agent_cfg["timeout_sec"], env, pulse.feed,
                             stop_when=auth_failed_event)
    except subprocess.TimeoutExpired as e:
        if looks_like_auth(e.output, e.stderr):
            raise auth_broken() from e
        raise FactoryError(f"агент не уложился в {agent_cfg['timeout_sec']}с") from e
    finally:
        clear_pulse()

    if proc.returncode != 0:
        if looks_like_auth(proc.stdout, proc.stderr):
            raise auth_broken()
        raise FactoryError(f"claude exit {proc.returncode}: {(proc.stderr or proc.stdout)[:1500]}")

    payload = pulse.result
    if payload is None:
        # Поток кончился без итогового события. Так бывает, если claude оборвали
        # снаружи: делать вид, что вердикта нет по существу, нельзя — это сбой.
        if looks_like_auth(proc.stdout, proc.stderr):
            raise auth_broken()
        raise FactoryError("claude не отдал итог потоком: "
                           + (proc.stderr or proc.stdout or "")[-800:])

    meta = {
        "cost_usd": payload.get("total_cost_usd"),
        "duration_ms": payload.get("duration_ms"),
        "num_turns": payload.get("num_turns"),
        "steps": pulse.steps,
        "is_error": payload.get("is_error"),
        "subtype": payload.get("subtype"),
        "session_id": payload.get("session_id"),
        "raw": payload,
    }

    # Агент дошёл до конца — значит с авторизацией всё в порядке, и кнопку входа
    # в меню-баре пора убрать. Иначе она осталась бы висеть до перезапуска.
    note_auth(True)

    verdict = extract_verdict(payload, verdict_key)
    if verdict is None:
        # структурированный ответ не доехал — не выдумываем вердикт, решим по коммитам
        verdict = {verdict_key: None, "summary": str(payload.get("result") or "")[:4000]}
    # Режем в одной точке, а не в двадцати местах, где summary идёт в комментарий.
    if isinstance(verdict.get("summary"), str):
        verdict["summary_full"] = verdict["summary"]
        verdict["summary"] = brief(verdict["summary"])
    return verdict, meta


# Сколько «что сделано» влезает в комментарий к карточке. Промпты просят одно
# предложение, но просьба — не гарантия: агент возвращал и абзац на полторы тысячи
# знаков, и карточка превращалась в стену текста, которую никто не читает. Поэтому
# режем ещё и здесь. Полный текст остаётся в summary_full и уходит в ревью на PR,
# где подробности как раз уместны.
BRIEF_LIMIT = 220


def brief(text: str, limit: int = BRIEF_LIMIT) -> str:
    """Первое предложение, не длиннее лимита. Пустое остаётся пустым."""
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    # Обрываем по концу предложения, но только там, где дальше начинается новое:
    # точка в «~13 мин.» или в «src/foo.ts» предложение не заканчивает.
    for end in range(len(text)):
        if text[end] in ".!?" and text[end - 1:end].isalnum():
            tail = text[end + 1:end + 3]
            if not tail or (tail[:1] == " " and tail[1:2].isupper()):
                if end + 1 <= limit:
                    return text[:end + 1]
                break
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def echoes_title(summary: str, title: str) -> bool:
    """
    Фраза агента — пересказ постановки, а не отчёт: «Поправил: Брать флаги из библиотеки
    автоматически и вернуть круг только основному бренду». В карточке такое ещё читается —
    заголовок рядом, — а в канале человек видит задачу, которую сам же и ставил, и гадает,
    что именно поправили и при чём тут круг.

    Считаем по значимым словам: короткие слова выкидываем, иначе предлоги роднят любые
    две фразы. Совпадение частичное — заголовок обычно пересказывают своими словами.
    """
    def significant(text: str) -> set:
        return {w for w in re.findall(r"\w+", str(text or "").lower()) if len(w) > 2}

    said, asked = significant(summary), significant(title)
    if not said or not asked:
        return False
    return len(said & asked) / len(said | asked) >= 0.6


def full_summary(verdict: dict) -> str:
    """Неурезанный отчёт агента — для ревью на PR, где подробности к месту."""
    return str(verdict.get("summary_full") or verdict.get("summary") or "")


def extract_verdict(payload: dict, key: str = "status"):
    """Структурированный ответ лежит либо отдельным полем, либо JSON-строкой в result."""
    for field in ("structured_output", "structured_result", "structuredOutput"):
        value = payload.get(field)
        if isinstance(value, dict) and key in value:
            return value
    result = payload.get("result")
    if isinstance(result, dict) and key in result:
        return result
    if isinstance(result, str):
        match = re.search(r"\{.*\}", result.strip(), re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict) and key in parsed:
                    return parsed
            except json.JSONDecodeError:
                pass
    return None


# --------------------------------------------------------------------------- #
# отчёты в карточку
# --------------------------------------------------------------------------- #

# Сколько прогон уже потратил. Бюджеты в конфиге — на одного агента, а прогон
# запускает их десятками: без общего потолка один жадный эпик съедает всё, что
# отведено на день. Считаем по факту, из meta самого агента.
SPENT = {"usd": 0.0}

# Всё, что ждёт человека, за прогон — одним списком со ссылками. Счётчика мало:
# «ждёт твоего ответа: 2» не говорит, каких карточек это касается, и человек всё
# равно идёт искать их на доске руками.
AWAITING: dict = {"count": 0, "cards": []}


def note_waiting(kaiten: Kaiten, card: dict, reason: str,
                 kind: str = "card", asks: list[str] | None = None) -> None:
    """Запоминает карточку, по которой ход человека, вместе с причиной и ссылкой."""
    card_id = card["id"]
    if any(item["id"] == card_id for item in AWAITING["cards"]):
        return
    AWAITING["cards"].append({
        "id": card_id,
        "title": (card.get("title") or "").strip(),
        "kind": kind,
        "reason": reason.replace(BLOCK_MARK, "").strip(" :"),
        "asks": asks or [],
        "url": kaiten.card_url(card),
    })


def note_spend(meta: dict | None) -> None:
    SPENT["usd"] += float((meta or {}).get("cost_usd") or 0.0)


def budget_left(cfg: dict) -> float | None:
    """Сколько ещё можно потратить. None — потолок не задан."""
    cap = cfg.get("max_spend_per_run")
    return None if not cap else max(0.0, float(cap) - SPENT["usd"])


def out_of_budget(cfg: dict) -> bool:
    left = budget_left(cfg)
    if left is None or left > 0:
        return False
    log(f"потолок расхода на прогон исчерпан (${SPENT['usd']:.2f}) — дальше не берём")
    return True


def format_meta(meta: dict) -> str:
    """
    Строка «сколько заняло и во что обошлось».

    Про шаги и время: когда агента обрывают по потолку, claude отдаёт мусорные
    метрики — девять миллисекунд и один шаг за девять минут настоящей работы.
    Свой счётчик шагов из пульса в этот момент честнее, поэтому берём больший
    из двух, а заведомо неправдоподобное время просто не показываем.
    """
    bits = []
    duration = int(meta.get("duration_ms") or 0)
    steps = max(int(meta.get("num_turns") or 0), int(meta.get("steps") or 0))
    if duration >= 1000:
        bits.append(f"{round(duration / 1000)}с")
    if meta.get("cost_usd"):
        bits.append(f"~${meta['cost_usd']:.2f}")
    if steps:
        bits.append(f"{steps} шагов")
    return ", ".join(bits)


# Причины, по которым claude обрывает сам себя. Это не вердикт агента: работа шла,
# просто её прекратили снаружи. Ключи — subtype из итогового события потока.
AGENT_STOP_REASONS = {
    "error_max_budget_usd": "упёрся в потолок расхода",
    "error_max_turns": "упёрся в потолок шагов",
    "error_during_execution": "оборвался на ошибке рантайма",
}

# Метка в комментарии: работа прервана и будет продолжена. По ней же следующий прогон
# понимает, что надо не начинать заново, а взять существующую ветку.
PARTIAL_MARK = "Работа прервана, продолжу с того же места"


def agent_was_cut(meta: dict) -> str:
    """Оборвали ли агента снаружи. Возвращает причину по-русски или пустую строку."""
    subtype = str((meta or {}).get("subtype") or "")
    if not subtype.startswith("error_"):
        return ""
    return AGENT_STOP_REASONS.get(subtype, subtype)


def count_partials(comments: list) -> int:
    """
    Сколько раз подряд агента обрывали с последней реплики человека.

    Нужен, чтобы не жечь деньги вечно: если задача не влезает в потолок, второй
    обрыв подряд значит, что её надо разбить или поднять лимит, а не пробовать снова.
    """
    count = 0
    for comment in reversed(comments):
        text = strip_html(comment.get("text", ""))
        if not text.startswith(AGENT_MARKS):
            break
        if PARTIAL_MARK in text:
            count += 1
    return count


def format_assumptions(verdict: dict) -> str:
    """
    Блок «Додумал сам» — решения, которые агент принял за человека.

    Ради него фабрике и разрешили не ждать ответа: она не встаёт на сутки из-за
    вопроса, но и не делает вид, что вопроса не было. Место ему в карточке, а не
    в PR: карточку человек читает, PR открывает не всегда.
    """
    return format_list("⚠️ Додумал сам — проверь", verdict.get("assumptions"))


def format_list(title: str, items) -> str:
    items = [i for i in (items or []) if str(i).strip()]
    if not items:
        return ""
    body = "\n".join(f"- {i}" for i in items)
    return f"\n\n**{title}**\n{body}"


def comment_success(verdict: dict, meta: dict, pr_url: str, branch: str) -> str:
    """
    Отчёт в карточку: ссылка, что сделано и что проверить. Больше ничего.

    Ветка, время и деньги прогона — в логе (`logs/card-*.json`): человеку в карточке
    они ни к чему, а абзац «рисков» с именами файлов читать никто не станет.
    """
    text = f"🤖 **Готово, нужен ревью.**\n\nPR: {pr_url}"
    text += f"\n\n{verdict.get('summary', '')}"
    if verdict.get("risks"):
        text += f"\n\n**Проверь:** {verdict['risks']}"
    text += format_assumptions(verdict)
    return text


def comment_question(verdict: dict, meta: dict) -> str:
    text = "🤖 **Не берусь — не хватает информации.** Ничего не менял.\n\n"
    text += verdict.get("summary", "")
    text += format_list("Что нужно уточнить", verdict.get("questions"))
    text += "\n\nПоправь описание карточки и верни её в «Очередь»."
    return text


def comment_failed(reason: str) -> str:
    return f"🤖 **Запуск упал.**\n\n```\n{reason[:1500]}\n```"


# --------------------------------------------------------------------------- #
# обработка одной карточки
# --------------------------------------------------------------------------- #

def resolve_repo(card: dict, cfg: dict, default_key: str | None = None) -> tuple[str, dict]:
    """
    В описании можно переопределить репозиторий строкой `repo: <ключ>`.

    Без неё берётся репозиторий профиля (у доски сабтасок он может быть свой),
    а если и его нет — default_repo из конфига.
    """
    key = default_key if default_key in (cfg.get("repos") or {}) else cfg["default_repo"]
    match = re.search(r"^\s*repo:\s*([\w.-]+)\s*$",
                      strip_html(card.get("description")) or "", re.MULTILINE | re.IGNORECASE)
    if match and match.group(1) in cfg["repos"]:
        key = match.group(1)
    # ключ едет вместе с настройками: по нему промпт берёт правила своего репозитория
    return key, {**cfg["repos"][key], "key": key}


# --------------------------------------------------------------------------- #
# эпик долга на спринт
# --------------------------------------------------------------------------- #

MONTHS_GENITIVE = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def sprint_label(anchor: date, days: int, today: date | None = None) -> str:
    """«10 августа — 24 августа» для спринта, в который попадает сегодняшний день."""
    today = today or date.today()
    start = anchor + timedelta(days=((today - anchor).days // days) * days)
    end = start + timedelta(days=days)
    return (f"{start.day} {MONTHS_GENITIVE[start.month - 1]}"
            f" — {end.day} {MONTHS_GENITIVE[end.month - 1]}")


def normalize_title(text: str) -> str:
    """Тире у людей разное, пробелы плавают — сравниваем по приведённому виду."""
    text = re.sub(r"[—–−-]", "-", (text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def is_debt_card(title: str, label: str, prefix: str) -> bool:
    """
    Наша карточка — та, что начинается ровно с «Долг» и содержит диапазон спринта.
    Чужой «Тех долг 10 августа — 24 августа» под это условие не подходит и остаётся
    нетронутым: у него перед словом стоит «Тех».
    """
    norm = normalize_title(title)
    head = normalize_title(prefix)
    if not re.match(rf"{re.escape(head)}\b", norm):
        return False
    return normalize_title(label) in norm


def fill_required_properties(kaiten: Kaiten, epic: dict, card: dict) -> None:
    """
    Заполняет обязательные поля типа карточки короткими значениями из конфига.
    Уже заполненное не перетираем — там мог написать человек.
    """
    wanted = epic.get("properties") or {}
    current = card.get("properties") or {}
    missing = {key: value for key, value in wanted.items() if not current.get(key)}
    if not missing:
        return
    log(f"  заполняю обязательные поля: {', '.join(sorted(missing))}")
    kaiten.patch_card(card["id"], {"properties": {**current, **missing}})


def sync_debt_description(kaiten: Kaiten, parent_id: int) -> None:
    """
    Описание карточки долга — короткий список того, что в нём делается.
    Собираем заново из дочерних карточек: так оно не разъезжается и не дублируется.
    """
    titles = [(child.get("title") or "").strip()
              for child in kaiten.children(parent_id)]
    description = "\n\n".join(f"* {title}" for title in titles if title)
    kaiten.patch_card(parent_id, {"description": description})
    log(f"  описание долга обновлено: {len(titles)} пунктов")


def ensure_debt_card(kaiten: Kaiten, epic: dict, dry_run: bool) -> dict:
    """Находит карточку долга на текущий спринт, а если её нет — создаёт в Development."""
    anchor = date.fromisoformat(epic["sprint_anchor"])
    label = sprint_label(anchor, epic["sprint_days"])
    title = f"{epic['title_prefix']}{label}"
    dev_column = epic["development_column_id"]

    for candidate in kaiten.cards_on_board(epic["board_id"]):
        if is_debt_card(candidate.get("title", ""), label, epic["title_prefix"]):
            log(f"  карточка долга: #{candidate['id']} «{candidate['title']}»")
            if epic.get("keep_in_development") and candidate.get("column_id") != dev_column:
                # только колонка: state эпика не трогаем. И только вперёд: карточку
                # долга человек может увезти дальше сам, и возвращать её назад —
                # ровно тот откат чужого решения, которого фабрика не делает
                order = column_order(kaiten, epic["board_id"])
                here = candidate.get("column_id")
                if here in order and dev_column in order \
                        and order.index(here) > order.index(dev_column):
                    log("  карточка долга уже правее Development — назад не двигаю")
                else:
                    log("  двигаю карточку долга в Development")
                    kaiten.move(candidate["id"], dev_column)
            if not dry_run:
                fill_required_properties(kaiten, epic, candidate)
            return candidate

    log(f"  карточки долга нет, создаю «{title}» в Development")
    if dry_run:
        return {"id": 0, "title": title}
    created = kaiten.create_card({
        "board_id": epic["board_id"],
        "column_id": dev_column,
        "lane_id": epic["lane_id"],
        "type_id": epic["card_type_id"],
        "title": title,
        "properties": epic.get("properties") or {},
    })
    log(f"  создана #{created['id']}")
    return created


def sprint_debt(cfg: dict) -> dict | None:
    """
    Настройки карточки долга спринта.

    Секция называется `sprint_debt`. Старое имя `epic` поддерживаем: оно слишком похоже
    на `epic_flow` — второй режим работы — и путало бы, но ломать чужие конфиги незачем.
    """
    return cfg.get("sprint_debt") or cfg.get("epic") or None


def wants_debt(profile: dict, card: dict) -> bool:
    """
    Привязывать ли карточку к долгу спринта.

    Долг существует, чтобы мелкий техдолг был виден в процессе, а не расползался
    по доске ничьим. Поэтому привязываем то, у чего нет своего родителя:

    - на своей доске — всё, как и раньше;
    - задачу, выросшую из инбокса, — где бы она ни лежала: своего эпика у неё нет,
      и без долга её в спринте никто не увидит;
    - сабтаску эпика — никогда: она уже висит на эпике, и второй родитель только
      раздувает описание долга;
    - чужую карточку, которую человек пометил тегом, — тоже никогда: это его
      карточка, и менять ей родителей мы не в праве.
    """
    if profile.get("attach_to_debt"):
        return True
    return bool(INBOX_ORIGIN_RE.search(strip_html(card.get("description")) or ""))


def attach_to_debt_card(kaiten: Kaiten, cfg: dict, card_id: int, dry_run: bool) -> str:
    """
    Вешает карточку дочерней на эпик долга. Возвращает строку для комментария.
    Ошибку не поднимает: PR уже создан, из-за неудачной привязки карточка не должна
    уезжать в «Упало».
    """
    epic = sprint_debt(cfg)
    if not epic:
        return ""
    try:
        parent = ensure_debt_card(kaiten, epic, dry_run)
        if dry_run:
            log(f"  [dry-run] привязал бы к «{parent['title']}»")
            return f"\n\nДолг спринта: «{parent['title']}» (dry-run)."
        if any(child.get("id") == card_id for child in kaiten.children(parent["id"])):
            log("  уже привязана к карточке долга")
        else:
            kaiten.add_child(parent["id"], card_id)
            log(f"  привязана к #{parent['id']} «{parent['title']}»")
        sync_debt_description(kaiten, parent["id"])
        return f"\n\nПривязана дочерней к «{parent['title']}»."
    except Exception as e:  # noqa: BLE001 — привязка не важнее уже созданного PR
        log(f"  !! не смог привязать к карточке долга: {e}")
        return f"\n\n⚠️ Не смог привязать к карточке долга спринта: {str(e)[:200]}"


# --------------------------------------------------------------------------- #
# техдолг спринта: задачи спринта без эпика
# --------------------------------------------------------------------------- #

def sprint_bounds(anchor: date, days: int, today: date | None = None) -> tuple[date, date]:
    """
    Первый и последний рабочий день спринта, в который попадает сегодняшний день.

    Выходные в хвосте не считаем: спринт с понедельника 05.10 команда называет
    «05.10–16.10», а не «05.10–18.10», и эпик с другими датами фабрика бы не узнала.
    """
    today = today or date.today()
    start = anchor + timedelta(days=((today - anchor).days // days) * days)
    end = start + timedelta(days=days - 1)
    while end.weekday() >= 5 and end > start:
        end -= timedelta(days=1)
    return start, end


def short_dates(start: date, end: date) -> str:
    return f"{start:%d.%m}–{end:%d.%m}"


def tech_debt_sections(cfg: dict) -> list[tuple[str, dict, dict]]:
    """
    Продукты, у которых задачи спринта без эпика собираются в «Техдолг спринта».

    Секция живёт в продукте, а не наверху: у каждого продукта своя спринт-доска
    и своя дорожка на доске эпиков. Уходящую доску не обходим — её спринт кончился.
    """
    return [(key, product, product["sprint_tech_debt"])
            for key, product in destinations(cfg).items()
            if product.get("sprint_tech_debt")]


def done_before(card: dict, start: date) -> bool:
    """
    Карточка закрыта до начала спринта — она из прошлого, и в нынешний техдолг
    ей не место. «Готово» на спринт-доске копится, пока его не заархивируют руками.
    """
    if card.get("state") != 3:
        return False
    stamp = parse_stamp(card.get("last_moved_to_done_at") or card.get("completed_at"))
    return bool(stamp) and stamp.astimezone().date() < start


def epic_parent(kaiten: Kaiten, card: dict, epic_boards: set, cache: dict) -> int | None:
    """
    Родитель-эпик карточки, если он есть.

    Родитель бывает и не эпиком: задача из инбокса висит на своей карточке инбокса,
    и к эпику она от этого не привязана. Эпик узнаём по доске родителя; родителей
    на спринт-доске немного, поэтому по каждому ходим раз за прогон.
    """
    for parent_id in card.get("parents_ids") or []:
        if parent_id not in cache:
            try:
                cache[parent_id] = kaiten.card(parent_id).get("board_id")
            except NotFoundError:
                cache[parent_id] = None
        if cache[parent_id] in epic_boards:
            return parent_id
    return None


def previous_debt_card(kaiten: Kaiten, debt: dict) -> dict | None:
    """Самый свежий «Техдолг спринта» в дорожке — образец для нового."""
    head = normalize_title(debt["title_prefix"])
    found = [card for card in kaiten.cards_on_board(debt["board_id"], lane_id=debt["lane_id"])
             if re.match(rf"{re.escape(head)}\b", normalize_title(card.get("title", "")))]
    if not found:
        return None
    latest = max(found, key=lambda card: card.get("created") or "")
    return kaiten.card(latest["id"])


def debt_properties(props: dict, old: str, new: str) -> dict:
    """
    Поля прошлого техдолга с датами нового спринта.

    Обязательные поля эпика без них Kaiten не примет, а придумывать их фабрике не
    из чего — поэтому копируем у прошлого. Пользователи в выдаче списка приходят
    объектами, а принимаются номерами.
    """
    result = {}
    for key, value in (props or {}).items():
        if isinstance(value, str) and old:
            value = value.replace(old, new)
        elif isinstance(value, list):
            value = [item["id"] if isinstance(item, dict) and "id" in item else item
                     for item in value]
        result[key] = value
    return result


def ensure_tech_debt(kaiten: Kaiten, product: dict, debt: dict, dry_run: bool) -> dict:
    """
    «Техдолг спринта <даты>» в дорожке продукта: найденный или только что созданный.

    Найденный не двигаем и не правим — это эпик команды, его ведут люди. Новый
    заводим по образцу прошлого: те же поля и описание, только даты свои.
    """
    start, end = sprint_bounds(date.fromisoformat(debt["sprint_anchor"]), debt["sprint_days"])
    label = short_dates(start, end)
    for card in kaiten.cards_on_board(debt["board_id"], lane_id=debt["lane_id"]):
        if is_debt_card(card.get("title", ""), label, debt["title_prefix"]):
            return card

    title = f"{debt['title_prefix']}{label}"
    template = previous_debt_card(kaiten, debt)
    old = ""
    if template:
        match = re.search(r"\d\d\.\d\d\s*[—–-]\s*\d\d\.\d\d", template.get("title") or "")
        old = match.group(0) if match else ""
        log(f"  техдолга на {label} нет — завожу «{title}» по образцу #{template['id']}")
    else:
        log(f"  техдолга на {label} нет — завожу «{title}»")
    description = (template or {}).get("description") or (
        f"Задачи спринта {label} на доске «{product.get('title') or ''}», у которых нет "
        "своего продуктового эпика: баги, мелкие доработки, инфраструктура.")
    body = {
        "board_id": debt["board_id"],
        "column_id": debt["column_id"],
        "lane_id": debt["lane_id"],
        "type_id": debt.get("card_type_id") or (template or {}).get("type_id"),
        "title": title,
        "description": description.replace(old, label) if old else description,
        "properties": debt_properties((template or {}).get("properties"), old, label),
    }
    if dry_run:
        log("  [dry-run] карточку не создаю")
        return {"id": 0, "title": title}
    created = kaiten.create_card({k: v for k, v in body.items() if v})
    log(f"  создан #{created['id']}")
    return created


def link_sprint_orphans(kaiten: Kaiten, cfg: dict, args) -> int:
    """
    Вешает задачи спринта без эпика на «Техдолг спринта».

    Каждая задача спринта должна быть к чему-то привязана, иначе в планировании её
    не видно. Задачи без продуктового эпика — баги, фиксы, мелочь из инбокса —
    собираются в техдолг текущего спринта. Агента тут нет: это обход доски и пара
    запросов, поэтому он ходит вместе с разведкой инбокса раз в несколько минут.
    """
    linked = 0
    for key, product, debt in tech_debt_sections(cfg):
        start, _ = sprint_bounds(date.fromisoformat(debt["sprint_anchor"]),
                                 debt["sprint_days"])
        epic_boards = {debt["board_id"], *((epic_flow(cfg) or {}).get("boards") or [])}
        cache: dict = {}
        orphans = []
        for card in kaiten.cards_on_board(product["board_id"], with_description=True):
            if done_before(card, start) or epic_parent(kaiten, card, epic_boards, cache):
                continue
            if hands_off(card, kaiten.comments(card["id"]), cfg):
                log(f"  #{card['id']} просили не трогать — в техдолг не вешаю")
                continue
            orphans.append(card)
        where = product.get("title") or key
        if not orphans:
            log(f"в спринте «{where}» все задачи при эпиках")
            continue
        log(f"в спринте «{where}» без эпика: {len(orphans)}")
        parent = ensure_tech_debt(kaiten, product, debt, args.dry_run)
        for card in orphans:
            if args.dry_run:
                log(f"  [dry-run] #{card['id']} «{card['title'][:60]}» → «{parent['title']}»")
                continue
            kaiten.add_child(parent["id"], card["id"])
            log(f"  #{card['id']} «{card['title'][:60]}» → #{parent['id']} «{parent['title']}»")
            linked += 1
    return linked


def sweep_sprint_orphans(kaiten: Kaiten, cfg: dict, args) -> None:
    """Обход техдолга, который не роняет прогон: он про порядок, а не про работу."""
    if args.prompt_only or not tech_debt_sections(cfg):
        return
    try:
        link_sprint_orphans(kaiten, cfg, args)
    except Exception as e:  # noqa: BLE001 — привязка к эпику не важнее разведки
        log(f"привязка к техдолгу спринта не задалась: {e}")


PR_TEMPLATE_PATHS = [
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/pull_request_template.md",
    ".github/PULL_REQUEST_TEMPLATE/default.md",
    "docs/PULL_REQUEST_TEMPLATE.md",
    "PULL_REQUEST_TEMPLATE.md",
    "pull_request_template.md",
]

# Заголовок раздела, куда вписываем рассказ агента. Всё остальное в шаблоне —
# чек-листы релиза, их надо сохранить дословно, человеку по ним работать.
DESCRIPTION_HEADING = re.compile(r"^#{1,4}[ \t]*(?:Описание|Description)\b.*$",
                                 re.IGNORECASE | re.MULTILINE)
SECTION_END = re.compile(r"^(?:#{1,4}[ \t]|_{3,}[ \t]*$|-{3,}[ \t]*$|<details>)", re.MULTILINE)


def find_pr_template(worktree: Path) -> Path | None:
    for rel in PR_TEMPLATE_PATHS:
        path = worktree / rel
        if path.is_file():
            return path
    directory = worktree / ".github" / "PULL_REQUEST_TEMPLATE"
    if directory.is_dir():
        files = sorted(directory.glob("*.md"))
        if files:
            return files[0]
    return None


def mock_backend_warning(card: dict) -> str:
    """
    Сабтаска, помеченная моком, несёт предупреждение и в PR: человек, который смотрит
    только гитхаб, иначе раскатает фронт без бека и получит пустой экран.
    """
    description = strip_html(card.get("description")) or ""
    return f"\n\n{MOCK_BACKEND_NOTE}\n" if MOCK_BACKEND_LINE in description else ""


def compose_pr_body(worktree: Path, card: dict, card_url: str, verdict: dict) -> str:
    """
    Шаблон PR из репозитория не выбрасываем: заполняем в нём раздел «Описание»,
    остальное оставляем как есть. Без шаблона — просто наш текст.
    """
    ours = (
        f"Карточка: [#{card['id']} {(card.get('title') or '').strip()}]({card_url})\n\n"
        f"{verdict.get('summary', '')}"
        + (f"\n\n**Риски:** {verdict['risks']}" if verdict.get("risks") else "")
        + mock_backend_warning(card)
        + "\n\n> ⚠️ Код написан агентом автоматически — нужен внимательный ревью."
    )

    template_path = find_pr_template(worktree)
    if template_path is None:
        return ours

    template = template_path.read_text(encoding="utf-8")
    heading = DESCRIPTION_HEADING.search(template)
    if heading is None:
        # раздела «Описание» нет — кладём наш текст перед шаблоном, шаблон не трогаем
        return f"{ours}\n\n---\n\n{template}"

    tail_start = heading.end()
    tail = template[tail_start:]
    end = SECTION_END.search(tail)
    tail_end = tail_start + (end.start() if end else len(tail))
    return f"{template[:tail_start]}\n\n{ours}\n\n{template[tail_end:]}"


def open_pr(worktree: Path, branch: str, base: str, card: dict, card_url: str,
            verdict: dict, cfg: dict, dry_run: bool, repo_key: str | None = None) -> str:
    title = f"#{card['id']} {(card.get('title') or '').strip()[:70]}"
    body = compose_pr_body(worktree, card, card_url, verdict)
    template = find_pr_template(worktree)
    log(f"  шаблон PR: {template.relative_to(worktree) if template else 'нет, беру наш текст'}")

    if dry_run:
        log(f"  [dry-run] gh pr create --base {base} --head {branch} "
            f"(тело {len(body)} символов)")
        return "(dry-run, PR не создан)"

    existing = run_bounded(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url",
         "--jq", ".[0].url"],
        worktree, 120,
    ).stdout.strip()
    if existing:
        log(f"  PR для ветки уже открыт: {existing}")
        notify_pr(cfg, repo_key, card, card_url, existing, verdict, dry_run, updated=True)
        return existing

    # тело отдаём файлом: шаблоны бывают на десятки килобайт, в argv их тащить незачем
    handle = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
    try:
        handle.write(body)
        handle.close()
        cmd = ["gh", "pr", "create", "--base", base, "--head", branch,
               "--title", title, "--body-file", handle.name]
        if (cfg.get("pr") or {}).get("draft", True):
            cmd.append("--draft")
        proc = run_bounded(cmd, worktree, 120)
    finally:
        os.unlink(handle.name)

    if proc.returncode != 0:
        raise FactoryError(f"gh pr create -> {proc.returncode}: {proc.stderr.strip()[:800]}")
    lines = proc.stdout.strip().splitlines()
    if not lines:
        raise FactoryError("gh pr create отработал, но не вернул ссылку на PR")
    # отправка живёт здесь, а не у вызывающих: open_pr зовут из двух мест, завтра
    # появится третье — и его забудут подключить. Одна точка, из которой нельзя не написать
    notify_pr(cfg, repo_key, card, card_url, lines[-1], verdict, dry_run)
    return lines[-1]


def note_event(event: dict) -> None:
    """
    Дописать строку в журнал исходов. Ошибки глотаем: витрина не важнее работы.

    Формат — jsonl: дописать строку дешевле, чем перечитывать и переписывать файл,
    а прогон может оборваться на любом шаге. Читает журнал `dashboard.py`.
    """
    if DRY_RUN:
        return
    try:
        LOGS.mkdir(exist_ok=True)
        with EVENTS_FILE.open("a", encoding="utf-8") as out:
            out.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as e:
        log(f"  журнал исходов не пишется: {e}")


def finish_status(card_id: int, title: str, outcome: str, meta, pr_url: str = "",
                  card_url: str = "", kind: str = "card", detail: str = "") -> None:
    """
    Чем кончилась работа по карточке: строкой в меню-бар и строкой в журнал.

    `detail` идёт только в журнал: в меню-баре исход стоит одной короткой строкой,
    и текст поломки её разорвёт, а витрине без него нечего показать в «не смог».
    """
    last = {
        "card_id": card_id,
        "title": title,
        "outcome": outcome,
        "pr": pr_url,
        # у разведки PR нет, но открыть карточку из меню-бара всё равно хочется
        "url": card_url,
        "cost_usd": (meta or {}).get("cost_usd"),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_status(card=None, phase=None, last=last)
    # «Упало» до журнала иначе не доходит вовсе: обёртка сломалась до агента, и файла
    # в logs/ по такой карточке нет — по одним вердиктам агентов её не сосчитать
    note_event({"kind": kind, **last, "detail": detail})


def own_subtask(card: dict) -> bool:
    """Карточка создана фабрикой из эпика, а не человеком."""
    return bool(EPIC_ORIGIN_RE.search(strip_html(card.get("description")) or ""))


def mine(profile: dict, card: dict) -> bool:
    """
    Моя ли это карточка на общей доске.

    Признак один и ставится мышкой — тег. Так человек может отдать разработчику любую
    свою задачу, не заводя её заново на отдельной доске: повесил тег — взял в работу,
    снял — забрал обратно.

    Строку «Из эпика: #<id>» проверяем следом, и только ради совместимости: сабтаски,
    созданные до тега, никуда с доски не делись, и терять их на полпути нельзя.
    """
    return has_tag(card, profile.get("tag") or BOT_TAG) or own_subtask(card)


def theirs(profile: dict, card: dict) -> bool:
    """
    Чужая ли карточка. На своей доске чужих нет, на доске команды — почти все.

    Проверять надо на каждом обходе колонок, а не только при выборке работы: на общей
    доске роли делят колонки, и «Вопрос от агента» указывает в ту же «В работе», где
    лежит живой бэклог команды. Без этого стража фабрика объявляла чужие карточки
    ждущими её ответа.
    """
    return bool(profile.get("own_only")) and not mine(profile, card)


def skip_hands_off(kaiten: Kaiten, cards: list, cfg: dict) -> list:
    """
    Убирает карточки, в которых просили не трогать. Комментарий не пишем: просили
    не подходить — значит и не отвечаем.
    """
    allowed = []
    for card in cards:
        stop = hands_off(card, kaiten.comments(card["id"]), cfg)
        if stop:
            log(f"#{card['id']} не трогаю: в карточке «{stop}»")
        else:
            allowed.append(card)
    return allowed


def fix_round_cards(kaiten: Kaiten, profile: dict) -> list:
    """
    Карточки, которым ревьювер вернул замечания.

    Если у доски есть своя колонка «Правки» — всё просто, берём её. Если нет (а на
    чужой доске её обычно нет), ревьювер возвращает карточку в рабочую колонку, и
    отличить её от карточки в работе можно только по переписке: последнее слово
    осталось за ревьювером.
    """
    fixes_column = role_column(profile, "fixes")
    in_progress = role_column(profile, "in_progress")
    if fixes_column and fixes_column != in_progress:
        return [card for card in kaiten.cards_in_column(profile["board_id"], fixes_column)
                if not theirs(profile, card)]


    found = []
    for card in kaiten.cards_in_column(profile["board_id"], in_progress):
        if theirs(profile, card):
            continue
        if blocked_by(kaiten, card["id"]):
            continue
        texts = [strip_html(c.get("text", "")) for c in kaiten.comments(card["id"])]
        if texts and texts[-1].startswith(REVIEWER_MARK):
            found.append(card)
    return found


def pick_cards(kaiten: Kaiten, cfg: dict, profile: dict) -> list:
    """
    Что берём в работу:
      1. всё из «Очереди»;
      2. из «Вопроса от агента» — только те, где человек уже ответил.

    Отличить ответ человека от вопроса агента по автору нельзя (пишем одним токеном),
    поэтому смотрим на метку AGENT_MARK: последний комментарий не агентский → ответили.
    """
    board_id = profile["board_id"]
    cols = profile["columns"]
    answered, waiting = [], 0
    for card in kaiten.cards_in_column(board_id, cols.get("question") or 0):
        if theirs(profile, card):
            continue
        # заблокированную карточку не берём, даже если человек уже ответил: блокер
        # снимает он же, и пока он висит — это его ход, а не наш
        stuck = blocked_by(kaiten, card["id"])
        if stuck:
            waiting += 1
            note_waiting(kaiten, card, stuck,
                         asks=open_questions(kaiten.comments(card["id"])))
            continue
        history = kaiten.comments(card["id"])
        last = strip_html(history[-1].get("text", "")) if history else ""
        if history and not last.startswith(AGENT_MARKS):
            answered.append(card)
        else:
            # Карточку могли положить в эту колонку руками. Если агент по ней ничего
            # не спрашивал, то и ждать от человека нечего — фабрика к ней не касалась
            if not any(strip_html(c.get("text", "")).startswith(AGENT_MARKS)
                       for c in history):
                continue
            waiting += 1
            note_waiting(kaiten, card, "не хватило данных, ответь комментарием",
                         asks=open_questions(history))
    if answered:
        log(f"в «Вопросе от агента» ответили на {len(answered)}: "
            f"{', '.join('#' + str(c['id']) for c in answered)}")
    if waiting:
        log(f"ещё {waiting} карточек ждут твоего ответа")
    # человечек в меню-баре по этому числу решает, вопрошать ему или спать. Досок может
    # быть несколько, поэтому копим, а не перетираем — иначе видно только последнюю
    AWAITING["count"] += waiting
    write_status(awaiting_answer=AWAITING["count"], waiting=AWAITING["cards"])

    fixes = fix_round_cards(kaiten, profile)
    if fixes:
        log(f"в «Правках» {len(fixes)}: {', '.join('#' + str(c['id']) for c in fixes)}")
    # правки вперёд: PR уже открыт, домучить его дешевле, чем начинать новую задачу.
    # потом отвеченные вопросы — человек только что написал, ему нужен быстрый отклик
    queue = kaiten.cards_in_column(board_id, cols["queue"])
    picked = skip_off_hours(skip_hands_off(kaiten, fixes + answered + queue, cfg), cfg)
    if profile.get("own_only"):
        before = len(picked)
        picked = [c for c in picked if not theirs(profile, c)]
        if before != len(picked):
            log(f"на доске «{profile['key']}» {before - len(picked)} чужих карточек — "
                f"их фабрика не трогает")
    free = []
    for card in picked:
        stuck = blocked_by(kaiten, card["id"])
        if stuck:
            note_waiting(kaiten, card, stuck)
            continue
        free.append(card)
    write_status(waiting=AWAITING["cards"])
    return free


# --------------------------------------------------------------------------- #
# мерж: работа, уехавшая в main, закрывает карточку
# --------------------------------------------------------------------------- #

# Колонки, из которых мерж уводит карточку в «Готово». Это те, где она стоит
# с открытым PR и ждёт человека. Рабочих колонок здесь нет намеренно: там карточку
# либо пишут, либо правят по замечаниям, и смерженный PR прошлого круга — не повод
# считать её законченной.
MERGE_ROLES = ("agent_review", "review")

# Ссылка на PR в комментарии фабрики. Своих комментариев про PR два — отчёт
# исполнителя и выжимка ревьювера, — и годится любой.
PR_LINK_RE = re.compile(r"https://\S+?/pull/\d+")

# По этой строке фабрика узнаёт свой же отчёт о закрытии карточки и не пишет второй.
# Строка одна на оба исхода — и «PR смержен», и «работа уехала в main», — потому что
# проверка нужна одна. Нужна она на доске, где «Готово» делит колонку с ревью:
# карточка остаётся на месте, и без проверки прогон отчитывался бы каждый час.
DONE_LINE = "двигаю карточку в «Готово»"


def pr_link(comments: list) -> str:
    """
    Ссылка на PR карточки из комментариев фабрики, самая свежая.

    Человеческие комментарии не читаем: ссылку в них мог оставить кто угодно и на
    что угодно, а по этой ссылке карточка закрывается.
    """
    for comment in reversed(comments):
        text = strip_html(comment.get("text", ""))
        if not text.startswith(AGENT_MARKS):
            continue
        found = PR_LINK_RE.search(text)
        if found:
            return found.group(0)
    return ""


def pr_status(repo: Path, prefixes: tuple[str, ...], card_id: int, pr_url: str) -> dict:
    """
    Что стало с PR карточки: `url`, `state` (`OPEN`, `MERGED`, `CLOSED`), `mergedAt`,
    `mergedBy`. Пустой словарь — PR не нашёлся, и это не поломка: карточку могли
    положить в колонку ревью руками.

    Путей два. Ссылка из комментария прямее: по ней `gh` находит PR сам, даже если
    он в другом репозитории. Ссылки нет (карточку вёл ещё старый прогон, комментарий
    стёрли) — ищем по имени ветки, и именно поиском по PR, а не через `ls-remote`:
    после мержа GitHub ветку удаляет, в репозитории её больше нет, а в списке PR она
    осталась.
    """
    fields = "url,state,mergedAt,mergedBy,headRefName"
    if pr_url.startswith("http"):
        proc = run_bounded(["gh", "pr", "view", pr_url, "--json", fields], repo, 120)
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout)
        log(f"  !! {pr_url} не прочитался: {proc.stderr.strip()[:160]}")

    for prefix in prefixes:
        head = f"{prefix}{card_id}"
        proc = run_bounded(["gh", "pr", "list", "--search", f"head:{head}",
                            "--state", "all", "--json", fields], repo, 120)
        if proc.returncode != 0:
            log(f"  !! PR по ветке {head} не искался: {proc.stderr.strip()[:160]}")
            return {}
        # `head:` в поиске GitHub совпадает по началу имени, поэтому `stepa/card-123`
        # поймал бы заодно и `stepa/card-1234-…`: имя ветки проверяем сами
        for pull in json.loads(proc.stdout or "[]"):
            name = pull.get("headRefName") or ""
            if name == head or name.startswith(f"{head}-"):
                return pull
    return {}


PR_PARTS_RE = re.compile(r"https://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)")

PR_FEEDBACK_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      commits(last: 1) { nodes { commit { committedDate } } }
      reviews(last: 50) { nodes { author { login } body submittedAt } }
      comments(last: 50) { nodes { author { login } body createdAt } }
      reviewThreads(last: 100) {
        nodes {
          id
          isResolved
          path
          line
          originalLine
          comments(first: 30) { nodes { author { login } body createdAt } }
        }
      }
    }
  }
}
"""

PR_REPLY_MUTATION = """
mutation($thread: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $thread, body: $body}) {
    comment { id }
  }
}
"""


def pr_feedback(repo: Path, pr_url: str) -> list[dict]:
    """
    Что люди написали в PR после последнего коммита ветки.

    Человек ревьюит в GitHub, а просит в треде одной фразой: «поправь замечания из PR».
    Исполнитель видит только карточку, и без этого списка ему не на что опереться.

    Каждое замечание — `ref` (номер, по которому агент на него ответит), `thread`
    (id инлайн-треда на GitHub, пусто у ревью и общих комментариев), `who`, `quote`
    (первая строка — чтобы было видно, на что ответ) и `text` — строка для промпта.

    «После последнего коммита» — граница того, что уже разобрано: ревью, оставленное
    до прошлого круга, тот круг и закрыл. Инлайн-тред берём, только если он не решён
    и последний человек писал в нём после коммита. Свои реплики (🔍, 🤖 — выжимка
    ревьювера, отчёты, ответы на замечания) отсекаются по метке: под каким логином их
    пишет фабрика, не важно. Пусто — и когда замечаний нет, и когда GitHub не ответил:
    круг правок от этого не встаёт, просто идёт по карточке.
    """
    parts = PR_PARTS_RE.match(pr_url or "")
    if not parts:
        return []
    owner, name, number = parts.groups()
    proc = run_bounded(["gh", "api", "graphql", "-f", f"query={PR_FEEDBACK_QUERY}",
                        "-f", f"owner={owner}", "-f", f"name={name}",
                        "-F", f"number={number}"], repo, 120)
    if proc.returncode != 0:
        log(f"  !! замечания из {pr_url} не прочитались: {proc.stderr.strip()[:160]}")
        return []
    try:
        pull = json.loads(proc.stdout)["data"]["repository"]["pullRequest"] or {}
    except (ValueError, KeyError, TypeError):
        log(f"  !! замечания из {pr_url} не разобрались")
        return []

    commits = (pull.get("commits") or {}).get("nodes") or []
    since = parse_stamp(((commits[-1] if commits else {}).get("commit") or {})
                        .get("committedDate"))

    def fresh(stamp) -> bool:
        moment = parse_stamp(stamp)
        return not since or not moment or moment > since

    def human(body: str) -> bool:
        return bool(body) and not body.startswith(AGENT_MARKS)

    def who(node: dict) -> str:
        return (node.get("author") or {}).get("login") or "?"

    found = []

    def add(author: str, head: str, body: str, thread: str = "", tail: list | None = None):
        ref = str(len(found) + 1)
        text = f"- `[{ref}]` **{author}**{head}: {body}"
        text += "".join(f"\n  - **{a}**: {b}" for a, b in tail or [])
        found.append({"ref": ref, "thread": thread, "who": author,
                      "quote": body.splitlines()[0][:200], "text": text})

    for review in (pull.get("reviews") or {}).get("nodes") or []:
        body = (review.get("body") or "").strip()
        if human(body) and fresh(review.get("submittedAt")):
            add(who(review), " (ревью)", body)
    for comment in (pull.get("comments") or {}).get("nodes") or []:
        body = (comment.get("body") or "").strip()
        if human(body) and fresh(comment.get("createdAt")):
            add(who(comment), "", body)
    for thread in (pull.get("reviewThreads") or {}).get("nodes") or []:
        said = [c for c in (thread.get("comments") or {}).get("nodes") or []
                if human((c.get("body") or "").strip())]
        if thread.get("isResolved") or not said or not fresh(said[-1].get("createdAt")):
            continue
        where = f"`{thread.get('path')}:{thread.get('line') or thread.get('originalLine')}`"
        add(who(said[0]), f", {where}", said[0]["body"].strip(), thread.get("id") or "",
            [(who(c), c["body"].strip()) for c in said[1:]])
    return found


def post_pr_replies(worktree: Path, pr_url: str, feedback: list[dict],
                    replies: list, dry_run: bool, token: str = "") -> int:
    """
    Ответы на замечания людей — в сам PR, рядом с замечаниями. Отдаёт, сколько ушло.

    Человек ревьюил в GitHub — там же он и ждёт ответа, а не в карточке, которую
    не открывал. Инлайн-замечание получает ответ в своём треде, ревью и общие
    комментарии — одним комментарием к PR с цитатой, на что это ответ: десяток
    отдельных комментариев зарос бы быстрее, чем их прочтут.

    Каждый ответ начинается с AGENT_MARK: так `pr_feedback` узнаёт свою реплику
    и следующим кругом не примет её за новое замечание. Пишется под тем, под кем
    залогинен `gh`, — пока это аккаунт человека. `token` — свой аккаунт бота, когда
    он появится: уйдёт в `GH_TOKEN` только этим вызовам.

    Треды не закрываем: «решено» говорит тот, кто спрашивал.
    """
    by_ref = {item["ref"]: item for item in feedback}
    said = {}
    for reply in replies or []:
        ref = str((reply or {}).get("ref") or "").strip("[] `")
        text = str((reply or {}).get("text") or "").strip()
        if ref in by_ref and text:
            said[ref] = text
    if not said:
        return 0
    env = {**os.environ, "GH_TOKEN": token} if token else None

    posted = 0
    loose = []
    for ref, text in said.items():
        item = by_ref[ref]
        if not item["thread"]:
            loose.append(f"> **{item['who']}:** {item['quote']}\n\n{text}")
            continue
        body = f"{AGENT_MARK} {text}"
        if dry_run:
            log(f"  [dry-run] ответ в тред замечания [{ref}]: {text[:80]}")
            continue
        proc = run_bounded(["gh", "api", "graphql", "-f", f"query={PR_REPLY_MUTATION}",
                            "-f", f"thread={item['thread']}", "-f", f"body={body}"],
                           worktree, 120, env=env)
        if proc.returncode != 0:
            log(f"  !! ответ на замечание [{ref}] не ушёл: {proc.stderr.strip()[:160]}")
            continue
        posted += 1

    if loose:
        body = f"{AGENT_MARK} **По замечаниям**\n\n" + "\n\n".join(loose)
        if dry_run:
            log(f"  [dry-run] общий ответ в PR на {len(loose)} замечаний")
        else:
            handle = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                                 encoding="utf-8")
            try:
                handle.write(body)
                handle.close()
                proc = run_bounded(["gh", "pr", "comment", pr_url, "--body-file",
                                    handle.name], worktree, 120, env=env)
            finally:
                os.unlink(handle.name)
            if proc.returncode != 0:
                log(f"  !! общий ответ в PR не ушёл: {proc.stderr.strip()[:160]}")
            else:
                posted += len(loose)
    return posted


def shipped_commit(repo: Path, repo_cfg: dict, card_id: int) -> dict:
    """
    Коммит карточки в базовой ветке: `sha` и `subject`. Пусто — не нашли.

    Второй признак «сделано», и без него первого не хватает. PR карточки может быть
    закрыт, а работа при этом уехать в `main` внутри чужого PR — так и вышло с правкой
    дринкита: её сквошнули в PR по Алматы, а свой PR закрыли. GitHub про такой PR
    честно отвечает «closed, не merged», и по нему одному карточка висела бы в ревью
    вечно.

    Ищем по номеру карточки, а не по коммитам ветки: при сквоше их sha в `main`
    не попадают вовсе, зато первые строки уезжают в тело мерж-коммита целиком —
    а исполнителю велено начинать первую строку с `#<id>`. Хвост `([^0-9]|$)` не даёт
    номеру склеиться с более длинным. Обход всей истории стоит десятые доли секунды
    даже на репозитории в двенадцать тысяч коммитов.
    """
    found = git(repo, "log", f"{repo_cfg['remote']}/{repo_cfg['base_branch']}",
                "-E", f"--grep=#{card_id}([^0-9]|$)", "--format=%H%x09%s", "-1",
                check=False)
    if not found:
        return {}
    sha, _, subject = found.strip().partition("\t")
    return {"sha": sha, "subject": subject}


def merged_label(stamp) -> str:
    """«10 сентября в 09:11» по местному времени. Пусто — даты в ответе не было."""
    when = parse_stamp(stamp)
    if not when:
        return ""
    local = when.astimezone()
    return f"{local.day} {MONTHS_GENITIVE[local.month - 1]} в {local:%H:%M}"


def comment_merged(pull: dict) -> str:
    """Отчёт о мерже в карточку: кто смержил, когда и куда идти смотреть."""
    text = f"{AGENT_MARK} **PR смержен — {DONE_LINE}.**"
    if pull.get("url"):
        text += f"\n\nPR: {pull['url']}"
    # без «@» перед именем: в Kaiten это обращение, а звать человека сюда уже незачем
    who = ((pull.get("mergedBy") or {}).get("login") or "").strip()
    when = merged_label(pull.get("mergedAt"))
    if who and when:
        text += f"\nСмержил {who}, {when}."
    elif who:
        text += f"\nСмержил {who}."
    elif when:
        text += f"\nСмержен {when}."
    return text


def comment_shipped(commit: dict, pull: dict) -> str:
    """
    Отчёт о том, что работа в базовой ветке, а своего мержа у карточки не было.

    Человеку важно не «карточка закрыта», а «куда девалась моя правка»: поэтому в тексте
    и мёртвый PR, и коммит, внутри которого правка уехала. Заголовок чужого коммита
    оставляем как есть — по нему и видно, в какой PR её забрали.
    """
    text = f"{AGENT_MARK} **Работа уехала в main — {DONE_LINE}.**"
    if pull.get("url"):
        text += f"\n\nСвой PR закрыт без мержа: {pull['url']}"
    else:
        text += "\n\nОткрытого PR у карточки нет."
    text += f"\nНо правка в main: коммит `{commit['sha'][:10]}`"
    if commit.get("subject"):
        text += f" «{commit['subject'][:90]}»"
    return text + ".\nПохоже, её забрали в другой PR."


def close_if_merged(kaiten: Kaiten, cfg: dict, profile: dict, card: dict,
                    done: int, fetched: set) -> bool:
    """
    Одна карточка: её работа уже в базовой ветке — уводим в «Готово». True, если увели.

    Признака два, и порядок между ними важен. Смерженный PR — прямой и самый частый,
    он же приносит «кто смержил и когда». PR ещё открыт — работа в полёте, ждём, что бы
    ни лежало в `main`. А закрытый без мержа (или ненайденный) PR — не приговор: работу
    могли забрать в чужой PR, и тогда решает сам `main`.
    """
    card_id = card["id"]
    if theirs(profile, card):
        return False
    comments = kaiten.comments(card_id)
    stop = hands_off(card, comments, cfg)
    if stop:
        log(f"#{card_id} не трогаю: в карточке «{stop}»")
        return False
    if any(DONE_LINE in text and text.startswith(AGENT_MARK)
           for text in (strip_html(c.get("text", "")) for c in comments)):
        return False  # об этой карточке уже отчитались
    # Свой блокер снимем сами, чужой — стоп: его повесил человек, и мерж этого
    # не отменяет. Пока он висит, карточка не наша.
    held = kaiten.blockers(card_id)
    if any(not ours(blocker) for blocker in held):
        log(f"#{card_id} под чужим блокером — в «Готово» не двигаю")
        return False

    key, repo_cfg = resolve_repo(card, cfg, profile.get("repo"))
    repo = Path(repo_cfg["path"]).expanduser()
    pull = pr_status(repo, branch_prefixes(cfg), card_id, pr_link(comments))
    state = (pull.get("state") or "").upper()
    where = (card.get("title") or "").strip()[:60]

    if state == "MERGED":
        report = comment_merged(pull)
        log(f"#{card_id} «{where}» — PR смержен")
    elif state == "OPEN":
        return False
    else:
        # Базовую ветку подтягиваем один раз на репозиторий за прогон: в фазе мержа
        # больше никто не ходит в сеть, и без этого `main` был бы вчерашним.
        if key not in fetched:
            git(repo, "fetch", repo_cfg["remote"], "--prune", check=False)
            fetched.add(key)
        commit = shipped_commit(repo, repo_cfg, card_id)
        if not commit:
            return False
        report = comment_shipped(commit, pull)
        log(f"#{card_id} «{where}» — своего мержа нет, но правка в main "
            f"({commit['sha'][:10]})")

    kaiten.comment(card_id, report)
    # Блокер «нужен человек» вешала фабрика — ей его и снимать: иначе карточка
    # встанет в «Готово» заблокированной и будет выглядеть сломанной. Чужих здесь
    # уже нет, их отсеяла проверка выше.
    for blocker in held:
        kaiten.unblock(card_id, blocker["id"])
    kaiten.move(card_id, done)
    log("  -> Готово")
    return True


def close_merged(kaiten: Kaiten, cfg: dict, args, profiles: list[dict]) -> int:
    """
    Фаза мержа: карточки, по которым PR уже в main, уезжают в «Готово».

    Последний шаг потока раньше делал человек руками: смержил PR — иди в Kaiten
    и перетащи карточку. Шаг мелкий, поэтому и забывался: в «На ревью» копились
    карточки, по которым всё давно влито, и по доске нельзя было понять, что
    действительно ждёт человека.

    Про мерж фабрике никто не сообщает — вебхук ей слушать негде, — поэтому
    состояние PR она спрашивает сама, раз в прогон, и только по карточкам из колонок
    ревью: один вызов `gh` на карточку, агента здесь нет вовсе. Смерженного PR
    для решения хватает не всегда: работу могли забрать в чужой PR, а свой закрыть, —
    тогда слово за базовой веткой, подробности в `shipped_commit`.

    Идёт первой в прогоне, до ревьювера: смерженный PR он иначе отревьюит заново
    за отдельные деньги.

    Ночное окно этой фазе не указ: ночной тег придуман для тяжёлых прогонов
    агента, а передвинуть карточку не дороже, чем её прочитать.
    """
    note_phase(args, "смотрю, что смержено")
    moved, fetched = 0, set()
    for profile in profiles:
        done = role_column(profile, "done")
        if not done:
            log(f"в «{profile['key']}» нет колонки «Готово» — мерж не смотрю")
            continue
        # роли могут делить колонку: обходим каждую по одному разу
        columns = [c for c in dict.fromkeys(role_column(profile, role)
                                            for role in MERGE_ROLES) if c]
        for column in columns:
            for card in kaiten.cards_in_column(profile["board_id"], column):
                try:
                    if close_if_merged(kaiten, cfg, profile, card, done, fetched):
                        moved += 1
                except Exception as e:  # noqa: BLE001 — одна карточка не роняет фазу
                    log(f"#{card['id']} мерж не проверился: {e}")
    log(f"смержено и закрыто карточек: {moved}" if moved else "новых мержей нет")
    # фазу за собой убираем: с --only-merged прогон на этом и кончается, а в трее
    # иначе навсегда останется «смотрю, что смержено»
    note_phase(args, None)
    return 0


# --------------------------------------------------------------------------- #
# ревьювер
# --------------------------------------------------------------------------- #

def build_review_prompt(card: dict, card_url: str, repo_cfg: dict, branch: str,
                        pr_url: str, round_number: int, max_rounds: int) -> str:
    template = REVIEW_TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "{{CARD_ID}}": str(card["id"]),
        "{{CARD_URL}}": card_url,
        "{{TITLE}}": card.get("title") or "(без заголовка)",
        "{{DESCRIPTION}}": strip_html(card.get("description")) or "(описания нет)",
        "{{BRANCH}}": branch,
        "{{BASE_BRANCH}}": repo_cfg["base_branch"],
        "{{REMOTE}}": repo_cfg["remote"],
        "{{PR_URL}}": pr_url or "(PR не найден)",
        "{{ROUND}}": str(round_number),
        "{{MAX_ROUNDS}}": str(max_rounds),
        **project_replacements(repo_cfg.get("key")),
    }
    return apply_template(template, replacements)


def format_findings(findings) -> str:
    """
    Замечания людям — по строке: что не так и что сделать.

    Файл, строка и «причина» сюда не идут: человек читает ревью между делом, и абзац
    на каждое замечание он пролистает. Исполнителю подробности нужны — он берёт их
    из лога ревью (`review_details`), а не из этого текста.
    """
    if not findings:
        return ""
    lines = []
    for item in findings:
        icon = SEVERITY_ICON.get(item.get("severity", ""), "•")
        fix = f" → {item['fix']}" if item.get("fix") else ""
        lines.append(f"{icon} {item.get('what', '')}{fix}")
    return "\n\n" + "\n".join(lines)


def latest_log(prefix: str) -> dict:
    """Последний лог вида `logs/<prefix>-<время>.json`. Пусто — лога нет или он битый."""
    found = sorted(LOGS.glob(f"{prefix}-*.json")) if LOGS.is_dir() else []
    if not found:
        return {}
    try:
        return json.loads(found[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def review_details(card_id: int) -> str:
    """
    Замечания последнего ревью целиком — с файлом, причиной и что делать.

    Людям уходит выжимка по строке, а исполнителю в круге правок без «где» и «почему»
    придётся искать заново то, что ревьювер уже нашёл. Лог пишется на каждое ревью.
    """
    findings = ((latest_log(f"review-{card_id}").get("review") or {})
                .get("findings") or [])
    lines = []
    for item in findings:
        icon = SEVERITY_ICON.get(item.get("severity", ""), "•")
        line = f"- {icon} `{item.get('where', '?')}` — {item.get('what', '')}"
        if item.get("why"):
            line += f"\n  причина: {item['why']}"
        if item.get("fix"):
            line += f"\n  → {item['fix']}"
        lines.append(line)
    return "\n".join(lines)


def comment_review(review: dict, meta: dict, round_number: int, max_rounds: int,
                   needs_changes: bool) -> str:
    head = ("нужны правки" if needs_changes else "замечаний нет")
    text = f"{REVIEWER_MARK} **Ревью, круг {round_number}/{max_rounds}: {head}.**\n\n"
    text += full_summary(review)
    text += format_findings(review.get("findings"))
    if needs_changes:
        text += "\n\nПоправлю и вернусь с обновлённым PR."
    return text


def count_review_rounds(comments: list) -> int:
    """
    Сколько кругов ревью прошло **с последней реплики человека**.

    Считать за всю историю нельзя. Потолок `reviewer.max_rounds` тогда упирается
    навсегда: эпик после третьего ревью уходит в «ждёт человека» и не выходит оттуда,
    сколько ему ни отвечай — счётчик-то не убывает. Поймали вживую на эпике ETA:
    блокеры сняты, человек ответил на оба вопроса, а фабрика два прогона подряд
    писала «ждёт твоего ответа».

    Ответ человека — новая вводная: стена, в которую упиралось ревью, пробита,
    и круги начинаются заново.
    """
    start = 0
    for index, comment in enumerate(comments):
        text = strip_html(comment.get("text", ""))
        if text and not text.startswith(AGENT_MARKS):
            start = index + 1
    return sum(1 for c in comments[start:]
               if strip_html(c.get("text", "")).startswith(REVIEWER_MARK)
               and "продолжение" not in strip_html(c.get("text", ""))[:40])


def post_pr_review(worktree: Path, pr_url: str, body: str) -> None:
    handle = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
    try:
        handle.write(body)
        handle.close()
        proc = run_bounded(["gh", "pr", "comment", pr_url, "--body-file", handle.name],
                           worktree, 120)
        if proc.returncode != 0:
            log(f"  !! не смог оставить комментарий в PR: {proc.stderr.strip()[:200]}")
    finally:
        os.unlink(handle.name)


def fetch_pr_notes(worktree: Path, pr_url: str) -> str:
    """
    Замечания ревьювера с PR.

    Подробное ревью живёт на гите, а не в карточке: в карточке от него один шум,
    его читают люди. Но круг правок этими замечаниями и живёт, поэтому перед правкой
    фабрика забирает их обратно с PR и кладёт в промпт сама.
    """
    if not pr_url.startswith("http"):
        return ""
    proc = run_bounded(
        ["gh", "pr", "view", pr_url, "--json", "comments",
         "--jq", '.comments[] | select(.body | startswith("' + REVIEWER_MARK + '")) | .body'],
        worktree, 120)
    if proc.returncode != 0:
        log(f"  !! не смог забрать замечания с PR: {proc.stderr.strip()[:160]}")
        return ""
    return proc.stdout.strip()


def comment_review_short(review: dict, meta: dict, round_number: int, max_rounds: int,
                         needs_changes: bool, pr_url: str, findings: list) -> str:
    """
    Короткий отчёт в карточку. Подробности остаются на гите.

    Раньше сюда уезжало ревью целиком, разрезанное на три комментария по 4096 символов.
    Человеку в карточке нужно другое: есть ли вопрос, какой именно, и куда идти читать.
    """
    if not needs_changes:
        text = f"{REVIEWER_MARK} **Замечаний нет.**\n\n{review.get('summary', '')}"
        if findings:
            text += f"\n\nМелочи ({len(findings)}) оставил комментарием в PR, они не блокируют."
    else:
        blocking = [f for f in findings if f.get("severity") in ("blocker", "major")]
        text = (f"{REVIEWER_MARK} **Нужны правки: {len(blocking)} "
                f"из {len(findings)}.**\n\n{review.get('summary', '')}")
        # только сами замечания, без файлов, причин и «как чинить» — это уже на гите
        if blocking:
            text += "\n"
        for item in blocking[:4]:
            icon = SEVERITY_ICON.get(item.get("severity", ""), "•")
            text += f"\n{icon} {item.get('what', '')}"
        if len(blocking) > 4:
            text += f"\n…и ещё {len(blocking) - 4}."
    if pr_url.startswith("http"):
        text += f"\n\nPR: {pr_url}"
    return text


def review_card(card_stub: dict, kaiten: Kaiten, cfg: dict, args, profile: dict) -> None:
    card_id = card_stub["id"]
    reviewer_cfg = cfg["reviewer"]
    max_rounds = reviewer_cfg.get("max_rounds", 3)

    card = kaiten.card(card_id)
    card_url = kaiten.card_url(card)
    comments = kaiten.comments(card_id)
    stop = hands_off(card, comments, cfg)
    if stop:
        log(f"#{card_id} не трогаю: в карточке «{stop}»")
        return
    wait = off_hours(card, cfg)
    if wait:
        log(f"#{card_id} откладываю: {wait}")
        return
    _, repo_cfg = resolve_repo(card, cfg, profile.get("repo"))
    repo = Path(repo_cfg["path"]).expanduser()
    worktree = WORKTREES / f"review-{card_id}"
    title = (card.get("title") or "").strip()

    log(f"#{card_id} «{title[:60]}» — ревью")

    branch = remote_branch_for_card(repo, repo_cfg["remote"], branch_prefixes(cfg), card_id)
    if not branch:
        log("  запушенной ветки нет")
        if args.prompt_only:
            return
        kaiten.comment(card_id, f"{REVIEWER_MARK} **Ревьювить нечего:** запушенной ветки "
                                f"`{branch_prefix(cfg)}{card_id}*` нет.")
        hand_over(kaiten, profile, card_id, "failed", "запушенной ветки нет")
        log("  -> Упало (ветки нет)")
        return

    round_number = count_review_rounds(comments) + 1
    if round_number > max_rounds and not args.prompt_only:
        kaiten.comment(card_id, f"{REVIEWER_MARK} **Ревью не сошлось за {max_rounds} круга.** "
                                f"Дальше нужен человек — смотри замечания выше.")
        hand_over(kaiten, profile, card_id, "review",
                  f"ревью не сошлось за {max_rounds} круга")
        log(f"  -> На ревью (исчерпаны {max_rounds} круга)")
        return

    try:
        if not args.prompt_only:
            write_status(card={"id": card_id, "title": title, "url": card_url},
                         phase=f"ревьювер, круг {round_number}")
        make_worktree(repo, f"stepa/review-{card_id}", repo_cfg["base_branch"],
                      repo_cfg["remote"], worktree, continue_from=branch)

        # ветка в ревью-worktree своя (stepa/review-N), поэтому PR ищем по имени
        # ревьюемой ветки, а не по текущей: `gh pr view` без --head смотрит на текущую
        pr_url = run_bounded(
            ["gh", "pr", "list", "--head", branch, "--state", "all",
             "--json", "url", "--jq", ".[0].url"],
            worktree, 120,
        ).stdout.strip()

        prompt = build_review_prompt(card, card_url, repo_cfg, branch, pr_url,
                                     round_number, max_rounds)
        if args.prompt_only:
            print("\n" + "=" * 78)
            print(prompt)
            print("=" * 78 + "\n")
            log("  промпт ревьювера показан, агент не запускался")
            return

        log(f"  ревьювер смотрит {branch} (круг {round_number}/{max_rounds})")
        review, meta = run_agent(prompt, worktree, reviewer_cfg,
                                 schema=REVIEW_SCHEMA, verdict_key="verdict")
        note_spend(meta)

        LOGS.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        (LOGS / f"review-{card_id}-{stamp}.json").write_text(
            json.dumps({"review": review, "meta": meta}, ensure_ascii=False, indent=2),
            encoding="utf-8")

        findings = review.get("findings") or []
        blocking = [f for f in findings if f.get("severity") in ("blocker", "major")]
        # вердикт агента уважаем, но блокеры в списке важнее его оптимизма
        needs_changes = review.get("verdict") == "needs_changes" or bool(blocking)
        log(f"  вердикт: {review.get('verdict')}, замечаний {len(findings)} "
            f"(блокирующих {len(blocking)})")

        # На гит уходит полное ревью, в карточку — короткая выжимка. Порядок важен:
        # если PR не принял комментарий, в карточке хотя бы останется ссылка на него,
        # а сами замечания уже не потеряются — они в logs/review-*.json
        full = comment_review(review, meta, round_number, max_rounds, needs_changes)
        if pr_url and reviewer_cfg.get("post_to_pr") and not args.dry_run:
            post_pr_review(worktree, pr_url, full)
        kaiten.comment(card_id, comment_review_short(
            review, meta, round_number, max_rounds, needs_changes, pr_url, findings))

        asks = [a for a in (review.get("needs_human") or []) if str(a).strip()]
        if asks:
            # Код написан, PR открыт — значит человек и так следующий в очереди, и
            # вопрос ревьювера это часть ревью, а не отдельная блокировка. Раньше
            # карточка уезжала в «Вопрос от агента» с блокером «не хватило данных»
            # и вставала в колонке «В работе», как будто её ещё пишут: на доске
            # сабтасок эти две роли делят одну колонку. А вопрос был вида «кто
            # посмотрит экран на стенде до мержа» — это ровно то, чем ревью и
            # занимается. Поэтому едем в колонку ревью, вопросы несём с собой.
            text = (f"{REVIEWER_MARK} **Ревью пройдено, но нужен твой взгляд.**\n\n"
                    f"{review.get('summary', '')}")
            for ask in asks[:4]:
                text += f"\n\n❓ {ask}"
            if pr_url.startswith("http"):
                text += f"\n\nОстальные замечания — в PR: {pr_url}"
            kaiten.comment(card_id, text + "\n\nОтветь комментарием в карточке.")
            hand_over(kaiten, profile, card_id, "review")
            finish_status(card_id, title, "на ревью, есть вопросы", meta, pr_url,
                          kind="review")
            log(f"  -> На ревью (человеку), вопросов: {len(asks)}")
            return

        if needs_changes:
            # правки идут в рабочую колонку: агент подхватит карточку следующим прогоном
            kaiten.move(card_id, role_column(profile, "fixes")
                        or role_column(profile, "in_progress"))
            finish_status(card_id, title, "правки", meta, kind="review")
            log("  -> Правки")
        else:
            hand_over(kaiten, profile, card_id, "review")
            finish_status(card_id, title, "ревью пройдено", meta, pr_url, kind="review")
            log("  -> На ревью (человеку)")

    except Exception as e:  # noqa: BLE001
        log(f"  !! {e}")
        if not args.prompt_only:
            kaiten.comment(card_id, comment_failed(str(e)))
            hand_over(kaiten, profile, card_id, "failed", str(e)[:120])
            finish_status(card_id, title, "упало на ревью", None, kind="review",
                          detail=str(e)[:200])
            log("  -> Упало")
    finally:
        if args.keep_worktree or cfg.get("keep_worktree"):
            log(f"  worktree ревью оставлен: {worktree}")
        else:
            drop_worktree(repo, worktree)


def link_review_cards(kaiten: Kaiten, cfg: dict, dry_run: bool) -> int:
    """Разовая операция: привязать всё, что уже висит в «На ревью», к долгу спринта."""
    if destinations(cfg):
        cards = [card for profile in board_profiles(cfg)
                 for card in kaiten.cards_in_column(profile["board_id"],
                                                    role_column(profile, "review"))
                 if not theirs(profile, card)]
    else:
        cards = kaiten.cards_in_column(cfg["kaiten"]["board_id"],
                                       cfg["kaiten"]["columns"]["review"])  # только рабочая доска
    if not cards:
        log("в «На ревью» пусто")
        return 0
    log(f"в «На ревью» {len(cards)} карточек, привязываю к долгу спринта")
    for card in cards:
        log(f"#{card['id']} «{(card.get('title') or '')[:60]}»")
        attach_to_debt_card(kaiten, cfg, card["id"], dry_run)
    return 0


def fix_round(kaiten: Kaiten, profile: dict, card: dict, comments: list) -> bool:
    """
    Правки после ревью: карточка либо лежит в своей колонке «Правки», либо (когда
    такой колонки на доске нет) стоит в рабочей, а последнее слово за ревьювером —
    или за человеком из треда Time, если PR уже есть.

    Второй случай — то, что делает тред на доске команды: «Правки» там — та же
    колонка, что «В работе» и «Вопрос», и без него просьба «поправь замечания из PR»
    читалась бы ответом на вопрос, а круг начинался бы с `main`, мимо открытого PR.
    """
    fixes_column = role_column(profile, "fixes")
    if fixes_column and fixes_column != role_column(profile, "in_progress"):
        return card.get("column_id") == fixes_column
    texts = [strip_html(c.get("text", "")) for c in comments]
    if not texts:
        return False
    if texts[-1].startswith(REVIEWER_MARK):
        return True
    return texts[-1].startswith(FROM_TIME_MARK) and bool(pr_link(comments))


def process(card_stub: dict, kaiten: Kaiten, cfg: dict, args, profile: dict) -> None:
    card_id = card_stub["id"]
    cols = profile["columns"]
    card = kaiten.card(card_id)
    card_url = kaiten.card_url(card)
    comments = kaiten.comments(card_id)
    stop = hands_off(card, comments, cfg)
    if stop:
        log(f"#{card_id} не трогаю: в карточке «{stop}»")
        return
    wait = off_hours(card, cfg)
    if wait:
        log(f"#{card_id} откладываю: {wait}")
        return
    # Своим выборкам это правило не нужно — они и так читают только колонки потока.
    # Нужно оно для `--card`: пальцем можно показать на любую карточку, в том числе
    # на ту, что человек увёл в «Ролаут» или уже закрыл, а первым делом здесь идёт
    # move в «В работе» — то есть откат чужого решения.
    if outside_flow(profile, card) or card.get("column_id") == cols.get("done"):
        log(f"#{card_id} стоит там, откуда фабрика карточки не забирает — "
            f"верни её в «Очередь», если надо переделать")
        return
    repo_key, repo_cfg = resolve_repo(card, cfg, profile.get("repo"))
    repo = Path(repo_cfg["path"]).expanduser()
    worktree = WORKTREES / f"card-{card_id}"

    # откуда пришла карточка: ответ на вопрос или правки после ревью
    returning = bool(cols.get("question")) and card.get("column_id") == cols["question"]
    fixing = fix_round(kaiten, profile, card, comments)
    title = (card.get("title") or "").strip()

    if not (repo / ".git").exists():
        raise FactoryError(f"репозиторий не найден: {repo}")

    # в имени ветки — номер и транслит заголовка, чтобы по списку веток было понятно,
    # о чём каждая. Номер идёт первым: по нему ветка ищется и им же ограничен пуш
    slug = slugify(title)
    branch = f"{branch_prefix(cfg)}{card_id}" + (f"-{slug}" if slug else "")
    continue_from = None
    # Прошлый заход оборвали на полушаге — берём его ветку, а не начинаем с нуля.
    # Иначе агент второй раз проходит тот же путь и второй раз упирается в потолок.
    resuming = count_partials(comments) > 0
    if fixing or resuming:
        # правки идут в ту же ветку и тот же PR, иначе ревью пойдёт по кругу с нуля
        continue_from = remote_branch_for_card(repo, repo_cfg["remote"],
                                               branch_prefixes(cfg), card_id)
        if continue_from:
            branch = continue_from

    # Сабтаска эпика продолжает ветку спеки: спека и реализация должны приехать
    # человеку **одним** PR. Раньше выходило два — отдельно спека, отдельно код, —
    # и ревьюить их приходилось врозь, а мержить в правильном порядке. Ветка спеки
    # уже содержит описание того, что делается, так что код ложится ровно поверх,
    # а open_pr видит открытый PR этой ветки и второго не создаёт.
    if not continue_from:
        epic_id = epic_of_card(card)
        if epic_id:
            spec_branch = remote_branch_for_card(repo, repo_cfg["remote"],
                                                 branch_prefixes(cfg), epic_id)
            if spec_branch and spec_branch.endswith("-spec"):
                branch = continue_from = spec_branch
                log(f"  пишу в ветку спеки эпика #{epic_id}: {branch}")

    reason = (" (правки после ревью)" if fixing else
              " (продолжаю прерванное)" if resuming else
              " (ответ на вопрос)" if returning else "")
    log(f"#{card_id} «{title[:60]}» -> {repo_key} / {branch}{reason}")

    if not args.prompt_only:
        kaiten.move(card_id, cols["in_progress"])
        write_status(card={"id": card_id, "title": title, "url": card_url},
                     phase="готовлю рабочую копию", returning=returning)

    try:
        make_worktree(repo, branch, repo_cfg["base_branch"], repo_cfg["remote"], worktree,
                      continue_from=continue_from)
        feedback = pr_feedback(repo, pr_link(comments)) if fixing else []
        if feedback:
            log(f"  в PR от людей замечаний: {len(feedback)}")
        prompt = build_prompt(card, comments, repo_cfg, branch, card_url,
                              returning=returning, fixing=fixing, feedback=feedback)

        if args.prompt_only:
            print("\n" + "=" * 78)
            print(prompt)
            print("=" * 78 + "\n")
            log("  промпт показан, агент не запускался, карточка не тронута")
            return

        log("  агент пошёл работать…")
        write_status(phase="агент работает")
        started = time.time()
        verdict, meta = run_agent(prompt, worktree, cfg["agent"])
        note_spend(meta)
        log(f"  агент вернулся за {round(time.time() - started)}с, "
            f"status={verdict.get('status')} {format_meta(meta)}")

        LOGS.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        (LOGS / f"card-{card_id}-{stamp}.json").write_text(
            json.dumps({"verdict": verdict, "meta": meta}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        base_ref = f"{repo_cfg['remote']}/{repo_cfg['base_branch']}"
        commits = git(worktree, "log", f"{base_ref}..HEAD", "--oneline")
        dirty = git(worktree, "status", "--porcelain")
        status = verdict.get("status")

        # Структурированного отчёта нет — после долгих сессий агент иногда просто
        # не зовёт StructuredOutput. Тогда в summary оказывалась его последняя реплика:
        # в карточке висело «фоновая установка зависимостей завершилась с кодом 0»,
        # хотя сделан был весь экран. Сообщение коммита говорит по делу.
        if status is None and commits:
            first = commits.splitlines()[0].split(" ", 1)
            if len(first) > 1:
                # «#69826636 Включить обязательный ввод…» -> «Включить обязательный ввод…»:
                # по правилам промпта первая строка коммита начинается с номера карточки,
                # а фраза уезжает в комментарий этой же карточки и в сообщение со ссылкой
                # на неё — номер там только мешает
                verdict["summary"] = re.sub(r"^#\d+\s+", "", first[1])

        # Агента оборвали снаружи — он не «ничего не сделал», он не доработал.
        # Раньше это выглядело как «агент не внёс изменений и не объяснил почему»:
        # карточка уезжала к человеку, а worktree с девятью минутами работы удалялся
        # в finally. Один такой обрыв стоил $5 и не оставил ни строчки.
        cut = agent_was_cut(meta)
        if cut:
            partials = count_partials(comments)
            if not args.dry_run and dirty:
                # Коммитим что есть: агента прервали на полушаге, и сам он уже не успеет
                git(worktree, "add", "-A")
                git(worktree, "commit", "-m",
                    f"#{card_id} промежуточный результат: {cut}", check=False)
                commits = git(worktree, "log", f"{base_ref}..HEAD", "--oneline")
            if commits and not args.dry_run and branch.startswith(branch_prefixes(cfg)):
                git(worktree, "push", "--force-with-lease", "-u", repo_cfg["remote"], branch)

            # Второй обрыв подряд — дальше пробовать бессмысленно: задача не влезает
            # в потолок, и каждый заход стоит денег ради того же результата.
            if partials >= 1 or not commits:
                text = (f"{AGENT_MARK} **Не укладываюсь в отведённый бюджет.** {cut} "
                        f"({format_meta(meta)}).\n\n"
                        + ("Успел закоммитить часть — она в ветке `" + branch + "`.\n\n"
                           if commits else "Закоммитить ничего не успел.\n\n")
                        + "Дальше сам не вытяну: либо разбей задачу на части поменьше, "
                        + "либо подними `agent.max_budget_usd` в конфиге фабрики.")
                kaiten.comment(card_id, text)
                hand_over(kaiten, profile, card_id, "question")
                finish_status(card_id, title, "не влез в бюджет", meta)
                log(f"  -> {cut}, второй раз подряд — отдаю человеку")
                return

            kaiten.comment(card_id,
                           f"{AGENT_MARK} **{PARTIAL_MARK}.** {cut} "
                           f"({format_meta(meta)}).\n\nСделанное закоммичено в ветку "
                           f"`{branch}` — следующим прогоном продолжу оттуда, "
                           f"начинать заново не буду.")
            finish_status(card_id, title, "прервался, продолжу", meta)
            log(f"  -> {cut}, работа сохранена в {branch}, продолжу следующим прогоном")
            return

        # Нет коммитов — обсуждать нечего, что бы агент ни написал.
        if not commits:
            if status not in ("unclear", "blocked"):
                verdict["status"] = "unclear"
                verdict.setdefault("questions", [])
                verdict["summary"] = (
                    "Ничего не поменял и сам не объяснил почему.\n\n"
                    + verdict.get("summary", "")
                )
            kaiten.comment(card_id, comment_question(verdict, meta))
            hand_over(kaiten, profile, card_id, "question")
            finish_status(card_id, title, "вопрос", meta)
            log("  -> Вопрос от агента (изменений нет)")
            return

        if status in ("unclear", "blocked"):
            kaiten.comment(
                card_id,
                comment_question(verdict, meta)
                + f"\n\n_Незапушенные наработки остались в локальной ветке `{branch}`._",
            )
            hand_over(kaiten, profile, card_id, "question")
            finish_status(card_id, title, "вопрос", meta)
            log(f"  -> Вопрос от агента (status={status}, коммиты не пушим)")
            return

        if not branch.startswith(branch_prefixes(cfg)):
            raise FactoryError(f"отказываюсь пушить ветку вне префикса: {branch}")

        write_status(phase="пушу ветку и открываю PR")
        if args.dry_run:
            log(f"  [dry-run] git push -u {repo_cfg['remote']} {branch}")
        else:
            git(worktree, "push", "--force-with-lease", "-u", repo_cfg["remote"], branch)

        if args.no_pr:
            pr_url = "(PR не создавался, --no-pr)"
        else:
            pr_url = open_pr(worktree, branch, repo_cfg["base_branch"], card, card_url,
                             verdict, cfg, args.dry_run, repo_key)

        epic_note = ""
        if wants_debt(profile, card):
            write_status(phase="привязываю к долгу спринта")
            epic_note = attach_to_debt_card(kaiten, cfg, card_id, args.dry_run)

        if pr_url.startswith("http") and mock_backend_warning(card) and not args.dry_run:
            post_pr_review(worktree, pr_url, MOCK_BACKEND_NOTE)

        # Ревьюили в GitHub — там же и ответ, после пуша: ссылаться на правку,
        # которой ещё нет в PR, нельзя
        if feedback and pr_url.startswith("http"):
            write_status(phase="отвечаю на замечания в PR")
            answered = post_pr_replies(worktree, pr_url, feedback,
                                       verdict.get("pr_replies") or [], args.dry_run,
                                       load_env().get("GITHUB_REPLY_TOKEN", ""))
            log(f"  ответил в PR на {answered} из {len(feedback)} замечаний")

        text = comment_success(verdict, meta, pr_url, branch)
        # Обе приписки — без значка в начале строки. Со значком они выглядели для
        # open_questions пунктом списка, и «не отчитался о проверках» уезжало человеку
        # в тред наравне с вопросами, хотя вопросом никогда не было.
        if dirty:
            text += ("\n\n**В рабочей копии остались незакоммиченные изменения** — "
                     "в PR они не поехали.")
        if not (verdict.get("checks") or []):
            text += ("\n\n**Проверок я не запускал** — ни тестов, ни линтера. "
                     "Смотреть этот PR стоит внимательнее обычного.")
        text += epic_note
        kaiten.comment(card_id, text)
        report_to_inbox(kaiten, card, pr_url)
        kaiten.move(card_id, cols["agent_review"])
        finish_status(card_id, title, "на ревью агента", meta, pr_url)
        log(f"  -> Ревью агента: {pr_url}")

    except AgentAuthError:
        # Единственная поломка, за которую карточку наказывать нельзя: агент не
        # начинался. Уроним прогон целиком — со следующей карточкой будет то же
        # самое, а «Упало» на ровном месте потом разбирать руками.
        raise
    except Exception as e:  # noqa: BLE001 — карточка не должна застревать в «В работе»
        log(f"  !! {e}")
        if args.prompt_only:
            return
        kaiten.comment(card_id, comment_failed(str(e)))
        hand_over(kaiten, profile, card_id, "failed", str(e)[:120])
        finish_status(card_id, title, "упало", None, detail=str(e)[:200])
        log("  -> Упало")
    finally:
        if args.keep_worktree or cfg.get("keep_worktree"):
            log(f"  worktree оставлен: {worktree}")
        else:
            drop_worktree(repo, worktree)


# --------------------------------------------------------------------------- #
# разведка инбокса
# --------------------------------------------------------------------------- #

TRIAGE_ROUND_NOTE = """
> 🧭 **Ты уже смотрел эту карточку и просил уточнений.** Твоя прошлая разведка — в
> комментариях выше, ответ человека — последним. Проверь, снялась ли неопределённость:
> снялась — давай вердикт `ready`, не снялась — спроси точнее, но не то же самое.
"""


def load_triage_state() -> dict:
    if TRIAGE_STATE_FILE.is_file():
        try:
            return json.loads(TRIAGE_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"cards": {}}


def save_triage_state(state: dict) -> None:
    """
    Счётчик неудач разведки. В сухом прогоне — тоже ничего не пишем.

    Тот же уговор, что и в `save_time_state`, и цена ошибки здесь выше: дойдя до
    предела, счётчик ставит `told` — «человеку про это сказали». Сказать должен
    комментарий в карточке, а в сухом прогоне он не уходит. Карточка после такого
    навсегда выпадает из выборки `pick_inbox_cards`, и никто про неё не узнаёт.
    """
    if DRY_RUN:
        return
    STATE.mkdir(exist_ok=True)
    tmp = TRIAGE_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(TRIAGE_STATE_FILE)


def note_triage_fail(state: dict, card_id: int, error: str, max_fails: int) -> bool:
    """
    Копим неудачи по карточке. Разведка ходит по расписанию каждые несколько минут,
    и карточка, на которой агент стабильно падает, иначе жгла бы деньги на каждом тике.

    Возвращает True ровно один раз — на том прогоне, где счётчик дошёл до предела.
    Это последняя возможность сказать человеку хоть что-то: дальше pick_inbox_cards
    молча выкидывает карточку из выборки, и она навсегда зависает в «Инбоксе».
    """
    cards = state.setdefault("cards", {})
    entry = cards.setdefault(str(card_id), {"fails": 0})
    entry["fails"] = entry.get("fails", 0) + 1
    entry["error"] = error[:300]
    entry["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    give_up = entry["fails"] >= max_fails and not entry.get("told")
    if give_up:
        entry["told"] = True
    save_triage_state(state)
    return give_up


def forget_triage_fail(state: dict, card_id: int) -> None:
    if (state.get("cards") or {}).pop(str(card_id), None) is not None:
        save_triage_state(state)


def count_triage_rounds(comments: list) -> int:
    return sum(1 for c in comments
               if strip_html(c.get("text", "")).startswith(TRIAGE_MARK)
               and "продолжение" not in strip_html(c.get("text", ""))[:40])


def pick_inbox_cards(kaiten: Kaiten, cfg: dict, state: dict) -> list[tuple[dict, int]]:
    """
    Кого разбираем в «Инбоксе»:
      1. карточки без разведки — их только что закинули, ответа и ждут;
      2. карточки, где после разведки написал человек: данные появились, вердикт мог
         измениться.

    Свои комментарии узнаём по метке: и агент, и человек пишут под одним токеном,
    по автору их не различить.
    """
    inbox = cfg["inbox"]
    fresh, answered, blocked, quiet = [], [], 0, 0
    max_rounds = inbox.get("max_rounds", 2)
    max_fails = inbox.get("max_fails", 2)

    for card in kaiten.cards_in_column(inbox["board_id"], inbox["column_id"]):
        card_id = card["id"]
        if (state.get("cards") or {}).get(str(card_id), {}).get("fails", 0) >= max_fails:
            blocked += 1
            continue
        comments = kaiten.comments(card_id)
        stop = hands_off(card, comments, cfg)
        if stop:
            log(f"#{card_id} не трогаю: в карточке «{stop}»")
            quiet += 1
            continue
        if off_hours(card, cfg):
            quiet += 1
            continue
        texts = [strip_html(c.get("text", "")) for c in comments]
        # задача уже стоит на рабочей доске — разведке тут больше делать нечего,
        # что бы дальше ни писали в переписке
        if any(t.startswith(TRIAGE_MARK) and any(line in t for line in HANDOFF_LINES)
               for t in texts):
            continue
        rounds = count_triage_rounds(comments)
        if not rounds:
            fresh.append((card, 1))
            continue
        if rounds >= max_rounds:
            continue
        if texts and not texts[-1].startswith(AGENT_MARKS):
            answered.append((card, rounds + 1))

    if blocked:
        log(f"{blocked} карточек инбокса пропущены: разведка по ним падала")
    if quiet:
        log(f"{quiet} карточек инбокса просили не трогать")
    # свежие вперёд и самые новые первыми: человек закинул карточку минуту назад
    fresh.sort(key=lambda pair: pair[0].get("created") or "", reverse=True)
    return fresh + answered


def ensure_scout_worktree(repo: Path, repo_cfg: dict, key: str) -> Path:
    """
    Рабочая копия для разведки: одна на все карточки и живёт между прогонами.

    Агент в ней только читает, так что делить её безопасно. Пересоздавать каждый прогон
    дорого: разведка ходит раз в несколько минут, а чекаут — девять тысяч файлов.
    Поэтому существующую копию просто подтягиваем к базовой ветке.
    """
    path = WORKTREES / f"scout-{key}"
    branch = f"stepa/scout-{key}"
    remote, base = repo_cfg["remote"], repo_cfg["base_branch"]
    git(repo, "fetch", remote, base)
    if (path / ".git").exists():
        # checkout, а не reset: копия, заведённая до переезда на `stepa/`, стоит
        # на старой ветке и так на неё и переезжает
        git(path, "checkout", "-f", "-B", branch, f"{remote}/{base}")
        git(path, "clean", "-fd", check=False)
        return path
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    git(repo, "worktree", "prune", check=False)
    git(repo, "worktree", "add", "-B", branch, str(path), f"{remote}/{base}")
    return path


def work_card_description(card: dict, comments: list, verdict: dict, card_url: str) -> str:
    """
    Описание рабочей карточки: что было в инбоксе плюс разведка.

    Исполнитель видит только рабочую карточку, а в инбоксе половина смысла живёт в
    комментариях — поэтому переносим их сюда, а не оставляем на той доске.
    """
    author = (card.get("owner") or {}).get("full_name") or "неизвестно кто"
    parts = [f"{INBOX_ORIGIN} #{card['id']} — {card_url} (автор: {author})"]

    original = strip_html(card.get("description"))
    if original:
        parts.append(f"**Как описали в инбоксе**\n\n{original}")

    human = [c for c in comments
             if not strip_html(c.get("text", "")).startswith(AGENT_MARKS)]
    if human:
        parts.append("**Что писали в комментариях**\n\n" + format_comments(human))

    # Места в коде сюда не идут: описание читают люди, а исполнитель возьмёт их
    # из лога разведки (`scout_hints`) — там же, где они и лежат целиком
    scout = [f"**Суть:** {verdict.get('problem', '')}",
             format_list("Как чинить", verdict.get("plan")).strip()]
    if verdict.get("risk"):
        scout.append(f"**Проверь:** {verdict['risk']}")
    parts.append("\n\n".join(block for block in scout if block))
    return "\n\n".join(parts)


def scout_hints(card: dict) -> str:
    """
    Где разведка нашла задачу в коде — для исполнителя, раз в описании этого больше нет.

    Карточку из инбокса узнаём по строке «Из инбокса: #<id>», разведку — по логу этой
    карточки инбокса. Нет лога (разведку делали на другой машине) — пусто, исполнитель
    найдёт сам.
    """
    found = INBOX_ORIGIN_RE.search(strip_html(card.get("description")) or "")
    if not found:
        return ""
    where = (latest_log(f"triage-{found.group(1)}").get("verdict") or {}).get("where") or []
    if not where:
        return ""
    return ("\n\n> 🧭 **Где разведка нашла это в коде** — подсказка, а не приговор: "
            "если в коде другое, верь коду.\n"
            + "\n".join(f"> - {item}" for item in where if str(item).strip()))


def handoff_profile(cfg: dict, product: str | None = None) -> dict:
    """
    Куда разведка ставит разобранную задачу.

    Стёпа ходит в инбокс и, если задача понятна, заводит её там же, где живёт работа
    команды, — на доске сабтасок, с тегом «это моё». Так у него одно рабочее место,
    а не собственная вселенная рядом с командной доской.

    Своя доска остаётся запасным вариантом: она нужна тем, у кого режима эпиков нет
    вовсе, и тому, что кладут на неё руками.

    С продуктами доску выбирает разведка: какой продукт она назвала, на ту доску
    задача и ляжет.
    """
    if destinations(cfg):
        return profile_for_product(cfg, product)
    target = ((cfg.get("inbox") or {}).get("target") or "subtasks").strip()
    flow = epic_flow(cfg)
    if target == "subtasks" and flow:
        # доска сабтасок может быть и рабочей: тогда берём рабочий профиль, чтобы
        # в логах и на витрине одна доска не называлась двумя именами
        board = flow["subtasks"]["board_id"]
        return profile_for_board(board_profiles(cfg), board) or subtask_profile(cfg, flow)
    return board_profiles(cfg)[0]


def find_work_card(kaiten: Kaiten, cfg: dict, inbox_card_id: int) -> dict | None:
    """
    Уже поставленная задача по этой карточке инбокса: ищем метку в описании.

    Смотрим обе доски — и ту, куда ставим сейчас, и свою: задачи, поставленные
    до переезда на доску команды, никуда не делись, и ставить их второй раз нельзя.
    """
    if destinations(cfg):
        # продуктовые доски, включая ту, с которой команда уехала: задача, поставленная
        # до переезда, никуда не делась, и ставить её второй раз нельзя
        boards = {profile["board_id"] for profile in board_profiles(cfg)}
    else:
        boards = {handoff_profile(cfg)["board_id"], cfg["kaiten"]["board_id"]}
    for board_id in boards:
        for candidate in kaiten.cards_on_board(board_id, with_description=True):
            found = INBOX_ORIGIN_RE.search(strip_html(candidate.get("description")))
            if found and int(found.group(1)) == inbox_card_id:
                return candidate
    return None


def link_as_child(kaiten: Kaiten, parent_id: int, child_id: int) -> str:
    """
    Вешает рабочую карточку дочерней на карточку инбокса: в Kaiten это единственная связь,
    которую видно на самой карточке, ссылки в описании там нет.

    Второму родителю это не мешает: долг спринта добавится позже, и карточка спокойно
    висит на двух — проверено, `parents_count` становится 2.
    """
    try:
        if any(child.get("id") == child_id for child in kaiten.children(parent_id)):
            return ""
        kaiten.add_child(parent_id, child_id)
        log(f"  привязал дочерней к #{parent_id}")
        return ""
    except Exception as e:  # noqa: BLE001 — задача уже поставлена, связь не важнее
        log(f"  !! не смог привязать дочерней к #{parent_id}: {e}")
        return f" (дочерней связать не смог: {str(e)[:120]})"


def hand_off_to_factory(kaiten: Kaiten, cfg: dict, card: dict, comments: list,
                        verdict: dict, card_url: str, dry_run: bool) -> str:
    """
    Ставит задачу в «Очередь» рабочей доски и возвращает строку для комментария.

    Ошибку не поднимает: разведка уже сделана, и её вывод человек должен увидеть,
    даже если постановка не удалась.
    """
    try:
        existing = find_work_card(kaiten, cfg, card["id"])
        if existing:
            log(f"  задача уже стоит: #{existing['id']}")
            note = link_as_child(kaiten, card["id"], existing["id"])
            return f"Задача по этой карточке уже стоит: {kaiten.card_url(existing)}{note}"

        description = work_card_description(card, comments, verdict, card_url)
        product = ""
        if destinations(cfg):
            product, repo = pick_product_repo(cfg, verdict.get("product") or "",
                                              verdict.get("repo"))
            line = repo_line(cfg, product, repo)
            if line:
                description += f"\n\n{line}"
        profile = handoff_profile(cfg, product)
        body = {
            "board_id": profile["board_id"],
            "column_id": role_column(profile, "queue"),
            "title": (card.get("title") or "").strip() or f"Задача из инбокса #{card['id']}",
            "description": description,
        }
        if profile.get("lane_id"):
            body["lane_id"] = profile["lane_id"]
        if profile.get("card_type_id"):
            body["type_id"] = profile["card_type_id"]

        if dry_run:
            log(f"  [dry-run] задачу в «Очередь» доски «{profile['key']}» не ставлю")
            return f"Поставил бы задачу на доску «{profile['key']}» (dry-run)."

        created = kaiten.create_card(body)
        if not created:
            raise FactoryError("Kaiten не вернул созданную карточку")
        # Тег обязателен: на доске команды без него исполнитель пройдёт мимо задачи,
        # которую сам же и завёл. Поэтому и не глотаем ошибку молча — без тега
        # карточка повиснет в чужом бэклоге ничьей.
        try:
            kaiten.add_tag(created["id"], bot_tag(cfg))
        except Exception as e:  # noqa: BLE001 — задача уже поставлена, снимать её поздно
            log(f"  !! тег на задачу не встал: {e}")
            kaiten.comment(created["id"],
                           f"{TRIAGE_MARK} Тег `{bot_tag(cfg)}` поставить не смог — "
                           f"повесь руками, иначе я эту задачу не увижу.")
        log(f"  задача поставлена: #{created['id']} в «Очередь» ({profile['key']})")
        note = link_as_child(kaiten, card["id"], created["id"])
        where = f" (доска «{profile['key']}»)" if destinations(cfg) else ""
        return f"{HANDOFF_LINE} #{created['id']} — {kaiten.card_url(created)}{where}{note}"
    except Exception as e:  # noqa: BLE001 — постановка не важнее уже сделанной разведки
        log(f"  !! не смог поставить задачу: {e}")
        return f"⚠️ Не смог поставить задачу на доску: {str(e)[:200]}"


def report_to_inbox(kaiten: Kaiten, card: dict, pr_url: str) -> None:
    """
    Рабочая карточка выросла из инбокса — сообщаем туда про PR. Человек, который закинул
    карточку, по фабричной доске не ходит и иначе про исход не узнает.
    """
    found = INBOX_ORIGIN_RE.search(strip_html(card.get("description")))
    if not found:
        return
    origin = int(found.group(1))
    try:
        kaiten.comment(origin, f"{TRIAGE_MARK} **По этой карточке открыт PR:** {pr_url}\n\n"
                               f"Рабочая карточка: {kaiten.card_url(card)}")
        log(f"  отчитался в карточку инбокса #{origin}")
    except Exception as e:  # noqa: BLE001 — PR уже создан, отчёт не важнее
        log(f"  !! не смог отчитаться в инбокс #{origin}: {e}")


# Поля ответа разведки, когда у команды несколько продуктов. Вставляются в описание
# формата ответа рядом с остальными полями.
TRIAGE_PRODUCT_FIELDS = """- `product` — на доску какого продукта ставить задачу, из списка продуктов выше. Выбирай
  по тому, где живёт код, который придётся менять, а не по словам в заголовке.
- `repo` — в каком репозитории исполнитель будет писать код. Задача, которой нужны
  два репозитория (фронт и бек), — всё равно один `repo`: тот, где основная работа,
  а про вторую часть напиши в `plan`. Оба поля заполняй при любом вердикте."""


def build_triage_prompt(card: dict, comments: list, card_url: str, repo_key: str,
                        repo_cfg: dict, round_number: int, cfg: dict | None = None,
                        extra_dirs: list[tuple[str, Path]] | None = None) -> str:
    template = TRIAGE_TEMPLATE_PATH.read_text(encoding="utf-8")
    many = bool(cfg and destinations(cfg))
    replacements = {
        "{{CARD_ID}}": str(card["id"]),
        "{{CARD_URL}}": card_url,
        "{{TITLE}}": card.get("title") or "(без заголовка)",
        "{{DESCRIPTION}}": strip_html(card.get("description")) or "(описания нет)",
        "{{COMMENTS}}": format_comments(comments),
        "{{CHECKLISTS}}": format_checklists(card),
        "{{REPO}}": repo_key,
        "{{BASE_BRANCH}}": repo_cfg["base_branch"],
        "{{REMOTE}}": repo_cfg["remote"],
        "{{ROUND_NOTE}}": TRIAGE_ROUND_NOTE if round_number > 1 else "",
        "{{REPOS}}": repos_note(cfg, repo_key, extra_dirs or []) if many else "",
        "{{PRODUCT_FIELDS}}": TRIAGE_PRODUCT_FIELDS if many else "",
        **project_replacements(repo_cfg.get("key")),
    }
    return apply_template(template, replacements)


def comment_triage(verdict: dict, meta: dict, round_number: int, handoff: str = "") -> str:
    status = verdict.get("status")
    text = f"{TRIAGE_MARK} **Разведка: {TRIAGE_HEAD[status]}.**\n\n{verdict.get('problem', '')}"
    text += format_list("Что нужно уточнить", verdict.get("questions"))
    text += format_list("Как чинить" if status == "ready" else "Гипотеза", verdict.get("plan"))
    if verdict.get("risk"):
        text += f"\n\n**Проверь:** {verdict['risk']}"

    if status == "ready":
        text += f"\n\n{handoff}" if handoff else (
            "\n\nЕсли согласен — заведи задачу и повесь на неё мой тег, "
            "я возьму её сам.")
    elif status == "needs_info":
        text += "\n\nОтветь комментарием здесь — я вернусь и пересмотрю."

    tail = [bit for bit in (EFFORT_LABEL.get(verdict.get("effort") or ""),) if bit]
    line = "разведка" + (f", круг {round_number}" if round_number > 1 else "")
    if tail:
        line += ": " + ", ".join(tail)
    text += f"\n\n_{line}. Код не менял._"
    return text


def comment_triage_budget(meta: dict) -> str:
    """
    Агент упёрся в `max_budget_usd` и не дошёл до вердикта. Молчать нельзя: карточка
    после этого из выборки выпадает, и человек так и не узнает, что её смотрели.
    """
    spent = format_meta(meta)
    return (f"{TRIAGE_MARK} **Разведка не уложилась в бюджет.**\n\n"
            f"Карточка оказалась глубже, чем автоматическая разведка: агент искал "
            f"{spent or 'до упора'} и до вывода не дошёл.\n\n"
            f"Дальше нужен человек. Помогает любая зацепка в карточке: на каком экране "
            f"это видно, как называется кнопка или строка, ссылка на код или на PR.")


def comment_triage_stuck(fails: int, error: str) -> str:
    """
    Разведка сорвалась столько раз подряд, что карточка выпала из выборки. Молчать
    нельзя ровно по той же причине, что и с бюджетом: человек ждёт разбора, а фабрика
    эту карточку уже вычеркнула — и без комментария он об этом никогда не узнает.
    """
    return (f"{TRIAGE_MARK} **Разведка сорвалась и больше не берётся.**\n\n"
            f"Попыток: {fails}, все впустую. Последняя ошибка: {error}\n\n"
            f"Дальше нужен человек. Карточка из автоматической разведки выпала и сама "
            f"туда не вернётся: если причина ушла, скажи — я зайду ещё раз.")


def triage_card(card_stub: dict, kaiten: Kaiten, cfg: dict, args,
                scouts: dict[str, Path], round_number: int,
                neighbours: list[tuple[str, Path]] | None = None) -> None:
    card_id = card_stub["id"]
    # в выдаче по колонке нет чек-листов, поэтому берём карточку целиком
    card = kaiten.card(card_id)
    card_url = kaiten.card_url(card)
    title = (card.get("title") or "").strip()
    comments = kaiten.comments(card_id)
    stop = hands_off(card, comments, cfg)
    if stop:
        log(f"#{card_id} не трогаю: в карточке «{stop}»")
        return
    wait = off_hours(card, cfg)
    if wait:
        log(f"#{card_id} откладываю: {wait}")
        return
    many = bool(destinations(cfg))
    repo_key, repo_cfg = resolve_repo(
        card, cfg, (product_repos(cfg, default_product(cfg)) or [None])[0] if many else None)

    round_note = f", круг {round_number}" if round_number > 1 else ""
    log(f"#{card_id} «{title[:60]}» — разведка{round_note}")

    # Инбокс общий на все продукты, и чей он, разведка узнаёт только посмотрев в код
    extra_dirs = [(key, path) for key, path in neighbours or [] if key != repo_key]

    prompt = build_triage_prompt(card, comments, card_url, repo_key, repo_cfg, round_number,
                                 cfg, extra_dirs)
    if args.prompt_only:
        print("\n" + "=" * 78)
        print(prompt)
        print("=" * 78 + "\n")
        log("  промпт разведки показан, агент не запускался, карточка не тронута")
        return

    write_status(card={"id": card_id, "title": title, "url": card_url},
                 phase="разведка инбокса", returning=False)

    if repo_key not in scouts:
        repo = Path(repo_cfg["path"]).expanduser()
        if not (repo / ".git").exists():
            raise ScoutSetupError(f"репозиторий не найден: {repo}")
        write_status(phase="готовлю копию для разведки")
        try:
            scouts[repo_key] = ensure_scout_worktree(repo, repo_cfg, repo_key)
        except FactoryError as e:
            raise ScoutSetupError(str(e)) from e
    worktree = scouts[repo_key]

    write_status(phase="разведка инбокса")
    verdict, meta = run_agent(prompt, worktree, cfg["triager"],
                              schema=with_products(TRIAGE_SCHEMA, cfg, required=False),
                              add_dirs=[path for _, path in extra_dirs])
    note_spend(meta)

    LOGS.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    (LOGS / f"triage-{card_id}-{stamp}.json").write_text(
        json.dumps({"verdict": verdict, "meta": meta}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    status = verdict.get("status")
    if status not in TRIAGE_HEAD:
        if meta.get("subtype") == "error_max_budget_usd":
            log(f"  разведка не уложилась в бюджет, {format_meta(meta)}")
            kaiten.comment(card_id, comment_triage_budget(meta))
            finish_status(card_id, title, "разведка: не уложилась в бюджет", meta,
                          card_url=card_url, kind="triage")
            return
        # структурированный ответ не доехал. В инбоксе сидят живые коллеги — писать им
        # «агент упал» незачем, просто считаем неудачу и молчим
        raise FactoryError(f"агент не вернул вердикт (status={status!r})")

    log(f"  вердикт: {status}, {format_meta(meta)}")

    # «данных хватает» — это не рекомендация: ставим задачу на рабочую доску, дальше её
    # подхватит исполнитель обычным порядком и сам привяжет к долгу спринта
    handoff = ""
    if status == "ready" and (cfg.get("inbox") or {}).get("create_cards", True):
        write_status(phase="ставлю задачу на доску")
        handoff = hand_off_to_factory(kaiten, cfg, card, comments, verdict,
                                      card_url, args.dry_run)

    body = comment_triage(verdict, meta, round_number, handoff)
    if args.dry_run:
        print("\n" + body + "\n")
    kaiten.comment(card_id, body)
    finish_status(card_id, title, f"разведка: {TRIAGE_HEAD[status]}", meta,
                  card_url=card_url, kind="triage")


def triage_inbox(kaiten: Kaiten, cfg: dict, args, only_card: int | None = None) -> int:
    """
    Фаза разведки: пройти по «Инбоксу» и отписаться в карточках. Ничего не двигаем,
    ничего не правим — это чужая доска, там сидит вся команда.
    """
    inbox = cfg.get("inbox") or {}
    if not (inbox.get("board_id") and inbox.get("column_id") and cfg.get("triager")):
        log("инбокс не настроен (нужны секции inbox и triager) — разведку пропускаю")
        return 0

    state = load_triage_state()
    max_fails = inbox.get("max_fails", 2)
    if only_card:
        rounds = count_triage_rounds(kaiten.comments(only_card)) + 1
        targets, pending = [({"id": only_card}, rounds)], 1
    else:
        targets = pick_inbox_cards(kaiten, cfg, state)
        pending = len(targets)
        if not targets:
            log("в инбоксе разбирать нечего")
            if not args.prompt_only:
                write_status(inbox_pending=0)
            return 0
        limit = args.limit if args.limit is not None else inbox.get("max_cards_per_run", 3)
        log(f"в инбоксе {pending} карточек на разведку, беру {min(limit, pending)}")
        targets = targets[:limit]

    if not args.prompt_only:
        write_status(inbox_pending=pending)

    scouts: dict[str, Path] = {}
    # Соседние репозитории — раз на прогон, а не на карточку: git fetch шести
    # репозиториев на каждую карточку — лишняя минута, а копии от этого не свежее
    neighbours: list[tuple[str, Path]] = []
    if destinations(cfg) and targets:
        if not args.prompt_only:
            write_status(phase="готовлю копии для разведки")
        neighbours = scout_dirs(cfg)
        # своя копия разведки — та же, что у соседей: второй раз её не подтягиваем
        scouts.update(neighbours)
    done = 0
    for card, round_number in targets:
        try:
            triage_card(card, kaiten, cfg, args, scouts, round_number, neighbours)
            forget_triage_fail(state, card["id"])
            done += 1
        except ScoutSetupError as e:
            # с остальными карточками будет то же самое, и карточки в этом не виноваты:
            # ни в счётчик неудач, ни в комментарий это идти не должно
            log(f"  !! разведка невозможна: {e}")
            break
        except AgentAuthError as e:
            # То же самое, но чинить это человеку — и остальным фазам прогона тоже
            # ничего не светит: везде упрёмся в тот же отказ. Роняем прогон целиком.
            log(f"  !! разведка невозможна: {e}")
            raise
        except Exception as e:  # noqa: BLE001 — одна карточка не должна валить прогон
            log(f"  !! #{card['id']} разведка не удалась: {e}")
            if note_triage_fail(state, card["id"], str(e), max_fails):
                log("  предел неудач — больше эту карточку не беру, говорю об этом в ней")
                try:
                    kaiten.comment(card["id"], comment_triage_stuck(max_fails, str(e)))
                except Exception as mute:  # noqa: BLE001 — сказать не вышло, прогон живёт
                    log(f"  !! и в карточку это не записалось: {mute}")

    if not args.prompt_only:
        write_status(card=None, phase=None, agent=None, inbox_pending=max(0, pending - done))
    return 0


# --------------------------------------------------------------------------- #
# режим эпиков: АЦ -> спека -> сабтаски -> ревью
# --------------------------------------------------------------------------- #

# Как называется чек-лист приёмочных критериев. По имени же его и находим обратно:
# другого признака «АЦ уже написаны» у нас нет
ACCEPTANCE_LIST = "Приёмочные критерии"

# Строки-маркеры в комментариях эпика. Состояние пайплайна выводится только из Kaiten:
# прогон идёт раз в час, человек между прогонами двигает карточки руками, и любое
# состояние, которое фабрика держала бы у себя, разъехалось бы с доской в первый же раз.
SPEC_LINE = "Спека:"
SPEC_FIXED_LINE = "Спека поправлена:"
SPEC_OK_LINE = "Спека отревьюена."

# Оба заголовка значат «спека лежит на гите и её можно ревьюить». Держим их одним
# списком: иначе после круга правок фаза не сдвигалась и агент переписывал спеку
# каждый прогон — рассинхрон маркеров второй раз стоил денег.
SPEC_HEADS = (SPEC_LINE, SPEC_FIXED_LINE)
SUBTASKS_LINE = "Разложил на сабтаски:"

# Строка в описании сабтаски: из какого эпика она выросла. По ней же считаются свои
# дети — у эпика могут висеть и карточки, которые подвесил человек, и ждать их закрытия
# фабрика не должна.
EPIC_ORIGIN = "Из эпика:"
EPIC_ORIGIN_RE = re.compile(r"Из эпика:\s*#(\d+)")

# Фазы. Порядок важен: фаза выводится первым совпавшим условием
EPIC_PHASES = ("acceptance", "waiting_answer", "spec", "spec_review", "spec_fix",
               "spec_settle", "decompose", "working", "closing")

# Где эпик стоит прямо сейчас — положение, а не действие. Раньше в меню-баре под
# заголовком «следующий шаг» показывались метки из EPIC_PHASE_LABELS, но это работа,
# которую фабрика собирается сделать, а не состояние: «сабтаски в работе» как
# «следующий шаг» читается бессмыслицей.
EPIC_PHASE_STATE = {
    "acceptance": "приёмочных критериев ещё нет",
    "waiting_answer": "ждёт твоего ответа",
    "spec": "критерии готовы, спеки нет",
    "spec_review": "спека написана, не отревьюена",
    "spec_fix": "у спеки есть замечания",
    "spec_settle": "круги ревью исчерпаны",
    "decompose": "спека принята, сабтасок нет",
    "working": "сабтаски в работе",
    "closing": "все сабтаски отревьюены",
    "unreadable": "часть сабтасок не прочиталась",
}

# Что фабрика сделает следующим шагом. Это и есть «дальше», в отличие от положения выше.
EPIC_PHASE_NEXT = {
    "acceptance": "напишет приёмочные критерии",
    "waiting_answer": "ждёт тебя — ничего не сделает",
    "spec": "напишет спеку",
    "spec_review": "отревьюит спеку",
    "spec_fix": "поправит спеку по замечаниям",
    "spec_settle": "примет спеку как есть и разложит на сабтаски",
    "decompose": "разложит на сабтаски",
    "working": "ждёт, пока сабтаски пройдут ревью",
    "closing": "закроет эпик",
    "unreadable": "подождёт до следующего прогона — Kaiten не отдал сабтаски",
}

# Формулировки нарочно безличные: фаза висит в трее и когда прогон не идёт, а
# «правлю спеку» в этот момент врёт — человек видит работу, которой нет.
EPIC_PHASE_LABELS = {
    "acceptance": "написать приёмочные критерии",
    "waiting_answer": "ждёт твоего ответа",
    "spec": "написать спеку",
    "spec_review": "отревьюить спеку",
    "spec_fix": "поправить спеку по замечаниям",
    "spec_settle": "принять спеку как есть",
    "decompose": "разложить на сабтаски",
    "working": "сабтаски в работе",
    "closing": "закрыть эпик",
    "unreadable": "сабтаски не прочитались",
}


def epic_flow(cfg: dict) -> dict | None:
    """
    Настройки режима эпиков. Нет секции — режима нет, как с инбоксом и долгом.

    Тег подставляем здесь, один раз: он тот же, что и на обычных карточках, — у
    разработчика не бывает отдельного имени для эпиков.
    """
    flow = cfg.get("epic_flow") or {}
    # сабтаскам нужна доска: либо одна на всё, либо по доске на продукт
    if not (flow.get("boards") and (flow.get("subtasks", {}).get("board_id")
                                    or destinations(cfg))):
        return None
    return {**flow, "tag": flow.get("tag") or bot_tag(cfg)}


def column_order(kaiten: Kaiten, board_id: int) -> list[int]:
    """
    Колонки доски слева направо, одним списком id.

    По этому порядку эпик и двигается: целевая колонка описана в процессе как
    «следующая после разработки», а не задана жёстко. Подколонки идут внутри своего
    родителя, и только когда сосед справа кончился — начинается следующая верхняя
    колонка; так их раскладывает flat_columns.
    """
    return [int(c["id"]) for c in flat_columns(kaiten.board(board_id))]


def epic_window(kaiten: Kaiten, flow: dict, board_id: int) -> tuple[int, int]:
    """
    Колонки, в которых фабрике позволено трогать эпик: «готов к разработке»
    и «в разработке». Ноль на месте любой — окна нет, эпики на этой доске не наши.

    Правее разработки эпик уже не её дело: там его смотрит человек, катится
    эксперимент, считается результат. Окна не было вовсе — эпик выбирался по одному
    тегу, и `take_epic` тащил его из «Rollout» обратно в разработку. Один такой
    возврат стоил $6.79: декомпозиция по второму кругу плюс прогон дубля-сабтаски.

    Колонку «готов к разработке» можно задать явно (`ready_column_id`), иначе это
    соседняя слева от разработки — так же, как колонка ревью берётся соседней справа.
    """
    dev = int(flow.get("development_column_id") or 0)
    if not dev:
        return 0, 0
    ready = int(flow.get("ready_column_id") or 0)
    if ready:
        return ready, dev
    order = column_order(kaiten, board_id)
    if dev not in order:
        return 0, 0
    position = order.index(dev) - 1
    return (order[position] if position >= 0 else 0), dev


def in_epic_window(kaiten: Kaiten, flow: dict, card: dict,
                   window: tuple[int, int] | None = None) -> bool:
    """Стоит ли эпик там, где фабрика имеет право его трогать."""
    ready, dev = window if window is not None else epic_window(
        kaiten, flow, card.get("board_id"))
    return card.get("column_id") in {column for column in (ready, dev) if column}


def acceptance_items(card: dict) -> list[dict]:
    """Пункты чек-листа приёмочных критериев. Пусто — значит АЦ ещё не писали."""
    for checklist in card.get("checklists") or []:
        if normalize_phrase(checklist.get("name")) == normalize_phrase(ACCEPTANCE_LIST):
            return checklist.get("items") or []
    return []


def said(comments: list, line: str) -> bool:
    """Говорил ли эпик-агент такое раньше. Человеческие реплики не считаются."""
    for comment in comments:
        text = strip_html(comment.get("text", ""))
        if text.startswith(EPIC_MARK) and line in text:
            return True
    return False


def own_blocker(kaiten: Kaiten, card_id: int, kind: str) -> dict | None:
    """Наш действующий блокер нужного вида. Снят — значит человек дал ход дальше."""
    reason = block_reason(kind)
    for blocker in kaiten.blockers(card_id):
        if str(blocker.get("reason") or "").startswith(reason):
            return blocker
    return None


def epic_subtasks(kaiten: Kaiten, epic_id: int, children: list) -> tuple[list, int]:
    """
    Свои сабтаски и число тех, которых не удалось прочитать.

    Свои — те, где в описании стоит «Из эпика: #<id>». У эпика могут висеть и карточки,
    подвешенные человеком. Если считать всех детей, эпик не закроется никогда —
    человек про свою карточку просто забудет.

    Нечитаемых считаем отдельно, и это важнее, чем кажется: пока их молча считали
    чужими, один оборванный ответ Kaiten («invalid continuation byte» на середине
    карточки) превращался в «сабтасок нет» — эпик раскладывался заново и заводил дубль.
    """
    own, unreadable = [], 0
    for child in children:
        description = strip_html(child.get("description")) or ""
        if not description and child.get("description_filled"):
            # `GET /cards/{id}/children` описание не отдаёт вовсе — только флаг
            # description_filled. Без дозапроса эпик не узнаёт собственных сабтасок,
            # навсегда остаётся в фазе «разложить на сабтаски» и каждый прогон
            # раскладывается заново, плодя дубли по $0.9 за комплект.
            try:
                description = strip_html((kaiten.card(child["id"]) or {}).get("description"))
            except Exception as e:  # noqa: BLE001 — одна недоступная карточка не повод падать
                log(f"  не смог прочитать сабтаску #{child.get('id')}: {e}")
                unreadable += 1
                continue
        match = EPIC_ORIGIN_RE.search(description or "")
        if match and int(match.group(1)) == epic_id:
            own.append(child)
    return own, unreadable


def last_word_is_review(comments: list) -> bool:
    """Последним по эпику высказался ревьювер — значит его замечания ещё не отработаны."""
    for comment in reversed(comments):
        text = strip_html(comment.get("text", ""))
        if not text.startswith(AGENT_MARKS):
            # человек ответил после ревью: его реплика тоже пойдёт в правку спеки
            return True
        if text.startswith(REVIEWER_MARK):
            return True
        if text.startswith(EPIC_MARK) and any(h in text for h in SPEC_HEADS):
            return False
    return False


def epic_phase(kaiten: Kaiten, cfg: dict, flow: dict, card: dict, comments: list) -> str:
    """
    В какой фазе эпик прямо сейчас. Выводится целиком из Kaiten — чек-лист, блокеры,
    комментарии, дети — чтобы прогон мог начаться с любого места и не зависеть
    от того, что фабрика помнит о прошлом разе.
    """
    card_id = card["id"]
    # Наш действующий блокер значит ход человека, агентов не запускаем. Раньше
    # проверялся только блокер апрува АЦ, и эпик, заблокированный на вопросе, каждый
    # прогон заново входил в ту же фазу — ревьювер часами пересматривал одну и ту же
    # неизменённую спеку по $1.3 за круг.
    #
    # Но ждать вечно тоже нельзя: вопрос вроде «какой текст у подписи» останавливал
    # эпик на сутки. Отвисевший своё блокер перестаёт держать — фабрика идёт дальше,
    # принимая решения сама и честно перечисляя их в «Додумал сам».
    held, stale = stale_holds(kaiten, flow, card_id)
    if held and not stale:
        return "waiting_answer"
    if not acceptance_items(card):
        return "acceptance"
    if not any(said(comments, head) for head in SPEC_HEADS):
        return "spec"
    if not said(comments, SPEC_OK_LINE):
        # Замечания к спеке адресованы её автору, а не человеку: спеку правит агент,
        # ровно как код в круге правок. Раньше фабрика вешала блокер и ждала человека,
        # а он ничего поправить не мог — спека лежит в репозитории.
        rounds = count_review_rounds(comments)
        if rounds >= (cfg.get("reviewer") or {}).get("max_rounds", 3):
            # Круги кончились. Раньше здесь эпик вставал и ждал человека — и это была
            # худшая из возможных развязок: к третьему кругу замечания мельчают
            # («1 из 4», «1 из 3»), спека давно исполнима, а задача стоит сутки из-за
            # несогласованной формулировки в одном пункте. Спека — не диссертация,
            # а руководство к работе: принимаем как есть, нерешённое несём дальше
            # открытым текстом.
            return "spec_settle"
        return "spec_fix" if last_word_is_review(comments) else "spec_review"

    children = kaiten.children(card_id)
    own, unreadable = epic_subtasks(kaiten, card_id, children)
    if unreadable:
        # «Не смог прочитать» — это «не знаю», а не «сабтаски нет». Иначе оборванный
        # ответ Kaiten читается как пустая декомпозиция, и эпик раскладывается заново.
        return "unreadable"
    if not own:
        return "decompose"

    # Сабтаски одного эпика могут лежать на разных досках — по доске на продукт,
    # — и колонки ревью и «Готово» у каждой доски свои
    fallback = subtask_profile(cfg, flow)
    profiles = board_profiles(cfg)
    finished = 0
    for child in own:
        profile = profile_for_board(profiles, child.get("board_id")) or fallback
        column = child.get("column_id")
        if column == role_column(profile, "done"):
            finished += 1
        elif column == role_column(profile, "review") and blocked_by(kaiten, child["id"]):
            # в колонке ревью с блокером — агентское ревью пройдено, дальше человек
            finished += 1
    log(f"  сабтасок {len(own)}, отревьюено {finished}")
    return "closing" if finished == len(own) else "working"


def subtask_profile(cfg: dict, flow: dict, product: str | None = None) -> dict:
    # own_only — как в board_profiles: доска командная, и правила у неё одни, с какой
    # бы стороны за профилем ни пришли. Разные правила на одну доску — готовая ловушка
    if destinations(cfg):
        return profile_for_product(cfg, product)
    subtasks = flow["subtasks"]
    return make_profile("сабтаски", subtasks, subtasks.get("repo"), attach_to_debt=False,
                        own_only=True, tag=bot_tag(cfg))


def pick_epics(kaiten: Kaiten, cfg: dict, flow: dict) -> list[dict]:
    """
    Эпики, которыми фабрика имеет право заниматься.

    Одного тега мало: он висит на карточке всю её жизнь, а работа фабрики кончается
    на колонке разработки. Поэтому кроме тега и чужого блокера смотрим на колонку —
    эпик берётся, только пока он в окне «готов к разработке» — «в разработке».
    Дальше его ведёт человек, и трогать там нечего.
    """
    tag = flow.get("tag") or BOT_TAG
    found, outside = [], 0
    for board_id in flow["boards"]:
        window = epic_window(kaiten, flow, int(board_id))
        if not window[1]:
            log(f"доска {board_id}: не пойму, где колонка разработки — эпики не трогаю")
            continue
        for card in kaiten.cards_on_board(int(board_id), with_description=True):
            if not has_tag(card, tag):
                continue
            if not in_epic_window(kaiten, flow, card, window):
                outside += 1
                continue
            comments = kaiten.comments(card["id"])
            stop = hands_off(card, comments, cfg)
            if stop:
                log(f"#{card['id']} не трогаю: в карточке «{stop}»")
                continue
            found.append(card)
    if outside:
        log(f"эпиков с тегом вне колонок фабрики: {outside} — они уже у человека")
    return skip_off_hours(found, cfg)



# Врезка в промпт спеки, когда идёт круг правок. Замечания ревьювера и реплики
# человека уже лежат в {{COMMENTS}} — здесь только объясняем, что с ними делать.
SPEC_FIX_NOTE = """
## Это круг правок

Спека уже написана и отревьюена, замечания лежат в комментариях выше — их оставил
ревьювер с меткой 🔍. Если после них писал человек, его слово важнее замечаний.

Не переписывай спеку с нуля: открой существующий файл, поправь ровно то, на что
указали, и закоммить поверх. Ветка та же, PR тот же — иначе ревью пойдёт по кругу
с чистого листа. По каждому замечанию либо сделай, либо объясни в `risks`, почему нет.
"""

# Предупреждение про мок бека. Едет в описание сабтаски, в тело PR и отдельным
# комментарием в PR: раскатывать такую правку до релиза бека нельзя, и человек,
# который смотрит только PR, должен это увидеть там же.
MOCK_BACKEND_LINE = "Бек ещё не готов: фронт сделан на моке."
MOCK_BACKEND_NOTE = ("⚠️ **Нельзя раскатывать и включать до релиза бека.** "
                     "Фронт работает на моке.")


# Фазы эпика, в которых работает агент: только им нужны соседние репозитории.
# Остальные — проверки по Kaiten, и тянуть ради них все репозитории незачем.
# `spec_settle` агента не зовёт, а сразу передаёт эпик в декомпозицию — та и подтянет.
EPIC_AGENT_PHASES = ("acceptance", "spec", "spec_fix", "spec_review", "decompose")

# Врезка в промпт декомпозиции, когда у команды несколько продуктов. Схема ответа
# и так требует `product` и `repo`; здесь — как их выбирать.
DECOMPOSE_PRODUCTS_NOTE = """
## Куда ставить сабтаски

У каждой сабтаски два обязательных поля: `product` — на чью доску она ляжет, и `repo` —
в каком репозитории исполнитель будет писать код. Выбирай из списка продуктов выше,
по тому, **где живёт код**, а не по тому, про какой продукт эпик: правка B2B-чекаута
на сайте — это продукт B2B, если код лежит в его репозитории, и продукт сайта, если
в репозитории сайта.

Правило «один репозиторий — одна сабтаска» здесь работает так же: эпик, задевший два
продукта, — это две сабтаски, по одной на репозиторий, и каждая мержится сама.

Ветка со спекой есть только в репозитории, где ты сейчас стоишь. Сабтаска в другом
репозитории спеки под рукой не имеет — переложи в её описание всё, что ей нужно из спеки,
и ссылку на PR спеки словами «подробности в спеке».
"""


def epic_prompt(path: Path, card: dict, card_url: str, repo_cfg: dict,
                extra: dict | None = None) -> str:
    replacements = {
        "{{CARD_ID}}": str(card["id"]),
        "{{CARD_URL}}": card_url,
        "{{TITLE}}": card.get("title") or "(без заголовка)",
        "{{DESCRIPTION}}": strip_html(card.get("description")) or "(описания нет)",
        "{{BASE_BRANCH}}": repo_cfg["base_branch"],
        "{{REMOTE}}": repo_cfg["remote"],
        "{{REPOS}}": "",
        "{{PRODUCT_RULES}}": "",
        **(extra or {}),
        **project_replacements(repo_cfg.get("key")),
    }
    return apply_template(path.read_text(encoding="utf-8"), replacements)


def comment_acceptance(verdict: dict, meta: dict) -> str:
    """АЦ уехали чек-листом, в комментарии — только просьба их проверить."""
    text = (f"{EPIC_MARK} **Приёмочные критерии готовы.**\n\n"
            f"{verdict.get('summary', '')}\n\n"
            f"Они лежат чек-листом в этой карточке. Прочитай и, если согласен, "
            f"**сними блокер** — по нему я и понимаю, что можно начинать. "
            f"Если что-то не так, напиши комментарием и снимай блокер уже после правок.")
    if verdict.get("backend_needed"):
        text += "\n\nПохоже, понадобится и бек — учту при декомпозиции."
    tail = format_meta(meta)
    text += f"\n\n_приёмочные критерии{': ' + tail if tail else ''}. Код не менял._"
    text += format_assumptions(verdict)
    return text


def comment_epic_unclear(verdict: dict, meta: dict, phase: str) -> str:
    text = (f"{EPIC_MARK} **Не хватает данных, чтобы {phase}.**\n\n"
            f"{verdict.get('summary', '')}")
    text += format_list("Что нужно уточнить", verdict.get("questions"))
    text += ("\n\nОтветь комментарием и **сними блокер** — я вернусь и продолжу "
             "с того же места.")
    tail = format_meta(meta)
    text += f"\n\n_{phase}{': ' + tail if tail else ''}. Код не менял._"
    return text


def subtask_description(epic: dict, epic_url: str, item: dict, spec_note: str) -> str:
    """
    Описание сабтаски. Первой строкой — откуда она выросла: по этой строке фабрика
    отличает своих детей от тех, что подвесил человек.
    """
    parts = [f"{EPIC_ORIGIN} #{epic['id']} — {epic_url}", ""]
    parts.append(item.get("description", "").strip())
    if spec_note:
        parts += ["", spec_note]
    if item.get("mocks_backend"):
        parts += ["", f"⚠️ {MOCK_BACKEND_LINE} Раскатывать и включать нельзя, "
                      f"пока бек не уедет в прод."]
    return "\n".join(parts)


def show_prompt(prompt: str, args, what: str) -> bool:
    """--prompt-only: показать промпт и не запускать агента. True — дальше не идём."""
    if not args.prompt_only:
        return False
    print("\n" + "=" * 78)
    print(prompt)
    print("=" * 78 + "\n")
    log(f"  промпт {what} показан, агент не запускался, карточка не тронута")
    return True


def log_phase(card_id: int, phase: str, verdict: dict, meta: dict) -> None:
    LOGS.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    (LOGS / f"epic-{phase}-{card_id}-{stamp}.json").write_text(
        json.dumps({"verdict": verdict, "meta": meta}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    log(f"  {phase}: {verdict.get('status') or verdict.get('verdict')}, {format_meta(meta)}")


def create_subtasks(kaiten: Kaiten, cfg: dict, flow: dict, epic: dict, epic_url: str,
                    items: list, verdict: dict, meta: dict, args) -> None:
    """
    Создаёт сабтаски на своей доске и вешает их дочерними на эпик.

    Бек не блокирует фронт: фронт делается сразу, на моке, а блокер вешается только
    на раскатку. Иначе половина работы простаивала бы, ожидая чужую команду.

    С продуктами сабтаска ложится на доску того продукта, который назвал агент, —
    эпик про B2B-заказ, задевший сайт, становится двумя карточками на двух досках.
    """
    limit = flow.get("max_subtasks", 5)
    if len(items) > limit:
        log(f"  сабтасок {len(items)}, беру первые {limit} — остальное человеку")
        items = items[:limit]

    spec_note = ""
    for comment in kaiten.comments(epic["id"]):
        text = strip_html(comment.get("text", ""))
        if text.startswith(EPIC_MARK) and SPEC_LINE in text:
            spec_note = text.split("\n")[0].replace(EPIC_MARK, "").strip("* ")
            break

    created, backend = [], [i for i in items if i.get("kind") == "backend"]
    for item in items:
        if destinations(cfg):
            product, repo = pick_product_repo(cfg, item.get("product") or "",
                                              item.get("repo"))
            item = {**item, "product": product, "repo": repo}
        profile = subtask_profile(cfg, flow, item.get("product"))
        description = subtask_description(epic, epic_url, item, spec_note)
        line = repo_line(cfg, item.get("product") or "", item.get("repo") or "")
        if line:
            description += f"\n\n{line}"
        body = {
            "board_id": profile["board_id"],
            "column_id": role_column(profile, "queue"),
            "title": item["title"].strip(),
            "description": description,
        }
        if profile.get("lane_id"):
            body["lane_id"] = profile["lane_id"]
        if profile.get("card_type_id"):
            body["type_id"] = profile["card_type_id"]
        where = (f", {profile['key']} / {item.get('repo')}" if item.get("product") else "")
        if args.dry_run:
            log(f"  [dry-run] сабтаска «{item['title'][:50]}» ({item.get('kind')}{where})")
            continue
        card = kaiten.create_card(body)
        if not card:
            log(f"  !! не создалась сабтаска «{item['title'][:40]}»")
            continue
        kaiten.add_child(epic["id"], card["id"])
        # Тег «это моё» — на каждую сабтаску: доска сабтасок общая, и без него
        # исполнитель пройдёт мимо собственной работы. Заодно команде сразу видно,
        # чьи это карточки в их бэклоге.
        kaiten.add_tag(card["id"], bot_tag(cfg))
        # Ночной тег наследуется от эпика: иначе эпик помечен «ночью», а код по нему
        # пишется в полдень — шумные сабтаски ровно за этим и откладывали
        night_tag, _, _ = night_config(cfg)
        if has_tag(epic, night_tag):
            kaiten.add_tag(card["id"], night_tag)
            log("    ночной тег унаследован от эпика")
        created.append((card, item))
        log(f"  + #{card['id']} «{item['title'][:50]}» ({item.get('kind')}{where})")

    # блокер раскатки — на фронт, который сделан на моке
    backend_card = next((c for c, i in created if i.get("kind") == "backend"), None)
    for card, item in created:
        if item.get("mocks_backend"):
            detail = (f"бек #{backend_card['id']}" if backend and backend_card
                      else "бек ещё не готов")
            hold(kaiten, card["id"], BLOCK_ROLLOUT, detail)

    def place(item: dict) -> str:
        if not item.get("product"):
            return item.get("kind") or ""
        title = (destinations(cfg).get(item["product"]) or {}).get("title") or item["product"]
        return f"{title}, {item.get('repo')}"

    lines = "\n".join(f"- #{c['id']} {i['title']} ({place(i)})" for c, i in created)
    text = (f"{EPIC_MARK} **{SUBTASKS_LINE}**\n\n{lines or '(в dry-run не создавал)'}\n\n"
            f"{verdict.get('summary', '')}")
    if any(i.get("mocks_backend") for _, i in created):
        text += f"\n\n{MOCK_BACKEND_NOTE}"
    text += (f"\n\n_декомпозиция, {format_meta(meta)}._"
             + format_assumptions(verdict))
    kaiten.comment(epic["id"], text)


def spec_pr_url(comments: list) -> str:
    """Ссылка на PR спеки из комментария, которым фабрика о нём объявила."""
    for comment in reversed(comments):
        text = strip_html(comment.get("text", ""))
        if text.startswith(EPIC_MARK) and any(h in text[:60] for h in SPEC_HEADS):
            found = re.search(r"https://\S+/pull/\d+", text)
            if found:
                return found.group(0)
    return ""


def epic_worktree(kaiten: Kaiten, cfg: dict, repo_cfg: dict, repo_key: str,
                  card_id: int, writable: bool) -> Path:
    """
    Рабочая копия для фазы эпика.

    Для чтения (АЦ, декомпозиция) переиспользуем постоянную копию разведчика: она
    уже есть и обновляется дешево. Для спеки нужна своя ветка — там будет коммит.
    """
    repo = Path(repo_cfg["path"]).expanduser()
    if not (repo / ".git").exists():
        raise FactoryError(f"репозиторий не найден: {repo}")
    if not writable:
        return ensure_scout_worktree(repo, repo_cfg, repo_key)
    worktree = WORKTREES / f"spec-{card_id}"
    branch = f"{branch_prefix(cfg)}{card_id}-spec"
    existing = remote_branch_for_card(repo, repo_cfg["remote"], branch_prefixes(cfg), card_id)
    continue_from = existing if existing and existing.endswith("-spec") else None
    make_worktree(repo, branch, repo_cfg["base_branch"], repo_cfg["remote"], worktree,
                  continue_from=continue_from)
    return worktree


# Врезка в промпт, когда на вопросы никто не ответил. Главное здесь — не «реши как
# хочешь», а «реши консервативно и покажи, что решил»: человек увидит список и поправит
# то, что его не устроит, а задача к этому моменту уже будет двигаться.
UNANSWERED_NOTE = """
## На эти вопросы никто не ответил

Их задал прошлый прогон, и человек не откликнулся за отведённое время:

{questions}

Ждать больше не нужно — **прими решения сам и работай дальше**. По каждому:

- выбирай самый осторожный вариант: тот, что ближе к тому, как уже сделано в коде
  или нарисовано в макете, и тот, который проще всего потом откатить;
- если вопрос организационный (кто, когда, в каком PR) — считай, что это делает
  кто-то другой отдельной задачей, и просто не закладывайся на это;
- ничего не выдумывай про данные, которых нет: если без факта нельзя, опиши развилку
  прямо в тексте и возьми вариант по умолчанию.

**Каждое такое решение верни отдельной строкой в `assumptions`** — коротко, в форме
«принял X, потому что Y». Это единственное место, по которому человек поймёт, где ты
додумывал за него. Молча принятое решение хуже неотвеченного вопроса.
"""


def release_stale_holds(kaiten: Kaiten, flow: dict, card: dict, comments: list,
                        args) -> list[str]:
    """
    Снимает отвисевшие своё блокеры и возвращает вопросы, на которые не ответили.

    Пустой список — либо блокеров не было, либо они ещё свежие: ждём дальше.
    """
    card_id = card["id"]
    held, stale = stale_holds(kaiten, flow, card_id)
    if not stale:
        return []
    questions = open_questions(comments) or [
        str(b.get("reason") or "").removeprefix(BLOCK_MARK).strip() for b in stale
    ]
    questions = [q for q in questions if q]
    hours = flow.get("answer_wait_hours", DEFAULT_ANSWER_WAIT_HOURS)
    log(f"  ответа нет {hours} ч — снимаю блокер и иду дальше сам")
    if args.prompt_only or args.dry_run:
        return questions
    for blocker in stale:
        try:
            kaiten.unblock(card_id, blocker["id"])
        except Exception as e:  # noqa: BLE001 — не смогли снять, но работу не бросаем
            log(f"  !! блокер {blocker.get('id')} не снялся: {e}")
    text = (f"{EPIC_MARK} **Ответа не дождался за {hours} ч — двигаюсь дальше сам.**\n\n"
            "Решения по открытым вопросам приму на своё усмотрение, самые осторожные "
            "из возможных, и перечислю их в следующем комментарии под заголовком "
            "«Додумал сам». Если что-то не так — поправь, это дешевле, чем стоять.")
    kaiten.comment(card_id, text)
    return questions


def advance_epic(kaiten: Kaiten, cfg: dict, flow: dict, card: dict, comments: list,
                 phase: str, args, unanswered: list[str] | None = None) -> None:
    """Продвигает эпик на одну фазу. Каждая фаза заканчивается либо шагом, либо блокером."""
    card_id = card["id"]
    card_url = kaiten.card_url(card)
    title = (card.get("title") or "").strip()
    # Спека живёт в репозитории главного продукта — его даёт дорожка эпика. Остальные
    # репозитории агент видит на чтение: эпик про B2B-заказ задевает и сайт, и бек,
    # и разложить его по доскам можно, только заглянув во все.
    if destinations(cfg):
        product = product_for_lane(cfg, card.get("lane_id"))
        main_repo = (product_repos(cfg, product) or [None])[0]
    else:
        main_repo = (flow.get("subtasks") or {}).get("repo")
    repo_key, repo_cfg = resolve_repo(card, cfg, main_repo)
    extra_dirs = (scout_dirs(cfg, keep=repo_key)
                  if destinations(cfg) and phase in EPIC_AGENT_PHASES else [])
    repos_block = {"{{REPOS}}": repos_note(cfg, repo_key, extra_dirs)}
    agent_cfg = {**cfg["triager"], **(flow.get("agent") or {})}
    unanswered_note = (UNANSWERED_NOTE.format(
        questions="\n".join(f"- {q}" for q in unanswered)) if unanswered else "")

    if phase == "acceptance":
        note_phase(args, "эпик: готовлю копию репозитория")
        worktree = epic_worktree(kaiten, cfg, repo_cfg, repo_key, card_id, writable=False)
        prompt = epic_prompt(ACCEPTANCE_TEMPLATE_PATH, card, card_url, repo_cfg,
                             {"{{COMMENTS}}": format_comments(comments),
                              "{{UNANSWERED}}": unanswered_note, **repos_block})
        if show_prompt(prompt, args, "приёмочных критериев"):
            return
        note_phase(args, "эпик: пишу приёмочные критерии")
        verdict, meta = run_agent(prompt, worktree, agent_cfg, schema=ACCEPTANCE_SCHEMA,
                                  add_dirs=[path for _, path in extra_dirs])
        note_spend(meta)
        log_phase(card_id, "acceptance", verdict, meta)

        criteria = [c for c in (verdict.get("criteria") or []) if str(c).strip()]
        if verdict.get("status") != "ready" or not criteria:
            kaiten.comment(card_id, comment_epic_unclear(verdict, meta,
                                                         "написать приёмочные критерии"))
            hold(kaiten, card_id, BLOCK_QUESTION, "нужны детали для приёмочных критериев")
            return
        kaiten.add_checklist(card_id, ACCEPTANCE_LIST, criteria)
        kaiten.comment(card_id, comment_acceptance(verdict, meta))
        hold(kaiten, card_id, BLOCK_ACCEPTANCE)
        log(f"  -> АЦ на апруве: {len(criteria)} пунктов")
        return

    if phase in ("spec", "spec_fix"):
        note_phase(args, "эпик: готовлю копию репозитория")
        worktree = epic_worktree(kaiten, cfg, repo_cfg, repo_key, card_id, writable=True)
        criteria = acceptance_items(card)
        fixing = phase == "spec_fix"
        prompt = epic_prompt(SPEC_TEMPLATE_PATH, card, card_url, repo_cfg, {
            "{{COMMENTS}}": format_comments(comments),
            "{{ACCEPTANCE}}": "\n".join(f"- {i.get('text')}" for i in criteria),
            "{{SPEC_DIR}}": flow.get("spec_dir") or "specs",
            "{{BRANCH}}": f"{branch_prefix(cfg)}{card_id}-spec",
            "{{SPEC_NOTE}}": SPEC_FIX_NOTE if fixing else "",
            "{{REVIEW_NOTES}}": (fetch_pr_notes(worktree, spec_pr_url(comments))
                                 if fixing else ""),
            "{{UNANSWERED}}": unanswered_note,
            **repos_block,
        })
        if show_prompt(prompt, args, "правки спеки" if fixing else "спеки"):
            return
        note_phase(args, "эпик: правлю спеку" if fixing else "эпик: пишу спеку")
        verdict, meta = run_agent(prompt, worktree, cfg["agent"], schema=SPEC_SCHEMA,
                                  add_dirs=[path for _, path in extra_dirs])
        note_spend(meta)
        log_phase(card_id, "spec", verdict, meta)

        base_ref = f"{repo_cfg['remote']}/{repo_cfg['base_branch']}"
        commits = git(worktree, "log", f"{base_ref}..HEAD", "--oneline")
        if verdict.get("status") != "done" or not commits:
            kaiten.comment(card_id, comment_epic_unclear(verdict, meta, "написать спеку"))
            hold(kaiten, card_id, BLOCK_QUESTION, "нужны детали для спеки")
            drop_worktree(Path(repo_cfg["path"]).expanduser(), worktree)
            return

        branch = git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
        if not args.dry_run:
            git(worktree, "push", "--force-with-lease", "-u", repo_cfg["remote"], branch)
        pr_url = open_pr(worktree, branch, repo_cfg["base_branch"], card, card_url,
                         verdict, cfg, args.dry_run, repo_key) if not args.no_pr else "(без PR)"
        head = SPEC_FIXED_LINE if fixing else SPEC_LINE
        kaiten.comment(card_id,
                       f"{EPIC_MARK} **{head}** {pr_url}\n\n"
                       f"{verdict.get('summary', '')}\n\n"
                       f"_{'правка спеки' if fixing else 'спека'}, {format_meta(meta)}. "
                       f"Дальше её посмотрит ревьювер._"
                       + format_assumptions(verdict))
        log(f"  -> спека: {pr_url}")
        if not (args.keep_worktree or cfg.get("keep_worktree")):
            drop_worktree(Path(repo_cfg["path"]).expanduser(), worktree)
        return

    if phase == "spec_settle":
        # Ревью и правки не сошлись за отведённые круги. Агента не запускаем: он уже
        # трижды правил, и каждый следующий круг стоит денег ради всё более мелких
        # придирок. Закрываем ревью своей властью и оставляем нерешённое в карточке —
        # исполнитель прочитает его вместе со спекой, а человек увидит, где недоспорили.
        # Только замечания ревьювера: у них значок важности. Без фильтра сюда лезут
        # и строки из «Додумал сам» — они уже показаны выше и нерешёнными не являются.
        marks = tuple(SEVERITY_ICON.values()) + ("❓",)
        leftovers = [q for q in open_questions(comments, limit=8) if q.startswith(marks)][:5]
        text = (f"{EPIC_MARK} **{SPEC_OK_LINE}** Ревью и автор не сошлись за "
                f"{count_review_rounds(comments)} круга — принимаю спеку как есть "
                f"и иду дальше: она исполнима, а спорить об остатке дороже, чем "
                f"поправить его при работе.")
        if leftovers:
            text += format_list("⚠️ Осталось нерешённым — посмотри при ревью кода",
                                leftovers)
        text += ("\n\nНе согласен — напиши комментарием, спека вернётся в правку "
                 "с твоей репликой.")
        if not args.prompt_only:
            kaiten.comment(card_id, text)
        log(f"  -> спека принята как есть, нерешённых замечаний: {len(leftovers)}")
        # Эта фаза мгновенная и агента не зовёт — тратить на неё целый тик расписания
        # незачем: сразу раскладываем эпик на сабтаски, ради чего спека и писалась.
        if args.prompt_only or out_of_budget(cfg):
            return
        return advance_epic(kaiten, cfg, flow, card, comments, "decompose", args,
                            unanswered)

    if phase == "spec_review":
        note_phase(args, "эпик: готовлю копию репозитория")
        worktree = epic_worktree(kaiten, cfg, repo_cfg, repo_key, card_id, writable=False)
        prompt = epic_prompt(SPEC_REVIEW_TEMPLATE_PATH, card, card_url, repo_cfg, {
            "{{ACCEPTANCE}}": "\n".join(f"- {i.get('text')}"
                                        for i in acceptance_items(card)),
            "{{SPEC_BRANCH}}": f"{branch_prefix(cfg)}{card_id}-spec",
            "{{UNANSWERED}}": unanswered_note,
            **repos_block,
        })
        if show_prompt(prompt, args, "ревью спеки"):
            return
        note_phase(args, "эпик: ревьювер смотрит спеку")
        review, meta = run_agent(prompt, worktree, cfg["reviewer"],
                                 schema=SPEC_REVIEW_SCHEMA, verdict_key="verdict",
                                 add_dirs=[path for _, path in extra_dirs])
        note_spend(meta)
        log_phase(card_id, "spec-review", review, meta)

        findings = review.get("findings") or []
        blocking = [f for f in findings if f.get("severity") in ("blocker", "major")]
        spec_pr = spec_pr_url(comments)
        asks = [a for a in (review.get("needs_human") or []) if str(a).strip()]

        if asks:
            # Стена, которую автор спеки не пробьёт: нужен макет, продуктовое решение
            # или доступ. Гонять круги правок в неё бессмысленно — каждый стоит денег
            # и заканчивается тем же замечанием.
            full = (f"{REVIEWER_MARK} **Нужен человек.**\n\n{full_summary(review)}"
                    + format_findings(findings))
            if spec_pr and not args.dry_run:
                post_pr_review(worktree, spec_pr, full)
            text = (f"{REVIEWER_MARK} **Дальше без тебя не выйдет.**\n\n"
                    f"{review.get('summary', '')}")
            for ask in asks[:4]:
                text += f"\n\n❓ {ask}"
            if spec_pr:
                text += f"\n\nОстальные замечания — в PR: {spec_pr}"
            text += ("\n\nОтветь комментарием и **сними блокер** — я вернусь и продолжу "
                     "с того же места.")
            text += f"\n\n_ревью спеки, {format_meta(meta)}._"
            kaiten.comment(card_id, text)
            hold(kaiten, card_id, BLOCK_QUESTION, asks[0][:90])
            log(f"  -> нужен человек: {len(asks)} вопросов")
            return

        if review.get("verdict") == "needs_changes" or blocking:
            # Полное ревью — комментарием в PR спеки: там его читает и человек,
            # и следующий круг правок (фабрика забирает его обратно через gh)
            full = (f"{REVIEWER_MARK} **Спеку надо поправить.**\n\n"
                    f"{full_summary(review)}" + format_findings(findings))
            if spec_pr and not args.dry_run:
                post_pr_review(worktree, spec_pr, full)

            text = (f"{REVIEWER_MARK} **Спеку надо поправить: {len(blocking)} "
                    f"из {len(findings)}.**\n\n{review.get('summary', '')}")
            for item in blocking[:3]:
                icon = SEVERITY_ICON.get(item.get("severity", ""), "•")
                text += f"\n\n{icon} {item.get('what', '')}"
            if len(blocking) > 3:
                text += f"\n\n…и ещё {len(blocking) - 3}."
            if spec_pr:
                text += f"\n\nПодробно, с причинами и что делать: {spec_pr}"
            text += ("\n\nПоправит агент следующим прогоном — от тебя ничего не нужно. "
                     "Хочешь вмешаться — напиши комментарием, твоя реплика уедет "
                     "в правку вместе с замечаниями.")
            kaiten.comment(card_id, text)
            log("  -> спека вернулась на правки к агенту")
            return

        kaiten.comment(card_id,
                       f"{EPIC_MARK} **{SPEC_OK_LINE}** {review.get('summary', '')}\n\n"
                       f"_ревью спеки, {format_meta(meta)}._")
        log("  -> спека принята")
        return

    if phase == "decompose":
        note_phase(args, "эпик: готовлю копию репозитория")
        worktree = epic_worktree(kaiten, cfg, repo_cfg, repo_key, card_id, writable=False)
        prompt = epic_prompt(DECOMPOSE_TEMPLATE_PATH, card, card_url, repo_cfg, {
            "{{ACCEPTANCE}}": "\n".join(f"- {i.get('text')}"
                                        for i in acceptance_items(card)),
            "{{SPEC_BRANCH}}": f"{branch_prefix(cfg)}{card_id}-spec",
            "{{MAX_SUBTASKS}}": str(flow.get("max_subtasks", 5)),
            "{{UNANSWERED}}": unanswered_note,
            "{{PRODUCT_RULES}}": DECOMPOSE_PRODUCTS_NOTE if destinations(cfg) else "",
            **repos_block,
        })
        if show_prompt(prompt, args, "декомпозиции"):
            return
        note_phase(args, "эпик: раскладываю на сабтаски")
        verdict, meta = run_agent(prompt, worktree, agent_cfg,
                                  schema=with_products(DECOMPOSE_SCHEMA, cfg, "subtasks"),
                                  add_dirs=[path for _, path in extra_dirs])
        note_spend(meta)
        log_phase(card_id, "decompose", verdict, meta)

        items = [i for i in (verdict.get("subtasks") or []) if i.get("title")]
        if verdict.get("status") != "ready" or not items:
            kaiten.comment(card_id, comment_epic_unclear(verdict, meta, "разложить на сабтаски"))
            hold(kaiten, card_id, BLOCK_QUESTION, "не смог разложить эпик на сабтаски")
            return
        create_subtasks(kaiten, cfg, flow, card, card_url, items, verdict, meta, args)
        return

    log(f"  фаза «{phase}» ничего не требует")


# Строки, по которым узнаём вопрос или замечание в комментарии агента. Ищем именно
# их, а не весь текст: человеку в меню нужен вопрос, а не пересказ исследования.
ASK_HEADS = ("Что нужно уточнить", "Замечания", "Что уточнить")
ASK_BULLETS = ("- ", "• ", "🛑 ", "⚠️ ", "💬 ", "❓ ")

# Заголовки, под которыми агент спрашивает человека. «Замечания» сюда не входят:
# замечания ревьювера адресованы исполнителю и живут в PR, а не в разговоре с людьми.
QUESTION_HEADS = ("Что нужно уточнить", "Что уточнить")

# Значок в начале строки делает её пунктом сам по себе — замечания ревьювера идут
# без заголовка, просто «🛑 …». А голый дефис — нет: списки в отчёте есть везде,
# и «⚠️ Додумал сам — проверь» состоит ровно из таких строк. Раз в тред уехало
# «Принял, что у Drinkit флаг должен остаться прямоугольным» — под видом вопроса,
# на который человек якобы должен ответить.
MARK_BULLETS = ("🛑 ", "⚠️ ", "💬 ", "❓ ")

# Продолжение длинного комментария, разрезанного по 4096 символов: «🤖 _(продолжение 2/3)_».
CONTINUED_RE = re.compile(r"^\S+\s+_\(продолжение \d+/\d+\)_")

# Значок в начале строки: в карточке он к месту — строк много и они разной
# срочности, — а в мессенджере лишний. Собираем из ASK_BULLETS, чтобы новый
# значок не пришлось дописывать в двух местах.
ASK_MARK_RE = re.compile("^(?:"
                         + "|".join(re.escape(b.strip()) for b in ASK_BULLETS)
                         + r")\s*")


def last_agent_say(comments: list) -> list[str]:
    """
    Последнее, что агент сказал в карточке, — одним куском.

    Куском, а не комментарием, потому что длинный ответ Kaiten режет по 4096 символов,
    и первая часть с блокерами оказывается не последней. Части узнаём по метке
    «(продолжение N/M)»: она для того и ставится.

    Раньше сюда собиралось всё, что агент наговорил после последней реплики человека.
    Но за один прогон карточка проходит несколько шагов — ревью с замечаниями, правки,
    отчёт, ревью начисто, — и человек между ними молчит. Замечания, которые исполнитель
    уже разобрал следующим же комментарием, считались открытыми и уезжали человеку
    как «вот что вылезло» — при том что последним словом ревьювера было «Замечаний нет».
    """
    say = []
    for comment in reversed(comments):
        text = strip_html(comment.get("text", ""))
        if not text.startswith(AGENT_MARKS):
            break
        say.append(text)
        if not CONTINUED_RE.match(text):
            break  # это первая часть: всё, что раньше, — уже другой шаг
    say.reverse()
    return say


def collect_asks(comments: list, heads: tuple, marks: tuple, limit: int) -> list[str]:
    """
    Пункты списка из последнего слова агента: под заголовками `heads` — любые,
    а со значком из `marks` — где бы они ни стояли.

    Голый дефис вне заголовка не берём: списками в отчёте написана половина текста,
    и без этого различения в вопросы попадало «Додумал сам — проверь».
    """
    asks = []
    for text in last_agent_say(comments):
        in_list = False
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if any(head.lower() in line.lower() for head in heads):
                in_list = True
                continue
            if line.startswith(marks) or (in_list and line.startswith(ASK_BULLETS)):
                # маркер severity оставляем: по нему сразу видно, что блокирует
                asks.append(re.sub(r"^[-•]\s*", "", line))
                in_list = True
                continue
            if in_list and not line.startswith(" "):
                in_list = False

    # блокеры вперёд: человек должен сначала увидеть то, из-за чего всё встало
    order = {"🛑": 0, "⚠️": 1, "💬": 2}
    asks.sort(key=lambda a: order.get(a[:2].strip(), 1))
    return [a[:180] for a in asks[:limit]]


def open_questions(comments: list, limit: int = 4) -> list[str]:
    """
    Чего фабрика ждёт от человека: вопросы агента и замечания ревьювера вместе.

    Без этого «ждёт твоего ответа» бесполезно: вопросы лежат в карточке, иногда
    разрезанные на три комментария по 4096 символов, и человек не понимает,
    на что отвечать.
    """
    return collect_asks(comments, ASK_HEADS, MARK_BULLETS, limit)


def human_questions(comments: list, limit: int = 4) -> list[str]:
    """
    То же, но только вопросы — то, на что человек должен ответить словами.

    Для разговора в Time. В карточке и в PR замечания ревьювера к месту: там их читает
    тот, кто будет смотреть код. В канале же вопрос — это обращение к человеку, и
    отправить ему под видом вопроса разбор чужих замечаний значит потребовать ответа
    там, где отвечать не на что: «это вопрос или что?» — справедливо спросили в треде.
    """
    return collect_asks(comments, QUESTION_HEADS, ("❓ ",), limit)


def take_epic(kaiten: Kaiten, flow: dict, card: dict) -> None:
    """
    Взяли эпик в работу — двигаем в колонку разработки, если он не там.

    Только вперёд. Назад фабрика эпик не тащит никогда: он ушёл правее — значит
    его ведёт человек. Пока проверки не было, `take_epic` возвращал эпик из
    «Rollout» в разработку, и всё начиналось по второму кругу.
    """
    target = int(flow.get("development_column_id") or 0)
    here = card.get("column_id")
    if not target or here == target:
        return
    order = column_order(kaiten, card["board_id"])
    if here in order and target in order and order.index(here) > order.index(target):
        log("  эпик уже правее разработки — назад не двигаю")
        return
    log("  двигаю эпик в колонку разработки")
    kaiten.move(card["id"], target)


def close_epic(kaiten: Kaiten, flow: dict, card: dict) -> str:
    """
    Все сабтаски отревьюены — эпик уезжает на полноценное ревью.

    Целевая колонка по умолчанию не задаётся, а вычисляется: «следующая после
    разработки». Если её выставили в конфиге — уважаем конфиг.
    """
    card_id = card["id"]
    if flow.get("stay_on_close"):
        # На доске эпиков бывает, что справа от разработки ревью нет вовсе: там сразу
        # «Rollout». Отревьюенные агентом сабтаски — это ещё не раскатка, их PR ждут
        # человека, — поэтому эпик остаётся на месте, а фабрика просто отпускает его,
        # сняв тег. Дальше по колонкам его двигает человек.
        log("  все сабтаски отревьюены — эпик оставляю в колонке, снимаю тег")
        return (f"{EPIC_MARK} **Все сабтаски отревьюены.** Эпик оставил в колонке: "
                f"дальше его ведёшь ты." + drop_work_tag(kaiten, flow, card))
    order = column_order(kaiten, card["board_id"])
    target = flow.get("review_column_id")
    if not target:
        development = int(flow["development_column_id"])
        if development not in order:
            return "не понял, куда двигать эпик: колонки разработки нет на этой доске"
        position = order.index(development) + 1
        if position >= len(order):
            return "не понял, куда двигать эпик: справа от колонки разработки пусто"
        target = order[position]
    target = int(target)

    # Человек мог утащить эпик дальше сам — тогда не возвращаем его назад. Сравниваем
    # позиции, а не id: пока здесь стояло `==`, эпик, уехавший в «Rollout», на первом же
    # прогоне откатывался обратно на ревью, которое человек уже прошёл руками.
    here = card.get("column_id")
    if here in order and target in order and order.index(here) >= order.index(target):
        return ""
    log(f"  все сабтаски отревьюены, двигаю эпик в колонку {target}")
    kaiten.move(card_id, target)
    return (f"{EPIC_MARK} **Все сабтаски отревьюены.** Эпик уехал на ревью."
            + drop_work_tag(kaiten, flow, card))


def drop_work_tag(kaiten: Kaiten, flow: dict, card: dict) -> str:
    """
    Снимает рабочий тег с закрытого эпика и возвращает строку для комментария.

    Второй выключатель к окну колонок, и главный из двух. Колонка говорит «сейчас
    не твой ход», а тег — «это вообще твоё»; пока он висел вечно, эпик, однажды
    оказавшийся в колонке разработки, снова становился фабричным — неважно, как он
    туда попал. Так ETA и вернулся в работу через полгода после раскатки.

    Теперь «эпик уехал на ревью» значит «фабрика с ним закончила». Надо переделать —
    повесь тег заново, это осознанное действие, а не побочный эффект уборки на доске.

    Ошибку не поднимаем: эпик уже уехал, и не снявшийся тег этого не отменяет.
    """
    name = flow.get("tag") or BOT_TAG
    found = tag_id(card, name)
    if not found:
        return ""
    try:
        kaiten.remove_tag(card["id"], found)
    except Exception as e:  # noqa: BLE001 — эпик уже закрыт, тег не важнее
        log(f"  !! тег «{name}» не снялся: {e}")
        return (f"\n\nТег `{name}` снять не смог — сними руками, иначе эпик снова "
                f"попадёт ко мне, если окажется в колонке разработки.")
    log(f"  снял тег «{name}» — эпик больше не мой")
    return f"\n\nТег `{name}` снял: дальше эпик твой. Надо переделать — повесь заново."


def epic_card_url(kaiten: Kaiten, card: dict) -> str:
    return kaiten.card_url(card)


def run_epics(kaiten: Kaiten, cfg: dict, args, only_card: int | None = None) -> int:
    """
    Фаза эпиков: по каждому определить, где он стоит, и сдвинуть на один шаг.

    За прогон эпик продвигается максимум на одну фазу. Так дешевле и понятнее:
    между фазами стоят гейты человека, и пытаться проскочить их пачкой смысла нет.
    """
    flow = epic_flow(cfg)
    if not flow:
        log("режим эпиков не настроен (нужна секция epic_flow) — пропускаю")
        return 0

    if only_card:
        # Окно колонок держим и здесь: --epic-card обходит тег и выборку, но не право
        # трогать чужую работу. Хочешь переделать уехавший эпик — верни его в колонку
        # разработки руками, это одно движение мышкой.
        epics = [kaiten.card(only_card)]
        if not in_epic_window(kaiten, flow, epics[0]):
            ready, dev = epic_window(kaiten, flow, epics[0].get("board_id"))
            log(f"#{only_card} стоит вне колонок фабрики (её дело — {ready} и {dev}) "
                f"— не трогаю")
            return 0
    else:
        epics = pick_epics(kaiten, cfg, flow)
        if not epics:
            log("эпиков с рабочим тегом нет")
            if not args.prompt_only:
                write_status(epics_waiting=0)
            return 0
        limit = flow.get("max_epics_per_run", 1)
        log(f"эпиков с тегом: {len(epics)}, беру {min(limit, len(epics))}")
        epics = epics[:limit]

    waiting = 0
    for card in epics:
        card_id = card["id"]
        title = (card.get("title") or "").strip()
        # чек-листы приезжают только в полной карточке, в выдаче по доске их нет
        card = kaiten.card(card_id)
        comments = kaiten.comments(card_id)

        foreign = [b for b in kaiten.blockers(card_id) if not ours(b)]
        if foreign:
            reason = str(foreign[0].get("reason") or "")
            log(f"#{card_id} «{title[:50]}» — чужой блокер: {reason[:60]} — не трогаю")
            waiting += 1
            note_waiting(kaiten, card, reason, kind="epic")
            continue

        phase = epic_phase(kaiten, cfg, flow, card, comments)
        log(f"#{card_id} «{title[:50]}» — фаза: {EPIC_PHASE_LABELS.get(phase, phase)}")

        if phase == "waiting_answer":
            waiting += 1
            mine = next((b for b in kaiten.blockers(card_id) if ours(b)), {})
            note_waiting(kaiten, card, str(mine.get("reason") or EPIC_PHASE_LABELS[phase]),
                         kind="epic", asks=open_questions(comments))
            continue

        if phase == "unreadable":
            # Kaiten не отдал часть сабтасок. Считать, что их нет, нельзя: так эпик
            # раскладывается заново и заводит дубли. Ждём следующего прогона — раньше
            # блокеров и раньше витрины: делать по «не знаю» вообще ничего не надо.
            log("  часть сабтасок не прочиталась — жду следующего прогона")
            continue

        if not args.prompt_only:
            write_status(card={"id": card_id, "title": title,
                               "url": kaiten.card_url(card)},
                         phase=f"эпик: {EPIC_PHASE_LABELS.get(phase, phase)}")

        # Ответа не дождались — снимаем свой блокер и идём дальше сами. Вопросы
        # не выбрасываем: они уедут агенту, он примет по ним решения и вернёт их
        # списком «Додумал сам».
        unanswered = release_stale_holds(kaiten, flow, card, comments, args)

        if phase == "closing":
            note = close_epic(kaiten, flow, card)
            if note:
                kaiten.comment(card_id, note)
            continue

        if phase == "working":
            # сабтаски разбираются обычными фазами работы и ревью по своему профилю
            take_epic(kaiten, flow, card)
            continue

        if out_of_budget(cfg):
            break

        take_epic(kaiten, flow, card)
        try:
            advance_epic(kaiten, cfg, flow, card, comments, phase, args, unanswered)
        except AgentAuthError:
            raise
        except FactoryError as e:
            log(f"  !! фаза «{phase}» не удалась: {e}")

    if not args.prompt_only:
        write_status(card=None, phase=None, agent=None, epics_waiting=waiting,
                     waiting=AWAITING["cards"])
    return 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

# Что показывать в меню-баре про каждую колонку. Порядок важен: роли на чужой доске
# делят колонки, и карточку относим к первому подходящему состоянию.
# Роль колонки -> (где карточка стоит, что фабрика сделает дальше).
FLOW_STATES = [
    ("in_progress", "в работе", "агент напишет код и откроет PR"),
    ("agent_review", "ждёт ревьювера", "ревьювер посмотрит PR"),
    ("fixes", "правки после ревью", "агент поправит по замечаниям"),
    ("review", "на ревью у человека", "твоя очередь — посмотреть PR"),
]


def snapshot_flow(kaiten: Kaiten, cfg: dict, profiles: list[dict]) -> None:
    """
    Снимок того, что у фабрики в работе прямо сейчас — для человечка в меню-баре.

    Без него в трее видно только карточку текущего прогона, да и то пока он идёт.
    А человек хочет развернуть меню и увидеть весь фронт работ: что пишется, что
    ждёт ревьювера, что уехало к нему самому.

    Заблокированные карточки сюда не попадают: они уже в списке «ждут тебя», и
    показывать их дважды значит запутать.
    """
    flow, seen = [], set()

    for profile in profiles:
        for role, state, upcoming in FLOW_STATES:
            column = role_column(profile, role)
            if not column:
                continue
            for card in kaiten.cards_in_column(profile["board_id"], column):
                card_id = card["id"]
                if card_id in seen or theirs(profile, card):
                    continue
                if blocked_by(kaiten, card_id):
                    continue
                seen.add(card_id)
                flow.append({
                    "id": card_id,
                    "title": (card.get("title") or "").strip(),
                    "kind": "card",
                    "state": state,
                    "next": upcoming,
                    "url": kaiten.card_url(card),
                })

    # эпики: у них состояние — это фаза, и она сама по себе объясняет, что происходит
    flow_cfg = epic_flow(cfg)
    if flow_cfg:
        for card in pick_epics(kaiten, cfg, flow_cfg):
            card_id = card["id"]
            if card_id in seen:
                continue
            full = kaiten.card(card_id)
            phase = epic_phase(kaiten, cfg, flow_cfg, full, kaiten.comments(card_id))
            if phase == "waiting_answer":
                continue  # он уже в списке «ждут тебя»
            seen.add(card_id)
            flow.append({
                "id": card_id,
                "title": (card.get("title") or "").strip(),
                "kind": "epic",
                "state": EPIC_PHASE_STATE.get(phase, phase),
                "next": EPIC_PHASE_NEXT.get(phase, EPIC_PHASE_LABELS.get(phase, phase)),
                "url": kaiten.card_url(card),
            })

    write_status(flow=flow)
    if flow:
        log("в работе: " + ", ".join(f"#{c['id']} ({c['state']})" for c in flow))


def profile_for_card(kaiten: Kaiten, profiles: list[dict], card_id: int) -> dict:
    """
    Какому профилю принадлежит карточка, вызванная руками через --card.

    Досок теперь может быть две, и колонки у них разные: подставить чужой профиль
    значит увезти карточку в колонку соседней доски.
    """
    if len(profiles) == 1:
        return profiles[0]
    board_id = (kaiten.card(card_id) or {}).get("board_id")
    for profile in profiles:
        if profile["board_id"] == board_id:
            return profile
    log(f"#{card_id} лежит на доске {board_id}, профиля для неё нет — беру «{profiles[0]['key']}»")
    return profiles[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Kaiten -> Claude Code -> PR")
    parser.add_argument("--dry-run", action="store_true",
                        help="прогнать агента, но ничего не писать в Kaiten/GitHub")
    parser.add_argument("--card", type=int, help="обработать конкретную карточку по id")
    parser.add_argument("--limit", type=int, help="сколько карточек взять за прогон")
    parser.add_argument("--keep-worktree", action="store_true", help="не удалять рабочую копию")
    parser.add_argument("--no-pr", action="store_true", help="запушить ветку, но не создавать PR")
    parser.add_argument("--prompt-only", action="store_true",
                        help="показать промпт и выйти: агент не запускается, карточка не двигается")
    parser.add_argument("--link-review", action="store_true",
                        help="привязать все карточки из «На ревью» к долгу спринта и выйти")
    parser.add_argument("--only-merged", action="store_true",
                        help="только фаза мержа: закрыть карточки со смерженным PR и выйти")
    parser.add_argument("--no-merged", action="store_true",
                        help="не проверять, что смержено")
    parser.add_argument("--only-review", action="store_true",
                        help="только фаза ревью: пройти «Ревью агента» и выйти")
    parser.add_argument("--only-work", action="store_true",
                        help="только фаза работы: пропустить ревью")
    parser.add_argument("--only-triage", action="store_true",
                        help="только разведка инбокса: посмотреть новые карточки и выйти")
    parser.add_argument("--no-triage", action="store_true",
                        help="пропустить разведку инбокса")
    parser.add_argument("--only-debt", action="store_true",
                        help="только привязать задачи спринта без эпика к техдолгу и выйти")
    parser.add_argument("--triage-card", type=int,
                        help="разведать конкретную карточку инбокса по id")
    parser.add_argument("--only-epics", action="store_true",
                        help="только фаза эпиков: продвинуть их и выйти")
    parser.add_argument("--no-epics", action="store_true", help="пропустить фазу эпиков")
    parser.add_argument("--only-time", action="store_true",
                        help="только прочитать треды в Time и выйти")
    parser.add_argument("--no-time", action="store_true",
                        help="полный прогон без чтения тредов в Time")
    parser.add_argument("--time-test", action="store_true",
                        help="послать в канал Time тестовое сообщение и выйти")
    parser.add_argument("--time-channels", action="store_true",
                        help="показать каналы бота списком (JSON) и выйти")
    parser.add_argument("--time-announce", type=int, metavar="CARD_ID",
                        help="объявить в канал уже открытый PR карточки и выйти")
    parser.add_argument("--epic-card", type=int,
                        help="продвинуть конкретный эпик по id, игнорируя тег и выборку")
    args = parser.parse_args()

    # Отсюда про сухой прогон узнают те, кто пишет состояние на диск
    global DRY_RUN
    DRY_RUN = args.dry_run

    if args.time_channels:
        # печатаем JSON — значит в stdout не должно быть ничего, кроме него
        global MACHINE_OUTPUT
        MACHINE_OUTPUT = True

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    env = load_env()
    # Долгоживущий токен агента, если человек его завёл (`claude setup-token`) и положил
    # в .env рядом с KAITEN_TOKEN. Без него claude ходит с обычной сессией, а она живёт
    # сутки: протухла ночью — и до утра фабрика молча падает на 401 в каждой карточке.
    # Значение не логируем и в статус не кладём.
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = env["CLAUDE_CODE_OAUTH_TOKEN"]
    domain = env.get("KAITEN_DOMAIN") or cfg["kaiten"]["domain"]
    kaiten = Kaiten(domain, env["KAITEN_TOKEN"], cfg["kaiten"]["space_id"], dry_run=args.dry_run)

    WORKTREES.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    if not args.prompt_only:
        # pid нужен менюбар-приложению, чтобы при выходе прибить и питон, и агента.
        # run_started — чтобы оно могло сказать, сколько прогон уже идёт: без этого
        # зависший git выглядел в трее как обычная работа.
        #
        # Карточку, на которую показали пальцем, объявляем сразу. Раньше тут стояло
        # card=None, и после кнопки «сделать следующий шаг» по конкретной задаче в
        # меню-баре висело безличное «смотрю доску»: подтверждения, что взяли именно
        # её, человек не получал до первого настоящего шага — а это полминуты.
        # Названия мы ещё не знаем, его подставит первый же write_status с карточкой;
        # меню-бар до тех пор берёт название из своего списка «В потоке» по номеру.
        target = args.card or args.epic_card or args.triage_card
        write_status(pid=os.getpid(),
                     card={"id": target} if target else None,
                     phase=f"беру #{target}" if target else "смотрю доску",
                     agent=None,
                     run_started=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        forget_auth_note()
    if args.dry_run:
        log("DRY-RUN: Kaiten и GitHub не трогаем, агент отработает по-настоящему")

    profiles = board_profiles(cfg)
    if len(profiles) > 1:
        log("досок в работе: " + ", ".join(f"«{p['key']}»" for p in profiles))

    # Снимок и на входе, и на выходе. Раньше он собирался только в самом конце полного
    # прогона, а --only-epics и --epic-card выходят раньше и его минуют: в трее висело
    # состояние часовой давности, и человек справедливо считал, что ничего не двигается.
    # Проверке связи снимок не нужен вовсе: она ничего не берёт из Kaiten, а обход досок
    # добавил бы к нажатию кнопки в окне настроек двадцать секунд ожидания.
    snapshot = not (args.prompt_only or args.time_test or args.time_channels
                    or args.time_announce)
    if snapshot and not refresh_flow(kaiten, cfg, profiles):
        # Дальше идти некуда: весь прогон — это решения по тому, что сейчас на доске,
        # а доски мы не видели. Семнадцатого сентября он всё-таки пошёл и по дороге
        # решил, что двух карточек больше не существует. Следующий запуск через
        # десять минут застанет сеть уже поднятой.
        log("Kaiten не отвечает — прогон отменяю, попробую в следующий раз")
        clear_phase()
        # 69 (EX_UNAVAILABLE), а не 1: меню-бар показывает человеку внятную причину,
        # а не «завершился с кодом 1». 75 тут занят чужим замком в run.sh.
        return 69
    try:
        return route(kaiten, cfg, args, profiles)
    finally:
        if snapshot:
            refresh_flow(kaiten, cfg, profiles)
            # Канал — та же витрина, только для тех, кто в Kaiten не смотрит. Прогон
            # только что развёз карточки по колонкам, и знать об этом канал должен
            # сейчас, а не через час, когда доска поедет в следующий раз.
            refresh_time_status(kaiten, cfg, args, profiles)


def refresh_flow(kaiten: Kaiten, cfg: dict, profiles: list[dict]) -> bool:
    """
    Снимок витрины. Отдаёт False, только если до Kaiten не достучались вовсе, — по
    этому входной снимок решает, стоит ли вообще начинать прогон. Любая другая
    беда со снимком False не даёт: витрина не важнее сделанной работы.
    """
    try:
        snapshot_flow(kaiten, cfg, profiles)
    except NetworkError as e:
        log(f"снимок состояния не собрался: {e}")
        return False
    except Exception as e:  # noqa: BLE001 — витрина не важнее сделанной работы
        log(f"снимок состояния не собрался: {e}")
    return True


def route(kaiten: Kaiten, cfg: dict, args, profiles: list[dict]) -> int:
    if args.link_review:
        return link_review_cards(kaiten, cfg, args.dry_run)

    if args.triage_card:
        return triage_inbox(kaiten, cfg, args, only_card=args.triage_card)
    if args.only_debt:
        sweep_sprint_orphans(kaiten, cfg, args)
        return 0
    if args.only_triage:
        # Каналы Time — и здесь: инбокс ходит раз в десять минут, а не раз в час, и
        # позвавший Стёпу человек узнает «взял» или «не возьму» сразу. Саму карточку
        # возьмёт в работу прогон доски
        if not args.no_time:
            try:
                follow_time_mentions(kaiten, cfg, args, profiles)
            except Exception as e:  # noqa: BLE001 — мессенджер не должен ронять разведку
                log(f"каналы в Time не задались: {e}")
        # Техдолг ходит вместе с инбоксом: оба дешёвые и про то, чтобы ни одна задача
        # не висела ничьей. После разведки — она могла только что поставить задачу
        # в спринт, и та сразу ляжет в техдолг, а не через круг
        try:
            return triage_inbox(kaiten, cfg, args)
        finally:
            sweep_sprint_orphans(kaiten, cfg, args)
    if args.epic_card:
        return run_epics(kaiten, cfg, args, only_card=args.epic_card)
    if args.only_epics:
        return run_epics(kaiten, cfg, args)
    if args.time_test:
        return time_test(cfg, args)
    if args.time_channels:
        return time_channels(cfg, args)
    if args.time_announce:
        return time_announce(kaiten, cfg, args, args.time_announce)
    if args.only_merged:
        return close_merged(kaiten, cfg, args, profiles)
    if args.only_time:
        follow_time_threads(kaiten, cfg, args, profiles)
        follow_time_mentions(kaiten, cfg, args, profiles)
        return 0

    # Мерж — первым: он дешёвый (один `gh` на карточку, агента нет) и убирает с доски
    # то, что уже влито. Перед тредами в Time намеренно: карточка уезжает в «Готово»,
    # и строка статуса в канале снимается тем же прогоном, а не следующим.
    if not (args.card or args.only_review or args.only_work or args.no_merged):
        try:
            close_merged(kaiten, cfg, args, profiles)
        except Exception as e:  # noqa: BLE001 — GitHub не должен ронять поток
            log(f"проверка мержа не задалась: {e}")

    # Треды в Time — до агентов: это дешёвый запрос без агента, а карточка после него
    # уезжает в «Правки», и работа в этом же прогоне её подхватит.
    if not (args.card or args.no_time):
        try:
            follow_time_threads(kaiten, cfg, args, profiles)
        except Exception as e:  # noqa: BLE001 — мессенджер не должен ронять поток
            log(f"треды в Time не задались: {e}")
        # Каналы — после тредов: взятая отсюда карточка встаёт в «Очередь», и работа
        # этого же прогона её подхватит
        try:
            follow_time_mentions(kaiten, cfg, args, profiles)
        except Exception as e:  # noqa: BLE001 — мессенджер не должен ронять поток
            log(f"каналы в Time не задались: {e}")

    # Разведка идёт первой из агентов: она дешёвая и быстрая, а на другом конце сидит
    # человек, который только что закинул карточку в инбокс и ждёт, что ему ответят.
    if not (args.card or args.only_review or args.only_work or args.no_triage):
        try:
            triage_inbox(kaiten, cfg, args)
        except AgentAuthError:
            raise
        except Exception as e:  # noqa: BLE001 — чужая доска не должна ронять основной поток
            log(f"разведка инбокса не задалась: {e}")
        sweep_sprint_orphans(kaiten, cfg, args)

    # Эпики следующими: почти всегда это дешёвая проверка — висит ли наш блокер, все ли
    # сабтаски отревьюены. Агент запускается, только когда фаза действительно сменилась.
    if not (args.card or args.only_review or args.only_work or args.no_epics):
        try:
            run_epics(kaiten, cfg, args)
        except Exception as e:  # noqa: BLE001 — чужая доска не должна ронять основной поток
            log(f"фаза эпиков не задалась: {e}")

    # Ревью идёт раньше работы: оно разблокирует цикл — либо отдаёт карточку человеку,
    # либо кладёт её в «Правки», откуда исполнитель тут же её и подхватит.
    if not args.card and not args.only_work:
        limit = args.limit if args.limit is not None else cfg.get("max_cards_per_run", 2)
        for profile in profiles:
            # в колонке ревью лежат и карточки, ждущие агента, и те, что он уже
            # отсмотрел и отдал человеку под блокером — вторые не наши
            to_review = skip_off_hours(
                [c for c in kaiten.cards_in_column(profile["board_id"],
                                                   role_column(profile, "agent_review"))
                 if not theirs(profile, c) and not blocked_by(kaiten, c["id"])], cfg)
            where = f" ({profile['key']})" if len(profiles) > 1 else ""
            if not to_review:
                log(f"в «Ревью агента»{where} пусто")
                continue
            log(f"в «Ревью агента»{where} {len(to_review)} карточек, "
                f"беру {min(limit, len(to_review))}")
            for card in to_review[:limit]:
                if out_of_budget(cfg):
                    break
                try:
                    review_card(card, kaiten, cfg, args, profile)
                except AgentAuthError:
                    raise
                except FactoryError as e:
                    log(f"#{card['id']} ревью не удалось начать: {e}")

    if args.only_review:
        write_status(card=None, phase=None)
        return 0

    limit = args.limit if args.limit is not None else cfg.get("max_cards_per_run", 2)

    if args.card:
        # карточку по номеру ищем среди досок: она может лежать и на доске сабтасок
        profile = profile_for_card(kaiten, profiles, args.card)
        work = [(profile, [{"id": args.card}])]
        pending = 1
    else:
        work, pending = [], 0
        for profile in profiles:
            cards = pick_cards(kaiten, cfg, profile)
            pending += len(cards)
            where = f" ({profile['key']})" if len(profiles) > 1 else ""
            if not cards:
                log(f"брать нечего{where}")
                continue
            log(f"к работе{where} {len(cards)} карточек, беру {min(limit, len(cards))}")
            work.append((profile, cards[:limit]))
        if not work:
            if not args.prompt_only:
                write_status(card=None, phase=None, queue=0)
            return 0
        if not args.prompt_only:
            write_status(queue=pending)

    for profile, cards in work:
        for card in cards:
            if out_of_budget(cfg):
                break
            try:
                process(card, kaiten, cfg, args, profile)
            except AgentAuthError:
                raise
            except FactoryError as e:
                log(f"#{card['id']} не удалось даже начать: {e}")
    if not args.prompt_only:
        write_status(card=None, phase=None, agent=None, run_started=None,
                     night_waiting=NIGHT_WAITING["count"])
    return 0


def clear_phase() -> None:
    """
    Убирает «чем занята» из статуса.

    Без этого прерванный или упавший прогон оставляет в status.json последнюю фазу,
    и человечек в меню-баре показывает работу, которой давно нет.
    """
    try:
        if STATUS_FILE.is_file():
            write_status(card=None, phase=None, agent=None, run_started=None)
    except Exception:  # noqa: BLE001 — на выходе падать уже незачем
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FactoryError as e:
        log(f"фатально: {e}")
        clear_phase()
        sys.exit(1)
    except KeyboardInterrupt:
        log("прервано")
        clear_phase()
        sys.exit(130)
    except BaseException:
        clear_phase()
        raise
