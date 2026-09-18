"""
Проверка того, что одиночный обрыв связи с Kaiten не стоит прогона.

Корпоративная сеть рвёт соединение посреди ответа регулярно, и раньше любой такой
обрыв отменял весь прогон: восемнадцатого сентября так пропали три круга подряд при
живом Kaiten. Теперь чтение повторяется — но ровно чтение: повтор записи означал бы
второй такой же комментарий в карточке у живых коллег, а этого не видно ни в логах,
ни в тестах, только глазами в Kaiten.

Запуск: `python3 tests/test_network.py`.
"""
import importlib.util
import subprocess
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "factory", Path(__file__).resolve().parent.parent / "factory.py")
f = importlib.util.module_from_spec(spec); spec.loader.exec_module(f)


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    assert ok, name


class Done:
    """То, что вернул бы subprocess.run: stdout, хвостом — код ответа."""
    def __init__(self, body="", http=200, returncode=0, stderr=""):
        self.stdout = f"{body}\n{http}"
        self.returncode = returncode
        self.stderr = stderr


def answers(*plan):
    """
    Заглушка curl: отдаёт заготовленные ответы по одному и считает заходы.
    Элемент плана — либо готовый Done, либо исключение, которое надо поднять.
    """
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        step = plan[min(len(calls), len(plan)) - 1]
        if isinstance(step, BaseException):
            raise step
        return step
    run.calls = calls
    return run


def kaiten(run, dry_run=False):
    f.subprocess.run = run
    return f.Kaiten("kaiten.example", "token", 1, dry_run=dry_run)


# Паузы между попытками записываем, а не выжидаем: тест не должен идти полминуты.
real_run, real_sleep = subprocess.run, f.time.sleep
naps = []
f.time.sleep = naps.append

TIMEOUT = Done(returncode=28, stderr="curl: (28) Operation timed out")

print("=== 1. чтение переживает обрыв ===")
run = answers(TIMEOUT, TIMEOUT, Done('[{"id": 1}]'))
check("два обрыва подряд — и всё равно ответ",
      kaiten(run)._request("GET", "/cards") == [{"id": 1}])
check("заходов ровно три", len(run.calls) == 3, str(len(run.calls)))
check("между ними ждали, и с каждым разом дольше", naps == [6, 12], str(naps))

print("=== 2. но не бесконечно ===")
naps.clear()
run = answers(TIMEOUT)
try:
    kaiten(run)._request("GET", "/cards")
    check("сдался и сказал об этом", False)
except f.NetworkError as e:
    check("сдался и сказал об этом", "curl 28" in str(e), str(e))
check("попыток столько, сколько отмерено",
      len(run.calls) == f.KAITEN_READ_RETRIES, str(len(run.calls)))

print("=== 3. запись не повторяется никогда ===")
for method in ("POST", "PATCH", "PUT", "DELETE"):
    run = answers(TIMEOUT)
    try:
        kaiten(run)._request(method, "/cards/1/comments", body={"text": "привет"})
    except f.NetworkError:
        pass
    check(f"{method} — один заход", len(run.calls) == 1, str(len(run.calls)))

print("=== 4. молчание шлюза — то же молчание сети ===")
run = answers(Done("gateway", http=503), Done("gateway", http=503), Done('{"id": 1}'))
check("503 повторяется и дожидается ответа",
      kaiten(run)._request("GET", "/cards/1") == {"id": 1})
run = answers(Done("gateway", http=502))
try:
    kaiten(run)._request("GET", "/cards/1")
    check("исчерпав попытки, зовётся сетевой бедой", False)
except f.NetworkError:
    check("исчерпав попытки, зовётся сетевой бедой", True)

print("=== 5. ответ по существу повторять нечего ===")
for http, kind in ((404, f.NotFoundError), (403, f.FactoryError), (422, f.FactoryError)):
    run = answers(Done("no", http=http))
    try:
        kaiten(run)._request("GET", "/cards/1")
        check(f"HTTP {http} — сразу ошибка", False)
    except kind:
        check(f"HTTP {http} — сразу ошибка, без повторов", len(run.calls) == 1,
              str(len(run.calls)))
    check(f"HTTP {http} — и не сетевая беда", not issubclass(kind, f.NetworkError))

print("=== 6. снимок витрины отменяет прогон, только если Kaiten правда молчит ===")
CFG = {"kaiten": {"board_id": 1000, "columns": {"queue": 101}}}
f.snapshot_flow = lambda *a, **kw: None
check("снимок собрался — идём дальше", f.refresh_flow(None, CFG, []) is True)


def dead(*a, **kw):
    raise f.NetworkError("Kaiten GET /cards -> curl 28")


f.snapshot_flow = dead
check("Kaiten молчит — прогон отменяем", f.refresh_flow(None, CFG, []) is False)


def broken(*a, **kw):
    raise RuntimeError("витрина не сошлась")


f.snapshot_flow = broken
check("а любая другая беда витрины прогону не помеха",
      f.refresh_flow(None, CFG, []) is True)

f.subprocess.run, f.time.sleep = real_run, real_sleep
print("\nвсё сошлось")
