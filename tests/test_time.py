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
        if not self.asks:
            return []
        body = "🤖 **Не хватило данных.**\n\n" + "\n".join(f"- {a}" for a in self.asks)
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
check("обращение к боту в карточку не тащим", "@fabrica" not in text, text)
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
f.TIME_STATE_FILE.write_text(json.dumps({"threads": {"555": {
    "channel_id": "chan-1", "root_id": "root", "last_post_id": "reply",
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

print("=== 17. вопросы ревьювера начинаются с «❓» — их тоже видно ===")


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
check("замечание тоже", "Быстрый заказ обходит обязательный телефон" in asked[0], asked[0])
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
replies = [json.loads(g[2])["message"] for g in GOT
           if g[0] == "POST" and g[1].endswith("/posts")]
check("в треде обещали ответить, а не поправить",
      any("схожу посмотрю и отвечу" in r for r in replies), str(replies))
check("и не сказали «взял в правки»",
      not any("взял в правки" in r for r in replies), str(replies))
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

print("=== 21. вторая пачка вопросов — это круг после правки ===")
check("в первый раз — просто вопросы",
      f.thread_asks(["❓ А где макет?"]) == "Есть вопрос: А где макет?")
check("во второй — с оговоркой про правку",
      f.thread_asks(["❓ А где макет?"], again=True).startswith("Перечитал ещё раз после правки"),
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

print("=== 22. «проверок не запускал» вопросом не считается ===")
report = ("🤖 **Готово, нужен ревью.**\n\nPR: https://pr/1\n\n"
          "Опустил контент экрана с ETA под плашку додокоинов.\n\n"
          "**Проверок я не запускал** — ни тестов, ни линтера. "
          "Смотреть этот PR стоит внимательнее обычного.\n\n"
          "**Что нужно уточнить**\n- На каком киоске видели съехавшую плашку?")
check("в тред уезжает только настоящий вопрос",
      f.open_questions([{"text": report}]) == ["На каком киоске видели съехавшую плашку?"],
      str(f.open_questions([{"text": report}])))

srv.shutdown()
print("\nвсё сошлось")
