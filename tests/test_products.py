"""
Продукты: у команды несколько спринт-досок, и у каждой свои репозитории.

Здесь цена ошибки — работа не на той доске или код не в том репозитории: бек сайта,
уехавший в админку B2B, или эпик, разложенный целиком на магазин, хотя
половина его живёт в сайте. Проверяем, что выбор продукта и репозитория доезжает
до карточки, а неверный выбор агента не ломает поток.
Запуск: `python3 tests/test_products.py`.
"""
import importlib.util
import tempfile
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "factory", Path(__file__).resolve().parent.parent / "factory.py")
f = importlib.util.module_from_spec(spec); spec.loader.exec_module(f)

# id выдуманные: репозиторий публичный, настоящие ловит `python3 setup.py --audit`
SHOP, SITE, B2B, OLD = 9100, 9200, 9300, 9400
EPICS, DEV = 9000, 9003


def columns(base):
    return {"queue": base + 1, "in_progress": base + 2, "question": base + 2,
            "failed": base + 2, "agent_review": base + 3, "fixes": base + 2,
            "review": base + 3, "done": base + 4}


CFG = {
    "bot": {"tag": "stepa"},
    "kaiten": {"domain": "example.kaiten.ru", "space_id": 1},
    "products": {
        "shop": {"title": "Магазин", "epic_lanes": [71], "board_id": SHOP,
                 "columns": columns(SHOP), "repos": ["shop"]},
        "site": {"title": "Сайт", "epic_lanes": [72], "board_id": SITE,
                 "columns": columns(SITE), "repos": ["site-front", "site-back"]},
        "b2b": {"title": "Корпы", "epic_lanes": [73], "board_id": B2B,
                "columns": columns(B2B), "repos": ["site-front", "b2b-admin", "b2b-back"]},
        "old": {"title": "Старая", "retired": True, "board_id": OLD,
                "columns": columns(OLD), "repos": ["shop"]},
    },
    "default_product": "shop",
    "default_repo": "shop",
    "repos": {key: {"path": f"/nowhere/{key}", "remote": "origin", "base_branch": "main",
                    "about": f"про {key}"}
              for key in ("shop", "site-front", "site-back", "b2b-admin", "b2b-back")},
    "epic_flow": {"boards": [EPICS], "development_column_id": DEV, "stay_on_close": True},
}


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    assert ok, name


print("=== 1. по доске на продукт, и все — командные ===")
profiles = f.board_profiles(CFG)
check("четыре доски, включая уходящую", [p["board_id"] for p in profiles]
      == [SHOP, SITE, B2B, OLD], str([p["board_id"] for p in profiles]))
check("все берут только карточки с тегом", all(p["own_only"] for p in profiles))
check("к долгу спринта не привязывают", not any(p["attach_to_debt"] for p in profiles))
check("репозиторий по умолчанию — первый у продукта",
      [p["repo"] for p in profiles] == ["shop", "site-front", "site-front", "shop"])
check("уходящая доска — не место для новой работы", "old" not in f.destinations(CFG))
check("режим эпиков включён и без доски сабтасок", f.epic_flow(CFG) is not None)

print("=== 2. главный продукт эпика — по дорожке ===")
check("дорожка сайта — сайт", f.product_for_lane(CFG, 72) == "site")
check("чужая дорожка — продукт по умолчанию", f.product_for_lane(CFG, 999) == "shop")
check("без дорожки — тоже", f.product_for_lane(CFG, None) == "shop")

print("=== 3. выбор агента проверяется, а не принимается на веру ===")
check("верная пара проходит как есть",
      f.pick_product_repo(CFG, "b2b", "b2b-back") == ("b2b", "b2b-back"))
check("общий репозиторий остаётся за названным продуктом",
      f.pick_product_repo(CFG, "b2b", "site-front") == ("b2b", "site-front"))
check("чужой репозиторий утаскивает задачу на доску своего продукта",
      f.pick_product_repo(CFG, "shop", "site-back") == ("site", "site-back"))
check("выдуманный репозиторий — главный у продукта",
      f.pick_product_repo(CFG, "site", "nope") == ("site", "site-front"))
check("выдуманный продукт — по репозиторию",
      f.pick_product_repo(CFG, "nope", "b2b-admin") == ("b2b", "b2b-admin"))
check("уходящий продукт не выбирается",
      f.pick_product_repo(CFG, "old", "shop") == ("shop", "shop"))

print("=== 4. строка repo: — только когда репозиторий не главный ===")
check("главный — строки нет", f.repo_line(CFG, "site", "site-front") == "")
check("второй — есть", f.repo_line(CFG, "site", "site-back") == "repo: site-back")
card = {"description": "Сделать ручку.\n\nrepo: site-back"}
key, repo = f.resolve_repo(card, CFG, "site-front")
check("исполнитель по этой строке уходит в нужный репозиторий", key == "site-back", key)
check("и ключ едет вместе с настройками — по нему берутся правила",
      repo.get("key") == "site-back")

print("=== 4б. разведка ставит задачу на доску названного продукта ===")
check("сайт — на доску сайта", f.handoff_profile(CFG, "site")["board_id"] == SITE)
check("неизвестный — на доску по умолчанию", f.handoff_profile(CFG, "nope")["board_id"] == SHOP)
check("уходящую доску не выбрать", f.handoff_profile(CFG, "old")["board_id"] == SHOP)

print("=== 5. схема ответа перечисляет продукты и репозитории ===")
schema = f.with_products(f.DECOMPOSE_SCHEMA, CFG, "subtasks")
item = schema["properties"]["subtasks"]["items"]
check("продукты — только живые", item["properties"]["product"]["enum"] == ["shop", "site", "b2b"])
check("репозитории без повторов",
      item["properties"]["repo"]["enum"] == ["shop", "site-front", "site-back",
                                             "b2b-admin", "b2b-back"])
check("у сабтаски оба поля обязательны", {"product", "repo"} <= set(item["required"]))
check("исходная схема не тронута",
      "product" not in f.DECOMPOSE_SCHEMA["properties"]["subtasks"]["items"]["properties"])
triage = f.with_products(f.TRIAGE_SCHEMA, CFG, required=False)
check("у разведки поля есть, но не обязательны",
      "product" in triage["properties"] and "product" not in triage["required"])
check("без продуктов схема как была", f.with_products(f.TRIAGE_SCHEMA, {}) is f.TRIAGE_SCHEMA)


print("=== 6. сабтаски эпика ложатся на доски своих продуктов ===")


class Board:
    def __init__(self):
        self.created, self.children, self.tags, self.comments_ = [], [], [], []
        self.blocks = []

    def comments(self, card_id):
        return []

    def create_card(self, body):
        card = {"id": 500 + len(self.created), **body}
        self.created.append(body)
        return card

    def add_child(self, parent, child):
        self.children.append((parent, child))

    def add_tag(self, card_id, name):
        self.tags.append((card_id, name))

    def block(self, card_id, reason):
        self.blocks.append((card_id, reason))

    def blockers(self, card_id):
        return []

    def comment(self, card_id, text):
        self.comments_.append(text)


board = Board()
epic = {"id": 42, "title": "Корпоративный чекаут", "tags": [], "lane_id": 73}
items = [
    {"kind": "frontend", "title": "Показать поле ИНН", "description": "фронт",
     "product": "b2b", "repo": "site-front", "mocks_backend": True},
    {"kind": "backend", "title": "Принять ИНН", "description": "бек",
     "product": "b2b", "repo": "b2b-back"},
    {"kind": "frontend", "title": "Подсказка в магазине", "description": "магазин",
     "product": "shop", "repo": "site-back"},
]
f.create_subtasks(board, CFG, {**CFG["epic_flow"], "max_subtasks": 5}, epic,
                  "https://example/42", items, {"summary": "поделил"}, {},
                  SimpleNamespace(dry_run=False))
boards = [body["board_id"] for body in board.created]
check("корпоративные — на доску корпов, третья — туда, где её репозиторий",
      boards == [B2B, B2B, SITE], str(boards))
check("в очередь своей доски", [body["column_id"] for body in board.created]
      == [B2B + 1, B2B + 1, SITE + 1])
check("главный репозиторий без строки repo:",
      "repo:" not in board.created[0]["description"])
check("бек — со строкой repo:", board.created[1]["description"].endswith("repo: b2b-back"))
check("перенаправленная — со своим репозиторием",
      board.created[2]["description"].endswith("repo: site-back"))
check("все дочерние эпика", [parent for parent, _ in board.children] == [42, 42, 42])
check("и все с тегом", [name for _, name in board.tags] == ["stepa"] * 3)
check("блокер раскатки указывает на бек, а не на первую попавшуюся",
      any("#501" in reason for _, reason in board.blocks), str(board.blocks))
check("в комментарии к эпику видно, куда что легло",
      "(Корпы, b2b-back)" in board.comments_[-1], board.comments_[-1][:300])


print("=== 7. эпик закрывается по колонкам доски каждой сабтаски ===")


class Phase:
    def __init__(self, children):
        self.children_ = children

    def children(self, card_id):
        return self.children_

    def card(self, card_id):
        return next(c for c in self.children_ if c["id"] == card_id)

    def blockers(self, card_id):
        return []


def child(card_id, board_id, column):
    return {"id": card_id, "board_id": board_id, "column_id": column,
            "description": "Из эпика: #42 — https://example/42"}


said = [{"text": f"{f.EPIC_MARK} **{f.SPEC_LINE}** x"}, {"text": f"{f.EPIC_MARK} {f.SPEC_OK_LINE}"}]
epic_card = {"id": 42, "checklists": [{"name": f.ACCEPTANCE_LIST, "items": [{"text": "a"}]}]}
f.stale_holds = lambda kaiten, flow, card_id: ([], [])
done_both = Phase([child(1, SITE, SITE + 4), child(2, B2B, B2B + 4)])
check("обе в «Готово» своих досок — эпик закрывается",
      f.epic_phase(done_both, CFG, f.epic_flow(CFG), epic_card, said) == "closing")
half = Phase([child(1, SITE, SITE + 4), child(2, B2B, B2B + 2)])
check("одна ещё в работе — эпик в работе",
      f.epic_phase(half, CFG, f.epic_flow(CFG), epic_card, said) == "working")

print("=== 8. закрытый эпик на доске без ревью остаётся на месте ===")


class Epics:
    def __init__(self):
        self.moves, self.tags_removed = [], []

    def move(self, card_id, column):
        self.moves.append((card_id, column))

    def remove_tag(self, card_id, tag):
        self.tags_removed.append((card_id, tag))


epics = Epics()
note = f.close_epic(epics, {**CFG["epic_flow"], "tag": "stepa"},
                    {"id": 42, "board_id": EPICS, "column_id": DEV,
                     "tags": [{"id": 7, "tag_id": 7, "name": "stepa"}]})
check("не двигаем", epics.moves == [], str(epics.moves))
check("тег сняли", epics.tags_removed == [(42, 7)], str(epics.tags_removed))
check("и сказали, что дальше ведёт человек", "дальше его ведёшь ты" in note, note)

print("=== 9. правила — свои у каждого репозитория ===")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "site-back").mkdir()
    (root / "docs.md").write_text("общие")
    (root / "site-back" / "docs.md").write_text("свои")
    f.PROJECT_DIR = root
    check("есть свой файл — берётся он", f.project_file("site-back", "docs.md").read_text() == "свои")
    check("нет своего — общий", f.project_file("shop", "docs.md").read_text() == "общие")
    check("без ключа — общий", f.project_file(None, "docs.md").read_text() == "общие")

print("=== 10. карта репозиториев в промпте ===")
note = f.repos_note(CFG, "site-front", [("site-back", Path("/tmp/scout-site-back"))])
check("стоит, где агент сейчас", "`site-front` — про site-front — ты здесь" in note, note)
check("соседи — с путём", "`/tmp/scout-site-back`" in note)
check("нет копии — так и сказано", "копии нет" in note)
check("уходящего продукта в карте нет", "Старая" not in note)
check("без продуктов — пусто", f.repos_note({}, "shop", []) == "")

print("\nвсё сошлось")
