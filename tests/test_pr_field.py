"""
Проверка того, как PR попадает в поле карточки.

Поля в Kaiten общие на всю компанию, и ошибки тут видны не в логах, а у живых людей:
перепутал поле — ссылка легла в чужое, переписал все поля карточки — у бага пропали
приоритет и источник. Завести поле может только админ, и 403 на каждой карточке
прогона — шум, которого быть не должно.

Запуск: `python3 tests/test_pr_field.py`.
"""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "factory", Path(__file__).resolve().parent.parent / "factory.py")
f = importlib.util.module_from_spec(spec); spec.loader.exec_module(f)


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    assert ok, name


PR = "https://github.com/acme/app/pull/7"


class Kaiten:
    """Заглушка: поля компании по имени, учёт заведённых полей и записей в карточку."""
    def __init__(self, fields, can_create=True):
        self.fields, self.can_create = fields, can_create
        self.created, self.patches = [], []

    def custom_properties(self, query):
        return [p for p in self.fields if query.lower() in p["name"].lower()]

    def create_custom_property(self, body):
        self.created.append(body)
        if not self.can_create:
            raise f.FactoryError("Kaiten POST /company/custom-properties -> HTTP 403: ")
        return {"id": 900, **body}

    def patch_card(self, card_id, body):
        self.patches.append((card_id, body))


def field(id_, name, type_="url", condition="active"):
    return {"id": id_, "name": name, "type": type_, "condition": condition}


COMPANY = [field(1, "Priority", "select"), field(2, "PR link", "string"),
           field(3, "PR link"), field(4, "FOF: PR link"), field(5, "PR link", condition="inactive")]


def put(kaiten, card, cfg=None, url=PR):
    f.pr_field_ids.clear()
    f.put_pr_in_field(kaiten, cfg if cfg is not None else {"pr": {}}, card, url)


print("какое поле")
k = Kaiten(COMPANY)
put(k, {"id": 10, "properties": {"id_7": [1], "id_8": "x"}})
check("из тёзок берём живую ссылку с точным именем",
      k.patches == [(10, {"properties": {"id_3": PR}})], k.patches)
check("остальные поля карточки обратно не шлём — их Kaiten не трогает",
      list(k.patches[0][1]["properties"]) == ["id_3"])
check("заводить ничего не стали", not k.created)

print("\nповторы")
k = Kaiten(COMPANY)
put(k, {"id": 10, "properties": {"id_3": PR}})
check("тот же PR второй раз не пишем", not k.patches)
put(k, {"id": 10, "properties": {"id_3": "https://github.com/acme/app/pull/6"}})
check("новый PR перетирает старый", k.patches == [(10, {"properties": {"id_3": PR}})])

print("\nполя нет")
k = Kaiten([field(2, "PR link", "string")])
put(k, {"id": 10, "properties": {}})
check("заводим поле-ссылку с этим именем",
      k.created == [{"name": "PR link", "type": "url", "show_on_facade": True}], k.created)
check("и кладём PR в него", k.patches == [(10, {"properties": {"id_900": PR}})], k.patches)

k = Kaiten([], can_create=False)
f.pr_field_ids.clear()
for card_id in (10, 11, 12):
    f.put_pr_in_field(k, {"pr": {}}, {"id": card_id, "properties": {}}, PR)
check("403 — карточку не роняем и в неё не пишем", not k.patches)
check("и второй раз за прогон заводить не пробуем", len(k.created) == 1, k.created)

print("\nвыключено")
k = Kaiten(COMPANY)
put(k, {"id": 10}, cfg={"pr": {"kaiten_field": ""}})
check("пустое имя — поле не пишем", not k.patches)
put(k, {"id": 10}, url="(dry-run, PR не создан)")
check("не ссылка — поле не пишем", not k.patches)
put(k, {"id": 10}, cfg={})
check("без секции pr берём поле по умолчанию", k.patches == [(10, {"properties": {"id_3": PR}})])

print("\nвсё сошлось")
