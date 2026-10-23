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
# что человек написал в тред. Фабрика реагирует только на обращение к себе,
# а бот в фальшивом Mattermost зовётся fabrica
QUESTION = ["@fabrica регистр поехал и в шапке тоже, поправь и там"]
BOT_ID = "bot-000"
CHANNEL_POSTS = []  # что лежит в канале для опроса упоминаний


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
        if "/users/username/" in self.path:
            name = self.path.rsplit("/", 1)[1]
            if name == "nobody":
                self.send_response(404); self.send_header("Content-Length", "0")
                self.end_headers(); return
            return self._reply({"id": "u-3", "username": name})
        if "/users/email/" in self.path:
            if self.path.endswith("/nobody%40example.com"):
                self.send_response(404); self.send_header("Content-Length", "0")
                self.end_headers(); return
            return self._reply({"id": "u-2", "username": "author"})
        if "/channels/" in self.path and "/posts?since=" in self.path:
            return self._reply({"order": [p["id"] for p in CHANNEL_POSTS],
                                "posts": {p["id"]: p for p in CHANNEL_POSTS}})
        if "/posts/" in self.path and "/thread" not in self.path:
            return self._reply({"id": "root", "message": "[PR](u) Убрал lowercase.\n[Карточка](k)"})
        if "/thread" in self.path:
            root = self.path.split("/posts/")[1].split("/thread")[0]
            return self._reply({"order": [root, "reply"], "posts": {
                root:    {"id": root, "user_id": BOT_ID, "message": "PR: ...",
                          "create_at": 1, "type": ""},
                "join":  {"id": "join", "user_id": "u-1", "message": "",
                          "create_at": 2, "type": "system_join_channel"},
                "reply": {"id": "reply", "user_id": "u-1", "create_at": 3, "type": "",
                          "message": QUESTION[0]},
            }})
        return self._reply({})

    def do_DELETE(self):
        GOT.append(("DELETE", self.path, None, self.headers.get("Authorization")))
        return self._reply({"status": "OK"})

    def do_PUT(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        GOT.append(("PUT", self.path, raw, self.headers.get("Authorization")))
        return self._reply({"id": "root", "message": json.loads(raw).get("message"),
                            "edit_at": 1})

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
    column = 107      # «На ревью»
    asks: list = []   # вопросы агента в карточке
    """
    Заглушка Kaiten: карточка в «На ревью», без блокеров.

    Все id здесь выдуманные — и доска, и колонки, и карточка. Настоящие сюда попасть
    не должны: репозиторий публичный, это ловит `python3 setup.py --audit`.
    """
    def __init__(self): self.comments_written, self.moves = [], []
    def card(self, card_id): return {"id": card_id, "title": "Поправить текст",
                                     "board_id": 1000, "column_id": self.column}
    def comment(self, card_id, text): self.comments_written.append((card_id, text))
    def comments(self, card_id):
        # ровно тем же шаблоном, каким вопросы пишет живой агент: по заголовку
        # «Что нужно уточнить» их и отличают от прочих списков в отчёте
        if not self.asks:
            return []
        body = ("🤖 **Не берусь — не хватает информации.** Ничего не менял."
                + f.format_list("Что нужно уточнить", self.asks))
        return [{"text": body, "created": "2026-09-09T00:00:00Z"}]
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
check("сказано, что поправил", body["message"].startswith("Чекай, поправил:"),
      body["message"])

print("=== 4б. пересказ заголовка вместо отчёта в тред не уходит ===")
echo = {"id": 556, "title": "Брать флаги из библиотеки автоматически и вернуть круг только основному бренду"}
check("эхо постановки распознано",
      f.echoes_title("Брать флаги из библиотеки автоматически и вернуть круг только основному бренду",
                     echo["title"]))
check("настоящий отчёт эхом не считается",
      not f.echoes_title("Флаги стали квадратными из flag-icons, круглая обрезка на месте.",
                         echo["title"]))
json.loads(state_file.read_text())  # состояние на месте, тред 555 переиспользуем
f.notify_pr(cfg, "kiosk", card, "https://kaiten/card/555",
            "https://github.com/o/r/pull/7",
            {"summary": card["title"]}, False, updated=True, env=env)
body = json.loads([g for g in GOT if g[0] == "POST" and g[1].endswith("/posts")][-1][2])
check("заголовок в чат не уехал", card["title"] not in body["message"], body["message"])
check("вместо него — куда смотреть", "в PR" in body["message"], body["message"])

print("=== 5. ответ человека в треде → комментарий в карточку и «Правки» ===")
class Args: dry_run = False; prompt_only = True
kaiten = FakeKaiten()
profiles = [f.make_profile("работа", {"board_id": 1000, "columns": {
    "queue": 101, "in_progress": 102, "question": 103, "failed": 104,
    "agent_review": 105, "fixes": 106, "review": 107, "done": 108}})]
f.load_env = lambda: env
f.blocked_by = lambda k, c: None
GOT.clear()
f.follow_time_threads(kaiten, cfg, Args(), profiles)
check("комментарий записан", len(kaiten.comments_written) == 1, str(kaiten.comments_written))
text = kaiten.comments_written[0][1]
check("это человеческий комментарий, не агентский",
      not text.startswith(f.AGENT_MARKS) and text.startswith(f.FROM_TIME_MARK))
check("виден автор", "Андрей" in text)
check("текст перенесён целиком", "поправь и там" in text)
check("обращение к боту в карточку не тащим", "@fabrica" not in text, text)
check("карточка уехала в «Правки»", kaiten.moves == [(555, 106)], str(kaiten.moves))
check("системное сообщение не перенесено", "system_join" not in text)
check("своё сообщение не перенесено", "PR: ..." not in text)
check("дочитано до последнего", json.loads(state_file.read_text())
      ["threads"]["555"]["last_post_id"] == "reply")
eyed = [g for g in GOT if g[0] == "POST" and g[1].endswith("/reactions")]
check("на сообщение поставлен глазок", len(eyed) == 1, str(eyed))
check("глазок — на том самом сообщении, от имени бота",
      json.loads(eyed[-1][2]) == {"user_id": BOT_ID, "post_id": "reply",
                                  "emoji_name": "eyes"}, eyed[-1][2])
check("и ни слова в тред: глазок — весь ответ",
      [g for g in GOT if g[0] == "POST" and g[1].endswith("/posts")] == [], str(GOT))
check("глазок записан в состояние — его потом снимать",
      json.loads(state_file.read_text())["threads"]["555"]["reacted"] == {"reply": "eyes"})

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

print("=== 9. статус в корневом сообщении ===")
f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"555": {
    "channel_id": "chan-1", "root_id": "root", "last_post_id": "reply",
    "base": "[PR](u) Убрал lowercase.\n[Карточка](k)", "status": ""}}}), encoding="utf-8")
GOT.clear()
kaiten5 = FakeKaiten()
f.follow_time_threads(kaiten5, cfg, Args(), profiles)
edits = [g for g in GOT if g[0] == "PUT"]
check("сообщение переписано", len(edits) == 1, str(GOT))
check("правкой, а не PATCH", edits[0][1].endswith("/posts/root/patch"), edits[0][1])
edited = json.loads(edits[0][2])["message"]
check("статус последней строкой по-человечески",
      edited.splitlines()[-1] == "пацанчики, позырьте плз", edited)
check("и без курсива", "_" not in edited.splitlines()[-1], edited)
check("текст сообщения не потерян", edited.startswith("[PR](u) Убрал lowercase."))
check("статус запомнен", json.loads(f.TIME_STATE_FILE.read_text())
      ["threads"]["555"]["status"] == "пацанчики, позырьте плз")

check("старый курсивный статус узнаётся как статус",
      f.is_status_line("_пацанчики, позырьте плз_")
      and f.is_status_line("пацанчики, позырьте плз")
      and not f.is_status_line("[Карточка](k)"))

print("=== 10. тот же статус второй раз не переписывается ===")
GOT.clear()
f.follow_time_threads(FakeKaiten(), cfg, Args(), profiles)
check("правок нет", [g for g in GOT if g[0] == "PUT"] == [])

print("=== 11. «Готово» — карточка уходит из состояния ===")
GOT.clear()
done = FakeKaiten(); done.column = 108
f.follow_time_threads(done, cfg, Args(), profiles)
edited = json.loads([g for g in GOT if g[0] == "PUT"][-1][2])["message"]
check("на «Готово» строка статуса снята", edited == "[PR](u) Убрал lowercase.\n[Карточка](k)",
      repr(edited))
check("тред больше не отслеживаем",
      "555" not in json.loads(f.TIME_STATE_FILE.read_text())["threads"])

print("=== 12. вопросы агента уезжают в тред ===")
# глазок на сообщении, с которого начался круг: круг кончится вопросами, и снять
# его придётся — ход опять человека. Заодно тут старая форма записи (список вместо
# «id -> эмодзи»): состояние на машине переживает обновление фабрики
f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"555": {
    "channel_id": "chan-1", "root_id": "root", "last_post_id": "reply",
    "eyed": ["reply"],
    "base": "[PR](u) Убрал lowercase.\n[Карточка](k)", "status": ""}}}), encoding="utf-8")
GOT.clear()
asking = FakeKaiten()
asking.asks = ["Какой текст у подписи?", "Включать для Польши?"]
f.follow_time_threads(asking, cfg, Args(), profiles)
posted = [json.loads(g[2])["message"] for g in GOT
          if g[0] == "POST" and g[1].endswith("/posts")]
in_thread = [m for m in posted if m.startswith("Есть следующие вопросы:")]
check("вопросы написаны в тред", len(in_thread) == 1, str(posted))
check("оба вопроса, списком", in_thread[0].count("\n- ") == 2, in_thread[0])
check("круг кончился вопросами — глазок снят",
      [g for g in GOT if g[0] == "DELETE"] != [] and
      json.loads(f.TIME_STATE_FILE.read_text())["threads"]["555"]["reacted"] == {},
      str(GOT))
check("и вместо него знак вопроса — видно, что сообщение прочитали",
      [json.loads(g[2])["emoji_name"] for g in GOT
       if g[0] == "POST" and g[1].endswith("/reactions")] == ["question"], str(GOT))
check("в треде без эмодзи", not any(m in in_thread[0] for m in ("❓", "⚠️", "🛑", "💬")),
      in_thread[0])
check("один вопрос — одной строкой",
      f.thread_asks(["❓ Включать для Польши?"]) == "Есть вопрос: Включать для Польши?",
      f.thread_asks(["❓ Включать для Польши?"]))
edited = json.loads([g for g in GOT if g[0] == "PUT"][-1][2])["message"]
check("статус говорит про вопросы",
      edited.splitlines()[-1] == "пацанчики, позырьте плз, есть вопросики", edited)
GOT.clear()
f.follow_time_threads(asking, cfg, Args(), profiles)
check("те же вопросы второй раз не пишутся",
      [g for g in GOT if g[0] == "POST" and g[1].endswith("/posts")] == [], str(GOT))

print("=== 13. «как дела» — ответ в тред, карточку не двигаем ===")
f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"555": {
    "channel_id": "chan-1", "root_id": "root", "last_post_id": None,
    "base": "[PR](u) Убрал lowercase.\n[Карточка](k)", "status": ""}}}), encoding="utf-8")
QUESTION[0] = "@fabrica как дела?"
GOT.clear()
asker = FakeKaiten()
f.follow_time_threads(asker, cfg, Args(), profiles)
replies = [json.loads(g[2])["message"] for g in GOT
           if g[0] == "POST" and g[1].endswith("/posts")]
check("ответил в тред", any("Сейчас:" in r for r in replies), str(replies))
check("сказал, где карточка", any("жду вашего ревью" in r for r in replies), str(replies))
check("и от первого лица, без ревьюверов и агентов",
      not any(word in r for r in replies for word in ("агент", "ревьювер", "фабрик")),
      str(replies))
check("в карточку вопрос не тащил", asker.comments_written == [],
      str(asker.comments_written))
check("карточку не двигал", asker.moves == [], str(asker.moves))

print("=== 14. а правку по-прежнему переносит ===")
QUESTION[0] = "@fabrica поправь ещё заголовок, он тоже в нижнем регистре"
f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"555": {
    "channel_id": "chan-1", "root_id": "root", "last_post_id": None,
    "base": "[PR](u) Убрал lowercase.\n[Карточка](k)", "status": ""}}}), encoding="utf-8")
GOT.clear()
fixer = FakeKaiten()
f.follow_time_threads(fixer, cfg, Args(), profiles)
check("перенёс в карточку", len(fixer.comments_written) == 1, str(fixer.comments_written))
check("и взял в правки", fixer.moves == [(555, 106)], str(fixer.moves))

print("=== 15. длинное сообщение с «как дела» внутри — это задача ===")
QUESTION[0] = ("@fabrica как дела? и заодно поправь, пожалуйста, заголовок промо-экрана — "
               "он тоже приводится к нижнему регистру, а должен быть как есть")
f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"555": {
    "channel_id": "chan-1", "root_id": "root", "last_post_id": None,
    "base": "[PR](u) Убрал lowercase.\n[Карточка](k)", "status": ""}}}), encoding="utf-8")
long_one = FakeKaiten()
f.follow_time_threads(long_one, cfg, Args(), profiles)
check("не принято за вопрос", len(long_one.comments_written) == 1,
      str(long_one.comments_written))

print("=== 16. чужой разговор в треде фабрика не трогает ===")
QUESTION[0] = "@s.volkov брат, дай апрувчик плз, и там выше прчики посмотри"
f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"555": {
    "channel_id": "chan-1", "root_id": "root", "last_post_id": None,
    "base": "[PR](u) Убрал lowercase.\n[Карточка](k)", "status": ""}}}), encoding="utf-8")
GOT.clear()
bystander = FakeKaiten()
f.follow_time_threads(bystander, cfg, Args(), profiles)
check("в карточку не тащит", bystander.comments_written == [],
      str(bystander.comments_written))
check("карточку не двигает", bystander.moves == [], str(bystander.moves))
check("в тред не отвечает",
      [g for g in GOT if g[0] == "POST" and g[1].endswith("/posts")] == [], str(GOT))
check("но сообщение считает прочитанным", json.loads(f.TIME_STATE_FILE.read_text())
      ["threads"]["555"]["last_post_id"] == "reply")

print("=== 17. вопрос ревьювера виден, а замечание прошлого круга — нет ===")


class Reviewer(FakeKaiten):
    """Ревью с замечанием и вопросом: у ревьювера свои маркеры, не «- »."""
    def comments(self, card_id):
        return [{"text": "🔍 **Нужны правки: 1 из 3.**\n\n"
                         "⚠️ Быстрый заказ обходит обязательный телефон",
                 "created": "2026-09-09T00:00:00Z"},
                {"text": "🔍 **Ревью пройдено, но нужен твой взгляд.**\n\n"
                         "❓ Какой unitId у пиццерии Алматы-9?",
                 "created": "2026-09-09T00:00:01Z"}]


QUESTION[0] = "коллеги, а кто посмотрит?"
f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"555": {
    "channel_id": "chan-1", "root_id": "root", "last_post_id": "reply",
    "base": "[PR](u) Убрал lowercase.\n[Карточка](k)", "status": ""}}}), encoding="utf-8")
GOT.clear()
f.follow_time_threads(Reviewer(), cfg, Args(), profiles)
asked = [json.loads(g[2])["message"] for g in GOT
         if g[0] == "POST" and g[1].endswith("/posts")]
check("сообщение одно", len(asked) == 1, str(asked))
check("вопрос ревьювера на месте", "Какой unitId у пиццерии Алматы-9?" in asked[0], asked[0])
# Замечание — из прошлого круга, и к тому времени его уже разобрали. Пока оно уезжало
# в тред наравне с вопросом, человек получал список, в котором нечего отвечать, и
# справедливо спрашивал в ответ: «это вопрос или что?»
check("замечание прошлого круга не поехало",
      "Быстрый заказ обходит обязательный телефон" not in asked[0], asked[0])
check("и ни одного значка", not any(m in asked[0] for m in ("❓", "⚠️", "🔍")), asked[0])

print("=== 18. сухой прогон состояние не трогает ===")


class DryArgs: dry_run = True; prompt_only = True


before = json.dumps({"threads": {"555": {
    "channel_id": "chan-1", "root_id": "root", "last_post_id": None,
    "base": "[PR](u) Убрал lowercase.\n[Карточка](k)", "status": ""}}})
f.TIME_STATE_FILE.write_text(before, encoding="utf-8")
QUESTION[0] = "@fabrica поправь ещё заголовок, он тоже в нижнем регистре"
GOT.clear()
f.DRY_RUN = True          # так же, как это делает main из --dry-run
try:
    dry = FakeKaiten()
    dry.asks = ["Какой текст у подписи?"]
    f.follow_time_threads(dry, cfg, DryArgs(), profiles)
    sent = [g for g in GOT if (g[0] == "POST" and g[1].endswith("/posts")) or g[0] == "PUT"]
    check("в Time ничего не ушло", sent == [], str(sent))
    check("состояние не тронуто",
          f.TIME_STATE_FILE.read_text(encoding="utf-8") == before,
          f.TIME_STATE_FILE.read_text(encoding="utf-8"))

    # счётчик неудач разведки — тот же уговор, и цена ошибки там выше: «told»
    # выставляется один раз, а сказать об этом человеку в сухом прогоне некому
    f.TRIAGE_STATE_FILE = f.STATE / "triage.json"
    f.note_triage_fail({"cards": {}}, 555, "агент упал", 2)
    check("счёт неудач разведки не пишется", not f.TRIAGE_STATE_FILE.exists())
finally:
    f.DRY_RUN = False
f.note_triage_fail({"cards": {}}, 555, "агент упал", 2)
check("а в настоящем прогоне пишется", f.TRIAGE_STATE_FILE.is_file())

print("=== 19. вопрос по задаче — это не постановка ===")
check("«как дела» — про статус", f.thread_intent("как дела?") == "status")
check("«поправь» — постановка", f.thread_intent("поправь заголовок") == "task")
check("«как дела и поправь» — постановка",
      f.thread_intent("как дела? и поправь заголовок") == "task")
check("вопрос по существу — вопрос",
      f.thread_intent("а на каком экране ты это воспроизводил?") == "question")
check("вопрос с «проверял» не путается с «проверь»",
      f.thread_intent("а ты проверял на большом экране?") == "question")
check("утверждение без вопроса — постановка",
      f.thread_intent("на большом экране плашка всё ещё едет") == "task")

f.load_env = lambda: env
QUESTION[0] = "@fabrica а на каком экране ты это воспроизводил?"
f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"555": {
    "channel_id": "chan-1", "root_id": "root", "last_post_id": None,
    "base": "[PR](u) Убрал lowercase.\n[Карточка](k)", "status": ""}}}), encoding="utf-8")
GOT.clear()
asked = FakeKaiten()
f.follow_time_threads(asked, cfg, Args(), profiles)
text = asked.comments_written[0][1]
check("вопрос перенесён в карточку", len(asked.comments_written) == 1, str(asked.comments_written))
check("и помечен вопросом, а не постановкой", "Вопрос из треда" in text, text)
check("агенту сказано отвечать словами", "ответь на него своими словами" in text, text)
check("карточку всё же взяли в работу", asked.moves == [(555, 106)], str(asked.moves))
check("в треде не обещали, а повесили глазок",
      [g for g in GOT if g[0] == "POST" and g[1].endswith("/posts")] == []
      and [g for g in GOT if g[0] == "POST" and g[1].endswith("/reactions")] != [],
      str(GOT))
check("в состоянии помечено, что круг отвечает на вопрос",
      json.loads(f.TIME_STATE_FILE.read_text())["threads"]["555"]["answering"] is True)

print("=== 20. итог такого круга — ответ, а не «поправил» ===")
GOT.clear()
f.notify_pr(cfg, "kiosk", card, "https://kaiten/card/555",
            "https://github.com/o/r/pull/7",
            {"summary": "Воспроизводится на iPad 1024×1366, на большом экране плашки нет."},
            False, updated=True, env=env)
answer = json.loads([g for g in GOT if g[0] == "POST" and g[1].endswith("/posts")][-1][2])
check("ушло в тот же тред", answer.get("root_id") == "root", str(answer))
check("это ответ, без слова «поправил»",
      answer["message"] == "Воспроизводится на iPad 1024×1366, на большом экране плашки нет.",
      answer["message"])
saved = json.loads(f.TIME_STATE_FILE.read_text())["threads"]["555"]
check("пометка снята", saved["answering"] is False, str(saved))
check("а круг правки записан", saved["fixed"] is True, str(saved))
dropped = [g for g in GOT if g[0] == "DELETE"]
check("глазок снят — дальше говорит сам ответ", len(dropped) == 1, str(dropped))
check("снят свой и с того самого сообщения",
      dropped[0][1].endswith(f"/users/{BOT_ID}/posts/reply/reactions/eyes"), dropped[0][1])
settled = [json.loads(g[2]) for g in GOT if g[0] == "POST" and g[1].endswith("/reactions")]
check("а на его месте галочка: «видел и сделал» остаётся на виду",
      settled == [{"user_id": BOT_ID, "post_id": "reply",
                   "emoji_name": "white_check_mark"}], str(settled))
check("и в состоянии его больше нет — галочку следующий круг не трогает",
      saved["reacted"] == {}, str(saved))

print("=== 21. вторая пачка вопросов — это круг после правки ===")
check("в первый раз — просто вопросы",
      f.thread_asks(["❓ А где макет?"]) == "Есть вопрос: А где макет?")
check("во второй — с оговоркой про правку",
      f.thread_asks(["❓ А где макет?"], again=True, fixed=True)
      .startswith("Перечитал ещё раз после правки"),
      f.thread_asks(["❓ А где макет?"], again=True, fixed=True))
check("а без правки «после правки» не врёт",
      f.thread_asks(["❓ А где макет?"], again=True)
      == "Перечитал ещё раз, и появился вопрос: А где макет?",
      f.thread_asks(["❓ А где макет?"], again=True))
f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"555": {
    "channel_id": "chan-1", "root_id": "root", "last_post_id": "reply", "fixed": True,
    "base": "[PR](u) Убрал lowercase.\n[Карточка](k)", "status": ""}}}), encoding="utf-8")
QUESTION[0] = "коллеги, а кто посмотрит?"
GOT.clear()
again = FakeKaiten()
again.asks = ["Правка меняет отступ только на малом экране", "На каком киоске видели?"]
f.follow_time_threads(again, cfg, Args(), profiles)
posted = [json.loads(g[2])["message"] for g in GOT
          if g[0] == "POST" and g[1].endswith("/posts")]
check("вопросы объяснены кругом ревью",
      any(m.startswith("Перечитал ещё раз после правки") for m in posted), str(posted))

print("=== 22. карточку, увезённую из потока, тред не тащит назад ===")
# 777 нет ни в одной роли профиля — это «Тестинг» или «Ролаут», куда её увёл человек
QUESTION[0] = "@fabrica поправь ещё заголовок, он тоже в нижнем регистре"
f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"555": {
    "channel_id": "chan-1", "root_id": "root", "last_post_id": None,
    "base": "[PR](u) Убрал lowercase.\n[Карточка](k)", "status": ""}}}), encoding="utf-8")
GOT.clear()
gone = FakeKaiten(); gone.column = 777
f.follow_time_threads(gone, cfg, Args(), profiles)
check("комментарий всё равно записан", len(gone.comments_written) == 1,
      str(gone.comments_written))
check("но карточку не двигали", gone.moves == [], str(gone.moves))
said = [json.loads(g[2])["message"] for g in GOT
        if g[0] == "POST" and g[1].endswith("/posts")]
check("и в треде сказали почему",
      any("не в моих колонках" in m for m in said), str(said))
check("значок — крест, а не глазок: глазок значил бы «делаю»",
      [json.loads(g[2])["emoji_name"] for g in GOT
       if g[0] == "POST" and g[1].endswith("/reactions")] == ["x"], str(GOT))

print("=== 23. «проверок не запускал» вопросом не считается ===")
report = ("🤖 **Готово, нужен ревью.**\n\nPR: https://pr/1\n\n"
          "Опустил контент экрана с ETA под плашку додокоинов.\n\n"
          "**Проверок я не запускал** — ни тестов, ни линтера. "
          "Смотреть этот PR стоит внимательнее обычного.\n\n"
          "**Что нужно уточнить**\n- На каком киоске видели съехавшую плашку?")
check("в тред уезжает только настоящий вопрос",
      f.open_questions([{"text": report}]) == ["На каком киоске видели съехавшую плашку?"],
      str(f.open_questions([{"text": report}])))

print("=== 24. блокер — крест и «меня блочит», а не глазок ===")
QUESTION[0] = "@fabrica поправь ещё заголовок, он тоже в нижнем регистре"
f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"555": {
    "channel_id": "chan-1", "root_id": "root", "last_post_id": None,
    "base": "[PR](u) Убрал lowercase.\n[Карточка](k)", "status": ""}}}), encoding="utf-8")
GOT.clear()
f.blocked_by = lambda k, c: "ждём макет от дизайна"
blocked = FakeKaiten()
f.follow_time_threads(blocked, cfg, Args(), profiles)
check("сообщение всё равно записано в карточку", len(blocked.comments_written) == 1,
      str(blocked.comments_written))
check("но карточку не двигали", blocked.moves == [], str(blocked.moves))
marks = [json.loads(g[2]) for g in GOT if g[0] == "POST" and g[1].endswith("/reactions")]
check("на сообщении крест", marks == [{"user_id": BOT_ID, "post_id": "reply",
                                       "emoji_name": "x"}], str(marks))
said = [json.loads(g[2])["message"] for g in GOT
        if g[0] == "POST" and g[1].endswith("/posts")]
check("и сказано, что именно блочит", said == ["Меня блочит: ждём макет от дизайна"],
      str(said))
check("крест запомнен — снимется, когда блокер уберут и правка поедет",
      json.loads(f.TIME_STATE_FILE.read_text())["threads"]["555"]["reacted"]
      == {"reply": "x"}, f.TIME_STATE_FILE.read_text())

# у блокера бывает пустая причина — «Меня блочит: заблокирована» звучало бы глупо
f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"555": {
    "channel_id": "chan-1", "root_id": "root", "last_post_id": None,
    "base": "[PR](u) Убрал lowercase.\n[Карточка](k)", "status": ""}}}), encoding="utf-8")
GOT.clear()
f.blocked_by = lambda k, c: f.NO_REASON
f.follow_time_threads(FakeKaiten(), cfg, Args(), profiles)
said = [json.loads(g[2])["message"] for g in GOT
        if g[0] == "POST" and g[1].endswith("/posts")]
check("блокер без причины — говорим своими словами",
      said == ["Меня блочит блокер на карточке — сними, и поеду."], str(said))
f.blocked_by = lambda k, c: None

print("=== 25. карточка переехала в конце прогона — канал узнаёт тем же прогоном ===")
# Ровно то, на чём фабрика попалась: треды читаются в начале прогона, а ревью
# уводит карточку человеку в конце — и в канале час висело «пока не смотрите»
# по работе, которую уже доделали.
f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"555": {
    "channel_id": "chan-1", "root_id": "root", "last_post_id": "reply",
    "base": "[PR](u) Убрал lowercase.\n[Карточка](k)",
    "status": "пацаны, пока не смотрите, сыровато"}}}), encoding="utf-8")
GOT.clear()
working = FakeKaiten(); working.column = 105      # «Ревью агента»
f.follow_time_threads(working, cfg, Args(), profiles)
check("пока карточка у ревьювера — статус тот же",
      [g for g in GOT if g[0] == "PUT"] == [], str(GOT))

GOT.clear()
reviewed = FakeKaiten(); reviewed.column = 107    # ревью прошло, карточка у человека
f.refresh_time_status(reviewed, cfg, Args(), profiles)
edits = [g for g in GOT if g[0] == "PUT"]
check("сообщение переписано в конце прогона", len(edits) == 1, str(GOT))
check("и в канале теперь «позырьте»",
      json.loads(edits[0][2])["message"].splitlines()[-1] == "пацанчики, позырьте плз",
      edits[0][2])
check("тред при этом не перечитывали",
      [g for g in GOT if "/thread" in g[1]] == [], str(GOT))

print("=== 26. упавший Time не роняет прогон ===")
broken = json.loads(json.dumps(cfg))
broken["notify"]["time"]["base_url"] = "http://127.0.0.1:1"   # там никто не слушает
f.refresh_time_status(FakeKaiten(), broken, Args(), profiles)
check("вернулись живыми", True)

print("=== 27. «Додумал сам» и разобранные замечания вопросами не считаются ===")
# Живой случай с карточкой про флаги: за прогон карточка прошла ревью с замечаниями,
# правки, отчёт и ревью начисто — человек всё это время молчал. В тред уехали
# замечания, которые исполнитель уже разобрал, и строка из «Додумал сам», а сверху
# было написано «вот что вылезло». В треде спросили: «это вопрос или что?»
flags = [
    {"text": "🔍 **Нужны правки: 2 из 5.**\n\n"
             "🛑 getByRole('img') не находит картинку с пустым alt\n\n"
             "⚠️ У Drinkit флаг стал квадратным",
     "created": "2026-09-16T05:50:19Z"},
    {"text": "🤖 **Готово, нужен ревью.**\n\nФлаг у Drinkit снова прямоугольный.\n\n"
             "**⚠️ Додумал сам — проверь**\n"
             "- Принял, что у Drinkit флаг должен остаться прямоугольным",
     "created": "2026-09-16T06:04:10Z"},
    {"text": "🔍 **Замечаний нет.**\n\nПодробно: https://github.com/o/r/pull/7",
     "created": "2026-09-16T08:17:33Z"},
]
check("в канал ехать нечему", f.human_questions(flags) == [], str(f.human_questions(flags)))
check("и «Додумал сам» вопросом не стало",
      not any("Принял, что" in a for a in f.open_questions(flags)), str(f.open_questions(flags)))

# а вот настоящий вопрос из того же отчёта — едет
asked_report = [flags[0], {"text": "🤖 **Готово, нужен ревью.**\n\nСделал."
                                   + f.format_list("Что нужно уточнить",
                                                   ["Круглый флаг только у Додо?"]),
                           "created": "2026-09-16T06:04:10Z"}]
check("вопрос из последнего отчёта на месте",
      f.human_questions(asked_report) == ["Круглый флаг только у Додо?"],
      str(f.human_questions(asked_report)))
check("а замечание прошлого круга — нет",
      not any("getByRole" in a for a in f.human_questions(asked_report)))

print("=== 28. длинный комментарий, разрезанный на части, читается целиком ===")
long_ask = f.split_comment("🤖 **Не берусь — не хватает информации.**\n\n"
                           + "Разбирался долго. " * 300
                           + f.format_list("Что нужно уточнить", ["А какой бизнес?"]))
check("Kaiten разрезал бы это на части", len(long_ask) > 1, str(len(long_ask)))
cut = [{"text": part, "created": f"2026-09-16T06:0{i}:00Z"}
       for i, part in enumerate(long_ask)]
check("вопрос найден в хвосте", f.human_questions(cut) == ["А какой бизнес?"],
      str(f.human_questions(cut)))

print("=== 29. Kaiten молчит — тред остаётся, 404 — уходит ===")
# Семнадцатого сентября мак проснулся раньше сети, `GET /cards/{id}` упал по
# таймауту, и это прочиталось как «карточку удалили»: два треда выкинулись из
# состояния, и строки статуса в канале замёрзли навсегда.
def with_broken_card(err):
    f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"555": {
        "channel_id": "chan-1", "root_id": "root", "last_post_id": "reply",
        "base": "[PR](u) Убрал lowercase.\n[Карточка](k)"}}}), encoding="utf-8")
    broken = FakeKaiten()
    def boom(card_id): raise err
    broken.card = boom
    f.sync_time_status(broken, cfg, Args(), profiles)
    return json.loads(f.TIME_STATE_FILE.read_text())["threads"]

check("сеть подвела — тред на месте",
      "555" in with_broken_card(f.NetworkError("curl 28: timed out")))
check("а 404 — карточки правда нет",
      "555" not in with_broken_card(f.NotFoundError("HTTP 404")))

print("=== 29б. строка статуса не замерзает над задачей, которой больше нет ===")
# В канале висело «пацаны, пока не смотрите, сыровато» над задачей, которую
# давно увели из колонок фабрики, — и человек читал это как ответ Стёпы.
def status_edits(kaiten_stub):
    f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"555": {
        "channel_id": "chan-1", "root_id": "root", "last_post_id": "reply",
        "base": "[PR](u) Убрал lowercase.\n[Карточка](k)",
        "status": "пацанчики, позырьте плз"}}}), encoding="utf-8")
    GOT.clear()
    f.sync_time_status(kaiten_stub, cfg, Args(), profiles)
    return [json.loads(g[2])["message"] for g in GOT if g[0] == "PUT"]

class Gone(FakeKaiten):
    def card(self, card_id): raise f.NotFoundError("HTTP 404")
check("карточку удалили — строку статуса сняли",
      status_edits(Gone()) == ["[PR](u) Убрал lowercase.\n[Карточка](k)"], str(GOT))

class Away(FakeKaiten):
    column = 999      # «Тестинг»: колонка доски, у которой нет роли фабрики
edits = status_edits(Away())
check("карточку увели из колонок фабрики — строку статуса сняли",
      edits == ["[PR](u) Убрал lowercase.\n[Карточка](k)"], str(edits))
check("а тред остался: вернут в работу — статус вернётся",
      "555" in json.loads(f.TIME_STATE_FILE.read_text())["threads"])
GOT.clear()
f.sync_time_status(Away(), cfg, Args(), profiles)
check("второй раз снимать нечего", [g for g in GOT if g[0] == "PUT"] == [], str(GOT))

print("=== 33. на доске команды «Ревью» — две роли, различает их последнее слово ===")
# Своей доски у Стёпы больше нет, он работает там же, где команда, а там колонок
# меньше, чем ролей: «Ревью» — это и вход ревьювера, и «дальше человек». По колонке
# их не различить, а для канала разница решающая: «пока не смотрите» против
# «позырьте». Раньше различал блокер «ревью агента пройдено, нужен человек», но он
# висел красным на каждой карточке в «Коде», где ход человека и так очевиден.
# Теперь — переписка: последнее слово фабрики за ревьювером, значит, он отдал.
shared = f.make_profile("работа", {"board_id": 2000, "columns": {
    "queue": 201, "in_progress": 202, "question": 202, "failed": 202,
    "agent_review": 205, "fixes": 202, "review": 205, "done": 208}}, own_only=True)
in_review = {"id": 556, "board_id": 2000, "column_id": 205}
report = {"text": "🤖 **Готово.** PR: https://github.com/acme/shop/pull/12"}
passed = {"text": "🔍 **Ревью, круг 1/3:** замечаний нет"}
asked_review = {"text": f"🔍 **{f.REVIEW_ASKS_HEAD}**\n\nВсё чисто.\n\n❓ Кто глянет на стенде?"}

check("ревьювер ещё не смотрел — «пока не смотрите»",
      f.time_status(shared, in_review, []) == ("agent_review", f.TIME_STATUSES["agent_review"]),
      str(f.time_status(shared, in_review, [])))
check("ревью пройдено — ход человека",
      f.time_status(shared, in_review, [], "", True) == ("review", f.TIME_STATUSES["review"]),
      str(f.time_status(shared, in_review, [], "", True)))
check("и на «как дела» он отвечает тем же",
      "жду вашего ревью" in f.time_status_answer(shared, in_review, [report, passed], []))
check("чужой блокер роль не меняет: это не наш способ сказать «твой ход»",
      f.time_status(shared, in_review, [], "ждём макет от дизайна")[0] == "agent_review")
check("на доске со своей колонкой под каждую роль ничего не поменялось",
      f.time_status(profiles[0], {"id": 556, "column_id": 107}, []) [0] == "review")
check("ревью человека блокером больше не выражается",
      "review" not in f.HANDOFF_BLOCKERS and f.review_in_place(shared)
      and not f.review_in_place(profiles[0]))

human = {"text": "гляну завтра"}
from_thread = {"text": f.from_thread_comment("Андрей", "поправь замечания из пр")}
check("после отчёта исполнителя — ждём ревьювера", not f.review_passed([report]))
check("последним был ревьювер — отдал человеку", f.review_passed([report, passed]))
check("реплика человека в карточке хода не меняет",
      f.review_passed([report, passed, human]))
check("просьба из треда — новая вводная", not f.review_passed([report, passed, from_thread]))
check("ответ на вопрос ревьювера — ход, он сам просил ответить в карточке",
      not f.review_passed([report, asked_review, human]))
check("а пока не ответили, вопрос ревьювера — тоже ход человека",
      f.review_passed([report, asked_review]))
check("исполнитель отработал после ревьювера — снова ждём ревьювера",
      not f.review_passed([passed, report]))
check("пустая карточка — ревью не было", not f.review_passed([]))

print("=== 34. просьба в треде по отревьюенной карточке везёт её в «Правки» ===")
# Пятнадцатого октября на «@стёпа поправь замечания из пр» бот ответил «Меня блочит:
# 🤖 ревью агента пройдено, нужен человек» — тому самому человеку, которого ждал.
# Блокера ревью больше нет, но «стоять на месте» в «Коде» всё равно нельзя: это вход
# ревьювера, и правка человека ушла бы к нему вместо исполнителя.
class Handed(FakeKaiten):
    held: list = []
    said = [report, passed]
    def __init__(self): super().__init__(); self.unblocked = []
    def card(self, card_id): return {"id": card_id, "title": "Поправить текст",
                                     "board_id": 2000, "column_id": self.column}
    def comments(self, card_id): return list(self.said)
    def blockers(self, card_id): return list(self.held)
    def unblock(self, card_id, blocker_id): self.unblocked.append((card_id, blocker_id))
    column = 205

def thread_once(kaiten, stuck):
    f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"555": {
        "channel_id": "chan-1", "root_id": "root", "last_post_id": None,
        "base": "[PR](u) Убрал lowercase.\n[Карточка](k)", "status": ""}}}),
        encoding="utf-8")
    GOT.clear()
    f.blocked_by = lambda k, c: stuck
    f.follow_time_threads(kaiten, cfg, Args(), [shared])
    f.blocked_by = lambda k, c: None
    marks = [json.loads(g[2])["emoji_name"] for g in GOT
             if g[0] == "POST" and g[1].endswith("/reactions")]
    said = [json.loads(g[2])["message"] for g in GOT
            if g[0] == "POST" and g[1].endswith("/posts")]
    return marks, said

QUESTION[0] = "@fabrica поправь замечания из пр"
mine = Handed()
marks, said = thread_once(mine, None)
check("карточка уехала в «Правки», хотя «Ревью» — колонка ревьювера",
      mine.moves == [(555, 202)], str(mine.moves))
check("на сообщении глазок, а не крест", marks == ["eyes"], str(marks))
check("и никакого «меня блочит»", said == [], str(said))
check("просьба записана в карточку", len(mine.comments_written) == 1,
      str(mine.comments_written))

waiting = Handed()
waiting.said = [report]
marks, _ = thread_once(waiting, None)
check("ревьювер ещё не смотрел — карточка ждёт его на месте, просьбу он прочтёт сам",
      waiting.moves == [] and marks == ["eyes"], f"{waiting.moves} {marks}")

asking = Handed()
asking.column = 202
question = f.block_reason(f.BLOCK_QUESTION)
asking.held = [{"id": 76, "reason": question}]
marks, said = thread_once(asking, question)
check("свой блокер «не хватило данных» сообщение по-прежнему снимает",
      asking.unblocked == [(555, 76)] and marks == ["eyes"] and said == [],
      f"{asking.unblocked} {marks} {said}")

mixed = Handed()
mixed.held = [{"id": 78, "reason": "ждём макет от дизайна"}]
marks, said = thread_once(mixed, mixed.held[0]["reason"])
check("чужой блокер — не снимаем", mixed.unblocked == [], str(mixed.unblocked))
check("и не двигаем", mixed.moves == [], str(mixed.moves))
check("а честно отвечаем крестом", marks == ["x"], str(marks))

gate = Handed()
gate.held = [{"id": 79, "reason": f.block_reason(f.BLOCK_ROLLOUT)}]
marks, _ = thread_once(gate, gate.held[0]["reason"])
check("блокер раскатки сообщением не снимается: правкой он не лечится",
      gate.unblocked == [] and marks == ["x"], f"{gate.unblocked} {marks}")

print("=== 35. круг из треда на доске команды — правка PR, а не задача с нуля ===")
# «Правки» там — та же колонка, что «В работе» и «Вопрос»: без этого просьба
# читалась бы ответом на вопрос, и ветка пошла бы от main мимо открытого PR
report = {"text": "🤖 PR открыт: https://github.com/acme/shop/pull/12"}
asked = {"text": f.from_thread_comment("Андрей", "поправь замечания из пр")}
on_shared = {"id": 555, "column_id": 202}
check("последнее слово из треда и PR есть — это правки",
      f.fix_round(None, shared, on_shared, [report, asked]))
check("PR ещё нет — это по-прежнему ответ, а не правки",
      not f.fix_round(None, shared, on_shared, [asked]))
check("в промпте — заметка про просьбу человека, а не про 🔍",
      f.fix_note([report, asked], []).startswith(f.HUMAN_FIX_NOTE))
check("после ревьювера — прежняя заметка",
      f.fix_note([asked, {"text": "🔍 Ревью: поправь"}], []).startswith(f.FIX_NOTE))

print("=== 36. замечания людей из PR доезжают до исполнителя ===")
import subprocess
reply = {"data": {"repository": {"pullRequest": {
    "commits": {"nodes": [{"commit": {"committedDate": "2026-10-15T09:00:00Z"}}]},
    "reviews": {"nodes": [
        {"author": {"login": "dev"}, "body": "Фикс рабочий, но см. замечания",
         "submittedAt": "2026-10-15T10:00:00Z"},
        {"author": {"login": "dev"}, "body": "старое, до прошлого круга",
         "submittedAt": "2026-10-15T08:00:00Z"},
        {"author": {"login": "dev"}, "body": "", "submittedAt": "2026-10-15T10:00:00Z"}]},
    "comments": {"nodes": [
        {"author": {"login": "bot"}, "body": "🔍 **Ревью, круг 1/3**",
         "createdAt": "2026-10-15T09:30:00Z"}]},
    "reviewThreads": {"nodes": [
        {"id": "T-a", "isResolved": False, "path": "a.ts", "line": 11, "originalLine": 11,
         "comments": {"nodes": [{"author": {"login": "dev"}, "body": "order 43 занят",
                                 "createdAt": "2026-10-15T10:00:00Z"}]}},
        {"id": "T-b", "isResolved": True, "path": "b.ts", "line": 3, "originalLine": 3,
         "comments": {"nodes": [{"author": {"login": "dev"}, "body": "решено",
                                 "createdAt": "2026-10-15T10:00:00Z"}]}},
        {"id": "T-c", "isResolved": False, "path": "c.ts", "line": None, "originalLine": 7,
         "comments": {"nodes": [{"author": {"login": "dev"}, "body": "переименуй",
                                 "createdAt": "2026-10-15T10:00:00Z"},
                                {"author": {"login": "dev2"}, "body": "плюсую",
                                 "createdAt": "2026-10-15T10:05:00Z"}]}}]}}}}}
calls = []
real_run = f.run_bounded
f.run_bounded = lambda cmd, cwd, timeout, env=None: (
    calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, json.dumps(reply), ""))
feedback = f.pr_feedback(Path("."), "https://github.com/acme/shop/pull/12")
said = [item["text"] for item in feedback]
f.run_bounded = real_run
check("номер PR уходит числом", "number=12" in calls[0] and "-F" in calls[0], str(calls))
check("ревью после коммита — в списке, с номером для ответа",
      "- `[1]` **dev** (ревью): Фикс рабочий, но см. замечания" in said, str(said))
check("ревью до коммита прошлый круг уже закрыл", not any("старое" in s for s in said))
check("пустое тело ревью не шумит", not any(s.endswith("(ревью): ") for s in said))
check("своя выжимка ревьювера не повторяется", not any("🔍" in s for s in said))
check("инлайн-замечание — с файлом и строкой", "- `[2]` **dev**, `a.ts:11`: order 43 занят" in said)
check("у инлайн-замечания запомнен тред — ответ уйдёт туда",
      [item["thread"] for item in feedback] == ["", "T-a", "T-c"], str(feedback))
check("решённый тред не тащим", not any("решено" in s for s in said))
check("ответы в треде — под замечанием, строка из originalLine",
      "- `[3]` **dev**, `c.ts:7`: переименуй\n  - **dev2**: плюсую" in said, str(said))
f.run_bounded = lambda cmd, cwd, timeout, env=None: subprocess.CompletedProcess(cmd, 1, "", "боль")
check("GitHub не ответил — пусто, а не падение",
      f.pr_feedback(Path("."), "https://github.com/acme/shop/pull/12") == [])
f.run_bounded = real_run
check("не ссылка на PR — в GitHub не ходим", f.pr_feedback(Path("."), "") == [])
check("в промпте — номера и просьба ответить",
      "`[2]`" in f.fix_note([report, asked], feedback) and "pr_replies" in
      f.fix_note([report, asked], feedback))

print("=== 37. ответы на замечания уходят в PR ===")
calls = []
f.run_bounded = lambda cmd, cwd, timeout, env=None: (
    calls.append((cmd, env)) or subprocess.CompletedProcess(cmd, 0, "{}", ""))
sent = f.post_pr_replies(Path("."), "https://github.com/acme/shop/pull/12", feedback, [
    {"ref": "2", "text": "Поставил order 44."},
    {"ref": "[3]", "text": "Переименовал."},
    {"ref": "1", "text": "Спасибо, поправил всё ниже."},
    {"ref": "9", "text": "такого замечания нет"},
    {"ref": "2", "text": ""}], dry_run=False)
f.run_bounded = real_run
threads = [c for c, _ in calls if "graphql" in c]
check("инлайн-замечания — ответом в свой тред",
      [next(a for a in c if a.startswith("thread=")) for c in threads]
      == ["thread=T-a", "thread=T-c"], str(threads))
check("ответ помечен — следующим кругом его не примут за замечание",
      all(next(a for a in c if a.startswith("body=")).startswith("body=" + f.AGENT_MARK)
          for c in threads))
loose = [c for c, _ in calls if c[:3] == ["gh", "pr", "comment"]]
check("ревью без строки — одним общим комментарием", len(loose) == 1, str(calls))
check("выдуманный номер и пустой ответ не уходят", sent == 3, str(sent))
check("без своего токена — под тем, кто залогинен в gh", all(e is None for _, e in calls))
calls.clear()
f.run_bounded = lambda cmd, cwd, timeout, env=None: (
    calls.append((cmd, env)) or subprocess.CompletedProcess(cmd, 0, "{}", ""))
f.post_pr_replies(Path("."), "https://github.com/acme/shop/pull/12", feedback,
                  [{"ref": "2", "text": "ок"}], dry_run=False, token="bot-token")
f.run_bounded = real_run
check("свой токен бота — только этим вызовам", calls[0][1]["GH_TOKEN"] == "bot-token")
calls.clear()
f.run_bounded = lambda *a, **k: calls.append(a) or subprocess.CompletedProcess([], 0, "", "")
f.post_pr_replies(Path("."), "https://github.com/acme/shop/pull/12", feedback,
                  [{"ref": "2", "text": "ок"}], dry_run=True)
f.run_bounded = real_run
check("сухой прогон в GitHub не пишет", calls == [])

print("=== 38. Стёпа пишет людям суть, без файлов и расхода ===")
verdict = {"summary": "Оплата больше не запускается дважды.", "risks": "Сценарий не прогонялся."}
meta = {"cost_usd": 8.1, "duration_ms": 760000, "num_turns": 93}
done = f.comment_success(verdict, meta, "https://github.com/acme/shop/pull/12", "stepa/card-1")
check("в отчёте нет ветки и расхода", "Ветка" not in done and "$" not in done, done)
check("но есть PR, суть и что проверить",
      "PR: https://github.com/acme/shop/pull/12" in done and "Оплата" in done
      and "**Проверь:**" in done, done)
review = {"summary": "Годно.", "findings": [
    {"severity": "minor", "where": "a.ts:11", "what": "order 43 занят",
     "why": "сценарии встанут в произвольном порядке", "fix": "поставить 44"}]}
pr_text = f.comment_review(review, meta, 1, 3, False)
check("в ревью на PR — одна строка на замечание",
      "order 43 занят → поставить 44" in pr_text, pr_text)
check("без файла, причины и расхода",
      "a.ts" not in pr_text and "причина" not in pr_text and "$" not in pr_text, pr_text)
short = f.comment_review_short({**review, "findings": [{**review["findings"][0],
                                                        "severity": "major"}]},
                               meta, 1, 3, True, "https://github.com/acme/shop/pull/12",
                               [{**review["findings"][0], "severity": "major"}])
check("в карточке тоже без файла и расхода", "a.ts" not in short and "$" not in short, short)
logs = Path(tempfile.mkdtemp())
f.LOGS = logs
(logs / "review-555-20261015-100000.json").write_text(json.dumps({"review": review}),
                                                       encoding="utf-8")
check("а исполнителю в круге правок — целиком, из лога ревью",
      "`a.ts:11`" in f.review_details(555) and "причина" in f.review_details(555))
(logs / "triage-900-20261015-100000.json").write_text(json.dumps({"verdict": {
    "where": ["`src/a.ts:3` — считает шаги"]}}), encoding="utf-8")
described = f.work_card_description({"id": 900, "description": "сломалось"}, [],
                                    {"problem": "Шаги сбиваются.",
                                     "where": ["`src/a.ts:3` — считает шаги"],
                                     "plan": ["Зафиксировать шаги"]}, "https://k/900")
check("в описании задачи из инбокса мест в коде нет", "src/a.ts" not in described, described)
check("исполнитель получает их подсказкой из разведки",
      "src/a.ts:3" in f.scout_hints({"description": described}))

print("=== 39. Стёпу тегнули в канале с карточкой — он её берёт ===")
class Sprint(FakeKaiten):
    """Доска команды: беклог, очередь, в работе, ревью, раскатка, готово."""
    cards = {}
    def __init__(self): super().__init__(); self.tags = []
    def card(self, card_id):
        if card_id not in self.cards:
            raise f.NotFoundError("HTTP 404")
        return {"id": card_id, "title": "Задача", "board_id": 2000, "tags": [],
                **self.cards[card_id]}
    def board(self, board_id):
        return {"columns": [{"id": i, "title": str(i), "sort_order": n}
                            for n, i in enumerate((200, 201, 202, 205, 207, 208))]}
    def add_tag(self, card_id, name): self.tags.append((card_id, name))

def mention(text, post_id="m1", root=""):
    return {"id": post_id, "user_id": "u-1", "type": "", "message": text,
            "create_at": 2_000_000_000_000, "root_id": root, "channel_id": "chan-1"}

def poll(kaiten, *posts):
    CHANNEL_POSTS[:] = list(posts)
    GOT.clear()
    f.follow_time_mentions(kaiten, cfg, Args(), [shared])
    state = json.loads(f.TIME_STATE_FILE.read_text())
    marks = [(json.loads(g[2])["post_id"], json.loads(g[2])["emoji_name"]) for g in GOT
             if g[0] == "POST" and g[1].endswith("/reactions")]
    said = [json.loads(g[2]) for g in GOT if g[0] == "POST" and g[1].endswith("/posts")]
    return state, marks, said

f.TIME_STATE_FILE.write_text(json.dumps({"threads": {}}), encoding="utf-8")
sprint = Sprint()
state, marks, said = poll(sprint, mention("@fabrica сделай https://x.kaiten.ru/777"))
check("первый опрос только ставит курсор — историю канала не разгребаем",
      state["channels"]["chan-1"]["since"] > 0 and not marks and not said
      and sprint.tags == [], str(state))
state["channels"]["chan-1"]["since"] = 1
f.TIME_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")

Sprint.cards = {777: {"column_id": 200}, 778: {"column_id": 202},
                779: {"column_id": 207}, 780: {"column_id": 201}}
state, marks, said = poll(sprint, mention(
    "@fabrica сделай, пожалуйста [вот](https://x.kaiten.ru/space/1/boards/card/777)"))
check("карточку из беклога взял: тег и «Очередь»",
      sprint.tags == [(777, "stepa")] and sprint.moves == [(777, 201)],
      f"{sprint.tags} {sprint.moves}")
check("просьба — комментарием в карточку, без ссылки и обращения",
      sprint.comments_written and "сделай, пожалуйста вот" in sprint.comments_written[0][1]
      and "kaiten.ru" not in sprint.comments_written[0][1], str(sprint.comments_written))
check("на сообщении глазок, слов нет", marks == [("m1", "eyes")] and said == [],
      f"{marks} {said}")
info = state["threads"]["777"]
check("тред запомнен чужим: корень — сообщение человека",
      info["root_id"] == "m1" and info["own_root"] is False, str(info))
check("курсор сдвинулся — второй раз то же сообщение не возьмём",
      state["channels"]["chan-1"]["since"] == 2_000_000_000_000)

state["channels"]["chan-1"]["since"] = 1
f.TIME_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
queued = Sprint()
state, marks, said = poll(queued, mention("@fabrica https://x.kaiten.ru/780", "m2"))
check("из «Очереди» никуда не двигаем, только тег",
      queued.tags == [(780, "stepa")] and queued.moves == [], f"{queued.tags} {queued.moves}")

for card_id, why in ((778, "Её уже кто-то делает"), (779, "уже дальше разработки"),
                     (999, "Не вижу такую карточку")):
    state["channels"]["chan-1"]["since"] = 1
    f.TIME_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    other = Sprint()
    state, marks, said = poll(other, mention(f"@fabrica глянь https://x.kaiten.ru/{card_id}",
                                             "m3", root="someone-root"))
    check(f"#{card_id}: крест и одна фраза в тот же тред — «{why}»",
          marks == [("m3", "x")] and len(said) == 1 and why in said[0]["message"]
          and said[0]["root_id"] == "someone-root", f"{marks} {said}")
    check(f"#{card_id}: карточку не трогали",
          other.tags == [] and other.moves == [] and other.comments_written == [])

state["channels"]["chan-1"]["since"] = 1
f.TIME_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
state, marks, said = poll(Sprint(), mention("@fabrica сделай задачку", "m4"))
check("без ссылки — просим ссылку", said and "ссылку на карточку" in said[0]["message"],
      str(said))

state["channels"]["chan-1"]["since"] = 1
f.TIME_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
state, marks, said = poll(Sprint(), mention("@fabrica и ещё раз", "m5", root="m1"))
check("сообщения из уже известного треда — не сюда, их читает тред",
      marks == [] and said == [], f"{marks} {said}")

print("=== 40. PR по карточке из канала — ответом в тот же тред ===")
GOT.clear()
f.load_env = lambda: env
f.notify_pr(cfg, "kiosk", {"id": 777, "title": "Задача"}, "https://k/777",
            "https://github.com/acme/shop/pull/7", {"summary": "Сделал задачу."}, False)
said = [json.loads(g[2]) for g in GOT if g[0] == "POST" and g[1].endswith("/posts")]
check("PR ушёл ответом в тред человека, а не новым сообщением в канал",
      len(said) == 1 and said[0].get("root_id") == "m1"
      and "pull/7" in said[0]["message"], str(said))
info = json.loads(f.TIME_STATE_FILE.read_text())["threads"]["777"]
check("статус теперь живёт в этом ответе — корень чужой",
      info["status_post"] == "new-post" and f.status_post(info) == "new-post", str(info))
check("глазок снят — за работу говорит PR",
      any(g[0] == "DELETE" and "reactions/eyes" in g[1] for g in GOT), str(GOT))
check("и сменился галочкой",
      any(g[0] == "POST" and g[1].endswith("/reactions")
          and json.loads(g[2])["emoji_name"] == "white_check_mark" for g in GOT), str(GOT))
check("свой тред по-прежнему держит статус в корне",
      f.status_post({"root_id": "root"}) == "root")
check("в чужом треде до PR статусу жить негде",
      f.status_post({"root_id": "m1", "own_root": False}) == "")

print("=== 41. после PR Стёпа зовёт в тред автора карточки ===")
f.TIME_STATE_FILE.write_text(json.dumps({"threads": {}}), encoding="utf-8")
owned = {"id": 888, "title": "Задача", "owner": {"email": "author@example.com"}}
GOT.clear()
f.notify_pr(cfg, "kiosk", owned, "https://k/888", "https://github.com/acme/shop/pull/8",
            {"summary": "Сделал задачу."}, False)
said = [json.loads(g[2]) for g in GOT if g[0] == "POST" and g[1].endswith("/posts")]
check("сначала PR в канал, потом тег в его тред",
      len(said) == 2 and "root_id" not in said[0] and "pull/8" in said[0]["message"]
      and said[1].get("root_id") == "new-post", str(said))
check("тег — отдельным ответом, сам PR без него",
      said[1]["message"].startswith("@author ") and "@author" not in said[0]["message"],
      str(said))
check("автор нашёлся по почте",
      any(g[1].endswith("/users/email/author%40example.com") for g in GOT), str(GOT))
check("и запомнен в треде",
      json.loads(f.TIME_STATE_FILE.read_text())["threads"]["888"]["author"] == "@author")
GOT.clear()
f.notify_pr(cfg, "kiosk", owned, "https://k/888", "https://github.com/acme/shop/pull/8",
            {"summary": "Поправил шапку."}, False, updated=True)
said = [json.loads(g[2])["message"] for g in GOT
        if g[0] == "POST" and g[1].endswith("/posts")]
check("«поправил» уже без тега — автор и так в треде",
      said == ["Чекай, поправил: Поправил шапку."], str(said))

print("=== 42. круг правок без коммита — без «поправил» ===")
GOT.clear()
f.notify_pr(cfg, "kiosk", owned, "https://k/888", "https://github.com/acme/shop/pull/8",
            {"summary": "Сербию оставил в PR — её сюда просил автор карточки."}, False,
            updated=True, changed=False)
said = [json.loads(g[2])["message"] for g in GOT
        if g[0] == "POST" and g[1].endswith("/posts")]
check("фраза агента как есть, без «Чекай, поправил:»",
      said == ["Сербию оставил в PR — её сюда просил автор карточки."], str(said))
saved = json.loads(f.TIME_STATE_FILE.read_text())["threads"]["888"]
check("правкой круг не записан, но отчёт был",
      saved["fixed"] is False and saved["reported"] is True, str(saved))
GOT.clear()
f.notify_pr(cfg, "kiosk", owned, "https://k/888", "https://github.com/acme/shop/pull/8",
            {}, False, updated=True, changed=False)
said = [json.loads(g[2])["message"] for g in GOT
        if g[0] == "POST" and g[1].endswith("/posts")]
check("и без фразы — тоже не «поправил»",
      len(said) == 1 and "поправил" not in said[0].lower(), str(said))

print("=== 43. ревьювер видит, что люди дописали к задаче ===")
review_prompt = f.build_review_prompt(
    {"id": 888, "title": "Выключить коинстор", "description": "Выключить флаг coinStore"},
    "https://k/888", {"base_branch": "main", "remote": "origin", "key": None},
    "stepa/card-888", "https://pr/8", 2, 3,
    [{"text": f.from_thread_comment("Andrey", "добавь сюда ещё Сербию")},
     {"text": f"{f.REVIEWER_MARK} **Ревью, круг 1/3: нужны правки.**"},
     {"text": f"{f.AGENT_MARK} Сербию оставил"}])
check("просьба из треда в промпте ревьювера",
      "добавь сюда ещё Сербию" in review_prompt, review_prompt[:600])
check("свои реплики фабрики туда не попали",
      "Ревью, круг 1/3" not in review_prompt and "Сербию оставил" not in review_prompt)
check("плейсхолдеров не осталось", "{{" not in review_prompt)

f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"889": {
    "channel_id": "chan-1", "root_id": "m9", "own_root": False}}}), encoding="utf-8")
GOT.clear()
f.notify_pr(cfg, "kiosk", dict(owned, id=889), "https://k/889",
            "https://github.com/acme/shop/pull/9", {"summary": "Сделал."}, False)
said = [json.loads(g[2]) for g in GOT if g[0] == "POST" and g[1].endswith("/posts")]
check("в чужом треде тоже: PR, потом тег, оба туда же",
      len(said) == 2 and all(m.get("root_id") == "m9" for m in said)
      and said[1]["message"].startswith("@author "), str(said))
check("в сообщение со статусом тег не попал",
      "@author" not in json.loads(f.TIME_STATE_FILE.read_text())["threads"]["889"]["base"])

client = f.Time(BASE, token="tok-123")
ticket = {"owner": {"email": "author@example.com"},
          "description": "Источник тикета: [тред в Time](https://t/x/pl/abc)\n\n"
                         "Создатель тикета: i.ivanov"}
GOT.clear()
check("подписан создатель тикета — тегаем его, а не владельца",
      f.author_mention(client, ticket, {}) == "@i.ivanov", str(GOT))
check("и ищем его по логину, почту не трогаем",
      any(g[1].endswith("/users/username/i.ivanov") for g in GOT)
      and not any("/users/email/" in g[1] for g in GOT), str(GOT))
check("создателя нет в Time — тегаем владельца",
      f.author_mention(client, dict(ticket, description="Создатель тикета: nobody"), {})
      == "@author")
check("подпись в жирном и с @ тоже читается",
      f.ticket_creator({"description": "**Создатель тикета:** @a.boev."}) == "a.boev")
check("нет подписи — нет создателя", f.ticket_creator({"description": "Просто текст"}) == "")
check("нет такого в Time — без тега",
      f.author_mention(client, {"owner": {"email": "nobody@example.com"}}, {}) == "")
check("у карточки нет автора — без тега", f.author_mention(client, {}, {}) == "")
check("на вебхуке искать нечем — без тега",
      f.author_mention(f.Time(BASE, webhook_url="x"),
                       {"owner": {"email": "author@example.com"}}, {}) == "")

srv.shutdown()
print("\nвсё сошлось")
