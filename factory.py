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
import subprocess
import sys
import tempfile
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
LOGS = ROOT / "logs"
STATE = ROOT / "state"
STATUS_FILE = STATE / "status.json"
# счётчик неудач разведки по карточкам: не долбить одну и ту же карточку каждые 10 минут
TRIAGE_STATE_FILE = STATE / "triage.json"

ENV_CANDIDATES = [ROOT / ".env", Path.home() / ".claude" / ".env"]

# Комментарии агента помечаем, чтобы отличать их от человеческих: и агент, и человек
# пишут под одним API-токеном, по автору их не различить.
AGENT_MARK = "🤖"
# Ревьювер пишет своей меткой: по ней считаем круги ревью
REVIEWER_MARK = "🔍"
# Разведчик инбокса — своей. Без вариационного селектора: эмодзи ездит через два API,
# и лишний невидимый символ ломал бы сравнение startswith
TRIAGE_MARK = "🧭"
# всё, что написано роботами. Комментарий не с этой метки — реплика человека
AGENT_MARKS = (AGENT_MARK, REVIEWER_MARK, TRIAGE_MARK)

# Строка в описании рабочей карточки: из какой карточки инбокса она выросла. По ней же
# ловится дубль, если разведка почему-то зайдёт на ту карточку второй раз.
INBOX_ORIGIN = "Из инбокса:"
INBOX_ORIGIN_RE = re.compile(r"Из инбокса:\s*#(\d+)")

# Строка в комментарии инбокса: задача уже поставлена. Разведка к такой карточке
# больше не возвращается, даже если в ней продолжают переписку.
HANDOFF_LINE = "Поставил задачу на «Доску для клода»:"

# Ручной стоп-кран: фраза в карточке выключает по ней всех агентов. Список можно
# дополнить в config.json, ключ stop_phrases.
STOP_PHRASES = [
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
        "joke": {"type": "string"},
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
        "joke": {"type": "string"},
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
        "joke": {"type": "string"},
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
        "joke": {"type": "string"},
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
        "joke": {"type": "string"},
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
        "joke": {"type": "string"},
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
        "joke": {"type": "string"},
    },
    "required": ["status", "problem"],
    "additionalProperties": False,
}

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

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


class ScoutSetupError(RuntimeError):
    """Разведка не смогла подготовить рабочую копию: это беда окружения, а не карточки."""


class FactoryError(RuntimeError):
    """Ошибка обёртки — карточка уедет в 'Упало'."""


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
    data.update(fields)
    data["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = STATUS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATUS_FILE)


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


# --------------------------------------------------------------------------- #
# Kaiten
# --------------------------------------------------------------------------- #

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
        """
        url = self.api + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        cmd = ["curl", "-sS", "-K", "-", "--max-time", "30",
               "-X", method, "-H", "Content-Type: application/json",
               "-w", "\n%{http_code}", url]
        if body is not None:
            cmd += ["--data-raw", json.dumps(body, ensure_ascii=False)]

        proc = subprocess.run(cmd, input=f'header = "Authorization: Bearer {self.token}"\n',
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise FactoryError(f"Kaiten {method} {path} -> curl {proc.returncode}: "
                               f"{proc.stderr.strip()[:300]}")

        raw, _, code = proc.stdout.rpartition("\n")
        if code.strip() and int(code) >= 400:
            raise FactoryError(f"Kaiten {method} {path} -> HTTP {code}: {raw[:500]}")
        return json.loads(raw) if raw.strip() else None

    def _write(self, method: str, path: str, body=None):
        if self.dry_run:
            log(f"  [dry-run] {method} {path} {json.dumps(body, ensure_ascii=False)[:160]}")
            return None
        return self._request(method, path, body)

    # -- чтение ------------------------------------------------------------- #

    def cards_on_board(self, board_id: int, with_description: bool = False) -> list:
        """Все живые карточки доски. API отдаёт максимум 100 за раз."""
        found, offset = [], 0
        while True:
            params = {"board_id": board_id, "archived": "false", "condition": 1,
                      "limit": 100, "offset": offset}
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
        data = self._request("GET", f"/cards/{card_id}/comments") or []
        return sorted(data, key=lambda c: c.get("created") or "")

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


def has_tag(card: dict, name: str) -> bool:
    """
    Есть ли на карточке такой тег.

    В выдаче списком ключ `tags` появляется только у карточек, у которых теги реально
    стоят: у остальных его нет вовсе. Поэтому `or []` здесь обязателен.
    """
    wanted = normalize_phrase(name)
    return any(normalize_phrase(tag.get("name")) == wanted for tag in (card.get("tags") or []))


# --------------------------------------------------------------------------- #
# ночные задачи
# --------------------------------------------------------------------------- #

# Карточку с этим тегом фабрика берёт только ночью. Смысл — тяжёлые и шумные задачи:
# долгие прогоны тестов, массовые правки, всё, что мешает работать днём.
NIGHT_TAG = "claude:night"
NIGHT_FROM = 22
NIGHT_TO = 5


def night_config(cfg: dict) -> tuple[str, int, int]:
    night = cfg.get("night") or {}
    return (night.get("tag") or NIGHT_TAG,
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
# это снятие означает «продолжай». Другого признака апрува у нас нет: и агент,
# и человек пишут под одним токеном, по автору их не различить.
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


def blocked_by(kaiten: Kaiten, card_id: int) -> str | None:
    """Причина, по которой карточку сейчас трогать нельзя. None — путь свободен."""
    for blocker in kaiten.blockers(card_id):
        return str(blocker.get("reason") or "заблокирована")
    return None


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


def make_profile(key: str, board: dict, repo_key: str | None = None,
                 attach_to_debt: bool = True, own_only: bool = False) -> dict:
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
        # команды так нельзя: там свой бэклог, и фабрика должна трогать только те
        # карточки, которые сама и создала — их узнаём по строке «Из эпика: #<id>».
        "own_only": own_only,
    }


def board_profiles(cfg: dict) -> list[dict]:
    """
    Доски, по которым ходят исполнитель и ревьювер.

    Рабочая доска есть всегда. Доска сабтасок появляется, когда включён режим эпиков:
    у неё и колонки другие, и репозиторий может быть другой.
    """
    profiles = [make_profile("работа", cfg["kaiten"])]
    subtasks = ((cfg.get("epic_flow") or {}).get("subtasks") or {})
    if subtasks.get("board_id"):
        # сабтаска уже висит дочерней на эпике: второй родитель в виде долга спринта
        # только раздует описание долга, поэтому туда её не привязываем
        profiles.append(make_profile("сабтаски", subtasks, subtasks.get("repo"),
                                     attach_to_debt=False, own_only=True))
    return profiles


def role_column(profile: dict, role: str) -> int | None:
    return profile["columns"].get(role)


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

def git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if check and proc.returncode != 0:
        raise FactoryError(f"git {' '.join(args)} -> {proc.returncode}: {proc.stderr.strip()[:800]}")
    return proc.stdout.strip()


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


def remote_branch_for_card(repo: Path, remote: str, prefix: str, card_id: int) -> str | None:
    """Ищет уже запушенную ветку карточки: имя могло смениться вместе с заголовком."""
    git(repo, "fetch", remote, "--prune", check=False)
    output = git(repo, "ls-remote", "--heads", remote,
                 f"{prefix}{card_id}", f"{prefix}{card_id}-*", check=False)
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
> Замечания ревьювера — в комментариях выше, помечены значком 🔍, бери самое последнее.
> Закрой их все: `🛑` и `⚠️` обязательно, `💬` — по возможности. Правь ровно то, на что
> указано, попутных изменений не вноси.
>
> Если с замечанием ты не согласен — не игнорируй его молча: сделай остальное, а по этому
> напиши в `risks`, почему считаешь текущий код верным. Это уйдёт человеку.
>
> Коммить обычным порядком, одним коммитом поверх прошлых.
"""


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


def project_replacements() -> dict:
    return {
        "{{PROJECT_DOCS}}": read_project_file(
            PROJECT_DOCS_PATH,
            "- `CLAUDE.md` в корне и ближайший `AGENTS.md` к пакету, который трогаешь.\n"
            "  (Список правил проекта не заполнен — смотри, что есть в репозитории.)"),
        "{{PROJECT_CHECKLIST}}": read_project_file(
            PROJECT_CHECKLIST_PATH,
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
                 returning: bool = False, fixing: bool = False) -> str:
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
        "{{RETURN_NOTE}}": FIX_NOTE if fixing else (RETURN_NOTE if returning else ""),
        **project_replacements(),
    }
    return apply_template(template, replacements)


# --------------------------------------------------------------------------- #
# агент
# --------------------------------------------------------------------------- #

def run_agent(prompt: str, cwd: Path, agent_cfg: dict,
              schema: dict | None = None, verdict_key: str = "status") -> tuple[dict, dict]:
    """Возвращает (verdict, meta). Кидает FactoryError, если claude не отработал."""
    schema = schema or VERDICT_SCHEMA
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
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

    env = os.environ.copy()
    # claude отказывается стартовать внутри другой сессии Claude Code
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), env=env, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=agent_cfg["timeout_sec"],
        )
    except subprocess.TimeoutExpired as e:
        raise FactoryError(f"агент не уложился в {agent_cfg['timeout_sec']}с") from e

    if proc.returncode != 0:
        raise FactoryError(f"claude exit {proc.returncode}: {(proc.stderr or proc.stdout)[:1500]}")

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise FactoryError(f"claude вернул не JSON: {proc.stdout[:800]}") from e

    meta = {
        "cost_usd": payload.get("total_cost_usd"),
        "duration_ms": payload.get("duration_ms"),
        "num_turns": payload.get("num_turns"),
        "is_error": payload.get("is_error"),
        "subtype": payload.get("subtype"),
        "session_id": payload.get("session_id"),
        "raw": payload,
    }

    verdict = extract_verdict(payload, verdict_key)
    if verdict is None:
        # структурированный ответ не доехал — не выдумываем вердикт, решим по коммитам
        verdict = {verdict_key: None, "summary": str(payload.get("result") or "")[:4000]}
    return verdict, meta


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

# Карточки, ждущие ответа человека, суммарно по всем доскам за прогон
AWAITING = {"count": 0}


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
    bits = []
    if meta.get("duration_ms"):
        bits.append(f"{round(meta['duration_ms'] / 1000)}с")
    if meta.get("cost_usd"):
        bits.append(f"~${meta['cost_usd']:.2f}")
    if meta.get("num_turns"):
        bits.append(f"{meta['num_turns']} шагов")
    return ", ".join(bits)


def format_list(title: str, items) -> str:
    items = [i for i in (items or []) if str(i).strip()]
    if not items:
        return ""
    body = "\n".join(f"- {i}" for i in items)
    return f"\n\n**{title}**\n{body}"


def format_joke(verdict: dict) -> str:
    joke = (verdict.get("joke") or "").strip()
    return f"\n\n> {joke}" if joke else ""


def comment_success(verdict: dict, meta: dict, pr_url: str, branch: str) -> str:
    text = f"🤖 **Готово, нужен ревью.**\n\nPR: {pr_url}\nВетка: `{branch}`"
    text += f"\n\n{verdict.get('summary', '')}"
    if verdict.get("risks"):
        text += f"\n\n**Риски:** {verdict['risks']}"
    if format_meta(meta):
        text += f"\n\n_Агент: {format_meta(meta)}._"
    text += format_joke(verdict)
    return text


def comment_question(verdict: dict, meta: dict) -> str:
    text = "🤖 **Не берусь — не хватает информации.** Ничего не менял.\n\n"
    text += verdict.get("summary", "")
    text += format_list("Что нужно уточнить", verdict.get("questions"))
    text += "\n\nПоправь описание карточки и верни её в «Очередь»."
    if format_meta(meta):
        text += f"\n\n_Агент: {format_meta(meta)}._"
    text += format_joke(verdict)
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
    return key, cfg["repos"][key]


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
                # только колонка: state эпика не трогаем
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
            verdict: dict, pr_cfg: dict, dry_run: bool) -> str:
    title = f"#{card['id']} {(card.get('title') or '').strip()[:70]}"
    body = compose_pr_body(worktree, card, card_url, verdict)
    template = find_pr_template(worktree)
    log(f"  шаблон PR: {template.relative_to(worktree) if template else 'нет, беру наш текст'}")

    if dry_run:
        log(f"  [dry-run] gh pr create --base {base} --head {branch} "
            f"(тело {len(body)} символов)")
        return "(dry-run, PR не создан)"

    existing = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url",
         "--jq", ".[0].url"],
        cwd=str(worktree), capture_output=True, text=True, stdin=subprocess.DEVNULL,
    ).stdout.strip()
    if existing:
        log(f"  PR для ветки уже открыт: {existing}")
        return existing

    # тело отдаём файлом: шаблоны бывают на десятки килобайт, в argv их тащить незачем
    handle = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
    try:
        handle.write(body)
        handle.close()
        cmd = ["gh", "pr", "create", "--base", base, "--head", branch,
               "--title", title, "--body-file", handle.name]
        if pr_cfg.get("draft", True):
            cmd.append("--draft")
        proc = subprocess.run(cmd, cwd=str(worktree), capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=120)
    finally:
        os.unlink(handle.name)

    if proc.returncode != 0:
        raise FactoryError(f"gh pr create -> {proc.returncode}: {proc.stderr.strip()[:800]}")
    lines = proc.stdout.strip().splitlines()
    if not lines:
        raise FactoryError("gh pr create отработал, но не вернул ссылку на PR")
    return lines[-1]


def finish_status(card_id: int, title: str, outcome: str, meta, pr_url: str = "",
                  card_url: str = "") -> None:
    write_status(card=None, phase=None, last={
        "card_id": card_id,
        "title": title,
        "outcome": outcome,
        "pr": pr_url,
        # у разведки PR нет, но открыть карточку из меню-бара всё равно хочется
        "url": card_url,
        "cost_usd": (meta or {}).get("cost_usd"),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


def own_subtask(card: dict) -> bool:
    """Карточка создана фабрикой из эпика, а не человеком."""
    return bool(EPIC_ORIGIN_RE.search(strip_html(card.get("description")) or ""))


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
        return kaiten.cards_in_column(profile["board_id"], fixes_column)


    found = []
    for card in kaiten.cards_in_column(profile["board_id"], in_progress):
        if profile.get("own_only") and not own_subtask(card):
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
        # заблокированную карточку не берём, даже если человек уже ответил: блокер
        # снимает он же, и пока он висит — это его ход, а не наш
        if blocked_by(kaiten, card["id"]):
            waiting += 1
            continue
        history = kaiten.comments(card["id"])
        last = strip_html(history[-1].get("text", "")) if history else ""
        if history and not last.startswith(AGENT_MARKS):
            answered.append(card)
        else:
            waiting += 1
    if answered:
        log(f"в «Вопросе от агента» ответили на {len(answered)}: "
            f"{', '.join('#' + str(c['id']) for c in answered)}")
    if waiting:
        log(f"ещё {waiting} карточек ждут твоего ответа")
    # человечек в меню-баре по этому числу решает, вопрошать ему или спать. Досок может
    # быть несколько, поэтому копим, а не перетираем — иначе видно только последнюю
    AWAITING["count"] += waiting
    write_status(awaiting_answer=AWAITING["count"])

    fixes = fix_round_cards(kaiten, profile)
    if fixes:
        log(f"в «Правках» {len(fixes)}: {', '.join('#' + str(c['id']) for c in fixes)}")
    # правки вперёд: PR уже открыт, домучить его дешевле, чем начинать новую задачу.
    # потом отвеченные вопросы — человек только что написал, ему нужен быстрый отклик
    queue = kaiten.cards_in_column(board_id, cols["queue"])
    picked = skip_off_hours(skip_hands_off(kaiten, fixes + answered + queue, cfg), cfg)
    if profile.get("own_only"):
        before = len(picked)
        picked = [c for c in picked if own_subtask(c)]
        if before != len(picked):
            log(f"на доске «{profile['key']}» {before - len(picked)} чужих карточек — "
                f"их фабрика не трогает")
    return [c for c in picked if not blocked_by(kaiten, c["id"])]


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
        **project_replacements(),
    }
    return apply_template(template, replacements)


def format_findings(findings) -> str:
    if not findings:
        return ""
    lines = []
    for item in findings:
        icon = SEVERITY_ICON.get(item.get("severity", ""), "•")
        lines.append(f"\n{icon} `{item.get('where', '?')}` — {item.get('what', '')}")
        if item.get("why"):
            lines.append(f"  причина: {item['why']}")
        if item.get("fix"):
            lines.append(f"  → {item['fix']}")
    return "\n\n**Замечания**\n" + "\n".join(lines)


def comment_review(review: dict, meta: dict, round_number: int, max_rounds: int,
                   needs_changes: bool) -> str:
    head = ("нужны правки" if needs_changes else "замечаний нет")
    text = f"{REVIEWER_MARK} **Ревью, круг {round_number}/{max_rounds}: {head}.**\n\n"
    text += review.get("summary", "")
    text += format_findings(review.get("findings"))
    if needs_changes:
        text += "\n\nОтправляю в «Правки», исполнитель поправит и вернёт на ревью."
    if format_meta(meta):
        text += f"\n\n_Ревьювер: {format_meta(meta)}._"
    text += format_joke(review)
    return text


def count_review_rounds(comments: list) -> int:
    return sum(1 for c in comments
               if strip_html(c.get("text", "")).startswith(REVIEWER_MARK)
               and "продолжение" not in strip_html(c.get("text", ""))[:40])


def post_pr_review(worktree: Path, pr_url: str, body: str) -> None:
    handle = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
    try:
        handle.write(body)
        handle.close()
        proc = subprocess.run(["gh", "pr", "comment", pr_url, "--body-file", handle.name],
                              cwd=str(worktree), capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=120)
        if proc.returncode != 0:
            log(f"  !! не смог оставить комментарий в PR: {proc.stderr.strip()[:200]}")
    finally:
        os.unlink(handle.name)


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

    branch = remote_branch_for_card(repo, repo_cfg["remote"],
                                    cfg["pr"]["branch_prefix"], card_id)
    if not branch:
        log("  запушенной ветки нет")
        if args.prompt_only:
            return
        kaiten.comment(card_id, f"{REVIEWER_MARK} **Ревьювить нечего:** запушенной ветки "
                                f"`{cfg['pr']['branch_prefix']}{card_id}*` нет.")
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
        make_worktree(repo, f"review/card-{card_id}", repo_cfg["base_branch"],
                      repo_cfg["remote"], worktree, continue_from=branch)

        # ветка в ревью-worktree своя (review/card-N), поэтому PR ищем по имени
        # ревьюемой ветки, а не по текущей: `gh pr view` без --head смотрит на текущую
        pr_url = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--state", "all",
             "--json", "url", "--jq", ".[0].url"],
            cwd=str(worktree), capture_output=True, text=True, stdin=subprocess.DEVNULL,
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

        body = comment_review(review, meta, round_number, max_rounds, needs_changes)
        kaiten.comment(card_id, body)
        if pr_url and reviewer_cfg.get("post_to_pr") and not args.dry_run:
            post_pr_review(worktree, pr_url, body)

        if needs_changes:
            # правки идут в рабочую колонку: агент подхватит карточку следующим прогоном
            kaiten.move(card_id, role_column(profile, "fixes")
                        or role_column(profile, "in_progress"))
            finish_status(card_id, title, "правки", meta)
            log("  -> Правки")
        else:
            hand_over(kaiten, profile, card_id, "review")
            finish_status(card_id, title, "ревью пройдено", meta, pr_url)
            log("  -> На ревью (человеку)")

    except Exception as e:  # noqa: BLE001
        log(f"  !! {e}")
        if not args.prompt_only:
            kaiten.comment(card_id, comment_failed(str(e)))
            hand_over(kaiten, profile, card_id, "failed", str(e)[:120])
            finish_status(card_id, title, "упало на ревью", None)
            log("  -> Упало")
    finally:
        if args.keep_worktree or cfg.get("keep_worktree"):
            log(f"  worktree ревью оставлен: {worktree}")
        else:
            drop_worktree(repo, worktree)


def link_review_cards(kaiten: Kaiten, cfg: dict, dry_run: bool) -> int:
    """Разовая операция: привязать всё, что уже висит в «На ревью», к долгу спринта."""
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
    такой колонки на доске нет) стоит в рабочей, а последнее слово за ревьювером.
    """
    fixes_column = role_column(profile, "fixes")
    if fixes_column and fixes_column != role_column(profile, "in_progress"):
        return card.get("column_id") == fixes_column
    texts = [strip_html(c.get("text", "")) for c in comments]
    return bool(texts) and texts[-1].startswith(REVIEWER_MARK)


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
    branch = f"{cfg['pr']['branch_prefix']}{card_id}" + (f"-{slug}" if slug else "")
    continue_from = None
    if fixing:
        # правки идут в ту же ветку и тот же PR, иначе ревью пойдёт по кругу с нуля
        continue_from = remote_branch_for_card(repo, repo_cfg["remote"],
                                               cfg["pr"]["branch_prefix"], card_id)
        if continue_from:
            branch = continue_from

    reason = " (правки после ревью)" if fixing else (" (ответ на вопрос)" if returning else "")
    log(f"#{card_id} «{title[:60]}» -> {repo_key} / {branch}{reason}")

    if not args.prompt_only:
        kaiten.move(card_id, cols["in_progress"])
        write_status(card={"id": card_id, "title": title, "url": card_url},
                     phase="готовлю рабочую копию", returning=returning)

    try:
        make_worktree(repo, branch, repo_cfg["base_branch"], repo_cfg["remote"], worktree,
                      continue_from=continue_from)
        prompt = build_prompt(card, comments, repo_cfg, branch, card_url,
                              returning=returning, fixing=fixing)

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

        # Нет коммитов — обсуждать нечего, что бы агент ни написал.
        if not commits:
            if status not in ("unclear", "blocked"):
                verdict["status"] = "unclear"
                verdict.setdefault("questions", [])
                verdict["summary"] = (
                    "Агент не внёс изменений и не объяснил почему.\n\n"
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

        if not branch.startswith(cfg["pr"]["branch_prefix"]):
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
                             verdict, cfg["pr"], args.dry_run)

        epic_note = ""
        if profile.get("attach_to_debt"):
            write_status(phase="привязываю к долгу спринта")
            epic_note = attach_to_debt_card(kaiten, cfg, card_id, args.dry_run)

        if pr_url.startswith("http") and mock_backend_warning(card) and not args.dry_run:
            post_pr_review(worktree, pr_url, MOCK_BACKEND_NOTE)

        text = comment_success(verdict, meta, pr_url, branch)
        if dirty:
            text += "\n\n⚠️ В рабочей копии остались незакоммиченные изменения — они не в PR."
        if not (verdict.get("checks") or []):
            text += ("\n\n⚠️ Агент не отчитался ни об одной запущенной проверке — "
                     "ревьюверу и человеку стоит смотреть внимательнее.")
        text += epic_note
        kaiten.comment(card_id, text)
        report_to_inbox(kaiten, card, pr_url)
        kaiten.move(card_id, cols["agent_review"])
        finish_status(card_id, title, "на ревью агента", meta, pr_url)
        log(f"  -> Ревью агента: {pr_url}")

    except Exception as e:  # noqa: BLE001 — карточка не должна застревать в «В работе»
        log(f"  !! {e}")
        if args.prompt_only:
            return
        kaiten.comment(card_id, comment_failed(str(e)))
        hand_over(kaiten, profile, card_id, "failed", str(e)[:120])
        finish_status(card_id, title, "упало", None)
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
    STATE.mkdir(exist_ok=True)
    tmp = TRIAGE_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(TRIAGE_STATE_FILE)


def note_triage_fail(state: dict, card_id: int, error: str) -> None:
    """
    Копим неудачи по карточке. Разведка ходит по расписанию каждые несколько минут,
    и карточка, на которой агент стабильно падает, иначе жгла бы деньги на каждом тике.
    """
    cards = state.setdefault("cards", {})
    entry = cards.setdefault(str(card_id), {"fails": 0})
    entry["fails"] = entry.get("fails", 0) + 1
    entry["error"] = error[:300]
    entry["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_triage_state(state)


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
        if any(t.startswith(TRIAGE_MARK) and HANDOFF_LINE in t for t in texts):
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
    remote, base = repo_cfg["remote"], repo_cfg["base_branch"]
    git(repo, "fetch", remote, base)
    if (path / ".git").exists():
        git(path, "reset", "--hard", f"{remote}/{base}")
        git(path, "clean", "-fd", check=False)
        return path
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    git(repo, "worktree", "prune", check=False)
    git(repo, "worktree", "add", "-B", f"scout/{key}", str(path), f"{remote}/{base}")
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

    scout = [f"**Разведка агента**\n\n{verdict.get('problem', '')}",
             format_list("Где это в коде", verdict.get("where")).strip(),
             format_list("Как чинить", verdict.get("plan")).strip()]
    if verdict.get("risk"):
        scout.append(f"**На что смотреть:** {verdict['risk']}")
    parts.append("\n\n".join(block for block in scout if block))

    parts.append("_Карточку поставил разведчик инбокса. Разведка — не приговор: "
                 "если в коде другое, верь коду._")
    return "\n\n".join(parts)


def find_work_card(kaiten: Kaiten, cfg: dict, inbox_card_id: int) -> dict | None:
    """Уже поставленная задача по этой карточке инбокса: ищем метку в описании."""
    for candidate in kaiten.cards_on_board(cfg["kaiten"]["board_id"], with_description=True):
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

        board = cfg["kaiten"]
        body = {
            "board_id": board["board_id"],
            "column_id": board["columns"]["queue"],
            "title": (card.get("title") or "").strip() or f"Задача из инбокса #{card['id']}",
            "description": work_card_description(card, comments, verdict, card_url),
        }
        if board.get("lane_id"):
            body["lane_id"] = board["lane_id"]
        if board.get("card_type_id"):
            body["type_id"] = board["card_type_id"]

        if dry_run:
            log("  [dry-run] задачу в «Очередь» не ставлю")
            return "Поставил бы задачу на «Доску для клода» (dry-run)."

        created = kaiten.create_card(body)
        if not created:
            raise FactoryError("Kaiten не вернул созданную карточку")
        log(f"  задача поставлена: #{created['id']} в «Очередь»")
        note = link_as_child(kaiten, card["id"], created["id"])
        return f"{HANDOFF_LINE} #{created['id']} — {kaiten.card_url(created)}{note}"
    except Exception as e:  # noqa: BLE001 — постановка не важнее уже сделанной разведки
        log(f"  !! не смог поставить задачу: {e}")
        return f"⚠️ Не смог поставить задачу на «Доску для клода»: {str(e)[:200]}"


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


def build_triage_prompt(card: dict, comments: list, card_url: str, repo_key: str,
                        repo_cfg: dict, round_number: int) -> str:
    template = TRIAGE_TEMPLATE_PATH.read_text(encoding="utf-8")
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
        **project_replacements(),
    }
    return apply_template(template, replacements)


def comment_triage(verdict: dict, meta: dict, round_number: int, handoff: str = "") -> str:
    status = verdict.get("status")
    text = f"{TRIAGE_MARK} **Разведка: {TRIAGE_HEAD[status]}.**\n\n{verdict.get('problem', '')}"
    text += format_list("Где это в коде", verdict.get("where"))
    text += format_list("Что нужно уточнить", verdict.get("questions"))
    text += format_list("Как чинить" if status == "ready" else "Гипотеза", verdict.get("plan"))
    if verdict.get("risk"):
        text += f"\n\n**На что смотреть:** {verdict['risk']}"

    if status == "ready":
        text += f"\n\n{handoff}" if handoff else (
            "\n\nЕсли согласен — переноси карточку в «Очередь» на «Доске для клода», "
            "фабрика возьмёт её сама.")
    elif status == "needs_info":
        text += "\n\nОтветь комментарием здесь — я вернусь и пересмотрю."

    tail = [bit for bit in (EFFORT_LABEL.get(verdict.get("effort") or ""), format_meta(meta))
            if bit]
    line = "разведка" + (f", круг {round_number}" if round_number > 1 else "")
    if tail:
        line += ": " + ", ".join(tail)
    text += f"\n\n_{line}. Код не менял._"
    text += format_joke(verdict)
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


def triage_card(card_stub: dict, kaiten: Kaiten, cfg: dict, args,
                scouts: dict[str, Path], round_number: int) -> None:
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
    repo_key, repo_cfg = resolve_repo(card, cfg)

    round_note = f", круг {round_number}" if round_number > 1 else ""
    log(f"#{card_id} «{title[:60]}» — разведка{round_note}")

    prompt = build_triage_prompt(card, comments, card_url, repo_key, repo_cfg, round_number)
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
    verdict, meta = run_agent(prompt, worktree, cfg["triager"], schema=TRIAGE_SCHEMA)
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
                          card_url=card_url)
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
    finish_status(card_id, title, f"разведка: {TRIAGE_HEAD[status]}", meta, card_url=card_url)


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
    done = 0
    for card, round_number in targets:
        try:
            triage_card(card, kaiten, cfg, args, scouts, round_number)
            forget_triage_fail(state, card["id"])
            done += 1
        except ScoutSetupError as e:
            # с остальными карточками будет то же самое, и карточки в этом не виноваты
            log(f"  !! разведка невозможна: {e}")
            break
        except Exception as e:  # noqa: BLE001 — одна карточка не должна валить прогон
            log(f"  !! #{card['id']} разведка не удалась: {e}")
            note_triage_fail(state, card["id"], str(e))

    if not args.prompt_only:
        write_status(card=None, phase=None, inbox_pending=max(0, pending - done))
    return 0


# --------------------------------------------------------------------------- #
# режим эпиков: АЦ -> спека -> сабтаски -> ревью
# --------------------------------------------------------------------------- #

# Метка комментариев эпик-агента. Своя, чтобы не путать с исполнителем и ревьювером
EPIC_MARK = "🧩"

# Как называется чек-лист приёмочных критериев. По имени же его и находим обратно:
# другого признака «АЦ уже написаны» у нас нет
ACCEPTANCE_LIST = "Приёмочные критерии"

# Строки-маркеры в комментариях эпика. Состояние пайплайна выводится только из Kaiten:
# прогон идёт раз в час, человек между прогонами двигает карточки руками, и любое
# состояние, которое фабрика держала бы у себя, разъехалось бы с доской в первый же раз.
SPEC_LINE = "Спека:"
SPEC_OK_LINE = "Спека отревьюена."
SUBTASKS_LINE = "Разложил на сабтаски:"

# Строка в описании сабтаски: из какого эпика она выросла. По ней же считаются свои
# дети — у эпика могут висеть и карточки, которые подвесил человек, и ждать их закрытия
# фабрика не должна.
EPIC_ORIGIN = "Из эпика:"
EPIC_ORIGIN_RE = re.compile(r"Из эпика:\s*#(\d+)")

# Фазы. Порядок важен: фаза выводится первым совпавшим условием
EPIC_PHASES = ("acceptance", "waiting_approval", "spec", "spec_review",
               "decompose", "working", "closing")

EPIC_PHASE_LABELS = {
    "acceptance": "пишу приёмочные критерии",
    "waiting_approval": "жду апрува АЦ",
    "spec": "пишу спеку",
    "spec_review": "ревью спеки",
    "decompose": "раскладываю на сабтаски",
    "working": "сабтаски в работе",
    "closing": "закрываю эпик",
}


def epic_flow(cfg: dict) -> dict | None:
    """Настройки режима эпиков. Нет секции — режима нет, как с инбоксом и долгом."""
    flow = cfg.get("epic_flow") or {}
    if not (flow.get("boards") and flow.get("subtasks", {}).get("board_id")):
        return None
    return flow


def next_column_after(kaiten: Kaiten, board_id: int, column_id: int) -> int | None:
    """
    Колонка справа от указанной.

    Эпик после ревью уезжает «в следующую после разработки», а не в жёстко заданную:
    так это описано в процессе. Подколонки считаются внутри своего родителя, и только
    когда сосед справа кончился — переходим к следующей верхней колонке.
    """
    board = kaiten.board(board_id)
    columns = flat_columns(board)
    ids = [int(c["id"]) for c in columns]
    if column_id not in ids:
        return None
    position = ids.index(column_id)
    return ids[position + 1] if position + 1 < len(ids) else None


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


def epic_subtasks(kaiten: Kaiten, epic_id: int, children: list) -> list:
    """
    Только свои сабтаски: те, где в описании стоит «Из эпика: #<id>».

    У эпика могут висеть и карточки, подвешенные человеком. Если считать всех детей,
    эпик не закроется никогда — человек про свою карточку просто забудет.
    """
    own = []
    for child in children:
        description = strip_html(child.get("description")) or ""
        match = EPIC_ORIGIN_RE.search(description)
        if match and int(match.group(1)) == epic_id:
            own.append(child)
    return own


def epic_phase(kaiten: Kaiten, cfg: dict, flow: dict, card: dict, comments: list) -> str:
    """
    В какой фазе эпик прямо сейчас. Выводится целиком из Kaiten — чек-лист, блокеры,
    комментарии, дети — чтобы прогон мог начаться с любого места и не зависеть
    от того, что фабрика помнит о прошлом разе.
    """
    card_id = card["id"]
    if not acceptance_items(card):
        return "acceptance"
    if own_blocker(kaiten, card_id, BLOCK_ACCEPTANCE):
        return "waiting_approval"
    if not said(comments, SPEC_LINE):
        return "spec"
    if not said(comments, SPEC_OK_LINE):
        return "spec_review"

    children = kaiten.children(card_id)
    own = epic_subtasks(kaiten, card_id, children)
    if not own:
        return "decompose"

    profile = subtask_profile(cfg, flow)
    review_column = role_column(profile, "review")
    done_column = role_column(profile, "done")
    finished = 0
    for child in own:
        column = child.get("column_id")
        if column == done_column:
            finished += 1
        elif column == review_column and blocked_by(kaiten, child["id"]):
            # в колонке ревью с блокером — агентское ревью пройдено, дальше человек
            finished += 1
    log(f"  сабтасок {len(own)}, отревьюено {finished}")
    return "closing" if finished == len(own) else "working"


def subtask_profile(cfg: dict, flow: dict) -> dict:
    subtasks = flow["subtasks"]
    return make_profile("сабтаски", subtasks, subtasks.get("repo"), attach_to_debt=False)


def pick_epics(kaiten: Kaiten, cfg: dict, flow: dict) -> list[dict]:
    """Карточки с рабочим тегом на перечисленных досках, у которых нет чужого блокера."""
    tag = flow.get("tag") or "claude:epic"
    found = []
    for board_id in flow["boards"]:
        for card in kaiten.cards_on_board(int(board_id), with_description=True):
            if not has_tag(card, tag):
                continue
            comments = kaiten.comments(card["id"])
            stop = hands_off(card, comments, cfg)
            if stop:
                log(f"#{card['id']} не трогаю: в карточке «{stop}»")
                continue
            found.append(card)
    return skip_off_hours(found, cfg)



# Предупреждение про мок бека. Едет в описание сабтаски, в тело PR и отдельным
# комментарием в PR: раскатывать такую правку до релиза бека нельзя, и человек,
# который смотрит только PR, должен это увидеть там же.
MOCK_BACKEND_LINE = "Бек ещё не готов: фронт сделан на моке."
MOCK_BACKEND_NOTE = ("⚠️ **Нельзя раскатывать и включать до релиза бека.** "
                     "Фронт работает на моке.")


def epic_prompt(path: Path, card: dict, card_url: str, repo_cfg: dict,
                extra: dict | None = None) -> str:
    replacements = {
        "{{CARD_ID}}": str(card["id"]),
        "{{CARD_URL}}": card_url,
        "{{TITLE}}": card.get("title") or "(без заголовка)",
        "{{DESCRIPTION}}": strip_html(card.get("description")) or "(описания нет)",
        "{{BASE_BRANCH}}": repo_cfg["base_branch"],
        "{{REMOTE}}": repo_cfg["remote"],
        **(extra or {}),
        **project_replacements(),
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
    return text + format_joke(verdict)


def comment_epic_unclear(verdict: dict, meta: dict, phase: str) -> str:
    text = (f"{EPIC_MARK} **Не хватает данных, чтобы {phase}.**\n\n"
            f"{verdict.get('summary', '')}")
    text += format_list("Что нужно уточнить", verdict.get("questions"))
    text += ("\n\nОтветь комментарием и **сними блокер** — я вернусь и продолжу "
             "с того же места.")
    tail = format_meta(meta)
    text += f"\n\n_{phase}{': ' + tail if tail else ''}. Код не менял._"
    return text + format_joke(verdict)


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
    """
    profile = subtask_profile(cfg, flow)
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
        body = {
            "board_id": profile["board_id"],
            "column_id": role_column(profile, "queue"),
            "title": item["title"].strip(),
            "description": subtask_description(epic, epic_url, item, spec_note),
        }
        if profile.get("lane_id"):
            body["lane_id"] = profile["lane_id"]
        if profile.get("card_type_id"):
            body["type_id"] = profile["card_type_id"]
        if args.dry_run:
            log(f"  [dry-run] сабтаска «{item['title'][:50]}» ({item.get('kind')})")
            continue
        card = kaiten.create_card(body)
        if not card:
            log(f"  !! не создалась сабтаска «{item['title'][:40]}»")
            continue
        kaiten.add_child(epic["id"], card["id"])
        # тег эпика наследуется: иначе эпик помечен «ночью», а код по нему пишется в полдень
        night_tag, _, _ = night_config(cfg)
        if has_tag(epic, night_tag):
            kaiten.add_tag(card["id"], night_tag)
            log("    тег ночной задачи унаследован от эпика")
        created.append((card, item))
        log(f"  + #{card['id']} «{item['title'][:50]}» ({item.get('kind')})")

    # блокер раскатки — на фронт, который сделан на моке
    for card, item in created:
        if item.get("mocks_backend"):
            detail = (f"бек #{created[0][0]['id']}" if backend and created
                      else "бек ещё не готов")
            hold(kaiten, card["id"], BLOCK_ROLLOUT, detail)

    lines = "\n".join(f"- #{c['id']} {i['title']} ({i.get('kind')})" for c, i in created)
    text = (f"{EPIC_MARK} **{SUBTASKS_LINE}**\n\n{lines or '(в dry-run не создавал)'}\n\n"
            f"{verdict.get('summary', '')}")
    if any(i.get("mocks_backend") for _, i in created):
        text += f"\n\n{MOCK_BACKEND_NOTE}"
    text += f"\n\n_декомпозиция, {format_meta(meta)}._" + format_joke(verdict)
    kaiten.comment(epic["id"], text)


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
    branch = f"{cfg['pr']['branch_prefix']}{card_id}-spec"
    existing = remote_branch_for_card(repo, repo_cfg["remote"],
                                      cfg["pr"]["branch_prefix"], card_id)
    continue_from = existing if existing and existing.endswith("-spec") else None
    make_worktree(repo, branch, repo_cfg["base_branch"], repo_cfg["remote"], worktree,
                  continue_from=continue_from)
    return worktree


def advance_epic(kaiten: Kaiten, cfg: dict, flow: dict, card: dict, comments: list,
                 phase: str, args) -> None:
    """Продвигает эпик на одну фазу. Каждая фаза заканчивается либо шагом, либо блокером."""
    card_id = card["id"]
    card_url = kaiten.card_url(card)
    title = (card.get("title") or "").strip()
    repo_key, repo_cfg = resolve_repo(card, cfg, (flow.get("subtasks") or {}).get("repo"))
    agent_cfg = {**cfg["triager"], **(flow.get("agent") or {})}

    if phase == "acceptance":
        worktree = epic_worktree(kaiten, cfg, repo_cfg, repo_key, card_id, writable=False)
        prompt = epic_prompt(ACCEPTANCE_TEMPLATE_PATH, card, card_url, repo_cfg,
                             {"{{COMMENTS}}": format_comments(comments)})
        if show_prompt(prompt, args, "приёмочных критериев"):
            return
        verdict, meta = run_agent(prompt, worktree, agent_cfg, schema=ACCEPTANCE_SCHEMA)
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

    if phase == "spec":
        worktree = epic_worktree(kaiten, cfg, repo_cfg, repo_key, card_id, writable=True)
        criteria = acceptance_items(card)
        prompt = epic_prompt(SPEC_TEMPLATE_PATH, card, card_url, repo_cfg, {
            "{{COMMENTS}}": format_comments(comments),
            "{{ACCEPTANCE}}": "\n".join(f"- {i.get('text')}" for i in criteria),
            "{{SPEC_DIR}}": flow.get("spec_dir") or "specs",
            "{{BRANCH}}": f"{cfg['pr']['branch_prefix']}{card_id}-spec",
        })
        if show_prompt(prompt, args, "спеки"):
            return
        verdict, meta = run_agent(prompt, worktree, cfg["agent"], schema=SPEC_SCHEMA)
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
                         verdict, cfg["pr"], args.dry_run) if not args.no_pr else "(без PR)"
        kaiten.comment(card_id,
                       f"{EPIC_MARK} **{SPEC_LINE}** {pr_url}\n\n"
                       f"{verdict.get('summary', '')}\n\n"
                       f"_спека, {format_meta(meta)}. Дальше её посмотрит ревьювер._"
                       + format_joke(verdict))
        log(f"  -> спека: {pr_url}")
        if not (args.keep_worktree or cfg.get("keep_worktree")):
            drop_worktree(Path(repo_cfg["path"]).expanduser(), worktree)
        return

    if phase == "spec_review":
        worktree = epic_worktree(kaiten, cfg, repo_cfg, repo_key, card_id, writable=False)
        prompt = epic_prompt(SPEC_REVIEW_TEMPLATE_PATH, card, card_url, repo_cfg, {
            "{{ACCEPTANCE}}": "\n".join(f"- {i.get('text')}"
                                        for i in acceptance_items(card)),
            "{{SPEC_BRANCH}}": f"{cfg['pr']['branch_prefix']}{card_id}-spec",
        })
        if show_prompt(prompt, args, "ревью спеки"):
            return
        review, meta = run_agent(prompt, worktree, cfg["reviewer"],
                                 schema=SPEC_REVIEW_SCHEMA, verdict_key="verdict")
        note_spend(meta)
        log_phase(card_id, "spec-review", review, meta)

        findings = review.get("findings") or []
        blocking = [f for f in findings if f.get("severity") in ("blocker", "major")]
        if review.get("verdict") == "needs_changes" or blocking:
            text = (f"{REVIEWER_MARK} **Спеку надо поправить.**\n\n"
                    f"{review.get('summary', '')}")
            for f in findings:
                text += (f"\n\n{SEVERITY_ICON.get(f.get('severity', ''), '•')} "
                         f"{f.get('what')}\n  → {f.get('fix')}")
            kaiten.comment(card_id, text)
            hold(kaiten, card_id, BLOCK_QUESTION, "ревьювер вернул замечания к спеке")
            log("  -> спека вернулась на правки")
            return
        kaiten.comment(card_id,
                       f"{EPIC_MARK} **{SPEC_OK_LINE}** {review.get('summary', '')}\n\n"
                       f"_ревью спеки, {format_meta(meta)}._" + format_joke(review))
        log("  -> спека принята")
        return

    if phase == "decompose":
        worktree = epic_worktree(kaiten, cfg, repo_cfg, repo_key, card_id, writable=False)
        prompt = epic_prompt(DECOMPOSE_TEMPLATE_PATH, card, card_url, repo_cfg, {
            "{{ACCEPTANCE}}": "\n".join(f"- {i.get('text')}"
                                        for i in acceptance_items(card)),
            "{{SPEC_BRANCH}}": f"{cfg['pr']['branch_prefix']}{card_id}-spec",
            "{{MAX_SUBTASKS}}": str(flow.get("max_subtasks", 5)),
        })
        if show_prompt(prompt, args, "декомпозиции"):
            return
        verdict, meta = run_agent(prompt, worktree, agent_cfg, schema=DECOMPOSE_SCHEMA)
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


def take_epic(kaiten: Kaiten, flow: dict, card: dict) -> None:
    """Взяли эпик в работу — двигаем в колонку разработки, если он не там."""
    target = flow.get("development_column_id")
    if not target or card.get("column_id") == target:
        return
    log("  двигаю эпик в колонку разработки")
    kaiten.move(card["id"], int(target))


def close_epic(kaiten: Kaiten, flow: dict, card: dict) -> str:
    """
    Все сабтаски отревьюены — эпик уезжает на полноценное ревью.

    Целевая колонка по умолчанию не задаётся, а вычисляется: «следующая после
    разработки». Если её выставили в конфиге — уважаем конфиг.
    """
    card_id = card["id"]
    target = flow.get("review_column_id")
    if not target:
        target = next_column_after(kaiten, card["board_id"],
                                   int(flow["development_column_id"]))
        if not target:
            return "не понял, куда двигать эпик: справа от колонки разработки пусто"
    # человек мог утащить эпик дальше сам — тогда не возвращаем его назад
    if card.get("column_id") == target:
        return ""
    log(f"  все сабтаски отревьюены, двигаю эпик в колонку {target}")
    kaiten.move(card_id, int(target))
    return f"{EPIC_MARK} **Все сабтаски отревьюены.** Эпик уехал на ревью."


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
        epics = [kaiten.card(only_card)]
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
            log(f"#{card_id} «{title[:50]}» — чужой блокер: "
                f"{foreign[0].get('reason', '')[:60]} — не трогаю")
            waiting += 1
            continue

        phase = epic_phase(kaiten, cfg, flow, card, comments)
        log(f"#{card_id} «{title[:50]}» — фаза: {EPIC_PHASE_LABELS.get(phase, phase)}")

        if phase == "waiting_approval":
            waiting += 1
            continue

        if not args.prompt_only:
            write_status(card={"id": card_id, "title": title,
                               "url": kaiten.card_url(card)},
                         phase=f"эпик: {EPIC_PHASE_LABELS.get(phase, phase)}")

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
            advance_epic(kaiten, cfg, flow, card, comments, phase, args)
        except FactoryError as e:
            log(f"  !! фаза «{phase}» не удалась: {e}")

    if not args.prompt_only:
        write_status(card=None, phase=None, epics_waiting=waiting)
    return 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

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
    parser.add_argument("--only-review", action="store_true",
                        help="только фаза ревью: пройти «Ревью агента» и выйти")
    parser.add_argument("--only-work", action="store_true",
                        help="только фаза работы: пропустить ревью")
    parser.add_argument("--only-triage", action="store_true",
                        help="только разведка инбокса: посмотреть новые карточки и выйти")
    parser.add_argument("--no-triage", action="store_true",
                        help="пропустить разведку инбокса")
    parser.add_argument("--triage-card", type=int,
                        help="разведать конкретную карточку инбокса по id")
    parser.add_argument("--only-epics", action="store_true",
                        help="только фаза эпиков: продвинуть их и выйти")
    parser.add_argument("--no-epics", action="store_true", help="пропустить фазу эпиков")
    parser.add_argument("--epic-card", type=int,
                        help="продвинуть конкретный эпик по id, игнорируя тег и выборку")
    args = parser.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    env = load_env()
    domain = env.get("KAITEN_DOMAIN") or cfg["kaiten"]["domain"]
    kaiten = Kaiten(domain, env["KAITEN_TOKEN"], cfg["kaiten"]["space_id"], dry_run=args.dry_run)

    WORKTREES.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    if not args.prompt_only:
        # pid нужен менюбар-приложению, чтобы при выходе прибить и питон, и агента
        write_status(pid=os.getpid(), card=None, phase="смотрю доску")
    if args.dry_run:
        log("DRY-RUN: Kaiten и GitHub не трогаем, агент отработает по-настоящему")

    profiles = board_profiles(cfg)
    if len(profiles) > 1:
        log("досок в работе: " + ", ".join(f"«{p['key']}»" for p in profiles))

    if args.link_review:
        return link_review_cards(kaiten, cfg, args.dry_run)

    if args.triage_card:
        return triage_inbox(kaiten, cfg, args, only_card=args.triage_card)
    if args.only_triage:
        return triage_inbox(kaiten, cfg, args)
    if args.epic_card:
        return run_epics(kaiten, cfg, args, only_card=args.epic_card)
    if args.only_epics:
        return run_epics(kaiten, cfg, args)

    # Разведка идёт первой: она дешёвая и быстрая, а на другом конце сидит человек,
    # который только что закинул карточку в инбокс и ждёт, что ему ответят.
    if not (args.card or args.only_review or args.only_work or args.no_triage):
        try:
            triage_inbox(kaiten, cfg, args)
        except Exception as e:  # noqa: BLE001 — чужая доска не должна ронять основной поток
            log(f"разведка инбокса не задалась: {e}")

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
                 if not (profile.get("own_only") and not own_subtask(c))
                 and not blocked_by(kaiten, c["id"])], cfg)
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
            except FactoryError as e:
                log(f"#{card['id']} не удалось даже начать: {e}")
    if not args.prompt_only:
        write_status(card=None, phase=None, night_waiting=NIGHT_WAITING["count"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FactoryError as e:
        log(f"фатально: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        log("прервано")
        sys.exit(130)
