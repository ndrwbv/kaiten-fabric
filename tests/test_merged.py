"""
Проверка фазы мержа без GitHub и без Kaiten.

Работа, уехавшая в main, закрывает карточку, и проверить это руками дорого: нужен
живой PR, живая доска и человек с правом мержа. Поэтому здесь `gh` и `git` подменены
заглушками, а Kaiten — фальшивкой, которая только записывает, о чём её попросили.
Запуск: `python3 tests/test_merged.py`.
"""
import importlib.util, json, subprocess, tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "factory", Path(__file__).resolve().parent.parent / "factory.py")
f = importlib.util.module_from_spec(spec); spec.loader.exec_module(f)

# Все id здесь выдуманные — и доска, и колонки, и карточка. Настоящие сюда попасть
# не должны: репозиторий публичный, это ловит `python3 setup.py --audit`.
BOARD = 1000
COLUMNS = {"queue": 101, "in_progress": 102, "question": 103, "failed": 104,
           "agent_review": 105, "fixes": 106, "review": 107, "done": 108}
CARD = 555
PR = "https://github.com/o/r/pull/7"
BRANCH = "ai/card-555-popravit-tekst"

CFG = {"default_repo": "kiosk",
       "repos": {"kiosk": {"path": "/nope", "remote": "origin", "base_branch": "main"}},
       "pr": {"branch_prefix": "ai/card-"}}

MERGED = {"url": PR, "state": "MERGED", "mergedAt": "2026-09-10T06:11:20Z",
          "mergedBy": {"login": "aboev"}, "headRefName": BRANCH}
OPEN = {"url": PR, "state": "OPEN", "mergedAt": None,
        "mergedBy": None, "headRefName": BRANCH}
# PR закрыли, не смержив: так бывает, когда правку забрали в чужой PR
CLOSED = {"url": PR, "state": "CLOSED", "mergedAt": None,
          "mergedBy": None, "headRefName": BRANCH}

REPORT = "🤖 **Готово, нужен ревью.**\n\nPR: " + PR + "\nВетка: `" + BRANCH + "`"


class Args:
    dry_run = False
    prompt_only = True   # чтобы фаза не писала status.json настоящего прогона


class FakeKaiten:
    """
    Заглушка Kaiten: одна карточка в «На ревью», отчёт исполнителя со ссылкой на PR.

    Всё, что фабрика пишет, складывается в списки — по ним и проверяем.
    """
    def __init__(self, comments=None, blockers=None, column=None, description=""):
        self.given = comments if comments is not None else [{"text": REPORT}]
        self.held = blockers if blockers is not None else []
        self.column = column or COLUMNS["review"]
        self.description = description
        self.written, self.moves, self.unblocked = [], [], []

    def cards_in_column(self, board_id, column_id):
        if board_id != BOARD or column_id != self.column:
            return []
        return [{"id": CARD, "title": "Поправить текст", "board_id": BOARD,
                 "column_id": column_id, "description": self.description}]

    def comments(self, card_id): return list(self.given)
    def comment(self, card_id, text): self.written.append(text)
    def move(self, card_id, column_id): self.moves.append((card_id, column_id))
    def blockers(self, card_id): return list(self.held)

    def unblock(self, card_id, blocker_id):
        self.unblocked.append(blocker_id)
        self.held = [b for b in self.held if b["id"] != blocker_id]


ASKED = []          # с чем позвали `gh`


def fake_gh(*replies):
    """
    Подменяет run_bounded: на каждый вызов отдаёт следующий заготовленный ответ.

    Ответ — то, что напечатал бы `gh --json`: словарь для `pr view`, список для
    `pr list`. None означает «команда упала».
    """
    queue = list(replies)

    def run(cmd, cwd, timeout, env=None):
        ASKED.append(cmd)
        reply = queue.pop(0) if queue else None
        if reply is None:
            return subprocess.CompletedProcess(cmd, 1, "", "gh: боль")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(reply), "")
    f.run_bounded = run


GITTED = []         # с чем позвали `git`


def fake_git(log_reply=""):
    """
    Подменяет git: `log` отдаёт заготовленную строку, `fetch` молчит.

    Строка — то, что напечатал бы `git log --format=%H%x09%s -1`; пустая означает
    «коммита карточки в базовой ветке нет».
    """
    def run(cwd, *args, check=True):
        GITTED.append(list(args))
        return log_reply if args and args[0] == "log" else ""
    f.git = run


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    assert ok, name


profiles = [f.make_profile("работа", {"board_id": BOARD, "columns": COLUMNS})]
# статус пишем в свой каталог, а не в state настоящей фабрики
f.STATE = Path(tempfile.mkdtemp())
f.STATUS_FILE = f.STATE / "status.json"
fake_git()   # по умолчанию коммита карточки в базовой ветке нет

print("=== 1. смерженный PR закрывает карточку ===")
ASKED.clear(); fake_gh(MERGED)
kaiten = FakeKaiten()
f.close_merged(kaiten, CFG, Args(), profiles)
check("уехала в «Готово»", kaiten.moves == [(CARD, COLUMNS["done"])], str(kaiten.moves))
check("gh спросили по ссылке из карточки",
      ASKED and ASKED[0][:4] == ["gh", "pr", "view", PR], str(ASKED))
check("gh позвали один раз", len(ASKED) == 1, str(ASKED))
check("отчёт написан", len(kaiten.written) == 1, str(kaiten.written))
text = kaiten.written[0]
check("это агентский комментарий", text.startswith(f.AGENT_MARK), text)
check("сказано, что смержен", "PR смержен" in text, text)
check("видно ссылку на PR", PR in text, text)
check("видно, кто смержил", "aboev" in text, text)
check("и когда — по-человечески", "10 сентября" in text, text)

print("=== 2. открытый PR карточку не двигает ===")
ASKED.clear(); fake_gh(OPEN)
kaiten = FakeKaiten()
f.close_merged(kaiten, CFG, Args(), profiles)
check("на месте", kaiten.moves == [], str(kaiten.moves))
check("и молчим", kaiten.written == [], str(kaiten.written))

print("=== 3. о том же мерже второй раз не отчитываемся ===")
ASKED.clear(); fake_gh(MERGED)
kaiten = FakeKaiten(comments=[{"text": REPORT},
                              {"text": f"🤖 **PR смержен — {f.DONE_LINE}.**"
                                       f"\n\nPR: {PR}"}])
f.close_merged(kaiten, CFG, Args(), profiles)
check("повторного комментария нет", kaiten.written == [], str(kaiten.written))
check("и повторного движения тоже", kaiten.moves == [], str(kaiten.moves))
check("gh даже не спрашивали", ASKED == [], str(ASKED))

print("=== 4. свой блокер снимаем, чужой уважаем ===")
ASKED.clear(); fake_gh(MERGED)
ours = {"id": 1, "reason": f"{f.BLOCK_MARK} {f.BLOCK_HUMAN_REVIEW}"}
kaiten = FakeKaiten(blockers=[dict(ours)])
f.close_merged(kaiten, CFG, Args(), profiles)
check("свой блокер снят", kaiten.unblocked == [1], str(kaiten.unblocked))
check("карточка уехала", kaiten.moves == [(CARD, COLUMNS["done"])], str(kaiten.moves))

ASKED.clear(); fake_gh(MERGED)
kaiten = FakeKaiten(blockers=[{"id": 2, "reason": "жду ответа от бека"}])
f.close_merged(kaiten, CFG, Args(), profiles)
check("под чужим блокером не двигаем", kaiten.moves == [], str(kaiten.moves))
check("и чужой блокер не снимаем", kaiten.unblocked == [], str(kaiten.unblocked))
check("gh не тревожим", ASKED == [], str(ASKED))

print("=== 5. «Клод, не трогай» сильнее мержа ===")
ASKED.clear(); fake_gh(MERGED)
kaiten = FakeKaiten(comments=[{"text": REPORT}, {"text": "клод не трогай, я сам закрою"}])
f.close_merged(kaiten, CFG, Args(), profiles)
check("не тронули", kaiten.moves == [] and kaiten.written == [], str(kaiten.moves))

print("=== 6. ссылки в карточке нет — ищем по имени ветки ===")
ASKED.clear(); fake_gh([MERGED])
kaiten = FakeKaiten(comments=[{"text": "🤖 **Взял в работу.**"}])
f.close_merged(kaiten, CFG, Args(), profiles)
check("искали по ветке карточки",
      ASKED and ASKED[0][:5] == ["gh", "pr", "list", "--search", "head:ai/card-555"],
      str(ASKED))
check("нашли и закрыли", kaiten.moves == [(CARD, COLUMNS["done"])], str(kaiten.moves))

print("=== 7. чужая ветка с похожим номером не считается ===")
ASKED.clear()
fake_gh([{**MERGED, "headRefName": "ai/card-5551-drugaya-zadacha"}])
kaiten = FakeKaiten(comments=[{"text": "🤖 **Взял в работу.**"}])
f.close_merged(kaiten, CFG, Args(), profiles)
check("карточку не тронули", kaiten.moves == [], str(kaiten.moves))

print("=== 8. gh упал — карточка остаётся как была ===")
ASKED.clear(); fake_gh(None, None)
kaiten = FakeKaiten()
f.close_merged(kaiten, CFG, Args(), profiles)
check("ничего не двигали", kaiten.moves == [], str(kaiten.moves))
check("после ссылки попробовали ветку", len(ASKED) == 2, str(ASKED))

print("=== 9. сухой прогон в Kaiten не пишет ===")
ASKED.clear(); fake_gh(MERGED)


class DryArgs:
    dry_run = True
    prompt_only = True


class DryKaiten(FakeKaiten):
    """Настоящий Kaiten в сухом прогоне глотает записи внутри _write — повторяем это."""
    def comment(self, card_id, text): pass
    def move(self, card_id, column_id): pass
    def unblock(self, card_id, blocker_id): pass


dry = DryKaiten(blockers=[dict(ours)])
f.close_merged(dry, CFG, DryArgs(), profiles)
check("записей нет", dry.moves == [] and dry.written == [], str(dry.moves))
check("но состояние PR всё равно проверили", len(ASKED) == 1, str(ASKED))

print("=== 10. без колонки «Готово» фаза ничего не делает ===")
ASKED.clear(); fake_gh(MERGED)
no_done = [f.make_profile("сабтаски", {
    "board_id": BOARD,
    "columns": {**{k: v for k, v in COLUMNS.items() if k != "done"}, "done": 0}})]
kaiten = FakeKaiten()
f.close_merged(kaiten, CFG, Args(), no_done)
check("карточку не трогаем", kaiten.moves == [], str(kaiten.moves))
check("и gh не зовём", ASKED == [], str(ASKED))

print("=== 11. на доске команды чужие карточки не закрываем ===")
ASKED.clear(); fake_gh(MERGED)
team = [f.make_profile("сабтаски", {"board_id": BOARD, "columns": COLUMNS},
                       own_only=True)]
kaiten = FakeKaiten()   # в описании нет «Из эпика: #…» — значит карточка не наша
f.close_merged(kaiten, CFG, Args(), team)
check("чужую не тронули", kaiten.moves == [], str(kaiten.moves))
check("gh не зовём", ASKED == [], str(ASKED))

ASKED.clear(); fake_gh(MERGED)
kaiten = FakeKaiten(description="Из эпика: #4242")
f.close_merged(kaiten, CFG, Args(), team)
check("свою — закрыли", kaiten.moves == [(CARD, COLUMNS["done"])], str(kaiten.moves))

print("=== 12. роли делят колонку — обходим её один раз ===")
ASKED.clear(); fake_gh(MERGED)
shared = dict(COLUMNS); shared["agent_review"] = COLUMNS["review"]
kaiten = FakeKaiten()
f.close_merged(kaiten, CFG, Args(),
               [f.make_profile("сабтаски", {"board_id": BOARD, "columns": shared})])
check("карточку закрыли один раз", kaiten.moves == [(CARD, COLUMNS["done"])],
      str(kaiten.moves))
check("и отчитались один раз", len(kaiten.written) == 1, str(kaiten.written))

print("=== 13. PR закрыт без мержа, но правка уехала в main ===")
# так вышло вживую: правку дринкита сквошнули в PR по Алматы, а свой PR закрыли
ASKED.clear(); GITTED.clear(); fake_gh(CLOSED)
fake_git("3b84ce18b51c47\t#69826636 Включить обязательную авторизацию (#4769)")
kaiten = FakeKaiten()
f.close_merged(kaiten, CFG, Args(), profiles)
check("уехала в «Готово»", kaiten.moves == [(CARD, COLUMNS["done"])], str(kaiten.moves))
check("базовую ветку подтянули", ["fetch", "origin", "--prune"] in GITTED, str(GITTED))
asked_log = next((a for a in GITTED if a and a[0] == "log"), [])
check("искали по номеру карточки в origin/main",
      asked_log[:2] == ["log", "origin/main"]
      and f"--grep=#{CARD}([^0-9]|$)" in asked_log, str(asked_log))
text = kaiten.written[0]
check("сказано, что работа в main", "Работа уехала в main" in text, text)
check("и что свой PR закрыт", "закрыт без мержа" in text and PR in text, text)
check("виден коммит", "3b84ce18b5" in text, text)
check("и в какой PR её забрали", "#4769" in text, text)

print("=== 14. PR закрыт и в main ничего — карточка ждёт человека ===")
ASKED.clear(); GITTED.clear(); fake_gh(CLOSED); fake_git("")
kaiten = FakeKaiten()
f.close_merged(kaiten, CFG, Args(), profiles)
check("не двигаем", kaiten.moves == [], str(kaiten.moves))
check("и молчим", kaiten.written == [], str(kaiten.written))
check("но в main всё же посмотрели",
      any(a and a[0] == "log" for a in GITTED), str(GITTED))

print("=== 15. открытый PR сильнее коммита в main ===")
# работа в полёте: круг правок мог уже уехать в main частями, закрывать рано
ASKED.clear(); GITTED.clear(); fake_gh(OPEN)
fake_git("3b84ce18b51c47\t#69826636 что-то там")
kaiten = FakeKaiten()
f.close_merged(kaiten, CFG, Args(), profiles)
check("не двигаем", kaiten.moves == [], str(kaiten.moves))
check("в main даже не смотрим", GITTED == [], str(GITTED))

print("=== 16. второй раз про main тоже не отчитываемся ===")
ASKED.clear(); GITTED.clear(); fake_gh(CLOSED)
fake_git("3b84ce18b51c47\t#69826636 что-то там")
kaiten = FakeKaiten(comments=[{"text": REPORT},
                              {"text": f"🤖 **Работа уехала в main — {f.DONE_LINE}.**"}])
f.close_merged(kaiten, CFG, Args(), profiles)
check("повторов нет", kaiten.written == [] and kaiten.moves == [], str(kaiten.written))
check("и наружу не ходим", ASKED == [] and GITTED == [], str(ASKED + GITTED))

print("=== 17. один fetch на репозиторий, сколько бы карточек ни было ===")
ASKED.clear(); GITTED.clear(); fake_gh(CLOSED, CLOSED); fake_git("")


class Two(FakeKaiten):
    """Две карточки в одной колонке — обе с закрытым PR."""
    def cards_in_column(self, board_id, column_id):
        if column_id != self.column:
            return []
        return [{"id": CARD, "title": "Раз", "board_id": BOARD,
                 "column_id": column_id, "description": ""},
                {"id": CARD + 1, "title": "Два", "board_id": BOARD,
                 "column_id": column_id, "description": ""}]


f.close_merged(Two(), CFG, Args(), profiles)
fetches = [a for a in GITTED if a and a[0] == "fetch"]
check("fetch ровно один", len(fetches) == 1, str(GITTED))
check("а в main смотрели по каждой",
      len([a for a in GITTED if a and a[0] == "log"]) == 2, str(GITTED))

print("\nвсё сошлось")
