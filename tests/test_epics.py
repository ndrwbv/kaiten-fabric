"""
Границы, за которые фабрика не выходит: окно колонок у эпика и правило
«карточку, которую увёл человек, назад не тащим».

Kaiten здесь заглушка: доска с колонками как у настоящей — Backlog, Ready for
Development, Development, Design review, Rollout, — и эпик, который по ней ездит.
Нужен потому, что цена ошибки высокая: один возврат эпика из «Rollout» обратно
в разработку стоил $6.79 — декомпозиция по второму кругу плюс прогон дубля.
Запуск: `python3 tests/test_epics.py`.
"""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "factory", Path(__file__).resolve().parent.parent / "factory.py")
f = importlib.util.module_from_spec(spec); spec.loader.exec_module(f)

# id выдуманные: репозиторий публичный, настоящие ловит `python3 setup.py --audit`
BACKLOG, READY, DEV, REVIEW, ROLLOUT = 9001, 9002, 9003, 9004, 9005
BOARD = 9000

FLOW = {"tag": "claude:epic", "boards": [BOARD], "development_column_id": DEV,
        "review_column_id": None, "subtasks": {"board_id": 9100}}


class FakeKaiten:
    """Доска эпиков: подколонки внутри «Delivery», как в настоящем Kaiten."""
    def __init__(self, cards=None):
        self.moves, self.boards_read, self.tags_removed = [], 0, []
        self.cards_ = cards or []

    def board(self, board_id):
        self.boards_read += 1
        return {"id": board_id, "columns": [
            {"id": BACKLOG, "title": "Backlog", "sort_order": 1},
            {"id": 9010, "title": "Delivery", "sort_order": 2, "subcolumns": [
                {"id": READY, "title": "Ready for Development", "sort_order": 1},
                {"id": DEV, "title": "Development", "sort_order": 2},
                {"id": REVIEW, "title": "Design review", "sort_order": 3},
            ]},
            {"id": ROLLOUT, "title": "Rollout", "sort_order": 3},
        ]}

    def cards_on_board(self, board_id, with_description=False):
        return self.cards_

    def comments(self, card_id):
        return []

    def move(self, card_id, column):
        self.moves.append((card_id, column))

    def remove_tag(self, card_id, tag):
        self.tags_removed.append((card_id, tag))


TAG_ID = 1142106


def epic(column, card_id=1, tagged=True):
    return {"id": card_id, "title": "Прогноз приготовления заказа", "board_id": BOARD,
            "column_id": column, "description": "",
            "tags": [{"id": TAG_ID, "tag_id": TAG_ID, "name": "claude:epic"}]
                    if tagged else []}


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    assert ok, name


print("=== 1. окно работы: «готов к разработке» и «в разработке» ===")
k = FakeKaiten()
check("вычисляется как соседняя слева от разработки",
      f.epic_window(k, FLOW, BOARD) == (READY, DEV), str(f.epic_window(k, FLOW, BOARD)))
check("явная настройка сильнее вычисления",
      f.epic_window(k, {**FLOW, "ready_column_id": BACKLOG}, BOARD) == (BACKLOG, DEV))
check("без колонки разработки окна нет",
      f.epic_window(k, {**FLOW, "development_column_id": 0}, BOARD) == (0, 0))
check("колонки разработки нет на доске — тоже нет",
      f.epic_window(k, {**FLOW, "development_column_id": 12345}, BOARD) == (0, 0))

print("=== 2. эпик внутри окна — наш, снаружи — нет ===")
for column, name, expected in ((READY, "«готов к разработке»", True),
                               (DEV, "«в разработке»", True),
                               (BACKLOG, "бэклог", False),
                               (REVIEW, "ревью", False),
                               (ROLLOUT, "rollout", False)):
    check(f"{name} — {'берём' if expected else 'не берём'}",
          f.in_epic_window(k, FLOW, epic(column)) is expected)

print("=== 3. выборка тегом больше не тащит уехавшие эпики ===")
picked = FakeKaiten([epic(READY, 1), epic(DEV, 2), epic(ROLLOUT, 3), epic(REVIEW, 4),
                     epic(DEV, 5, tagged=False)])
found = f.pick_epics(picked, {}, FLOW)
check("взяты только те, что в окне", [c["id"] for c in found] == [1, 2],
      str([c["id"] for c in found]))
check("карточка без тега не наша, даже в колонке разработки",
      5 not in [c["id"] for c in found])

print("=== 4. take_epic двигает только вперёд ===")
forward = FakeKaiten()
f.take_epic(forward, FLOW, epic(READY))
check("из «готов к разработке» в разработку — двигает",
      forward.moves == [(1, DEV)], str(forward.moves))

back = FakeKaiten()
f.take_epic(back, FLOW, epic(ROLLOUT))
check("из «Rollout» назад — не двигает", back.moves == [], str(back.moves))

same = FakeKaiten()
f.take_epic(same, FLOW, epic(DEV))
check("уже в разработке — ничего не делает", same.moves == [])
check("и за доской в Kaiten не ходит", same.boards_read == 0, str(same.boards_read))

print("=== 5. нечитаемая сабтаска — «не знаю», а не «её нет» ===")


class Children(FakeKaiten):
    """Дети есть, но описание одного из них Kaiten отдать не смог."""
    def __init__(self, broken):
        super().__init__()
        self.broken = broken

    def card(self, card_id):
        if card_id == self.broken:
            raise f.FactoryError("'utf-8' codec can't decode byte 0xd0")
        return {"id": card_id, "description": "Из эпика: #1"}


kids = [{"id": 11, "description": "", "description_filled": True},
        {"id": 12, "description": "", "description_filled": True}]
own, unreadable = f.epic_subtasks(Children(broken=12), 1, kids)
check("прочитанная сабтаска найдена", [c["id"] for c in own] == [11], str(own))
check("нечитаемая посчитана отдельно", unreadable == 1, str(unreadable))
own, unreadable = f.epic_subtasks(Children(broken=0), 1, kids)
check("когда всё читается — нечитаемых ноль", unreadable == 0 and len(own) == 2)

print("=== 6. карточка вне колонок фабрики — не наша ===")
# доска «сабтаски» как в жизни: у «Тестинга» и «Ролаута» роли нет, туда карточку
# уводит человек, и вытаскивать её оттуда назад в работу нельзя никогда
work = f.make_profile("сабтаски", {"board_id": 9100, "columns": {
    "queue": 1, "in_progress": 2, "question": 2, "failed": 2,
    "agent_review": 3, "fixes": 2, "review": 3, "done": 6}})
for column, name, expected in ((1, "«Очередь»", False), (2, "«В работе»", False),
                               (3, "«Ревью»", False), (6, "«Готово»", False),
                               (4, "«Тестинг»", True), (5, "«Ролаут»", True)):
    check(f"{name} — {'вне потока' if expected else 'в потоке'}",
          f.outside_flow(work, {"column_id": column}) is expected)
check("без профиля правило молчит, а не запрещает всё",
      f.outside_flow(None, {"column_id": 4}) is False)

print("=== 7. движение внутри потока назад остаётся разрешённым ===")
# «Правки» и «Вопрос» стоят левее «Ревью агента», и круг правок обязан работать:
# запрет «никогда не влево» сломал бы фабрику, а не починил
check("«Правки» → «В работе» — свои колонки",
      not f.outside_flow(work, {"column_id": work["columns"]["fixes"]}))
check("«Вопрос» → «В работе» — тоже свои",
      not f.outside_flow(work, {"column_id": work["columns"]["question"]}))

print("=== 8. закрытый эпик перестаёт быть фабричным ===")
check("id тега находится по имени", f.tag_id(epic(DEV), "claude:epic") == TAG_ID)
check("чужого тега нет", f.tag_id(epic(DEV), "claude:night") is None)
check("у карточки без тегов — тоже None", f.tag_id(epic(DEV, tagged=False), "claude:epic") is None)

closing = FakeKaiten()
note = f.close_epic(closing, FLOW, epic(DEV))
check("эпик уехал на колонку правее", closing.moves == [(1, REVIEW)], str(closing.moves))
check("и тег снят", closing.tags_removed == [(1, TAG_ID)], str(closing.tags_removed))
check("в комментарии сказано про тег", "Тег `claude:epic` снял" in note, note)

stayed = FakeKaiten()
note = f.close_epic(stayed, FLOW, epic(ROLLOUT))
check("эпик уже правее — не двигаем", stayed.moves == [], str(stayed.moves))
check("и тег не трогаем: движения не было", stayed.tags_removed == [], str(stayed.tags_removed))
check("и комментария нет", note == "", repr(note))


class TagFails(FakeKaiten):
    def remove_tag(self, card_id, tag):
        raise f.FactoryError("403")


broken = TagFails()
note = f.close_epic(broken, FLOW, epic(DEV))
check("тег не снялся — эпик всё равно уехал", broken.moves == [(1, REVIEW)], str(broken.moves))
check("и человеку сказано снять руками", "сними руками" in note, note)

print("\nвсё сошлось")
