"""
Техдолг спринта: задачи спринта без эпика уходят дочерними в «Техдолг спринта <даты>».

Цена ошибки здесь — чужие карточки. Спринт-доска командная, эпик техдолга ведут
люди, и лишняя привязка (задача, у которой уже есть продуктовый эпик, или закрытая
в прошлом спринте) видна всей команде в планировании. Kaiten — заглушка.
Запуск: `python3 tests/test_tech_debt.py`.
"""
import importlib.util
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "factory", Path(__file__).resolve().parent.parent / "factory.py")
f = importlib.util.module_from_spec(spec); spec.loader.exec_module(f)

# id выдуманные: репозиторий публичный, настоящие ловит `python3 setup.py --audit`
SPRINT, EPICS, INBOX, LANE, DELIVERY = 9100, 9000, 9500, 71, 9003
PRODUCT_EPIC, INBOX_CARD = 8001, 8002

DEBT = {"board_id": EPICS, "lane_id": LANE, "column_id": DELIVERY, "card_type_id": 77,
        "title_prefix": "Техдолг спринта ", "sprint_anchor": "2026-10-05", "sprint_days": 14}


def columns(base):
    return {"queue": base + 1, "in_progress": base + 2, "agent_review": base + 3}


CFG = {
    "products": {
        "shop": {"title": "Магазин", "board_id": SPRINT, "columns": columns(SPRINT),
                 "sprint_tech_debt": DEBT},
        "old": {"title": "Старая", "retired": True, "board_id": 9400,
                "columns": columns(9400), "sprint_tech_debt": DEBT},
        "site": {"title": "Сайт", "board_id": 9200, "columns": columns(9200)},
    },
    "epic_flow": {"boards": [EPICS]},
}


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    assert ok, name


start, end = f.sprint_bounds(date.fromisoformat(DEBT["sprint_anchor"]), DEBT["sprint_days"])
LABEL = f.short_dates(start, end)
OLD_LABEL = f.short_dates(start - timedelta(days=14), end - timedelta(days=14))


class FakeKaiten:
    def __init__(self, sprint_cards, lane_cards, comments=None):
        self.sprint_cards, self.lane_cards = sprint_cards, lane_cards
        self.comments_ = comments or {}
        self.created, self.children_added, self.reads = [], [], []
        self.full = {PRODUCT_EPIC: {"id": PRODUCT_EPIC, "board_id": EPICS},
                     INBOX_CARD: {"id": INBOX_CARD, "board_id": INBOX}}

    def cards_on_board(self, board_id, with_description=False, lane_id=None):
        if board_id == SPRINT:
            return self.sprint_cards
        assert board_id == EPICS and lane_id == LANE, (board_id, lane_id)
        return self.lane_cards

    def card(self, card_id):
        self.reads.append(card_id)
        if card_id not in self.full:
            raise f.NotFoundError(str(card_id))
        return self.full[card_id]

    def comments(self, card_id):
        return self.comments_.get(card_id, [])

    def custom_property(self, property_id):
        return {"id": property_id, "type": "user" if property_id in (3, 4) else "select"}

    def user_uids(self, user_ids):
        return {i: f"uid-{i}" for i in user_ids if i != 44}

    def create_card(self, body):
        self.created.append(body)
        return {"id": 8900, "title": body["title"]}

    def add_child(self, parent_id, child_id):
        self.children_added.append((parent_id, child_id))


def task(card_id, parents=None, state=2, done_at=None, title="Задача"):
    return {"id": card_id, "title": title, "description": "", "parents_ids": parents,
            "state": state, "last_moved_to_done_at": done_at}


LIVE = SimpleNamespace(dry_run=False, prompt_only=False)

print("=== 1. даты спринта ===")
check("с понедельника по пятницу второй недели",
      f.sprint_bounds(date(2026, 10, 5), 14, date(2026, 10, 14))
      == (date(2026, 10, 5), date(2026, 10, 16)))
check("в выходные хвоста — всё ещё тот же спринт",
      f.sprint_bounds(date(2026, 10, 5), 14, date(2026, 10, 18))[0] == date(2026, 10, 5))
check("с понедельника — следующий",
      f.sprint_bounds(date(2026, 10, 5), 14, date(2026, 10, 19))
      == (date(2026, 10, 19), date(2026, 10, 30)))
check("подпись как у команды", f.short_dates(date(2026, 10, 5), date(2026, 10, 16))
      == "05.10–16.10")

print("=== 2. кого вешаем ===")
debt_card = {"id": 8500, "title": f"Техдолг спринта {LABEL}", "lane_id": LANE}
k = FakeKaiten([
    task(1),                                   # без родителей
    task(2, [INBOX_CARD]),                     # только карточка инбокса — это не эпик
    task(3, [PRODUCT_EPIC]),                   # при продуктовом эпике
    task(4, [INBOX_CARD, PRODUCT_EPIC]),       # инбокс и эпик сразу
    task(5, state=3, done_at="2020-01-01T10:00:00Z"),   # закрыта до спринта
    task(6, state=3, done_at=f"{start.isoformat()}T10:00:00Z"),  # закрыта в спринте
    task(7, [404404]),                         # родитель удалён
    task(8, title="Стёпа не трогай: разберусь сам"),
], [debt_card])
linked = f.link_sprint_orphans(k, CFG, LIVE)
check("висят на техдолге ровно задачи без эпика",
      sorted(child for _, child in k.children_added) == [1, 2, 6, 7],
      str(k.children_added))
check("на найденный техдолг, новый не заводим",
      {parent for parent, _ in k.children_added} == {8500} and not k.created)
check("счёт привязанных", linked == 4, str(linked))
check("уходящую доску и продукт без секции не обходим",
      [key for key, _, _ in f.tech_debt_sections(CFG)] == ["shop"])
check("каждого родителя спрашиваем раз за прогон",
      k.reads.count(INBOX_CARD) == 1, str(k.reads))

print("=== 3. все при эпиках — ничего не трогаем ===")
k = FakeKaiten([task(3, [PRODUCT_EPIC])], [])
f.link_sprint_orphans(k, CFG, LIVE)
check("пустой техдолг не заводим", not k.created and not k.children_added)

print("=== 4. техдолга на этот спринт нет — заводим по образцу прошлого ===")
old = {"id": 8400, "title": f"Техдолг спринта {OLD_LABEL}", "lane_id": LANE,
       "created": "2026-01-01T00:00:00Z"}
other = {"id": 8401, "title": "Тех долг гильдии", "lane_id": LANE,
         "created": "2026-12-01T00:00:00Z"}
k = FakeKaiten([task(1)], [old, other])
k.full[8400] = {**old, "type_id": 77, "board_id": EPICS,
                "description": f"Работа спринта {OLD_LABEL} без эпика",
                "properties": {"id_1": [5], "id_2": f"Спринт {OLD_LABEL}",
                               "id_3": [{"id": 42, "full_name": "Человек"}], "id_4": [43, 44]}}
f.link_sprint_orphans(k, CFG, LIVE)
body = k.created[0] if k.created else {}
check("название с датами этого спринта", body.get("title") == f"Техдолг спринта {LABEL}",
      body.get("title", ""))
check("в дорожке и колонке из конфига",
      (body.get("board_id"), body.get("lane_id"), body.get("column_id"))
      == (EPICS, LANE, DELIVERY))
check("описание прошлого с новыми датами",
      body.get("description") == f"Работа спринта {LABEL} без эпика", body.get("description"))
check("поля прошлого: даты новые, люди uuid, ненайденных нет",
      body.get("properties") == {"id_1": [5], "id_2": f"Спринт {LABEL}",
                                 "id_3": ["uid-42"], "id_4": ["uid-43"]},
      str(body.get("properties")))
check("задача уехала в новый техдолг", k.children_added == [(8900, 1)], str(k.children_added))

print("=== 5. сухой прогон ничего не пишет ===")
k = FakeKaiten([task(1)], [old])
k.full[8400] = {**old, "properties": {}}
f.link_sprint_orphans(k, CFG, SimpleNamespace(dry_run=True, prompt_only=False))
check("ни карточки, ни привязки", not k.created and not k.children_added)

print("\nвсё зелёное")
