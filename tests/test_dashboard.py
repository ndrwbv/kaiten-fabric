"""
Проверка витрины без GitHub, без Kaiten и без единого запущенного прогона.

Числа на витрине — это выводы: «уехало в main», «не хватило данных», «не влез
в бюджет». Каждый такой вывод собирается из трёх-четырёх признаков сразу, и
ошибиться в нём легко, а заметить ошибку глазами почти нельзя: витрина выглядит
одинаково правдоподобно и с правильными числами, и с неправильными.

Запуск: `python3 tests/test_dashboard.py`.
"""
import importlib.util, re, subprocess, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# витрина импортирует factory — из tests/ его иначе не видно
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("dashboard", ROOT / "dashboard.py")
d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)

# Все id выдуманные: репозиторий публичный, настоящие ловит `python3 setup.py --audit`
CARD = 55512345
OTHER = 55599999


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    assert ok, name


def run_file(kind, card_id, stamp):
    return re.match(d.RUN_FILE_RE, f"{kind}-{card_id}-{stamp}.json")


print("=== 1. прогон исполнителя разбирается в строку витрины ===")
run = d.parse_run(run_file("card", CARD, "20260916-042044"), {
    "verdict": {"status": "done", "summary": "Сделал", "questions": [], "risks": "риск"},
    "meta": {"cost_usd": 9.7780419, "steps": 183, "subtype": "success"},
})
check("вид работы", run["kind"] == "card" and run["group"] == "card", run["group"])
check("карточка", run["card_id"] == CARD)
check("деньги", run["cost"] == 9.778, str(run["cost"]))
check("вердикт", run["status"] == "done")
check("время в UTC", run["at"].startswith("2026-09-16T04:20:44"), run["at"])

print("=== 2. у ревьювера вердикт лежит под своим ключом ===")
run = d.parse_run(run_file("review", CARD, "20260910-105423"), {
    "review": {"verdict": "needs_changes", "summary": "Вернул",
               "findings": [{"severity": "major"}, {"severity": "minor"}]},
    "meta": {"cost_usd": 1.41, "subtype": "success"},
})
check("вердикт ревью", run["status"] == "needs_changes", str(run["status"]))
check("замечания посчитаны", run["major"] == 1 and run["minor"] == 1)

print("=== 3. обрыв по бюджету — это не вердикт агента ===")
run = d.parse_run(run_file("card", CARD, "20260901-155440"), {
    "verdict": {"status": None, "summary": ""},
    "meta": {"cost_usd": 5.05, "subtype": "error_max_budget_usd"},
})
check("статуса нет", run["status"] is None)
check("зато сказано, что оборвали", run["cut"] == "упёрся в потолок расхода", run["cut"])

print("=== 4. фазы эпика считаются одной группой ===")
check("спека", d.kind_group("epic-spec") == "epic")
check("ревью спеки", d.kind_group("epic-spec-review") == "epic")
check("а ревью кода — нет", d.kind_group("review") == "review")

print("=== 5. исход: работа в main сильнее закрытого PR ===")
# так и бывает: свой PR закрыли, а правку забрали в сборный релиз
card = {"pr": {"state": "CLOSED"}, "shipped": {"sha": "3b84ce18"}, "column": None,
        "status": "done", "cut": "", "last_outcome": ""}
check("уехало в main", d.card_outcome(card) == "merged", d.card_outcome(card))

print("=== 6. исход: открытый PR значит, что ход человека ===")
card = {"pr": {"state": "OPEN"}, "shipped": None, "column": "review",
        "status": "done", "cut": "", "last_outcome": ""}
check("ждёт человека", d.card_outcome(card) == "waiting", d.card_outcome(card))
card["column"] = "fixes"
check("а из «Правок» — на правках", d.card_outcome(card) == "fixes", d.card_outcome(card))

print("=== 7. исход: до PR не дошло ===")
base = {"pr": None, "shipped": None, "column": None, "cut": "", "last_outcome": ""}
check("вопрос агента", d.card_outcome({**base, "status": "unclear"}) == "question")
check("преграда", d.card_outcome({**base, "status": "blocked"}) == "question")
check("потолок расхода",
      d.card_outcome({**base, "status": None, "cut": "упёрся в потолок расхода"}) == "budget")
check("поломка обёртки",
      d.card_outcome({**base, "status": "done", "last_outcome": "упало: git push"}) == "broken")
check("закрытый PR без коммита",
      d.card_outcome({**base, "status": "done", "pr": {"state": "CLOSED"}}) == "closed")
check("и просто ничего", d.card_outcome({**base, "status": "done"}) == "none")

print("=== 8. «Готово» на доске — тоже сделано ===")
check("даже без PR",
      d.card_outcome({**base, "status": None, "column": "done"}) == "merged")

print("=== 9. чего не хватило: сначала деньги, потом поломка, потом вопросы ===")
text = d.missing({"outcome": "budget", "cap": 12.0, "last_cost": 5.05,
                  "questions": ["а на каком экране?"], "risks": "", "summary": ""})
check("назван потолок", "5.05" in text[0] and "12" in text[0], str(text))
check("вопрос тоже остался", text[1] == "а на каком экране?", str(text))
text = d.missing({"outcome": "broken", "detail": "gh pr create -> 1",
                  "questions": [], "risks": "", "summary": ""})
check("поломка объяснена", text == ["gh pr create -> 1"], str(text))
text = d.missing({"outcome": "question", "questions": [], "risks": "",
                  "summary": "Ничего не поменял"})
check("на худой конец — выжимка", text == ["Ничего не поменял"], str(text))

print("=== 10. в main смотрим только на известные карточки ===")
# в сообщении сборного релиза номеров много, и почти все они — чужие
commits = f"aa11bb22\x1f1757000000\x1f#{CARD} правка\x1fв составе #{OTHER} и #12345\x1e"
calls = []


def fake_git_log(cmd, cwd=None, capture_output=False, text=False, timeout=None):
    calls.append(cmd)
    return subprocess.CompletedProcess(cmd, 0, commits, "")


real_run, d.subprocess.run = d.subprocess.run, fake_git_log
cfg = {"repos": {"kiosk": {"path": str(ROOT), "remote": "origin", "base_branch": "main"}}}
found = d.shipped_to_main(cfg, {CARD})
check("свою карточку нашли", CARD in found, str(found))
check("чужие номера не приписали", OTHER not in found and 12345 not in found, str(found))
check("без списка карточек и не ходим", d.shipped_to_main(cfg, set()) == {} and len(calls) == 1)
d.subprocess.run = real_run

print("=== 11. в ряду по дням нет дыр ===")
today = datetime.now()
runs = [{"day": (today - timedelta(days=n)).strftime("%Y-%m-%d"), "cost": 1.0,
         "group": "card", "cut": "", "status": "done"} for n in (0, 4)]
trend = d.daily(runs, [], 5)
check("ровно окно", len(trend) == 5, str(len(trend)))
check("дни подряд", [p["day"] for p in trend] ==
      [(today - timedelta(days=n)).strftime("%Y-%m-%d") for n in (4, 3, 2, 1, 0)],
      str([p["day"] for p in trend]))
check("в пустых днях нули", trend[1]["cost"] == 0 and trend[1]["runs"] == 0)
check("в рабочих — деньги", trend[0]["cost"] == 1.0 and trend[-1]["cost"] == 1.0)

print("=== 12. неудачный прогон виден в ряду ===")
runs = [{"day": today.strftime("%Y-%m-%d"), "cost": 2.0, "group": "card",
         "cut": "упёрся в потолок расхода", "status": None}]
check("посчитан", d.daily(runs, [], 1)[-1]["failed"] == 1)

print("=== 13. кнопка зовёт run.sh с теми же флагами, что и меню-бар ===")
started = []


class FakePopen:
    def __init__(self, cmd, **kwargs):
        started.append(cmd)


real_popen, d.subprocess.Popen = d.subprocess.Popen, FakePopen
real_lock, d.run_lock = d.run_lock, lambda: {"held": False}

d.start_run("board")
check("доска — без разведки и эпиков",
      started[-1][-1].endswith("./run.sh --no-triage --no-epics"), started[-1][-1])
d.start_run("inbox")
check("инбокс", started[-1][-1].endswith("--only-triage"), started[-1][-1])
d.start_run("full")
check("полный прогон — без флагов", started[-1][-1].endswith("./run.sh "), started[-1][-1])
d.start_run("card", card=CARD)
check("по карточке", started[-1][-1].endswith(f"--card {CARD}"), started[-1][-1])
d.start_run("card", card=CARD, epic=True)
check("по эпику", started[-1][-1].endswith(f"--epic-card {CARD}"), started[-1][-1])
d.start_run("board", dry=True)
check("сухой прогон", started[-1][-1].endswith("--dry-run"), started[-1][-1])
check("через логин-шелл", started[-1][:2] == ["/bin/zsh", "-ilc"], str(started[-1][:2]))
answer = d.start_run("вздор")
check("чужой режим не запускается", not answer["ok"] and len(started) == 6, answer["text"])

print("=== 14. пока прогон идёт, второй не стартует ===")
d.run_lock = lambda: {"held": True, "alive": True, "pid": 4242, "age_min": 7.0}
answer = d.start_run("board")
check("отказ", not answer["ok"] and "уже идёт" in answer["text"], answer["text"])
check("и ничего не запущено", len(started) == 6, str(len(started)))
d.subprocess.Popen, d.run_lock = real_popen, real_lock

print("=== 15. хвост лога читается с конца ===")
log = Path(tempfile.mkdtemp()) / "run.log"
log.write_text("\n".join(f"строка {n}" for n in range(1, 501)), encoding="utf-8")
lines = d.tail(log, 3)
check("последние три", lines == ["строка 498", "строка 499", "строка 500"], str(lines))
check("файла нет — и ладно", d.tail(log.parent / "нет.log", 5) == [])

print("\nвсё сошлось")
