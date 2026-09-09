"""
Проверка отправки в Time без самого Time.

Поднимает фальшивый Mattermost на localhost и гоняет по нему оба пути: сообщение
про PR и чтение треда. Нужен, потому что руками этот код не проверить — для живого
Time нужен бот, а его создаёт администратор. Запуск: `python3 tests/test_time.py`.
"""
import importlib.util, json, os, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "factory", Path(__file__).resolve().parent.parent / "factory.py")
f = importlib.util.module_from_spec(spec); spec.loader.exec_module(f)

GOT = []          # что фальшивый сервер получил
BOT_ID = "bot-000"


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _reply(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        GOT.append(("GET", self.path, None, self.headers.get("Authorization")))
        if self.path.endswith("/users/me"):
            return self._reply({"id": BOT_ID, "username": "fabrica"})
        if "/thread" in self.path:
            root = self.path.split("/posts/")[1].split("/thread")[0]
            return self._reply({"order": [root, "reply"], "posts": {
                root:    {"id": root, "user_id": BOT_ID, "message": "PR: ...",
                          "create_at": 1, "type": ""},
                "join":  {"id": "join", "user_id": "u-1", "message": "",
                          "create_at": 2, "type": "system_join_channel"},
                "reply": {"id": "reply", "user_id": "u-1", "create_at": 3, "type": "",
                          "message": "регистр поехал и в шапке тоже, поправь и там"},
            }})
        return self._reply({})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        GOT.append(("POST", self.path, raw, self.headers.get("Authorization")))
        if self.path.endswith("/users/ids"):
            return self._reply([{"id": "u-1", "username": "aboev", "nickname": "Андрей"}])
        if self.path.endswith("/posts"):
            return self._reply({"id": "new-post", "channel_id": "chan-1"})
        return self._reply({"ok": True})


srv = HTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


class FakeKaiten:
    """
    Заглушка Kaiten: карточка в «На ревью», без блокеров.

    Все id здесь выдуманные — и доска, и колонки, и карточка. Настоящие сюда попасть
    не должны: репозиторий публичный, это ловит `python3 setup.py --audit`.
    """
    def __init__(self): self.comments_written, self.moves = [], []
    def card(self, card_id): return {"id": card_id, "title": "Поправить текст",
                                     "board_id": 1000, "column_id": 107}
    def comment(self, card_id, text): self.comments_written.append((card_id, text))
    def move(self, card_id, column): self.moves.append((card_id, column))
    def blockers(self, card_id): return []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    assert ok, name


print("=== 1. resolve_secret: порядок поиска ===")
os.environ["TIME_TEST_SECRET"] = "из-окружения"
check("окружение выигрывает",
      f.resolve_secret("TIME_TEST_SECRET", {"TIME_TEST_SECRET": "из-env-файла"}) == "из-окружения")
del os.environ["TIME_TEST_SECRET"]
check(".env как третий источник",
      f.resolve_secret("TIME_TEST_SECRET", {"TIME_TEST_SECRET": "из-env-файла"}) == "из-env-файла")
check("нет нигде — пусто, не падение", f.resolve_secret("TIME_NOPE_12345", {}) == "")
check("Keychain без записи молчит", f.keychain_secret("TIME_NOPE_12345") == "")

print("=== 2. без секции notify фабрика в Time не ходит ===")
before = len(GOT)
f.notify_pr({}, "kiosk", {"id": 1, "title": "т"}, "url", "https://pr/1", {}, False)
check("notify_pr молчит", len(GOT) == before)
check("клиента нет", f.time_client({}, {}) is None)

print("=== 3. бот: сообщение про PR и запомненный корень треда ===")
state_file = Path(tempfile.mkdtemp()) / "time.json"
f.TIME_STATE_FILE = state_file
f.STATE = state_file.parent
cfg = {"notify": {"time": {"transport": "bot", "base_url": BASE,
                           "channels": {"kiosk": "chan-1"}}},
       "pr": {"draft": True}}
env = {"TIME_BOT_TOKEN": "tok-123"}
card = {"id": 555, "title": "Поправить текст в дринкит в банере"}
f.notify_pr(cfg, "kiosk", card, "https://kaiten/card/555",
            "https://github.com/o/r/pull/7", {"summary": "Убрал lowercase."},
            False, env=env)
sent = [g for g in GOT if g[0] == "POST" and g[1].endswith("/posts")]
check("сообщение ушло", len(sent) == 1)
body = json.loads(sent[-1][2])
check("в нужный канал", body["channel_id"] == "chan-1", body.get("channel_id"))
check("не в тред (это корень)", "root_id" not in body)
check("ссылка на PR подписана словом PR", "[PR](https://github.com/o/r/pull/7)"
      in body["message"], body["message"])
check("рядом фраза, что он делает", "Убрал lowercase." in body["message"])
check("вторая строка — ссылка на карточку",
      body["message"].splitlines()[-1].startswith("[Карточка](https://kaiten"))
check("и больше ничего: две строки", len(body["message"].splitlines()) == 2,
      body["message"])
check("токен ушёл заголовком", sent[-1][3] == "Bearer tok-123", sent[-1][3])
saved = json.loads(state_file.read_text())["threads"]["555"]
check("корень треда запомнен", saved["root_id"] == "new-post", str(saved))

print("=== 4. правка по тому же PR — ответом в тот же тред ===")
f.notify_pr(cfg, "kiosk", card, "https://kaiten/card/555",
            "https://github.com/o/r/pull/7",
            {"summary": "Поправил шапку."}, False, updated=True, env=env)
body = json.loads([g for g in GOT if g[0] == "POST" and g[1].endswith("/posts")][-1][2])
check("ушло в тред", body.get("root_id") == "new-post", str(body))
check("сказано, что поправил", "Поправил" in body["message"])

print("=== 5. ответ человека в треде → комментарий в карточку и «Правки» ===")
class Args: dry_run = False; prompt_only = True
kaiten = FakeKaiten()
profiles = [f.make_profile("работа", {"board_id": 1000, "columns": {
    "queue": 101, "in_progress": 102, "question": 103, "failed": 104,
    "agent_review": 105, "fixes": 106, "review": 107, "done": 108}})]
f.load_env = lambda: env
f.blocked_by = lambda k, c: None
f.follow_time_threads(kaiten, cfg, Args(), profiles)
check("комментарий записан", len(kaiten.comments_written) == 1, str(kaiten.comments_written))
text = kaiten.comments_written[0][1]
check("это человеческий комментарий, не агентский",
      not text.startswith(f.AGENT_MARKS) and text.startswith(f.FROM_TIME_MARK))
check("виден автор", "Андрей" in text)
check("текст перенесён целиком", "поправь и там" in text)
check("карточка уехала в «Правки»", kaiten.moves == [(555, 106)], str(kaiten.moves))
check("системное сообщение не перенесено", "system_join" not in text)
check("своё сообщение не перенесено", "PR: ..." not in text)
acked = json.loads([g for g in GOT if g[0] == "POST" and g[1].endswith("/posts")][-1][2])
check("в тред подтвердили", "взял в правки" in acked["message"].lower(), acked["message"])
check("дочитано до последнего", json.loads(state_file.read_text())
      ["threads"]["555"]["last_post_id"] == "reply")

print("=== 6. второй прогон — то же сообщение второй раз не переносится ===")
kaiten2 = FakeKaiten()
f.follow_time_threads(kaiten2, cfg, Args(), profiles)
check("повторов нет", kaiten2.comments_written == [], str(kaiten2.comments_written))

print("=== 7. allow_from: чужие в треде не командуют ===")
f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"555": {
    "channel_id": "chan-1", "root_id": "root", "last_post_id": None}}}), encoding="utf-8")
cfg_allow = json.loads(json.dumps(cfg)); cfg_allow["notify"]["time"]["allow_from"] = ["someone"]
kaiten3 = FakeKaiten()
f.follow_time_threads(kaiten3, cfg_allow, Args(), profiles)
check("сообщение не от разрешённого — не перенесено", kaiten3.comments_written == [])

print("=== 8. вебхук: пишет, но тред читать нечем ===")
GOT.clear()
hook = {"notify": {"time": {"transport": "webhook", "base_url": BASE,
                            "channels": {"default": "chan-1"}}}}
f.notify_pr(hook, "kiosk", card, "https://kaiten/card/1", "https://pr/9",
            {"summary": "Сделал."}, False, env={"TIME_WEBHOOK_URL": BASE + "/hooks/abc"})
hooked = [g for g in GOT if g[1] == "/hooks/abc"]
check("ушло на адрес вебхука", len(hooked) == 1, str(GOT))
check("URL вебхука не в argv, а в конфиге curl", "text" in json.loads(hooked[0][2]))
f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"1": {"channel_id": "c",
                                                           "root_id": "root"}}}), encoding="utf-8")
kaiten4 = FakeKaiten()
# секрет вебхука на месте: клиент создастся, и упереться он должен именно в can_read
f.load_env = lambda: {"TIME_WEBHOOK_URL": BASE + "/hooks/abc"}
check("клиент на вебхуке создаётся",
      f.time_client(hook, f.load_env()) is not None)
check("но читать им нечем", f.time_client(hook, f.load_env()).can_read is False)
GOT.clear()
f.follow_time_threads(kaiten4, hook, Args(), profiles)
check("треды не читаются", kaiten4.comments_written == [])
check("и в сеть за ними не ходили", GOT == [], str(GOT))

srv.shutdown()
print("\nвсё сошлось")
