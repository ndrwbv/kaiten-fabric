#!/usr/bin/env python3
"""
Витрина фабрики: что сделано, что застряло и сколько это стоило.

Меню-бар показывает текущую секунду: какая карточка в работе и что агент делает
прямо сейчас. Здесь — всё остальное: история, деньги, тренды и список задач,
которые фабрика не вытянула, с объяснением, чего именно ей не хватило.

Своей базы у витрины нет, и заводить её незачем — всё уже записано:

- `logs/*.json` — по файлу на каждый запуск агента: вердикт, расход, шаги;
- `logs/events.jsonl` — журнал исходов, по строке на законченную работу;
- `logs/run.log`   — поток событий прогона, отсюда же берутся названия карточек;
- GitHub          — что смержено и что уехало в main чужим PR;
- Kaiten          — где карточки стоят прямо сейчас;
- `state/status.json` — чем прогон занят в эту секунду.

Пишет витрина ровно одно — запуск прогона кнопкой, и делает это тем же `run.sh`,
что и меню-бар: с замком, окружением логин-шелла и записью в `logs/run.log`.

Запуск:

    python3 dashboard.py              # http://127.0.0.1:8777 и открыть браузер
    python3 dashboard.py --port 9000 --no-open
"""
import argparse
import http.server
import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Вся логика, клиенты и пути живут в фабрике. Витрина ничего из этого не повторяет:
# разойдутся — и она будет показывать не ту фабрику, которая работает.
import factory

PAGE = factory.ROOT / "dashboard.html"
RUN_LOG = factory.LOGS / "run.log"
LOCK = factory.STATE / "run.lock"

# --------------------------------------------------------------------------- #
# прогоны: logs/*.json
# --------------------------------------------------------------------------- #

# Имя файла прогона: `card-70376197-20260916-042044.json`. Метка времени в UTC —
# так её пишет factory.py, а показываем местное время.
RUN_FILE_RE = re.compile(r"^(?P<kind>card|review|triage|epic-[a-z-]+)-"
                         r"(?P<card>\d+)-(?P<stamp>\d{8}-\d{6})\.json$")

KIND_LABELS = {
    "card": "работа",
    "review": "ревью",
    "triage": "разведка",
    "epic-acceptance": "эпик: критерии",
    "epic-spec": "эпик: спека",
    "epic-spec-review": "эпик: ревью спеки",
    "epic-decompose": "эпик: сабтаски",
}

# Вердикты всех четырёх агентов в одном словаре: у исполнителя, ревьювера,
# разведчика и эпик-агента наборы разные, но пересечений нет.
STATUS_LABELS = {
    "done": "сделал",
    "unclear": "не понял задачу",
    "blocked": "упёрся в преграду",
    "ok": "ревью пройдено",
    "needs_changes": "вернул на правки",
    "ready": "данных хватает",
    "needs_info": "данных не хватает",
    "not_a_task": "это не задача",
}

# группы для денег: четыре фазы эпика в одну колонку, иначе легенда не читается
def kind_group(kind: str) -> str:
    return "epic" if kind.startswith("epic-") else kind


GROUP_LABELS = {"card": "работа", "review": "ревью", "triage": "разведка",
                "epic": "эпики"}

_runs_cache: dict[str, dict] = {}


def parse_run(match: re.Match, data: dict) -> dict:
    """Один файл прогона — в одну строку витрины."""
    meta = data.get("meta") or {}
    # у ревьювера вердикт лежит под своим ключом, и поле статуса там тоже своё
    verdict = data.get("verdict") or data.get("review") or {}
    at = datetime.strptime(match["stamp"], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    findings = verdict.get("findings") or []
    return {
        "kind": match["kind"],
        "group": kind_group(match["kind"]),
        "card_id": int(match["card"]),
        "at": at.isoformat(),
        "day": at.astimezone().strftime("%Y-%m-%d"),
        "cost": round(float(meta.get("cost_usd") or 0), 4),
        "steps": meta.get("steps") or meta.get("num_turns") or 0,
        "status": verdict.get("status") or verdict.get("verdict"),
        # обрыв снаружи — не вердикт агента: работа шла, её прекратили по потолку
        "cut": factory.AGENT_STOP_REASONS.get(meta.get("subtype"), ""),
        "summary": (verdict.get("summary") or "").strip(),
        "questions": [q for q in (verdict.get("questions") or []) if q],
        "risks": (verdict.get("risks") or verdict.get("risk") or "").strip(),
        "major": sum(1 for f in findings if f.get("severity") == "major"),
        "minor": sum(1 for f in findings if f.get("severity") == "minor"),
    }


def parse_runs() -> list[dict]:
    """
    Все прогоны агентов, разобранные и отсортированные по времени.

    Разбор кэшируется по mtime: файлов сотни, а меняется из них только последний.
    """
    runs = []
    for path in factory.LOGS.glob("*.json"):
        match = RUN_FILE_RE.match(path.name)
        if not match:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        cached = _runs_cache.get(path.name)
        if cached and cached["mtime"] == mtime:
            runs.append(cached["run"])
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        run = parse_run(match, data)
        _runs_cache[path.name] = {"mtime": mtime, "run": run}
        runs.append(run)
    runs.sort(key=lambda run: run["at"])
    return runs


def read_events() -> list[dict]:
    """Журнал исходов. Его пишет `finish_status`, и до сентября 2026 его не было."""
    if not factory.EVENTS_FILE.is_file():
        return []
    events = []
    try:
        for line in factory.EVENTS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    events.sort(key=lambda e: e.get("at") or "")
    return events


# --------------------------------------------------------------------------- #
# названия карточек
# --------------------------------------------------------------------------- #

# В `run.log` каждая взятая карточка представляется: `#70376197 «Флаги ...»`.
# Ходить за названиями в Kaiten незачем: половина карточек давно в архиве.
TITLE_RE = re.compile(r"#(\d{5,})\s+«([^»]{1,200})»")

_titles_cache: dict = {"mtime": 0.0, "size": 0, "titles": {}}


def card_titles() -> dict[int, str]:
    if not RUN_LOG.is_file():
        return {}
    stat = RUN_LOG.stat()
    if stat.st_mtime == _titles_cache["mtime"] and stat.st_size == _titles_cache["size"]:
        return _titles_cache["titles"]
    titles = {}
    try:
        with RUN_LOG.open(encoding="utf-8", errors="replace") as log:
            for line in log:
                for card_id, title in TITLE_RE.findall(line):
                    titles[int(card_id)] = title.strip()
    except OSError:
        return _titles_cache["titles"]
    _titles_cache.update({"mtime": stat.st_mtime, "size": stat.st_size, "titles": titles})
    return titles


def tail(path: Path, lines: int) -> list[str]:
    """Хвост файла без чтения всего файла: лог растёт до мегабайтов."""
    if not path.is_file():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            block, data = 64 * 1024, b""
            while end > 0 and data.count(b"\n") <= lines:
                step = min(block, end)
                end -= step
                handle.seek(end)
                data = handle.read(step) + data
        return data.decode("utf-8", "replace").splitlines()[-lines:]
    except OSError:
        return []


# --------------------------------------------------------------------------- #
# наружное: GitHub, main, Kaiten
# --------------------------------------------------------------------------- #

PR_FIELDS = ("number,url,state,mergedAt,createdAt,closedAt,headRefName,title,"
             "additions,deletions")

# Номер карточки в теле коммита. Пять цифр и больше — чтобы не поймать «#4789»:
# так в сообщениях ссылаются на PR, а карточки Kaiten восьмизначные.
COMMIT_CARD_RE = re.compile(r"#(\d{5,})")


def pull_requests(cfg: dict) -> dict[int, dict]:
    """
    PR фабрики по всем репозиториям конфига: `{id карточки: PR}`.

    Один вызов `gh` на репозиторий: ветки фабрики все с общим префиксом, и поиск
    по `head:` отдаёт их пачкой вместе с состоянием и датой мержа.
    """
    prefix = (cfg.get("pr") or {}).get("branch_prefix") or "ai/card-"
    branch_re = re.compile(re.escape(prefix) + r"(\d+)")
    found: dict[int, dict] = {}
    for key, repo in (cfg.get("repos") or {}).items():
        path = Path(repo.get("path") or "")
        if not path.is_dir():
            continue
        proc = subprocess.run(
            ["gh", "pr", "list", "--search", f"head:{prefix}", "--state", "all",
             "--limit", "200", "--json", PR_FIELDS],
            cwd=path, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            raise RuntimeError(f"gh pr list ({key}): {proc.stderr.strip()[:200]}")
        for pull in json.loads(proc.stdout or "[]"):
            match = branch_re.match(pull.get("headRefName") or "")
            if not match:
                continue
            card_id = int(match.group(1))
            pull["repo"] = key
            older = found.get(card_id)
            # по карточке мог открыться второй PR — верим свежему
            if not older or (pull.get("createdAt") or "") > (older.get("createdAt") or ""):
                found[card_id] = pull
    return found


def shipped_to_main(cfg: dict, known: set[int]) -> dict[int, dict]:
    """
    Карточки, чья правка лежит в базовой ветке: `{id: коммит}`.

    Второй признак «сделано», и без него первого мало: PR карточки могли закрыть,
    а работу забрать в чужой PR — фабрика считает такую карточку готовой, и витрина
    должна считать так же. Ищем по номеру карточки в сообщении коммита: исполнителю
    велено начинать первую строку с `#<id>`, и при сквоше она уезжает в main целиком.

    Одним вызовом на репозиторий, а не по вызову на карточку: история за полгода
    разбирается быстрее, чем `git log` успеет запуститься два десятка раз. Зато
    сверяться приходится со списком известных карточек: номер в тексте коммита —
    это чаще всего номер PR или чужой задачи, и без сверки в «сделано» попало бы
    полрепозитория.
    """
    found: dict[int, dict] = {}
    if not known:
        return found
    for key, repo in (cfg.get("repos") or {}).items():
        path = Path(repo.get("path") or "")
        if not path.is_dir():
            continue
        ref = f"{repo.get('remote', 'origin')}/{repo.get('base_branch', 'main')}"
        proc = subprocess.run(
            ["git", "log", ref, "--since=8 months ago", "--no-merges",
             "--format=%H%x1f%ct%x1f%s%x1f%b%x1e"],
            cwd=path, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            continue  # ветки может не быть — это не поломка витрины
        for record in proc.stdout.split("\x1e"):
            parts = record.strip("\n").split("\x1f")
            if len(parts) < 4:
                continue
            sha, stamp, subject, body = parts[0], parts[1], parts[2], parts[3]
            mentioned = {int(n) for n in COMMIT_CARD_RE.findall(subject + " " + body)}
            for card_id in mentioned & known:
                # первым идёт самый свежий коммит, он и остаётся
                found.setdefault(card_id, {
                    "sha": sha[:8], "subject": subject, "repo": key,
                    "at": datetime.fromtimestamp(int(stamp), timezone.utc).isoformat(),
                })
    return found


# Колонки доски по порядку потока. Роли могут делить колонку — тогда колонка
# достаётся первой роли из этого списка, как и в самой фабрике.
ROLES = [
    ("queue", "Очередь", "фабрика берёт отсюда"),
    ("in_progress", "В работе", "агент пишет код"),
    ("agent_review", "Ревью агента", "ревьювер смотрит PR"),
    ("fixes", "Правки", "агент правит по замечаниям"),
    ("review", "На ревью", "твой ход"),
    ("question", "Вопрос от агента", "ждёт твоего ответа"),
    ("failed", "Упало", "сломалось, нужен человек"),
    ("done", "Готово", "работа в main"),
]


def board_state(cfg: dict) -> list[dict]:
    """Где карточки стоят прямо сейчас — по доскам и колонкам."""
    env = factory.load_env()
    domain = env.get("KAITEN_DOMAIN") or cfg["kaiten"]["domain"]
    kaiten = factory.Kaiten(domain, env["KAITEN_TOKEN"], cfg["kaiten"]["space_id"])
    boards = []
    for profile in factory.board_profiles(cfg):
        cards = kaiten.cards_on_board(profile["board_id"])
        by_column = defaultdict(list)
        for card in cards:
            by_column[card.get("column_id")].append(card)
        columns, taken = [], set()
        for role, label, hint in ROLES:
            column_id = profile["columns"].get(role)
            if not column_id or column_id in taken:
                continue
            taken.add(column_id)
            columns.append({
                "role": role, "label": label, "hint": hint,
                "cards": [{
                    "id": card["id"],
                    "title": (card.get("title") or "").strip(),
                    "url": kaiten.card_url(card),
                    "blocked": bool(card.get("blocked")),
                } for card in sorted(by_column.get(column_id, []),
                                     key=lambda c: c.get("sort_order") or 0)],
            })
        # всё, что стоит в колонках без роли, увёл человек — это уже не поток фабрики
        outside = sum(len(cards) for column, cards in by_column.items()
                      if column not in taken)
        boards.append({"key": profile["key"], "columns": columns, "outside": outside})
    return boards


class Outside:
    """
    Кэш всего, за чем надо ходить наружу.

    GitHub и Kaiten отвечают секундами, а страница обновляется каждые полминуты:
    без кэша витрина сама себе устроила бы очередь. Свежесть обновляется в фоне,
    страница же всегда получает ответ сразу — пусть и слегка вчерашний.
    """

    TTL = 180

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.data = {"prs": {}, "shipped": {}, "boards": [], "at": None, "errors": {}}
        self.busy = False

    def refresh(self) -> None:
        data = {"prs": {}, "shipped": {}, "boards": [], "errors": {}}
        known = {run["card_id"] for run in parse_runs()}
        calls = (("prs", lambda cfg: pull_requests(cfg)),
                 ("shipped", lambda cfg: shipped_to_main(cfg, known)),
                 ("boards", lambda cfg: board_state(cfg)))
        for name, call in calls:
            try:
                data[name] = call(self.cfg)
            except Exception as e:  # noqa: BLE001 — витрина не падает из-за одного источника
                data["errors"][name] = str(e)[:200]
                data[name] = self.data.get(name) or ({} if name != "boards" else [])
        data["at"] = time.time()
        with self.lock:
            self.data = data
            self.busy = False

    def snapshot(self, force: bool = False) -> dict:
        with self.lock:
            data = self.data
            age = time.time() - (data["at"] or 0)
            stale = data["at"] is None or age > self.TTL
            if self.busy or not (stale or force):
                return data
            self.busy = True
        if data["at"] is None or force:
            self.refresh()          # первый заход ждёт: показывать нечего
            with self.lock:
                return self.data
        threading.Thread(target=self.refresh, daemon=True).start()
        return data


# --------------------------------------------------------------------------- #
# сборка витрины
# --------------------------------------------------------------------------- #

# Исходы карточки. Порядок ключей — порядок колонок в витрине.
OUTCOMES = {
    "merged": "уехало в main",
    "waiting": "ждёт тебя",
    "review": "у ревьювера",
    "working": "в работе",
    "fixes": "на правках",
    "question": "не хватило данных",
    "budget": "не влез в бюджет",
    "broken": "упало",
    "closed": "PR закрыт без мержа",
    "none": "не дошло до PR",
}

FAILED_OUTCOMES = ("question", "budget", "broken", "closed", "none")

# Почему исполнитель взялся за карточку ещё раз. Те же четыре причины, что различает
# и сама фабрика, когда берёт карточку в работу (см. `reason` в `process`).
ROUND_LABELS = {
    "first": "первый заход",
    "fixing": "правки после ревью",
    "resuming": "продолжил прерванное",
    "returning": "после ответа человека",
    "again": "повторный заход",
}

# Заходы, которые значат «сделано не с первого раза». Продолжение прерванного сюда
# не входит: это та же попытка, просто разорванная потолком расхода.
REWORK_ROUNDS = ("fixing", "returning", "again")

# Что человек должен сделать с карточкой, по роли колонки, где она стоит.
WAIT_REASONS = {
    "review": "посмотреть PR",
    "question": "ответить на вопрос агента",
    "failed": "разобрать поломку",
}
BLOCKED_REASON = "снять блокер"


def moment(stamp) -> datetime | None:
    """ISO-метка в datetime. Пусто или мусор — None."""
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def seconds_between(start, end=None) -> float:
    """Сколько секунд прошло от метки до метки (по умолчанию — до сейчас)."""
    began = moment(start)
    if not began:
        return 0.0
    finished = moment(end) or datetime.now(timezone.utc)
    return max((finished - began).total_seconds(), 0.0)


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def card_outcome(card: dict) -> str:
    """
    Чем кончилось по карточке — и, если ещё не кончилось, чей сейчас ход.

    Работа в main сильнее всего остального: карточку могли закрыть чужим PR, и это
    «сделано», а не «PR закрыт». Дальше решает колонка: она и есть ответ на вопрос,
    кто держит мяч — фабрика, ревьювер или человек. И только если карточки на доске
    уже нет (уехала в архив), разбираем, чем кончился последний заход.
    """
    pull = card.get("pr") or {}
    column = card.get("column")
    if pull.get("state") == "MERGED" or card.get("shipped") or column == "done":
        return "merged"
    if column == "failed" or card.get("last_outcome", "").startswith("упало"):
        return "broken"
    if column == "question":
        return "question"
    if column == "agent_review":
        # блокер на карточке ревьювера значит, что он уже отработал и позвал человека:
        # на доске, где ревью агента и ревью человека делят колонку, разницы больше нет
        return "waiting" if card.get("blocked") else "review"
    if column == "review":
        return "waiting"
    if column == "fixes":
        return "fixes"
    if column in ("in_progress", "queue"):
        return "working"
    if pull.get("state") == "OPEN":
        return "waiting"
    if card.get("cut") or card.get("last_outcome") == "не влез в бюджет":
        return "budget"
    if card["status"] in ("unclear", "blocked"):
        return "question"
    if pull.get("state") == "CLOSED":
        return "closed"
    return "none"


def missing(card: dict) -> list[str]:
    """Чего не хватило — человеческим языком, из вердикта последнего прогона."""
    reasons = []
    if card["outcome"] == "budget":
        cap = card.get("cap")
        spent = card.get("last_cost") or 0
        reasons.append(f"упёрся в потолок расхода: ${spent:.2f}"
                       + (f" при потолке ${cap:.0f}" if cap else ""))
    if card["outcome"] == "broken" and card.get("detail"):
        reasons.append(card["detail"])
    reasons += card.get("questions") or []
    if not reasons and card.get("risks"):
        reasons.append(card["risks"])
    if not reasons and card.get("summary"):
        reasons.append(card["summary"])
    return [r[:400] for r in reasons[:5]]


def classify_rounds(history: list[dict]) -> list[str]:
    """
    Почему исполнитель брался за карточку в каждый из заходов.

    В логах прогона причины нет, но восстанавливается она однозначно — теми же
    признаками, по которым её определяет сама фабрика, когда берёт карточку:

    - перед заходом ревьювер вернул замечания  → правки после ревью (`fixing`);
    - прошлый заход оборвали по потолку        → продолжение (`resuming`);
    - прошлый заход кончился вопросом          → человек ответил (`returning`);
    - ничего из этого                          → человек вернул карточку сам.

    Последний случай — самый интересный: значит, работу приняли, а потом всё равно
    пришлось переделывать.
    """
    rounds, previous = [], None
    for run in history:
        if run["kind"] != "card":
            previous = run
            continue
        if previous is None:
            rounds.append("first")
        elif previous["kind"] == "review":
            rounds.append("fixing" if previous["status"] == "needs_changes" else "again")
        elif previous["cut"]:
            rounds.append("resuming")
        elif previous["status"] in ("unclear", "blocked"):
            rounds.append("returning")
        else:
            rounds.append("again")
        previous = run
    return rounds


def collect_cards(runs: list[dict], events: list[dict], outside: dict,
                  titles: dict[int, str], cfg: dict) -> list[dict]:
    """Карточки рабочей доски: по одной строке на каждую, со всей её историей."""
    domain = cfg["kaiten"]["domain"]
    space = cfg["kaiten"]["space_id"]
    cap = float((cfg.get("agent") or {}).get("max_budget_usd") or 0)

    # где карточка стоит сейчас и как называется — из живого снимка доски
    board_cards: dict[int, dict] = {}
    for board in outside.get("boards") or []:
        for column in board["columns"]:
            for card in column["cards"]:
                board_cards[card["id"]] = {**card, "role": column["role"]}

    last_event: dict[int, dict] = {}
    for event in events:
        if event.get("kind") in ("card", "review") and event.get("card_id"):
            last_event[int(event["card_id"])] = event

    history: dict[int, list[dict]] = defaultdict(list)
    for run in runs:
        if run["kind"] in ("card", "review"):
            history[run["card_id"]].append(run)

    result = []
    for card_id, story in history.items():
        work = [run for run in story if run["kind"] == "card"]
        reviews = [run for run in story if run["kind"] == "review"]
        rounds = classify_rounds(story)
        counted = {name: rounds.count(name) for name in ROUND_LABELS}
        last_work = work[-1] if work else {}
        on_board = board_cards.get(card_id) or {}
        event = last_event.get(card_id) or {}
        pull = (outside.get("prs") or {}).get(card_id)
        pr_title = re.sub(r"^#\d+\s*", "", (pull or {}).get("title") or "")

        card = {
            "id": card_id,
            # Названия ищем там, где они целые. В логе и в журнале они подрезаны под
            # меню-бар: туда влезает 60 знаков, и «...на финальном экране к» — это
            # не обрыв витрины, а всё, что фабрика про карточку записала.
            "title": (on_board.get("title") or pr_title or titles.get(card_id)
                      or event.get("title") or ""),
            "url": f"https://{domain}/space/{space}/boards/card/{card_id}",
            "pr": pull,
            "shipped": (outside.get("shipped") or {}).get(card_id),
            "column": on_board.get("role"),
            "blocked": bool(on_board.get("blocked")),
            "first": story[0]["at"],
            "last": story[-1]["at"],
            "runs": len(story),
            "work_runs": len(work),
            "review_runs": len(reviews),
            "needs_changes": sum(1 for r in reviews if r["status"] == "needs_changes"),
            "rounds": counted,
            # заходы, не считая продолжений прерванного: это та же попытка
            "passes": len(work) - counted["resuming"],
            "rework": sum(counted[name] for name in REWORK_ROUNDS),
            "cost": round(sum(run["cost"] for run in story), 2),
            "last_cost": last_work.get("cost", 0.0),
            "cut": last_work.get("cut", ""),
            "status": next((r["status"] for r in reversed(work) if r["status"]), None),
            "questions": next((r["questions"] for r in reversed(work) if r["questions"]), []),
            "risks": next((r["risks"] for r in reversed(work) if r["risks"]), ""),
            "summary": next((r["summary"] for r in reversed(work) if r["summary"]), ""),
            "last_outcome": event.get("outcome") or "",
            "detail": event.get("detail") or "",
            "cap": cap,
        }
        card["outcome"] = card_outcome(card)
        card["clean"] = card["outcome"] == "merged" and card["rework"] == 0
        card["missing"] = missing(card) if card["outcome"] in FAILED_OUTCOMES else []

        # Ход перешёл человеку тогда, когда фабрика сделала последний шаг. Если PR
        # открыли позже (бывает: ревьювер прошёлся, а PR создался следующим шагом) —
        # считаем от PR: раньше этого смотреть всё равно было нечего.
        handover = max([stamp for stamp in (card["last"], (pull or {}).get("createdAt"))
                        if stamp] or [""])
        card["handover"] = handover
        merged_at = (pull or {}).get("mergedAt") or (card["shipped"] or {}).get("at")
        card["merged_at"] = merged_at if card["outcome"] == "merged" else None
        card["merge_wait_s"] = (round(seconds_between(handover, merged_at))
                                if card["merged_at"] else 0)
        for heavy in ("summary", "risks", "questions", "detail"):
            card.pop(heavy, None)
        result.append(card)

    result.sort(key=lambda c: c["last"], reverse=True)
    return result


def waiting_list(cards: list[dict], outside: dict, cfg: dict) -> list[dict]:
    """
    Карточки, по которым ход человека, — с временем ожидания.

    Собираем по доске, а не по логам: карточку мог положить в «На ревью» человек,
    и в логах фабрики её не будет вовсе, а ждать она всё равно ждёт. Заблокированные
    считаются всегда и в любой колонке: блокер — это и есть способ фабрики сказать
    «дальше ты», когда отдельной колонки под роль на доске нет.
    """
    domain = cfg["kaiten"]["domain"]
    known = {card["id"]: card for card in cards}
    waiting = []
    for board in outside.get("boards") or []:
        for column in board["columns"]:
            for entry in column["cards"]:
                reason = WAIT_REASONS.get(column["role"], "")
                if entry.get("blocked"):
                    reason = BLOCKED_REASON
                if not reason:
                    continue
                card = known.get(entry["id"]) or {}
                pull = card.get("pr") or {}
                since = card.get("handover") or pull.get("createdAt") or ""
                waiting.append({
                    "id": entry["id"],
                    "title": entry["title"] or card.get("title") or "",
                    "url": entry["url"],
                    "board": board["key"],
                    "column": column["label"],
                    "reason": reason,
                    "since": since,
                    "wait_s": round(seconds_between(since)) if since else 0,
                    "pr": {"url": pull.get("url"), "number": pull.get("number")}
                          if pull.get("url") else None,
                    "runs": card.get("work_runs", 0),
                    "rework": card.get("rework", 0),
                })
    waiting.sort(key=lambda item: item["wait_s"], reverse=True)
    return waiting


def daily(runs: list[dict], cards: list[dict], days: int) -> list[dict]:
    """Ряд по дням: деньги по фазам, открытые PR, мержи и неудачи."""
    series: dict[str, dict] = {}

    def day(stamp: str) -> dict:
        return series.setdefault(stamp, {
            "day": stamp, "cost": 0.0, "runs": 0,
            "by_group": {key: 0.0 for key in GROUP_LABELS},
            "opened": 0, "merged": 0, "failed": 0,
        })

    for run in runs:
        point = day(run["day"])
        point["cost"] += run["cost"]
        point["runs"] += 1
        point["by_group"][run["group"]] += run["cost"]
        if run["cut"] or run["status"] in ("unclear", "blocked"):
            point["failed"] += 1

    for card in cards:
        pull = card.get("pr") or {}
        if pull.get("createdAt"):
            day(local_day(pull["createdAt"]))["opened"] += 1
        if card.get("merged_at"):
            day(local_day(card["merged_at"]))["merged"] += 1

    # дни без работы тоже рисуем: без них тихая неделя выглядит как плотная
    if series:
        first = datetime.strptime(min(series), "%Y-%m-%d")
        if days:
            edge = datetime.now() - timedelta(days=days - 1)
            first = max(first, edge)
        cursor, today = first.date(), datetime.now().date()
        while cursor <= today:
            day(cursor.strftime("%Y-%m-%d"))
            cursor += timedelta(days=1)

    points = sorted(series.values(), key=lambda p: p["day"])
    for point in points:
        point["cost"] = round(point["cost"], 2)
        point["by_group"] = {k: round(v, 2) for k, v in point["by_group"].items()}
    return points[-days:] if days else points


def weekly(cards: list[dict], weeks: int = 10) -> list[dict]:
    """
    Ряд по неделям: сколько задач уехало в main, сколько с первого раза, сколько
    потерялось. Недели, а не дни: по дням у фабрики то густо, то пусто, и тренд
    в такой ряби не виден.
    """
    series: dict[str, dict] = {}

    def week(stamp: str) -> dict:
        when = moment(stamp)
        if not when:
            return {}
        local = when.astimezone()
        monday = (local - timedelta(days=local.weekday())).strftime("%Y-%m-%d")
        return series.setdefault(monday, {"week": monday, "merged": 0, "clean": 0,
                                          "rework": 0, "failed": 0})

    for card in cards:
        if card.get("merged_at"):
            point = week(card["merged_at"])
            if point:
                point["merged"] += 1
                point["clean" if card["clean"] else "rework"] += 1
        elif card["outcome"] in FAILED_OUTCOMES:
            point = week(card["last"])
            if point:
                point["failed"] += 1

    if series:
        cursor = datetime.strptime(min(series), "%Y-%m-%d").date()
        today = datetime.now().date()
        while cursor <= today:
            week(cursor.isoformat())
            cursor += timedelta(days=7)
    return sorted(series.values(), key=lambda p: p["week"])[-weeks:]


def local_day(stamp: str) -> str:
    """ISO-метка (обычно UTC от GitHub) — в местный день."""
    when = moment(stamp)
    return when.astimezone().strftime("%Y-%m-%d") if when else ""


def since(days: int) -> str:
    """Граница окна в виде ISO-метки, с которой сравниваются прогоны."""
    if not days:
        return ""
    edge = datetime.now(timezone.utc).timestamp() - days * 86400
    return datetime.fromtimestamp(edge, timezone.utc).isoformat()


def run_lock() -> dict:
    """Кто держит замок прогона. Тот же замок, что ставит и снимает `run.sh`."""
    if not LOCK.is_dir():
        return {"held": False}
    pid = 0
    try:
        pid = int((LOCK / "pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    alive = False
    if pid:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
    try:
        age = (time.time() - LOCK.stat().st_mtime) / 60
    except OSError:
        age = 0
    return {"held": True, "pid": pid, "alive": alive, "age_min": round(age, 1)}


def status_file() -> dict:
    try:
        return json.loads(factory.STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def pulse() -> dict:
    """Живая часть: что фабрика делает в эту секунду. Дёргается чаще всего остального."""
    status = status_file()
    lock = run_lock()
    return {
        "running": bool(lock.get("alive")),
        "lock": lock,
        "phase": status.get("phase"),
        "phase_since": status.get("phase_since"),
        "card": status.get("card"),
        "agent": status.get("agent"),
        "run_started": status.get("run_started"),
        "updated": status.get("updated"),
        "last": status.get("last"),
        "auth": status.get("auth"),
        "queue": status.get("queue"),
        "inbox_pending": status.get("inbox_pending"),
        "night_waiting": status.get("night_waiting"),
        "epics_waiting": status.get("epics_waiting"),
        "flow": status.get("flow") or [],
        "waiting": status.get("waiting") or [],
    }


def overview(cfg: dict, outside: Outside, days: int, force: bool = False) -> dict:
    """
    Всё, что показывает витрина, одним ответом.

    Главный вопрос страницы — «помогает ли эта штука работать», и числа подобраны
    под него: сколько работы уехало в main, сколько из неё пришлось переделывать
    и сколько задач стоит и ждёт человека. Деньги тоже считаются, но живут
    на отдельной странице: они отвечают на другой вопрос.
    """
    external = outside.snapshot(force=force)
    runs = parse_runs()
    events = read_events()
    titles = card_titles()
    cards = collect_cards(runs, events, external, titles, cfg)
    waiting = waiting_list(cards, external, cfg)

    edge = since(days)
    window_runs = [run for run in runs if run["at"] >= edge]
    window_ids = {run["card_id"] for run in window_runs}
    window_cards = [card for card in cards if card["id"] in window_ids]

    by_group = defaultdict(float)
    for run in window_runs:
        by_group[run["group"]] += run["cost"]

    merged = [c for c in window_cards if c["outcome"] == "merged"]
    clean = [c for c in merged if c["clean"]]
    reworked = [c for c in merged if not c["clean"]]
    with_pr = [c for c in window_cards if c.get("pr")]
    failed = [c for c in window_cards if c["outcome"] in FAILED_OUTCOMES]

    # ожидание считаем только по тем, где оно осмысленно: по смерженным — сколько
    # задача пролежала готовой, по ждущим — сколько лежит прямо сейчас
    merge_waits = [c["merge_wait_s"] for c in merged if c["merge_wait_s"]]
    live_waits = [item["wait_s"] for item in waiting if item["wait_s"]]

    returns = defaultdict(int)
    for card in merged:
        for name in REWORK_ROUNDS:
            returns[name] += card["rounds"][name]

    triage_runs = [run for run in window_runs if run["kind"] == "triage"]
    triage_by_status = defaultdict(int)
    for run in triage_runs:
        triage_by_status[run["status"] or "нет вердикта"] += 1

    epic_runs = [run for run in window_runs if run["group"] == "epic"]
    cut_runs = [run for run in window_runs if run["cut"]]
    cost = round(sum(run["cost"] for run in window_runs), 2)

    return {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "days": days,
        "outside_at": external.get("at"),
        "errors": external.get("errors") or {},
        "pulse": pulse(),
        "totals": {
            "merged": len(merged),
            "clean": len(clean),
            "reworked": len(reworked),
            "returns": dict(returns),
            "reviewer_returns": sum(c["needs_changes"] for c in merged),
            "done": len(with_pr),
            "failed": len(failed),
            "cards": len(window_cards),
            "waiting": len(waiting),
            "wait_median_s": round(median(live_waits)),
            "wait_max_s": round(max(live_waits)) if live_waits else 0,
            "merge_wait_median_s": round(median(merge_waits)),
            "merge_wait_max_s": round(max(merge_waits)) if merge_waits else 0,
            "cost": cost,
            "by_group": {key: round(value, 2) for key, value in by_group.items()},
            "runs": len(window_runs),
            "cost_per_merged": round(sum(c["cost"] for c in merged) / len(merged), 2)
                               if merged else 0,
            "cut_runs": len(cut_runs),
            "cut_cost": round(sum(run["cost"] for run in cut_runs), 2),
        },
        "trend": daily(window_runs, window_cards, days or 0),
        "weeks": weekly(cards),
        "boards": external.get("boards") or [],
        "cards": window_cards,
        "waiting_cards": waiting,
        "stuck": [c for c in window_cards if c["outcome"] in FAILED_OUTCOMES],
        "triage": {
            "runs": len(triage_runs),
            "cards": len({run["card_id"] for run in triage_runs}),
            "cost": round(sum(run["cost"] for run in triage_runs), 2),
            "by_status": dict(triage_by_status),
        },
        "epics": {
            "runs": len(epic_runs),
            "cost": round(sum(run["cost"] for run in epic_runs), 2),
            "cards": len({run["card_id"] for run in epic_runs}),
        },
        "top_cost": sorted(window_cards, key=lambda c: c["cost"], reverse=True)[:5],
        "labels": {"kind": KIND_LABELS, "status": STATUS_LABELS,
                   "group": GROUP_LABELS, "outcome": OUTCOMES, "round": ROUND_LABELS},
        "caps": {
            "agent": (cfg.get("agent") or {}).get("max_budget_usd"),
            "reviewer": (cfg.get("reviewer") or {}).get("max_budget_usd"),
            "triager": (cfg.get("triager") or {}).get("max_budget_usd"),
            "run": cfg.get("max_spend_per_run"),
        },
    }


# --------------------------------------------------------------------------- #
# запуск прогона
# --------------------------------------------------------------------------- #

# Те же режимы, что кнопками в меню-баре: витрина не придумывает своих способов
# запускать фабрику, а зовёт `run.sh` с теми же флагами.
RUN_MODES = {
    "board": ["--no-triage", "--no-epics"],
    "full": [],
    "inbox": ["--only-triage"],
    "epics": ["--only-epics"],
    "review": ["--only-review"],
    "merged": ["--only-merged"],
}


def start_run(mode: str, card: int = 0, epic: bool = False, dry: bool = False) -> dict:
    """Запустить прогон так же, как это делает меню-бар."""
    lock = run_lock()
    if lock.get("alive"):
        return {"ok": False, "text": f"прогон уже идёт (pid {lock['pid']}, "
                                     f"{lock['age_min']:.0f} мин)"}
    if card:
        flags = ["--epic-card", str(card)] if epic else ["--card", str(card)]
    elif mode in RUN_MODES:
        flags = list(RUN_MODES[mode])
    else:
        return {"ok": False, "text": f"неизвестный режим: {mode}"}
    if dry:
        flags.append("--dry-run")

    command = f"cd {shell_quote(str(factory.ROOT))} && ./run.sh " + " ".join(flags)
    # -ilc: интерактивный логин-шелл. Только он даёт агенту то же окружение, что
    # и терминал — версию node из nvm и NPM_TOKEN из ~/.zshrc
    subprocess.Popen(["/bin/zsh", "-ilc", command],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    what = f"#{card}" if card else mode
    return {"ok": True, "text": f"прогон пошёл: {what}" + (" (сухой)" if dry else "")}


def stop_run() -> dict:
    """
    Погасить прогон целиком: и обёртку, и питон, и агента.

    Бьём по группе процессов, а не по pid: `run.sh` запускает питон, тот — `claude`,
    и убитая вершина оставила бы агента дожигать бюджет в одиночестве.
    """
    lock = run_lock()
    if not lock.get("alive"):
        return {"ok": False, "text": "прогон и так не идёт"}
    pid = lock["pid"]
    try:
        group = os.getpgid(pid)
    except OSError as e:
        return {"ok": False, "text": f"процесс {pid} не нашёлся: {e}"}
    try:
        os.killpg(group, signal.SIGTERM)
        time.sleep(2)
        try:
            os.kill(pid, 0)
            os.killpg(group, signal.SIGKILL)
        except OSError:
            pass
    except OSError as e:
        return {"ok": False, "text": f"не гасится: {e}"}
    return {"ok": True, "text": f"прогон {pid} остановлен"}


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------- #
# сервер
# --------------------------------------------------------------------------- #

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "fabrica-dashboard"
    cfg: dict = {}
    outside: Outside = None

    def do_GET(self) -> None:                      # noqa: N802 — имя от базового класса
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/":
            return self.send_page()
        if url.path == "/api/overview":
            days = int((query.get("days") or ["30"])[0])
            force = (query.get("force") or ["0"])[0] == "1"
            return self.send_json(overview(self.cfg, self.outside, days, force))
        if url.path == "/api/pulse":
            return self.send_json(pulse())
        if url.path == "/api/log":
            lines = min(int((query.get("n") or ["120"])[0]), 2000)
            return self.send_json({"lines": tail(RUN_LOG, lines)})
        self.send_error(404)

    def do_POST(self) -> None:                     # noqa: N802
        url = urllib.parse.urlparse(self.path)
        # Кнопка тратит настоящие деньги, поэтому запуск требует заголовка, который
        # чужая страница поставить не может: браузер не пошлёт его без preflight.
        if self.headers.get("X-Fabrica") != "1":
            return self.send_json({"ok": False, "text": "чужой запрос"}, code=403)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or "{}")
        except (ValueError, json.JSONDecodeError):
            body = {}
        if url.path == "/api/run":
            return self.send_json(start_run(
                body.get("mode") or "board", int(body.get("card") or 0),
                bool(body.get("epic")), bool(body.get("dry"))))
        if url.path == "/api/stop":
            return self.send_json(stop_run())
        self.send_error(404)

    def send_page(self) -> None:
        try:
            page = PAGE.read_bytes()
        except OSError:
            return self.send_error(500, "dashboard.html рядом с dashboard.py не нашёлся")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(page)

    def send_json(self, data, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass  # страницу закрыли посреди запроса — обычное дело

    def log_message(self, fmt, *args) -> None:
        pass  # в консоли витрины полезны только её собственные сообщения


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Витрина фабрики в браузере")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--no-open", action="store_true", help="не открывать браузер")
    args = parser.parse_args()

    if not factory.CONFIG_PATH.is_file():
        print("нет config.json — запусти сначала: python3 setup.py")
        return 1
    cfg = json.loads(factory.CONFIG_PATH.read_text(encoding="utf-8"))

    Handler.cfg = cfg
    Handler.outside = Outside(cfg)
    # первый обход наружного делаем сразу и в фоне: пока человек читает страницу,
    # PR-ы и доска уже приедут
    threading.Thread(target=Handler.outside.refresh, daemon=True).start()

    address = ("127.0.0.1", args.port)
    try:
        server = Server(address, Handler)
    except OSError as e:
        print(f"порт {args.port} занят ({e}) — возьми другой: --port 9000")
        return 1
    url = f"http://127.0.0.1:{args.port}/"
    print(f"витрина фабрики: {url}   (Ctrl+C — выход)")
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nпока")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
