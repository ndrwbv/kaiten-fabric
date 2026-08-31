#!/usr/bin/env python3
"""
Установка фабрики: находит id досок и колонок в Kaiten и пишет config.json.

    python3 setup.py              мастер: пошагово собрать config.json
    python3 setup.py --check      доктор: проверить, что всё на месте
    python3 setup.py --audit      проверить, что в репозиторий не утекло внутреннее

Разведка (пригодится, если конфиг правишь руками или это делает агент):

    python3 setup.py --spaces [строка]   пространства, можно с поиском по названию
    python3 setup.py --boards SPACE_ID   доски пространства
    python3 setup.py --board BOARD_ID    колонки и дорожки доски
    python3 setup.py --card-types [строка]

Ко всему добавляется --json: тогда вывод машинный, без рамок и вопросов.

Ходим через curl, а не через urllib: на рабочих маках стоит корпоративный MITM-прокси,
его корневой сертификат лежит в системном хранилище. curl туда смотрит, python — в свой
bundled certifi, и падает на CERTIFICATE_VERIFY_FAILED.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"
ENV_PATH = ROOT / ".env"
ENV_CANDIDATES = [ENV_PATH, Path.home() / ".claude" / ".env"]

PROJECT_DIR = ROOT / "project"
PROJECT_FILES = ["docs.md", "checklist.md"]


class SetupError(Exception):
    pass


# --------------------------------------------------------------------------- #
# как выглядит рабочая доска
# --------------------------------------------------------------------------- #

# Колонки, из которых состоит поток. type — это тип колонки в Kaiten:
# 1 = очередь, 2 = в работе, 3 = готово. Фабрика ходит по ним по своим правилам,
# но тип влияет на то, как Kaiten считает время и рисует доску.
COLUMN_ROLES = [
    ("queue",        "Очередь",          1, "фабрика берёт карточки отсюда"),
    ("in_progress",  "В работе",         2, "агент пишет код"),
    ("question",     "Вопрос от агента", 2, "агенту не хватило данных, ждём человека"),
    ("failed",       "Упало",            2, "агент или обёртка сломались"),
    ("agent_review", "Ревью агента",     2, "сюда уезжает готовый PR, отсюда его берёт ревьювер"),
    ("fixes",        "Правки",           2, "ревьювер вернул замечания"),
    ("review",       "На ревью",         2, "ревью пройдено, дальше смотрит человек"),
    ("done",         "Готово",           3, "фабрика сюда ничего не двигает"),
]

# Синонимы для угадывания роли по названию уже существующей колонки. Список короткий
# намеренно: лучше переспросить, чем молча выбрать не ту колонку и начать двигать
# в неё чужие карточки.
COLUMN_SYNONYMS = {
    "queue":        ["очередь", "queue", "todo", "to do", "бэклог", "backlog"],
    "in_progress":  ["в работе", "in progress", "doing", "wip"],
    "question":     ["вопрос", "question", "уточнение", "blocked"],
    "failed":       ["упало", "failed", "ошибка", "error"],
    "agent_review": ["ревью агента", "agent review", "авторевью"],
    "fixes":        ["правки", "fixes", "доработк"],
    "review":       ["на ревью", "review", "код-ревью", "code review"],
    "done":         ["готово", "done", "закрыто", "closed"],
}


# --------------------------------------------------------------------------- #
# Kaiten
# --------------------------------------------------------------------------- #

class Kaiten:
    def __init__(self, domain: str, token: str):
        self.domain = domain
        self.token = token
        self.api = f"https://{domain}/api/latest"

    def request(self, method: str, path: str, body=None, params=None):
        url = self.api + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        cmd = ["curl", "-sS", "-K", "-", "--max-time", "30",
               "-X", method, "-H", "Content-Type: application/json",
               "-w", "\n%{http_code}", url]
        if body is not None:
            cmd += ["--data-raw", json.dumps(body, ensure_ascii=False)]
        try:
            proc = subprocess.run(
                cmd, input=f'header = "Authorization: Bearer {self.token}"\n',
                capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            raise SetupError("не найден curl — поставь Xcode Command Line Tools") from None
        if proc.returncode != 0:
            raise SetupError(f"Kaiten {method} {path}: curl {proc.returncode} "
                             f"{proc.stderr.strip()[:200]}")
        raw, _, code = proc.stdout.rpartition("\n")
        if code.strip() and int(code) >= 400:
            raise SetupError(f"Kaiten {method} {path}: HTTP {code} {raw[:300]}")
        return json.loads(raw) if raw.strip() else None

    # -- чтение ------------------------------------------------------------- #

    def spaces(self) -> list:
        return self.request("GET", "/spaces") or []

    def boards(self, space_id: int) -> list:
        return self.request("GET", f"/spaces/{space_id}/boards") or []

    def board(self, board_id: int) -> dict:
        return self.request("GET", f"/boards/{board_id}")

    def card_types(self) -> list:
        return self.request("GET", "/card-types") or []

    # -- запись ------------------------------------------------------------- #
    #
    # Пути здесь вложенные не для красоты: плоские /boards/{id} и /columns/{id}
    # Kaiten отдаёт 405 и 404. Документация обещает обратное, но это неправда.

    def create_board(self, space_id: int, title: str) -> dict:
        return self.request("POST", f"/spaces/{space_id}/boards", {"title": title})

    def create_column(self, board_id: int, title: str, ctype: int, sort_order: int) -> dict:
        return self.request("POST", f"/boards/{board_id}/columns",
                            {"title": title, "type": ctype, "sort_order": sort_order})

    def rename_column(self, board_id: int, column_id: int, title: str,
                      ctype: int, sort_order: int) -> dict:
        return self.request("PATCH", f"/boards/{board_id}/columns/{column_id}",
                            {"title": title, "type": ctype, "sort_order": sort_order})


# --------------------------------------------------------------------------- #
# окружение
# --------------------------------------------------------------------------- #

def read_env_file(path: Path) -> dict:
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def find_env() -> tuple[dict, Path | None]:
    """Первый .env, в котором есть KAITEN_TOKEN, выигрывает. Иначе — окружение."""
    for path in ENV_CANDIDATES:
        values = read_env_file(path)
        if values.get("KAITEN_TOKEN"):
            return values, path
    if os.environ.get("KAITEN_TOKEN"):
        return dict(os.environ), None
    return {}, None


# --------------------------------------------------------------------------- #
# ввод-вывод мастера
# --------------------------------------------------------------------------- #

def head(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m")


def hint(text: str) -> None:
    print(f"\033[2m{text}\033[0m")


def ok(text: str) -> None:
    print(f"  \033[32m✓\033[0m {text}")


def bad(text: str) -> None:
    print(f"  \033[31m✗\033[0m {text}")


def warn(text: str) -> None:
    print(f"  \033[33m!\033[0m {text}")


def ask(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{question}{suffix}: ").strip()
    except EOFError:
        raise SetupError("ввод закончился — мастеру нужен интерактивный терминал") from None
    return answer or default


def ask_yes(question: str, default: bool = True) -> bool:
    marks = "Y/n" if default else "y/N"
    answer = ask(f"{question} ({marks})").lower()
    if not answer:
        return default
    return answer[0] in "yд1"


def ask_int(question: str, default: int | None = None) -> int:
    while True:
        answer = ask(question, str(default) if default is not None else "")
        if answer.lstrip("-").isdigit():
            return int(answer)
        print("  нужно число")


def choose(items: list, title: str, label, allow_none: bool = False) -> dict | None:
    """Показывает пронумерованный список и возвращает выбранный элемент."""
    print()
    for i, item in enumerate(items, start=1):
        print(f"  {i:>2}. {label(item)}")
    if allow_none:
        print("   0. пропустить")
    while True:
        answer = ask(title)
        if not answer.isdigit():
            print("  нужен номер из списка")
            continue
        number = int(answer)
        if allow_none and number == 0:
            return None
        if 1 <= number <= len(items):
            return items[number - 1]
        print("  нет такого номера")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


# --------------------------------------------------------------------------- #
# шаги мастера
# --------------------------------------------------------------------------- #

def step_token() -> tuple[str, str]:
    """Возвращает (domain, token). При необходимости пишет .env рядом со скриптом."""
    head("1. Доступ в Kaiten")
    env, path = find_env()
    token = env.get("KAITEN_TOKEN", "")
    domain = env.get("KAITEN_DOMAIN", "")

    if token:
        source = str(path) if path else "переменных окружения"
        ok(f"KAITEN_TOKEN найден в {source}")
    else:
        hint("Токен: Kaiten → аватар в правом верхнем углу → Профиль → API-ключ.")
        hint("Он даёт те же права, что и ты сам, поэтому в репозиторий не попадает —")
        hint(f"мастер положит его в {ENV_PATH.name}, а он в .gitignore.")
        token = ask("KAITEN_TOKEN")
        if not token:
            raise SetupError("без токена дальше никак")

    if not domain:
        domain = ask("Домен Kaiten (например company.kaiten.ru)")
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    if not domain:
        raise SetupError("нужен домен Kaiten")

    kaiten = Kaiten(domain, token)
    try:
        me = kaiten.request("GET", "/users/current")
        ok(f"вошли как {me.get('full_name') or me.get('username') or 'пользователь'}")
    except SetupError as e:
        raise SetupError(f"токен не подошёл: {e}") from None

    # Пишем .env только если его ещё нет: чужой файл с чужими ключами не трогаем.
    if not path and not ENV_PATH.exists():
        ENV_PATH.write_text(f"KAITEN_DOMAIN={domain}\nKAITEN_TOKEN={token}\n", encoding="utf-8")
        ENV_PATH.chmod(0o600)
        ok(f"токен сохранён в {ENV_PATH.name} (права 600, файл в .gitignore)")
    return domain, token


def step_space(kaiten: Kaiten) -> int:
    head("2. Пространство")
    hint("Пространств в компании может быть много — найдём твоё по названию.")
    spaces = kaiten.spaces()
    while True:
        query = normalize(ask("Часть названия пространства"))
        found = [s for s in spaces if query in normalize(s.get("title"))]
        if not found:
            print(f"  ничего не нашлось среди {len(spaces)} пространств, попробуй иначе")
            continue
        if len(found) > 30:
            print(f"  нашлось {len(found)} — уточни запрос")
            continue
        space = choose(found, "Номер пространства", lambda s: f"{s['title']}  (id {s['id']})")
        ok(f"пространство «{space['title']}», id {space['id']}")
        return int(space["id"])


def provision_board(kaiten: Kaiten, space_id: int, title: str) -> dict:
    """Создаёт доску и раскладывает на ней колонки потока."""
    board = kaiten.create_board(space_id, title)
    board_id = int(board["id"])
    ok(f"доска «{title}» создана, id {board_id}")

    # Новая доска приезжает с одной колонкой «To Do» — переименовываем её в «Очередь»,
    # чтобы не оставлять на доске лишнюю пустую колонку.
    existing = sorted(kaiten.board(board_id).get("columns") or [],
                      key=lambda c: c.get("sort_order") or 0)
    columns = {}
    key, name, ctype, _ = COLUMN_ROLES[0]
    kaiten.rename_column(board_id, existing[0]["id"], name, ctype, 1)
    columns[key] = int(existing[0]["id"])
    print(f"     1. {name}")
    for order, (key, name, ctype, _) in enumerate(COLUMN_ROLES[1:], start=2):
        created = kaiten.create_column(board_id, name, ctype, order)
        columns[key] = int(created["id"])
        print(f"    {order:>2}. {name}")

    lanes = kaiten.board(board_id).get("lanes") or []
    return {
        "board_id": board_id,
        "columns": columns,
        "lane_id": int(lanes[0]["id"]) if lanes else None,
        "card_type_id": int(board.get("default_card_type_id") or 1),
    }


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


def match_columns(board: dict) -> dict:
    """Угадывает роли колонок по названиям. Что не угадалось — None."""
    columns = flat_columns(board)
    guessed = {}
    for key, name, _, _ in COLUMN_ROLES:
        wanted = [normalize(name)] + COLUMN_SYNONYMS.get(key, [])
        hit = next((c for c in columns
                    if any(w in normalize(c.get("title")) for w in wanted)), None)
        guessed[key] = int(hit["id"]) if hit else None
    return guessed


def confirm_columns(board: dict, guessed: dict) -> dict:
    """Показывает разложенные роли и даёт поправить руками."""
    columns = flat_columns(board)
    by_id = {int(c["id"]): c.get("path") for c in columns}

    print()
    for key, name, _, purpose in COLUMN_ROLES:
        column_id = guessed.get(key)
        title = by_id.get(column_id) if column_id else None
        if title:
            print(f"  \033[32m✓\033[0m {name:<18} → «{title}»  \033[2m{purpose}\033[0m")
        else:
            print(f"  \033[31m✗\033[0m {name:<18} → не нашлась  \033[2m{purpose}\033[0m")

    missing = [k for k, v in guessed.items() if not v]
    if not missing and ask_yes("\nВсё разложилось верно?"):
        return guessed

    if missing:
        warn(f"не нашлось колонок: {len(missing)} — покажу список и спрошу по каждой")
    for key, name, _, purpose in COLUMN_ROLES:
        if guessed.get(key) and not missing:
            continue
        if guessed.get(key):
            continue
        print(f"\n  \033[1m{name}\033[0m — {purpose}")
        column = choose(columns, "  Номер колонки",
                        lambda c: f"{c.get('title')}  (id {c['id']})")
        guessed[key] = int(column["id"])
    return guessed


def pick_board(kaiten: Kaiten, space_id: int, title: str,
               allow_none: bool = False) -> dict | None:
    boards = kaiten.boards(space_id)
    if not boards:
        raise SetupError("в пространстве нет ни одной доски")
    return choose(boards, title, lambda b: f"{b['title']}  (id {b['id']})",
                  allow_none=allow_none)


def step_work_board(kaiten: Kaiten, space_id: int) -> dict:
    head("3. Рабочая доска")
    hint("Это доска, с которой фабрика берёт задачи и куда отчитывается.")
    hint("Ей нужны восемь колонок в строгом порядке — их можно создать автоматически.")

    if ask_yes("Создать новую доску с готовыми колонками?"):
        title = ask("Название доски", "Доска для клода")
        return provision_board(kaiten, space_id, title)

    hint("Тогда возьмём существующую и разложим её колонки по ролям.")
    chosen = pick_board(kaiten, space_id, "Номер рабочей доски")
    board = kaiten.board(int(chosen["id"]))
    columns = confirm_columns(board, match_columns(board))
    lanes = board.get("lanes") or []
    return {
        "board_id": int(chosen["id"]),
        "columns": columns,
        "lane_id": int(lanes[0]["id"]) if lanes else None,
        "card_type_id": int(board.get("default_card_type_id") or 1),
    }


def step_inbox(kaiten: Kaiten, space_id: int) -> dict | None:
    head("4. Инбокс (по желанию)")
    hint("Инбокс — сырой поток задач и багов от команды. Разведчик читает новые")
    hint("карточки, ищет описанное в коде и пишет комментарий: о чём задача, где это")
    hint("в коде и хватает ли данных. Чужие карточки он не двигает и не правит.")
    if not ask_yes("Включить разведку инбокса?", default=False):
        hint("Пропускаем. Включить потом — секция inbox в config.json.")
        return None

    chosen = pick_board(kaiten, space_id, "Номер доски инбокса", allow_none=True)
    if not chosen:
        return None
    board = kaiten.board(int(chosen["id"]))
    columns = flat_columns(board)
    hint("\nИз какой колонки брать новые карточки?")
    column = choose(columns, "Номер колонки", column_label)
    ok(f"инбокс: «{chosen['title']}» → «{column.get('path')}»")
    return {
        "board_id": int(chosen["id"]),
        "column_id": int(column["id"]),
        "max_cards_per_run": 3,
        "max_rounds": 2,
        "max_fails": 2,
        "create_cards": True,
    }


def step_epic(kaiten: Kaiten, space_id: int) -> dict | None:
    head("5. Доска эпиков для техдолга (по желанию)")
    hint("Каждый спринт фабрика заводит одну карточку-эпик «Долг <даты спринта>» и")
    hint("вешает на неё дочерними всё, что сделала. Так поток видно в отчётах, и он")
    hint("не растворяется в отдельных карточках.")
    if not ask_yes("Привязывать сделанное к карточке долга?", default=False):
        hint("Пропускаем. Включить потом — секция epic в config.json.")
        return None

    chosen = pick_board(kaiten, space_id, "Номер доски эпиков", allow_none=True)
    if not chosen:
        return None
    board = kaiten.board(int(chosen["id"]))
    columns = flat_columns(board)
    hint("\nВ какой колонке держать карточку долга?")
    column = choose(columns, "Номер колонки", column_label)

    lanes = board.get("lanes") or []
    lane_id = int(lanes[0]["id"]) if lanes else None
    if len(lanes) > 1:
        lane = choose(lanes, "Номер дорожки",
                      lambda l: f"{l.get('title') or '(без названия)'}  (id {l['id']})")
        lane_id = int(lane["id"])

    card_type_id = int(board.get("default_card_type_id") or 1)
    if ask_yes("Тип карточки на этой доске особенный (не «Задача»)?", default=False):
        query = normalize(ask("Часть названия типа карточки"))
        found = [t for t in kaiten.card_types()
                 if query in normalize(t.get("name") or t.get("title"))][:30]
        if found:
            chosen_type = choose(found, "Номер типа",
                                 lambda t: f"{t.get('name') or t.get('title')}  (id {t['id']})")
            card_type_id = int(chosen_type["id"])

    hint("\nПервый день любого спринта — от него считаются даты в названии карточки.")
    anchor = ask("Начало спринта (ГГГГ-ММ-ДД)")
    days = ask_int("Длина спринта в днях", 14)

    epic = {
        "board_id": int(chosen["id"]),
        "lane_id": lane_id,
        "development_column_id": int(column["id"]),
        "card_type_id": card_type_id,
        "title_prefix": "Долг ",
        "sprint_anchor": anchor,
        "sprint_days": days,
        "keep_in_development": True,
        "properties": {},
    }
    hint("Если тип карточки требует обязательных полей, их надо перечислить в")
    hint("epic.properties — иначе Kaiten не даст создать карточку. Как узнать id полей:")
    hint("  заведи такую карточку руками и посмотри GET /cards/<id> → properties")
    return epic


def step_repo() -> tuple[str, dict]:
    head("6. Репозиторий")
    hint("Фабрика не трогает твою рабочую копию: на каждую карточку она делает")
    hint("отдельный git worktree рядом с собой.")
    while True:
        raw = ask("Путь к репозиторию")
        path = Path(raw).expanduser()
        if (path / ".git").exists():
            break
        print(f"  в {path} нет .git — это не репозиторий")

    name = ask("Короткое имя (им репозиторий указывают в карточке)", path.name)
    remote = ask("Remote", "origin")
    base = subprocess.run(["git", "-C", str(path), "symbolic-ref",
                           "--short", f"refs/remotes/{remote}/HEAD"],
                          capture_output=True, text=True).stdout.strip()
    base = base.rpartition("/")[2] or "main"
    base = ask("Базовая ветка", base)
    ok(f"{name} → {path} ({remote}/{base})")
    return name, {"path": str(path), "remote": remote, "base_branch": base}


def step_project_files() -> None:
    head("7. Правила проекта")
    hint("Промпты агентов держатся общими, а всё, что специфично для твоего проекта —")
    hint("какие файлы правил читать и на что смотреть на ревью — лежит в project/.")
    PROJECT_DIR.mkdir(exist_ok=True)
    for name in PROJECT_FILES:
        target = PROJECT_DIR / name
        example = PROJECT_DIR / f"{name.removesuffix('.md')}.example.md"
        if target.exists():
            ok(f"project/{name} уже есть, не трогаю")
        elif example.exists():
            shutil.copy(example, target)
            ok(f"project/{name} создан из примера")
        else:
            target.write_text("", encoding="utf-8")
            warn(f"project/{name} создан пустым")
    hint("Загляни в них перед первым прогоном: пустые правила — рабочий вариант,")
    hint("но агент тогда ревьюит по общим соображениям, а не по вашим договорённостям.")


def build_config(domain: str, space_id: int, work: dict, inbox: dict | None,
                 epic: dict | None, repo_name: str, repo: dict) -> dict:
    """Собирает config.json: скелет берём из примера, чтобы не потерять комментарии."""
    config = json.loads(strip_jsonc(EXAMPLE_PATH.read_text(encoding="utf-8")))
    config["kaiten"].update({
        "domain": domain,
        "space_id": space_id,
        "board_id": work["board_id"],
        "lane_id": work["lane_id"],
        "card_type_id": work["card_type_id"],
        "columns": work["columns"],
    })
    config["default_repo"] = repo_name
    config["repos"] = {repo_name: repo}
    if inbox:
        config["inbox"] = inbox
    else:
        config.pop("inbox", None)
    if epic:
        config["epic"] = epic
    else:
        config.pop("epic", None)
    return config


def wizard() -> int:
    print("\n\033[1mФабрика агентов — установка\033[0m")
    hint("Мастер найдёт id досок и колонок и напишет config.json.")
    hint("Прервать можно в любой момент: Ctrl-C, ничего не сломается.")

    if CONFIG_PATH.exists():
        warn(f"{CONFIG_PATH.name} уже есть")
        if not ask_yes("Перезаписать?", default=False):
            print("Ничего не меняю.")
            return 0

    domain, token = step_token()
    kaiten = Kaiten(domain, token)
    space_id = step_space(kaiten)
    work = step_work_board(kaiten, space_id)
    inbox = step_inbox(kaiten, space_id)
    epic = step_epic(kaiten, space_id)
    repo_name, repo = step_repo()
    step_project_files()

    config = build_config(domain, space_id, work, inbox, epic, repo_name, repo)
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")

    head("Готово")
    ok(f"{CONFIG_PATH.name} записан")
    print(f"\n  Доска: https://{domain}/space/{space_id}/boards")
    print("\n  Дальше:")
    print("    python3 setup.py --check     проверить, что всё на месте")
    print("    ./run.sh --dry-run           прогон без записи в Kaiten и GitHub")
    print("    ./build-app.sh --install     приложение в меню-баре\n")
    return 0


# --------------------------------------------------------------------------- #
# доктор
# --------------------------------------------------------------------------- #

def strip_jsonc(text: str) -> str:
    """В примере конфига есть //-комментарии, json их не ест."""
    return re.sub(r'^\s*//.*$', '', text, flags=re.MULTILINE)


def check() -> int:
    print("\n\033[1mПроверка установки\033[0m")
    problems = 0

    head("Инструменты")
    for tool, why, probe in [
        ("python3", "обёртка фабрики", None),
        ("curl", "запросы в Kaiten", None),
        ("git", "ветки и worktree", None),
        ("claude", "сам агент", ["claude", "--version"]),
        ("gh", "создание PR", ["gh", "auth", "status"]),
    ]:
        if not shutil.which(tool):
            bad(f"{tool} не найден — {why}")
            problems += 1
            continue
        if probe:
            result = subprocess.run(probe, capture_output=True, text=True)
            if result.returncode != 0:
                bad(f"{tool} есть, но не отвечает: "
                    f"{(result.stderr or result.stdout).strip().splitlines()[0][:80]}")
                problems += 1
                continue
        ok(f"{tool} — {why}")

    head("Конфигурация")
    if not CONFIG_PATH.exists():
        bad(f"{CONFIG_PATH.name} не найден — запусти python3 setup.py")
        return problems + 1
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        bad(f"{CONFIG_PATH.name} — сломанный JSON: {e}")
        return problems + 1
    ok(f"{CONFIG_PATH.name} читается")

    for name in PROJECT_FILES:
        path = PROJECT_DIR / name
        if not path.exists():
            warn(f"project/{name} нет — агент будет работать по общим соображениям")
        elif not path.read_text(encoding="utf-8").strip():
            warn(f"project/{name} пустой — правила проекта агенту не достанутся")
        else:
            ok(f"project/{name}")

    head("Kaiten")
    env, _ = find_env()
    token = env.get("KAITEN_TOKEN")
    if not token:
        bad("KAITEN_TOKEN не найден ни в .env, ни в окружении")
        return problems + 1
    domain = env.get("KAITEN_DOMAIN") or config["kaiten"]["domain"]
    kaiten = Kaiten(domain, token)
    try:
        me = kaiten.request("GET", "/users/current")
        ok(f"токен рабочий, вошли как {me.get('full_name') or me.get('username')}")
    except SetupError as e:
        bad(str(e))
        return problems + 1

    try:
        board = kaiten.board(int(config["kaiten"]["board_id"]))
        ok(f"рабочая доска «{board['title']}»")
        columns = flat_columns(board)
        have = {int(c["id"]) for c in columns}
        titles = {int(c["id"]): c.get("path") for c in columns}
        for key, name, _, _ in COLUMN_ROLES:
            column_id = config["kaiten"]["columns"].get(key)
            if not column_id:
                bad(f"колонка {key} ({name}) не указана в конфиге")
                problems += 1
            elif int(column_id) not in have:
                bad(f"колонка {key} — id {column_id} на доске не найден")
                problems += 1
            else:
                ok(f"{key:<13} → «{titles[int(column_id)]}»")
    except SetupError as e:
        bad(f"рабочая доска недоступна: {e}")
        problems += 1

    for section, label in (("inbox", "инбокс"), ("epic", "доска эпиков")):
        block = config.get(section)
        if not block:
            hint(f"  {label}: выключен")
            continue
        try:
            board = kaiten.board(int(block["board_id"]))
            ok(f"{label}: «{board['title']}»")
        except SetupError as e:
            bad(f"{label} недоступен: {e}")
            problems += 1

    head("Репозитории")
    for name, repo in (config.get("repos") or {}).items():
        path = Path(repo["path"]).expanduser()
        if not (path / ".git").exists():
            bad(f"{name}: в {path} нет .git")
            problems += 1
            continue
        remote = subprocess.run(["git", "-C", str(path), "remote", "get-url", repo["remote"]],
                                capture_output=True, text=True)
        if remote.returncode != 0:
            bad(f"{name}: нет remote «{repo['remote']}»")
            problems += 1
        else:
            ok(f"{name} → {path} ({repo['remote']}/{repo['base_branch']})")

    print()
    if problems:
        print(f"\033[31mПроблем: {problems}\033[0m. Почини их до первого прогона.\n")
    else:
        print("\033[32mВсё на месте.\033[0m Можно запускать: ./run.sh --dry-run\n")
    return 1 if problems else 0


# --------------------------------------------------------------------------- #
# аудит: что уедет в репозиторий
# --------------------------------------------------------------------------- #

# Ищем не по списку названий компаний — такой список сам по себе был бы утечкой.
# Вместо этого берём то, что лежит в твоём локальном конфиге, и проверяем, не
# просочилось ли оно в файлы, которые git собирается закоммитить.
GENERIC_PATTERNS = [
    (r"[\w.+-]+@[\w-]+\.[\w.]+", "почта"),
    (r"/Users/[^/\s\"']+", "домашний каталог"),  # audit:ignore — шаблон находит сам себя
    (r"\bghp_[A-Za-z0-9]{20,}\b", "токен GitHub"),
    (r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", "токен GitHub"),
    (r"\bsk-[A-Za-z0-9-]{20,}\b", "похоже на API-ключ"),
    (r"\bBearer\s+[A-Za-z0-9._-]{16,}", "заголовок с токеном"),
    # Имя и фамилия в примере: в логах и карточках всегда есть живые люди, и в примеры
    # они переезжают незаметно. Ложных срабатываний на прозе почти нет — два слова
    # с большой буквы подряд в русском тексте встречаются редко.
    (r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}\b", "похоже на имя человека"),
]  # audit:ignore — иначе шаблоны находят сами себя

# SSH-адреса публичных хостингов выглядят как почта, но ею не являются.
NOT_EMAIL = re.compile(r"^(?:git|hg)@(?:github|gitlab|bitbucket)\.com$")

# Строку с этой пометкой аудит пропускает. Нужна там, где внутреннее на вид —
# на самом деле часть кода самого аудита или заведомо публичный адрес.
AUDIT_IGNORE = "audit:ignore"

# Файлы целиком вне проверки: в .gitignore пути перечислены по смыслу задачи.
AUDIT_SKIP = {".gitignore"}


def tracked_files() -> list[Path]:
    """Файлы, которые git реально положит в коммит: с учётом .gitignore."""
    if not (ROOT / ".git").exists():
        # репозитория ещё нет — считаем кандидатами всё, кроме заведомо игнорируемого
        ignored = {"worktrees", "logs", "state", "__pycache__", ".git", "node_modules"}
        return [p for p in ROOT.rglob("*")
                if p.is_file() and not (set(p.relative_to(ROOT).parts) & ignored)]
    listing = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True, text=True)
    return [ROOT / line for line in listing.stdout.splitlines() if (ROOT / line).is_file()]


def own_secrets(config: dict) -> list[tuple[str, str]]:
    """Строки из локального конфига, которых в публичном репозитории быть не должно."""
    found = []
    kaiten = config.get("kaiten") or {}
    if kaiten.get("domain"):
        found.append((kaiten["domain"], "домен Kaiten"))
    for key in ("space_id", "board_id", "lane_id"):
        if kaiten.get(key):
            found.append((str(kaiten[key]), f"kaiten.{key}"))
    for key, value in (kaiten.get("columns") or {}).items():
        found.append((str(value), f"id колонки {key}"))
    for section in ("inbox", "epic"):
        for key, value in (config.get(section) or {}).items():
            if isinstance(value, int) and value > 9999:
                found.append((str(value), f"{section}.{key}"))
    for name, repo in (config.get("repos") or {}).items():
        found.append((repo["path"], f"путь к репозиторию {name}"))
        remote = subprocess.run(
            ["git", "-C", repo["path"], "remote", "get-url", repo.get("remote", "origin")],
            capture_output=True, text=True).stdout.strip()
        if remote:
            slug = re.sub(r"^.*[:/]([^/]+/[^/]+?)(?:\.git)?$", r"\1", remote)
            if slug and slug != remote:
                found.append((slug, f"репозиторий {name} на GitHub"))
    return found


def audit() -> int:
    print("\n\033[1mЧто уедет в репозиторий\033[0m")
    hint("Проверяем файлы, которые git положит в коммит, на внутреннее и на секреты.")

    files = tracked_files()
    print(f"\n  файлов под контролем git: {len(files)}")

    needles = []
    if CONFIG_PATH.exists():
        try:
            needles = own_secrets(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError):
            warn("config.json не разобрался — проверю только по общим шаблонам")
    else:
        hint("  config.json нет — проверяю только по общим шаблонам")

    hits = 0
    for path in sorted(files):
        rel = path.relative_to(ROOT).as_posix()
        if rel.split("/")[0] in AUDIT_SKIP or rel in AUDIT_SKIP:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if AUDIT_IGNORE in line:
                continue
            for needle, why in needles:
                if len(str(needle)) > 3 and str(needle) in line:
                    bad(f"{rel}:{lineno} — {why}: {str(needle)[:60]}")
                    hits += 1
            for pattern, why in GENERIC_PATTERNS:
                for match in re.findall(pattern, line):
                    if "example" in match.lower() or "placeholder" in match.lower():
                        continue
                    if NOT_EMAIL.match(match):
                        continue
                    bad(f"{rel}:{lineno} — {why}: {match[:60]}")
                    hits += 1

    head("Файлы, которых в репозитории быть не должно")
    for name in ("config.json", ".env", "state", "logs", "worktrees"):
        leaked = [p for p in files if p.relative_to(ROOT).as_posix().split("/")[0] == name]
        if leaked:
            bad(f"{name} попадёт в коммит ({len(leaked)} файлов) — проверь .gitignore")
            hits += len(leaked)
        else:
            ok(f"{name} — не попадёт")

    print()
    if hits:
        print(f"\033[31mНаходок: {hits}\033[0m. Каждую надо либо убрать, либо занести "
              f"в .gitignore.\n")
        return 1
    print("\033[32mЧисто.\033[0m Внутреннего и секретов в коммите нет.\n")
    return 0


# --------------------------------------------------------------------------- #
# разведка
# --------------------------------------------------------------------------- #

def connect() -> Kaiten:
    env, _ = find_env()
    token = env.get("KAITEN_TOKEN")
    if not token:
        raise SetupError("KAITEN_TOKEN не найден: положи его в .env рядом со скриптом")
    domain = env.get("KAITEN_DOMAIN")
    if not domain and CONFIG_PATH.exists():
        domain = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["kaiten"]["domain"]
    if not domain:
        raise SetupError("не знаю домен Kaiten: добавь KAITEN_DOMAIN в .env")
    return Kaiten(domain, token)


def dump(data, as_json: bool, render) -> int:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        render(data)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Установка и проверка фабрики",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument("--check", action="store_true", help="проверить установку")
    parser.add_argument("--audit", action="store_true",
                        help="проверить, что в репозиторий не утекло внутреннее")
    parser.add_argument("--spaces", nargs="?", const="", metavar="СТРОКА",
                        help="пространства, можно с поиском по названию")
    parser.add_argument("--boards", type=int, metavar="SPACE_ID", help="доски пространства")
    parser.add_argument("--board", type=int, metavar="BOARD_ID",
                        help="колонки и дорожки доски")
    parser.add_argument("--card-types", nargs="?", const="", metavar="СТРОКА",
                        help="типы карточек, можно с поиском")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    args = parser.parse_args()

    if args.check:
        return check()
    if args.audit:
        return audit()

    if args.spaces is not None:
        query = normalize(args.spaces)
        found = [{"id": s["id"], "title": s["title"]} for s in connect().spaces()
                 if query in normalize(s.get("title"))]
        return dump(found, args.json,
                    lambda d: [print(f"{s['id']:>10}  {s['title']}") for s in d])

    if args.boards:
        found = [{"id": b["id"], "title": b["title"]} for b in connect().boards(args.boards)]
        return dump(found, args.json,
                    lambda d: [print(f"{b['id']:>10}  {b['title']}") for b in d])

    if args.board:
        board = connect().board(args.board)
        data = {
            "id": board["id"],
            "title": board["title"],
            "default_card_type_id": board.get("default_card_type_id"),
            "columns": [{"id": c["id"], "title": c.get("title"), "path": c.get("path"),
                         "type": c.get("type"), "sort_order": c.get("sort_order")}
                        for c in flat_columns(board)],
            "lanes": [{"id": l["id"], "title": l.get("title")}
                      for l in board.get("lanes") or []],
            "guessed_columns": match_columns(board),
        }

        def render(d):
            print(f"\n{d['title']}  (id {d['id']})\n")
            print("  колонки:")
            for c in d["columns"]:
                print(f"    {c['id']:>10}  {c['path']}")
            print("\n  дорожки:")
            for l in d["lanes"]:
                print(f"    {l['id']:>10}  {l['title'] or '(без названия)'}")
            print("\n  роли колонок, как их видит фабрика:")
            for key, name, _, _ in COLUMN_ROLES:
                value = d["guessed_columns"].get(key)
                print(f"    {key:<13} {value if value else '— не нашлась'}")
            print()
        return dump(data, args.json, render)

    if args.card_types is not None:
        query = normalize(args.card_types)
        found = [{"id": t["id"], "name": t.get("name") or t.get("title")}
                 for t in connect().card_types()
                 if query in normalize(t.get("name") or t.get("title"))]
        return dump(found, args.json,
                    lambda d: [print(f"{t['id']:>10}  {t['name']}") for t in d])

    return wizard()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SetupError as e:
        print(f"\n\033[31mОшибка:\033[0m {e}\n", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nПрервано. Ничего не изменилось.\n")
        sys.exit(130)
