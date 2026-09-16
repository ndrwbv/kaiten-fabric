"""
Проверка того, как разработчик отличает свою работу от чужой.

Признак один — тег, и цена ошибки в нём несимметрична: не узнал своё — работа
встала, узнал чужое — фабрика полезла в живой бэклог команды. Ни то ни другое
не видно из логов сразу, поэтому проверяем здесь.

Запуск: `python3 tests/test_tags.py`.
"""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "factory", Path(__file__).resolve().parent.parent / "factory.py")
f = importlib.util.module_from_spec(spec); spec.loader.exec_module(f)

# Все id выдуманные: репозиторий публичный, настоящие ловит `python3 setup.py --audit`
OWN_BOARD = 1000
SUB_BOARD = 2000
COLUMNS = {"queue": 101, "in_progress": 102, "question": 103, "failed": 104,
           "agent_review": 105, "fixes": 106, "review": 107, "done": 108}
SUB_COLUMNS = {"queue": 201, "in_progress": 202, "agent_review": 203, "done": 204}

CFG = {
    "kaiten": {"board_id": OWN_BOARD, "columns": COLUMNS},
    "inbox": {"board_id": 3000, "column_id": 301},
    "epic_flow": {"boards": [4000], "development_column_id": 401,
                  "subtasks": {"board_id": SUB_BOARD, "columns": SUB_COLUMNS}},
}


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    assert ok, name


def tagged(*names):
    return {"id": 1, "description": "", "tags": [{"name": n, "tag_id": 7} for n in names]}


print("=== 1. имя и тег берутся из конфига ===")
check("по умолчанию — Стёпа", f.bot_name({}) == "Stepa Tugarev" and f.bot_tag({}) == "stepa")
check("но переименовать можно",
      f.bot_tag({"bot": {"tag": "vasya"}}) == "vasya"
      and f.bot_name({"bot": {"name": "Vasya"}}) == "Vasya")

print("=== 2. ночной тег растёт из имени бота ===")
check("по умолчанию", f.night_config({})[0] == "stepa:night", f.night_config({})[0])
check("переименовали бота — переехал и ночной тег",
      f.night_config({"bot": {"tag": "vasya"}})[0] == "vasya:night")
check("явно заданный в конфиге сильнее",
      f.night_config({"night": {"tag": "ночью"}})[0] == "ночью")

print("=== 3. у эпиков тот же тег, второго имени не бывает ===")
check("тег эпика = тег бота", f.epic_flow(CFG)["tag"] == "stepa")
check("но заданный явно сильнее",
      f.epic_flow({**CFG, "epic_flow": {**CFG["epic_flow"], "tag": "epic"}})["tag"] == "epic")

print("=== 4. «это моё» — по тегу ===")
profile = f.make_profile("сабтаски", {"board_id": SUB_BOARD, "columns": SUB_COLUMNS},
                         own_only=True, tag="stepa")
check("с тегом — моё", f.mine(profile, tagged("stepa")))
check("с чужим тегом — не моё", not f.mine(profile, tagged("багфикс")))
check("без тегов — не моё", not f.mine(profile, {"id": 1, "description": ""}))
check("ночной тег сам по себе владения не даёт", not f.mine(profile, tagged("stepa:night")))
check("но старая сабтаска из эпика — всё ещё моя",
      f.mine(profile, {"id": 1, "description": "Из эпика: #44"}))

print("=== 5. на своей доске чужих нет, на общей — почти все ===")
own = f.make_profile("работа", {"board_id": OWN_BOARD, "columns": COLUMNS}, tag="stepa")
check("на своей берём всё", not f.theirs(own, {"id": 1, "description": ""}))
check("на общей — только помеченное", f.theirs(profile, {"id": 1, "description": ""}))
check("а помеченное берём", not f.theirs(profile, tagged("stepa")))

print("=== 6. разведка ставит задачу туда, где работает команда ===")
check("по умолчанию — доска сабтасок",
      f.handoff_profile(CFG)["board_id"] == SUB_BOARD, str(f.handoff_profile(CFG)))
check("и с тегом", f.handoff_profile(CFG)["tag"] == "stepa")
own_target = {**CFG, "inbox": {**CFG["inbox"], "target": "own"}}
check("но можно вернуть на свою", f.handoff_profile(own_target)["board_id"] == OWN_BOARD)
no_epics = {k: v for k, v in CFG.items() if k != "epic_flow"}
check("без режима эпиков сабтасок нет — ставим на свою",
      f.handoff_profile(no_epics)["board_id"] == OWN_BOARD)

print("=== 7. к долгу спринта привязываем то, у чего нет своего родителя ===")
check("на своей доске — всё",
      f.wants_debt({"attach_to_debt": True}, {"description": "что угодно"}))
sub = {"attach_to_debt": False}
check("задачу из инбокса — да", f.wants_debt(sub, {"description": "Из инбокса: #5"}))
check("сабтаску эпика — нет, она уже на эпике",
      not f.wants_debt(sub, {"description": "Из эпика: #44"}))
check("чужую карточку с тегом — нет, это не наша карточка",
      not f.wants_debt(sub, {"description": "Поправить текст"}))

print("=== 8. задача из инбокса: тег, инбокс-родитель и след для долга ===")

INBOX_CARD = 3001


class FakeKaiten:
    """Kaiten, который только записывает, о чём его попросили."""

    def __init__(self):
        self.created, self.tags, self.children_of, self.written = [], [], [], []
        self.next_id = 5001

    def cards_on_board(self, board_id, with_description=False):
        return []                       # такой задачи ещё нет

    def create_card(self, body):
        card = {"id": self.next_id, **body}
        self.next_id += 1
        self.created.append(body)
        return card

    def add_tag(self, card_id, name):
        self.tags.append((card_id, name))

    def children(self, card_id):
        return []

    def add_child(self, parent_id, child_id):
        self.children_of.append((parent_id, child_id))

    def comment(self, card_id, text):
        self.written.append(text)

    def card_url(self, card):
        return f"https://kaiten.example/card/{card['id']}"


kaiten = FakeKaiten()
inbox_card = {"id": INBOX_CARD, "title": "Кнопка не нажимается", "description": "жмёшь — тишина"}
note = f.hand_off_to_factory(kaiten, CFG, inbox_card, [],
                             {"problem": "не работает кнопка", "plan": ["починить"]},
                             "https://kaiten.example/card/3001", dry_run=False)
created = kaiten.created[0]
check("завёл на доске сабтасок", created["board_id"] == SUB_BOARD, str(created["board_id"]))
check("в «Очередь»", created["column_id"] == SUB_COLUMNS["queue"], str(created["column_id"]))
check("повесил свой тег", kaiten.tags == [(5001, "stepa")], str(kaiten.tags))
check("привязал дочерней к карточке инбокса",
      kaiten.children_of == [(INBOX_CARD, 5001)], str(kaiten.children_of))
check("в описании — откуда задача выросла",
      f.INBOX_ORIGIN_RE.search(created["description"]) is not None)
check("а значит, к долгу спринта её привяжет и доска сабтасок",
      f.wants_debt({"attach_to_debt": False}, created))
check("и в инбокс отчитался ссылкой", "5001" in note, note)

print("=== 9. не встал тег — говорит об этом, а не молчит ===")


class NoTags(FakeKaiten):
    def add_tag(self, card_id, name):
        raise RuntimeError("Kaiten не в духе")


kaiten = NoTags()
f.hand_off_to_factory(kaiten, CFG, inbox_card, [], {"problem": "", "plan": []},
                      "https://kaiten.example/card/3001", dry_run=False)
check("предупредил прямо в карточке",
      any("тег" in text.lower() and "руками" in text for text in kaiten.written),
      str(kaiten.written))

print("=== 10. стоп-фраза знает разработчика по имени ===")
for phrase in ("Стёпа не трогай эту карточку", "СТЕПА НЕ ТРОГАЙ", "не трогай стёпу"):
    check(f"«{phrase[:22]}»", bool(f.hands_off({"title": "Тест", "description": phrase},
                                               [], {})))
check("а обычный текст не выключает",
      not f.hands_off({"title": "Стёпа, посмотри плз", "description": ""}, [], {}))

print("\nвсё сошлось")
