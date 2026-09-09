// Менюбар-приложение для фабрики: расписание, текущее состояние, ручной запуск.
//
// Таймер живёт внутри приложения — закрыл приложение, фабрика перестала ходить
// на доску. Ничего в launchd/cron не прописывается.
//
// Сборка: ./build-app.sh (он подставляет FABRICA_ROOT и собирает .app).

import AppKit
import Foundation

// `bakedRoot` — путь к папке фабрики, его генерирует build-app.sh в Root.swift.
// Переменная окружения FABRICA_ROOT перебивает его, если приложение переехало.

let intervalChoices: [(title: String, minutes: Int)] = [
    ("Каждые 15 минут", 15),
    ("Каждые 30 минут", 30),
    ("Каждый час", 60),
    ("Каждые 2 часа", 120),
    ("Каждые 4 часа", 240),
    ("Выключено", 0),
]

// Разведка инбокса дешёвая и короткая, поэтому может ходить часто: карточку закинули —
// через несколько минут в ней уже лежит комментарий. Но она же и единственная фаза,
// которая тратит деньги на карточки, о которых её никто не просил, так что в другом
// конце списка — раз в день: инбокс пополняется рывками, и разбирать его по расписанию
// кофеварки нужно не всегда.
let inboxIntervalChoices: [(title: String, minutes: Int)] = [
    ("Каждые 5 минут", 5),
    ("Каждые 10 минут", 10),
    ("Каждые 15 минут", 15),
    ("Каждые 30 минут", 30),
    ("Раз в день", 1440),
    ("Выключено", 0),
]

// Проверка эпика и шаг эпика — разные вещи, и путать их дорого стоило. Сам чек дешёвый:
// фабрика смотрит блокеры, чек-лист и комментарии и почти всегда уходит ни с чем — эпик
// ждёт человека. Агент запускается, только когда фаза действительно сменилась, а сменить
// её может лишь человек (ответил, снял блокер) или предыдущий шаг. Поэтому частый чек не
// значит частых трат — он значит, что снятый блокер подхватится через десять минут,
// а не через два часа.
let epicsIntervalChoices: [(title: String, minutes: Int)] = [
    ("Каждые 10 минут", 10),
    ("Каждые 15 минут", 15),
    ("Каждый час", 60),
    ("Каждые 2 часа", 120),
    ("Каждые 4 часа", 240),
    ("Раз в день", 1440),
    ("Выключено", 0),
]

/// Карточка, по которой ход человека. Счётчика мало: нужно видеть, какая именно
/// карточка и чего она ждёт, иначе всё равно лезть искать её на доске.
struct Waiting {
    var id: Int
    var title: String
    /// "epic" или "card" — эпик рисуется пазлом и меняет иконку в меню-баре
    var kind: String
    var reason: String
    /// Конкретные вопросы и замечания. Без них «ждёт ответа» бесполезно:
    /// непонятно, на что именно отвечать.
    var asks: [String]
    var url: String?

    var isEpic: Bool { kind == "epic" }
    var icon: String { isEpic ? "🧩" : "❓" }
}

/// Карточка, которая прямо сейчас в потоке: пишется, ждёт ревьювера, уехала к человеку.
/// Нужна, чтобы развернув меню было видно весь фронт работ, а не только текущий прогон.
struct InFlow {
    var id: Int
    var title: String
    var kind: String
    /// Где карточка стоит сейчас — положение, а не работа.
    var state: String
    /// Что фабрика сделает следующим шагом. Раньше в меню под видом «следующего
    /// шага» показывалось положение, и выходило «следующий шаг: сабтаски в работе».
    var next: String
    var url: String?

    var isEpic: Bool { kind == "epic" }
    var icon: String { isEpic ? "🧩" : "•" }
}

/// Пускает ли claude агента. Фабрика записывает это в статус, когда упирается
/// в 401 на живом запросе, и снимает отметку после удачного прогона агента.
/// Сам меню-бар в связку ключей не лезет: ему хватает флага.
struct Auth {
    var ok: Bool
    var expires: Date?

    /// Срок вышел по часам. Сам по себе это ещё не приговор — claude обычно
    /// продлевает сессию молча, — поэтому кнопку рисуем по `ok`, а это только
    /// для подсказки.
    var stale: Bool { expires.map { $0 < Date() } ?? false }
}

struct Status {
    var phase: String?
    var cardID: Int?
    var cardTitle: String?
    var cardURL: String?
    var returning = false
    var awaitingAnswer = 0
    var pid: Int32?
    /// Когда начался прогон и когда сменилась фаза. По ним видно, сколько шаг длится:
    /// без этого зависший git выглядел в трее как обычная работа.
    var runStarted: Date?
    var phaseSince: Date?
    var lastOutcome: String?
    var lastTitle: String?
    var lastCardID: Int?
    var lastPR: String?
    var lastURL: String?
    var lastCost: Double?
    /// Что агент делает прямо сейчас, сколько шагов сделал и когда подал голос.
    /// Пока этого не было, «Работает: пишу спеку» висело неподвижно по двадцать
    /// минут, и понять, работа это или зависшая сеть, было нечем.
    var agentAction: String?
    var agentSteps = 0
    var agentBeat: Date?
    var inboxPending = 0
    var epicsWaiting = 0
    var nightWaiting = 0
    var waiting: [Waiting] = []
    var flow: [InFlow] = []
    var auth: Auth?
}

final class Fabrica: NSObject, NSApplicationDelegate {

    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    private let root: URL
    private let defaults = UserDefaults.standard

    /// Ответ `security` про свежесть сессии и когда мы его получили. Спрашивать на
    /// каждую отрисовку меню незачем: это подпроцесс, а меню перерисовывается часто.
    private var sessionFresh: Bool?
    private var sessionCheckedAt: Date?

    private var scheduleTimer: Timer?
    private var inboxTimer: Timer?
    private var epicsTimer: Timer?
    private var pollTimer: Timer?
    private var runner: Process?
    private var nextRun: Date?
    private var nextInboxRun: Date?
    private var nextEpicsRun: Date?
    // Расписание попало в занятое время — прогон не теряем, а делаем сразу после
    // текущего. Очередь, а не три флага: режимов стало три, и флаги начали путаться.
    private var pendingModes: [RunMode] = []
    private var status = Status()
    private var lastError: String?
    private var mood: Mood?
    /// Адрес доски — из config.json, чтобы приложение не знало про конкретную команду.
    private var boardURL: String?

    private var intervalMinutes: Int {
        get { defaults.object(forKey: "intervalMinutes") as? Int ?? 60 }
        set { defaults.set(newValue, forKey: "intervalMinutes") }
    }

    private var inboxIntervalMinutes: Int {
        get { defaults.object(forKey: "inboxIntervalMinutes") as? Int ?? 10 }
        set { defaults.set(newValue, forKey: "inboxIntervalMinutes") }
    }

    private var epicsIntervalMinutes: Int {
        get { defaults.object(forKey: "epicsIntervalMinutes") as? Int ?? 15 }
        set { defaults.set(newValue, forKey: "epicsIntervalMinutes") }
    }

    override init() {
        let env = ProcessInfo.processInfo.environment["FABRICA_ROOT"]
        root = URL(fileURLWithPath: env ?? bakedRoot, isDirectory: true)
        super.init()
        boardURL = readBoardURL()
    }

    /// Собирает адрес доски из config.json. Конфига может не быть (фабрику ещё не
    /// настроили) — тогда пункт меню просто не открывает ничего.
    private func readBoardURL() -> String? {
        let url = root.appendingPathComponent("config.json")
        guard let data = try? Data(contentsOf: url),
              let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let kaiten = json["kaiten"] as? [String: Any],
              let domain = kaiten["domain"] as? String,
              let space = kaiten["space_id"] as? NSNumber
        else { return nil }
        return "https://\(domain)/space/\(space.intValue)/boards"
    }

    // MARK: - жизненный цикл

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem.menu = menu
        menu.delegate = self
        rescheduleTimer()
        rescheduleInboxTimer()
        rescheduleEpicsTimer()
        readStatusFile()
        redraw()
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopRun()
    }

    // MARK: - расписание

    private func rescheduleTimer() {
        scheduleTimer?.invalidate()
        scheduleTimer = nil
        nextRun = nil
        let minutes = intervalMinutes
        guard minutes > 0 else { return }
        let seconds = TimeInterval(minutes * 60)
        nextRun = Date().addingTimeInterval(seconds)
        let timer = Timer(timeInterval: seconds, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.nextRun = Date().addingTimeInterval(seconds)
            self.startRun(manual: false, mode: .board)
        }
        RunLoop.main.add(timer, forMode: .common)
        scheduleTimer = timer
    }

    private func rescheduleInboxTimer() {
        inboxTimer?.invalidate()
        inboxTimer = nil
        nextInboxRun = nil
        let minutes = inboxIntervalMinutes
        guard minutes > 0 else { return }
        let seconds = TimeInterval(minutes * 60)
        nextInboxRun = Date().addingTimeInterval(seconds)
        let timer = Timer(timeInterval: seconds, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.nextInboxRun = Date().addingTimeInterval(seconds)
            self.startRun(manual: false, mode: .inbox)
        }
        RunLoop.main.add(timer, forMode: .common)
        inboxTimer = timer
    }

    private func rescheduleEpicsTimer() {
        epicsTimer?.invalidate()
        epicsTimer = nil
        nextEpicsRun = nil
        let minutes = epicsIntervalMinutes
        guard minutes > 0 else { return }
        let seconds = TimeInterval(minutes * 60)
        nextEpicsRun = Date().addingTimeInterval(seconds)
        let timer = Timer(timeInterval: seconds, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.nextEpicsRun = Date().addingTimeInterval(seconds)
            self.startRun(manual: false, mode: .epics)
        }
        RunLoop.main.add(timer, forMode: .common)
        epicsTimer = timer
    }

    // MARK: - запуск прогона

    /// Что запускаем. Раньше это были булевы флаги, но их стало три и они путались.
    enum RunMode: Equatable {
        /// Всё подряд: инбокс, эпики, ревью, работа. Остался для ночного LaunchAgent —
        /// он зовёт run.sh без флагов.
        case full
        /// Только доска: ревью и работа. Инбокс и эпики ходят по своим расписаниям,
        /// и полный прогон делал бы их работу второй раз — за отдельные деньги.
        case board
        case inbox, epics
        /// Одна карточка: у эпика своя фаза, у обычной — обычный поток
        case one(id: Int, epic: Bool)

        var flag: String {
            switch self {
            case .full: return ""
            case .board: return " --no-triage --no-epics"
            case .inbox: return " --only-triage"
            case .epics: return " --only-epics"
            case .one(let id, let epic):
                return epic ? " --epic-card \(id)" : " --card \(id)"
            }
        }
    }

    private func startRun(manual: Bool, mode: RunMode = .board) {
        guard runner == nil else {
            if manual { NSSound.beep() }
            // по расписанию — не теряем: запустим сразу после текущего прогона
            else if !pendingModes.contains(mode) { pendingModes.append(mode) }
            return
        }
        lastError = nil
        // то, что сейчас и так делаем, из очереди убираем
        pendingModes.removeAll { $0 == mode || (mode == .full && $0 != .board) }

        let script = "cd \(shellQuote(root.path)) && ./run.sh" + mode.flag
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        // -ilc: интерактивный логин-шелл. Только он даёт то же окружение, что и терминал:
        // nvm-версию node и NPM_TOKEN из .zshrc, без которых агент не соберёт фронт.
        process.arguments = ["-ilc", script]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice

        process.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self else { return }
                self.runner = nil
                self.pollTimer?.invalidate()
                self.pollTimer = nil
                if proc.terminationStatus == 75 {
                    // run.sh увидел чужой замок и вышел. Молчать нельзя: человек нажал
                    // кнопку и ждёт хоть какой-то реакции
                    self.lastError = "прогон уже идёт — этот запуск пропущен"
                } else if proc.terminationStatus != 0
                            && proc.terminationReason != .uncaughtSignal {
                    self.lastError = "run.sh завершился с кодом \(proc.terminationStatus)"
                }
                self.readStatusFile()
                self.redraw()
                if !self.pendingModes.isEmpty {
                    let next = self.pendingModes.removeFirst()
                    DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
                        self.startRun(manual: false, mode: next)
                    }
                }
            }
        }

        do {
            try process.run()
            runner = process
        } catch {
            lastError = "не удалось запустить run.sh: \(error.localizedDescription)"
            redraw()
            return
        }

        let poll = Timer(timeInterval: 2, repeats: true) { [weak self] _ in
            self?.readStatusFile()
            self?.redraw()
        }
        RunLoop.main.add(poll, forMode: .common)
        pollTimer = poll
        redraw()
    }

    /// Гасим весь хвост: сначала детей питона (это и есть claude), потом сам питон,
    /// потом шелл. Прицельно по pid, чтобы не задеть чужие сессии claude.
    private func stopRun() {
        guard let process = runner else { return }
        if let pid = status.pid {
            shell("pkill -TERM -P \(pid) 2>/dev/null; kill -TERM \(pid) 2>/dev/null")
        }
        process.terminate()
        runner = nil
        pollTimer?.invalidate()
        pollTimer = nil
    }

    // MARK: - чтение состояния

    private func readStatusFile() {
        let url = root.appendingPathComponent("state/status.json")
        guard let data = try? Data(contentsOf: url),
              let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { return }

        var s = Status()
        s.phase = json["phase"] as? String
        s.returning = json["returning"] as? Bool ?? false
        s.awaitingAnswer = (json["awaiting_answer"] as? NSNumber)?.intValue ?? 0
        s.inboxPending = (json["inbox_pending"] as? NSNumber)?.intValue ?? 0
        s.epicsWaiting = (json["epics_waiting"] as? NSNumber)?.intValue ?? 0
        s.nightWaiting = (json["night_waiting"] as? NSNumber)?.intValue ?? 0
        if let waiting = json["waiting"] as? [[String: Any]] {
            s.waiting = waiting.compactMap { item in
                guard let id = (item["id"] as? NSNumber)?.intValue else { return nil }
                return Waiting(id: id,
                               title: item["title"] as? String ?? "",
                               kind: item["kind"] as? String ?? "card",
                               reason: item["reason"] as? String ?? "",
                               asks: item["asks"] as? [String] ?? [],
                               url: item["url"] as? String)
            }
            // эпики первыми: они блокируют целый поток, а карточка — только себя
            s.waiting.sort { $0.isEpic && !$1.isEpic }
        }
        if let flow = json["flow"] as? [[String: Any]] {
            s.flow = flow.compactMap { item in
                guard let id = (item["id"] as? NSNumber)?.intValue else { return nil }
                return InFlow(id: id,
                              title: item["title"] as? String ?? "",
                              kind: item["kind"] as? String ?? "card",
                              state: item["state"] as? String ?? "",
                              next: item["next"] as? String ?? "",
                              url: item["url"] as? String)
            }
        }
        if let agent = json["agent"] as? [String: Any] {
            s.agentAction = agent["action"] as? String
            s.agentSteps = (agent["steps"] as? NSNumber)?.intValue ?? 0
        }
        s.pid = (json["pid"] as? NSNumber)?.int32Value
        let stamps = ISO8601DateFormatter()
        s.runStarted = (json["run_started"] as? String).flatMap { stamps.date(from: $0) }
        s.phaseSince = (json["phase_since"] as? String).flatMap { stamps.date(from: $0) }
        s.agentBeat = ((json["agent"] as? [String: Any])?["beat"] as? String)
            .flatMap { stamps.date(from: $0) }
        if let auth = json["auth"] as? [String: Any] {
            s.auth = Auth(ok: auth["ok"] as? Bool ?? true,
                          expires: (auth["expires"] as? String)
                              .flatMap { stamps.date(from: $0) })
        }
        if let card = json["card"] as? [String: Any] {
            s.cardID = (card["id"] as? NSNumber)?.intValue
            s.cardTitle = card["title"] as? String
            s.cardURL = card["url"] as? String
        }
        if let last = json["last"] as? [String: Any] {
            s.lastOutcome = last["outcome"] as? String
            s.lastTitle = last["title"] as? String
            s.lastCardID = (last["card_id"] as? NSNumber)?.intValue
            s.lastPR = last["pr"] as? String
            s.lastURL = last["url"] as? String
            s.lastCost = (last["cost_usd"] as? NSNumber)?.doubleValue
        }
        status = s
    }

    // MARK: - отрисовка меню

    /// Настроение человечка. Работа важнее всего, дальше — авария, потом висящие вопросы.
    /// Свежая ли сейчас авторизация claude. nil — узнать не удалось.
    ///
    /// Спрашиваем сам claude-овский секрет, а не фабрику: отметку в статусе ставит и
    /// снимает только прогон, а человек, который минуту назад вошёл, ждёт, что кнопка
    /// пропадёт сразу, а не через десять минут до ближайшего тика. Читаем один срок
    /// годности, сам токен нам не нужен и в приложение не попадает.
    private func sessionIsFresh() -> Bool? {
        if let at = sessionCheckedAt, Date().timeIntervalSince(at) < 5 { return sessionFresh }
        sessionCheckedAt = Date()
        sessionFresh = nil

        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/security")
        task.arguments = ["find-generic-password", "-s", "Claude Code-credentials", "-w"]
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = FileHandle.nullDevice
        guard (try? task.run()) != nil else { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()

        guard let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let oauth = json["claudeAiOauth"] as? [String: Any],
              let expires = (oauth["expiresAt"] as? NSNumber)?.doubleValue
        else { return nil }
        sessionFresh = Date(timeIntervalSince1970: expires / 1000) > Date()
        return sessionFresh
    }

    /// Показывать ли кнопку входа. Отметку фабрики уважаем, но не слепо: если сессия
    /// уже свежая — человек вошёл сам, и висящая кнопка только раздражает. Обратного
    /// не делаем: сами тревогу не поднимаем, приговор всегда за фабрикой.
    private var needsLogin: Bool {
        status.auth?.ok == false && sessionIsFresh() != true
    }

    /// Идёт ли прогон на самом деле. Свой процесс видно напрямую, чужой (запущенный
    /// из терминала или ночным агентом) — только по pid из статуса.
    private var runIsAlive: Bool {
        if runner != nil { return true }
        guard let pid = status.pid else { return false }
        return kill(pid, 0) == 0
    }

    /// Сколько идёт текущий шаг, словами. nil — если шага нет.
    private func elapsed() -> String? {
        guard let since = status.phaseSince ?? status.runStarted else { return nil }
        let minutes = Int(Date().timeIntervalSince(since) / 60)
        if minutes < 1 { return "меньше минуты" }
        return "\(minutes) мин"
    }

    /// Давно ли агент подавал голос. nil — если он сейчас не работает.
    private func silence() -> TimeInterval? {
        guard let beat = status.agentBeat else { return nil }
        return Date().timeIntervalSince(beat)
    }

    /// Прогон завис.
    ///
    /// Раньше это был час от начала прогона — и он молчал ровно там, где нужен:
    /// агент работает от шести до двадцати шести минут, и всё это время зависшая
    /// сеть выглядела как обычная работа. Теперь решает пульс: живой агент шлёт
    /// событие каждые несколько секунд, и пять минут тишины значит, что он не
    /// работает, а во что-то уткнулся. Пульса нет вовсе — остаётся прежний
    /// сторож по времени: фабрика может стоять и в git push, где событий не бывает.
    private var runLooksStuck: Bool {
        guard runIsAlive else { return false }
        if let quiet = silence() { return quiet > 5 * 60 }
        guard let since = status.phaseSince ?? status.runStarted else { return false }
        return Date().timeIntervalSince(since) > 15 * 60
    }

    private func currentMood() -> Mood {
        if runLooksStuck { return .alert }
        if runIsAlive { return .working }
        if lastError != nil { return .alert }
        // Эпик важнее обычного вопроса: вопросов в «Вопросе от агента» может висеть
        // сколько угодно и подолгу, а эпик заблокирован и ждёт решения именно сейчас.
        if status.waiting.contains(where: { $0.isEpic }) { return .epicAsking }
        if !status.waiting.isEmpty || status.awaitingAnswer > 0 { return .asking }
        return .sleeping
    }

    /// Иконка статичная — перерисовываем только когда настроение действительно сменилось.
    private func paintIcon() {
        let next = currentMood()
        guard next != mood else { return }
        mood = next
        statusItem.button?.image = Sprites.image(next, height: 18)
    }

    private func redraw() {
        let running = runner != nil
        paintIcon()
        statusItem.button?.toolTip = runIsAlive
            ? "Фабрика работает: \(status.phase ?? "")"
            : (status.waiting.first.map { "#\($0.id): \($0.reason)" }
               ?? "Фабрика")

        menu.removeAllItems()
        menu.addItem(disabled(headline()))
        // Карточку показываем по факту работы, а не только своего процесса, и имя
        // добираем из потока: в первые секунды прогона фабрика знает лишь номер,
        // а человек, ткнувший «сделать следующий шаг», хочет видеть, что взяли его
        // задачу, а не безличное «смотрю доску».
        if runIsAlive, let id = status.cardID {
            let name = status.cardTitle?.isEmpty == false
                ? status.cardTitle!
                : (status.flow.first { $0.id == id }?.title ?? "")
            let mark = status.returning ? "↩︎ " : ""
            menu.addItem(disabled("   \(mark)#\(id) \(truncate(name, 46))"))
        }
        // Что агент делает прямо сейчас. Ради этой строки всё и затевалось: без неё
        // между «пошёл работать» и «вернулся» проходило до получаса полной тишины.
        if runIsAlive, let action = status.agentAction {
            var line = "   ⚙ \(truncate(action, 40))"
            if status.agentSteps > 0 { line += " · шаг \(status.agentSteps)" }
            let item = disabled(line)
            item.toolTip = silence().map { "последнее движение \(Int($0)) с назад" }
            menu.addItem(item)
        }
        if let error = lastError {
            menu.addItem(disabled("   ⚠️ \(truncate(error, 50))"))
        }
        // Протухшая авторизация — единственная поломка, которая валит фабрику целиком:
        // ни одна карточка не поедет, пока человек не войдёт заново. Раньше это было
        // невидимо — карточки просто уезжали в «Упало» с «агент не уложился», и неделю
        // никто не понимал почему. Теперь причина написана и чинится отсюда же.
        if needsLogin {
            let item = action("⚠️ claude не авторизован — войти", #selector(loginClicked))
            item.toolTip = "Откроется терминал с `claude auth login`. "
                + "Пока не войдёшь, ни одна карточка не поедет."
            menu.addItem(item)
        }
        if runLooksStuck {
            let item = action("⚠️ Прогон висит \(elapsed() ?? "долго") — остановить",
                             #selector(stopClicked))
            item.toolTip = "Ни один шаг столько не занимает. Чаще всего зависает сеть "
                + "в git push; лог: logs/run.log"
            menu.addItem(item)
        }
        // Всё, чего ждут от человека, — одной секцией и ссылками на сами карточки.
        // Голый счётчик «ждёт ответа: N» открывал доску, и карточки приходилось
        // искать глазами; теперь каждая строка ведёт прямо в свою.
        if !status.waiting.isEmpty {
            menu.addItem(disabled("Ждут тебя: \(status.waiting.count)"))
            for card in status.waiting {
                let item = NSMenuItem(
                    title: "   \(card.icon) #\(card.id) \(truncate(card.title, 38))",
                    action: nil, keyEquivalent: "")
                item.submenu = cardMenu(id: card.id, url: card.url, epic: card.isEpic)
                item.toolTip = card.reason
                menu.addItem(item)
                if !card.reason.isEmpty {
                    menu.addItem(disabled("        \(truncate(card.reason, 54))"))
                }
                for ask in card.asks {
                    menu.addItem(disabled("        • \(truncate(ask, 54))"))
                }
            }
        }



        // Весь фронт работ: что пишется, что ждёт ревьювера, что уехало к человеку.
        // Раньше в меню была видна одна карточка текущего прогона, и то пока он идёт.
        if !status.flow.isEmpty {
            menu.addItem(disabled("В потоке: \(status.flow.count)"))
            for card in status.flow {
                let item = NSMenuItem(
                    title: "   \(card.icon) #\(card.id) \(truncate(card.title, 36))",
                    action: nil, keyEquivalent: "")
                // Две разные вещи, и путать их нельзя: где карточка стоит и что
                // фабрика с ней сделает. Пока это была одна строка, в меню висело
                // «следующий шаг: сабтаски в работе» — положение, выданное за работу.
                let active = runIsAlive && status.cardID == card.id
                let now = active
                    ? "\(card.state)" + (elapsed().map { ", \($0)" } ?? "")
                    : card.state
                item.toolTip = card.next.isEmpty ? now : "\(now) → дальше: \(card.next)"
                item.submenu = cardMenu(id: card.id, url: card.url, epic: card.isEpic)
                menu.addItem(item)
                menu.addItem(disabled("        сейчас: \(truncate(now, 46))"))
                if !card.next.isEmpty {
                    menu.addItem(disabled("        дальше: \(truncate(card.next, 46))"))
                }
            }
        }

        if status.inboxPending > 0 {
            let item = action("В инбоксе не разобрано: \(status.inboxPending)",
                              #selector(openBoard))
            item.toolTip = "Карточки инбокса, до которых разведка ещё не дошла"
            menu.addItem(item)
        }

        // Ночные карточки днём молча пропускаются. Без этой строки человек решил бы,
        // что фабрика их потеряла, и полез бы разбираться.
        if status.nightWaiting > 0 {
            let item = action("Ждут ночи: \(status.nightWaiting)", #selector(openBoard))
            item.toolTip = "Карточки с ночным тегом — фабрика возьмёт их в отведённое окно"
            menu.addItem(item)
        }

        menu.addItem(.separator())
        if running {
            menu.addItem(action("Остановить прогон", #selector(stopClicked)))
        }
        // Кнопки видны всегда. Раньше во время прогона они пропадали, а прогоны идут
        // по десять минут каждый час — человек открывал меню и не находил кнопки,
        // решая, что её просто нет. Пока прогон идёт, они неактивны.
        do {
            let check = action("Проверить доску сейчас", #selector(runClicked))
            let inbox = action("Разобрать инбокс сейчас", #selector(inboxClicked))
            let epics = action("Продвинуть эпики сейчас", #selector(epicsClicked))
            check.toolTip = "Ревью и работа по карточкам доски"
            epics.toolTip = "Только фаза эпиков: критерии, спека, декомпозиция"
            for item in [check, inbox, epics] {
                item.isEnabled = !runIsAlive
                if runIsAlive { item.toolTip = "Идёт прогон — дождись или останови его" }
                menu.addItem(item)
            }
        }

        // Три расписания, и каждое ходит только за своим. Раньше «Расписание» тянуло
        // за собой ещё и инбокс с эпиками, так что реже сделать эпики, не трогая
        // доску, было нельзя — а шаг эпика стоит несколько долларов.
        menu.addItem(intervalMenu(title: "Доска: расписание", choices: intervalChoices,
                                  current: intervalMinutes, next: nextRun,
                                  selector: #selector(intervalClicked(_:)),
                                  hint: "Только доска: ревью и работа по карточкам"))
        menu.addItem(intervalMenu(title: "Инбокс: расписание", choices: inboxIntervalChoices,
                                  current: inboxIntervalMinutes, next: nextInboxRun,
                                  selector: #selector(inboxIntervalClicked(_:)),
                                  hint: "Только инбокс: посмотреть новые карточки и отписаться"))
        menu.addItem(intervalMenu(title: "Эпики: расписание", choices: epicsIntervalChoices,
                                  current: epicsIntervalMinutes, next: nextEpicsRun,
                                  selector: #selector(epicsIntervalClicked(_:)),
                                  hint: "Только эпики: критерии, спека, ревью спеки, "
                                      + "декомпозиция. Чаще всего это дешёвая проверка: "
                                      + "агент идёт работать, лишь когда фаза сменилась"))

        menu.addItem(.separator())
        if let outcome = status.lastOutcome, let id = status.lastCardID {
            var line = "Последняя: #\(id) → \(outcome)"
            if let cost = status.lastCost { line += String(format: " (~$%.2f)", cost) }
            let item = action(line, #selector(openLast))
            item.toolTip = status.lastTitle
            menu.addItem(item)
        }
        menu.addItem(action("Открыть доску", #selector(openBoard)))
        menu.addItem(action("Показать лог", #selector(openLog)))
        menu.addItem(authMenu())

        menu.addItem(.separator())
        menu.addItem(action("Выйти", #selector(quitClicked)))
    }

    /// Подменю авторизации claude. Лежит внизу рядом с логом и доступно всегда:
    /// протухшая сессия — не редкость, а раз в сутки, и лазить за этим в терминал
    /// руками человек не должен.
    private func authMenu() -> NSMenuItem {
        let head = NSMenuItem(title: "Авторизация клода", action: nil, keyEquivalent: "")
        let submenu = NSMenu()

        let login = NSMenuItem(title: "Войти заново",
                               action: #selector(loginClicked), keyEquivalent: "")
        login.target = self
        login.toolTip = "claude auth login в терминале: откроется браузер"
        submenu.addItem(login)

        // Долгоживущий токен — единственный способ не возвращаться сюда каждые сутки.
        let token = NSMenuItem(title: "Завести долгоживущий токен…",
                               action: #selector(tokenClicked), keyEquivalent: "")
        token.target = self
        token.toolTip = "claude setup-token в терминале. Выданный токен положи в "
            + "~/.claude/.env строкой CLAUDE_CODE_OAUTH_TOKEN=… — фабрика подхватит его "
            + "сама, и сессия перестанет протухать"
        submenu.addItem(token)

        head.submenu = submenu
        if needsLogin {
            head.toolTip = "Сейчас не авторизован — фабрика стоит"
        } else if let expires = status.auth?.expires {
            head.toolTip = status.auth!.stale
                ? "Сессия истекла в \(clock(expires)) — claude мог продлить её сам"
                : "Сессия до \(clock(expires))"
        } else {
            head.toolTip = "Про авторизацию пока ничего не известно"
        }
        return head
    }

    /// Подменю с интервалами: галочка на текущем, время следующего запуска — в подсказке.
    private func intervalMenu(title: String, choices: [(title: String, minutes: Int)],
                              current: Int, next: Date?, selector: Selector,
                              hint: String) -> NSMenuItem {
        let head = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        let submenu = NSMenu()
        for choice in choices {
            let item = NSMenuItem(title: choice.title, action: selector, keyEquivalent: "")
            item.target = self
            item.tag = choice.minutes
            item.state = choice.minutes == current ? .on : .off
            submenu.addItem(item)
        }
        head.submenu = submenu
        head.toolTip = current > 0 && next != nil
            ? "\(hint). Следующая в \(clock(next!))"
            : "\(hint). Выключено"
        return head
    }

    private func clock(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm"
        return formatter.string(from: date)
    }

    private func headline() -> String {
        if runIsAlive {
            let phase = status.phase ?? "запускаюсь"
            let age = elapsed().map { ", \($0)" } ?? ""
            return runLooksStuck
                ? "Похоже, завис: \(phase)\(age)"
                : "Работает: \(phase)\(age)"
        }
        // Прогон кончился, а карточка в статусе осталась — значит его оборвали
        // на полушаге. Молчать нельзя: человек нажал кнопку и ждёт результата.
        if status.cardID != nil, status.phase != nil {
            return "Прогон оборвался на #\(status.cardID ?? 0)"
        }
        // Прогон не идёт. Если по эпику ждут ответа — говорим об этом, а не про
        // расписание: иначе выходит, что фабрика «чем-то занята», хотя она стоит.
        if let first = status.waiting.first {
            if status.waiting.count > 1 { return "Ждёт тебя: \(status.waiting.count)" }
            return first.isEpic
                ? "Ждёт твоего ответа по эпику #\(first.id)"
                : "Ждёт твоего ответа по #\(first.id)"
        }
        guard intervalMinutes > 0 else { return "Расписание выключено" }
        guard let next = nextRun else { return "Ждёт" }
        if !status.flow.isEmpty {
            return "В потоке \(status.flow.count), проверка в \(clock(next))"
        }
        return "Ждёт, следующая проверка в \(clock(next))"
    }

    // MARK: - действия

    @objc private func runClicked() { startRun(manual: true, mode: .board) }
    @objc private func inboxClicked() { startRun(manual: true, mode: .inbox) }
    @objc private func epicsClicked() { startRun(manual: true, mode: .epics) }
    @objc private func stopClicked() { stopRun(); redraw() }
    @objc private func quitClicked() { NSApp.terminate(nil) }

    @objc private func intervalClicked(_ sender: NSMenuItem) {
        intervalMinutes = sender.tag
        rescheduleTimer()
        redraw()
    }

    @objc private func inboxIntervalClicked(_ sender: NSMenuItem) {
        inboxIntervalMinutes = sender.tag
        rescheduleInboxTimer()
        redraw()
    }

    @objc private func epicsIntervalClicked(_ sender: NSMenuItem) {
        epicsIntervalMinutes = sender.tag
        rescheduleEpicsTimer()
        redraw()
    }

    @objc private func openCardClicked(_ sender: NSMenuItem) {
        if let url = sender.representedObject as? String { open(url) } else { openBoard() }
    }

    /// Продвинуть одну конкретную карточку, не дожидаясь расписания и не гоняя весь
    /// прогон. Общая кнопка «Продвинуть эпики» берёт все эпики подряд, а тут человек
    /// показывает пальцем на ту задачу, которая его сейчас интересует.
    @objc private func pushCardClicked(_ sender: NSMenuItem) {
        guard let target = sender.representedObject as? [String: Any],
              let id = target["id"] as? Int else { return }
        let isEpic = (target["epic"] as? Bool) ?? false
        startRun(manual: true, mode: .one(id: id, epic: isEpic))
    }

    /// Меню действий для одной карточки: открыть или продвинуть.
    private func cardMenu(id: Int, url: String?, epic: Bool) -> NSMenu {
        let submenu = NSMenu()
        let openItem = action("Открыть карточку", #selector(openCardClicked))
        openItem.representedObject = url
        submenu.addItem(openItem)

        let push = action(epic ? "Сделать следующий шаг по эпику"
                               : "Сделать следующий шаг", #selector(pushCardClicked))
        push.representedObject = ["id": id, "epic": epic] as [String: Any]
        push.isEnabled = !runIsAlive
        push.toolTip = runIsAlive
            ? "Идёт прогон — дождись или останови его"
            : "Запустить фабрику только по этой карточке"
        submenu.addItem(push)
        return submenu
    }

    @objc private func openBoard() {
        if let boardURL { open(boardURL) }
    }

    @objc private func openLast() {
        if let pr = status.lastPR, pr.hasPrefix("http") { open(pr) }
        else if let url = status.lastURL, url.hasPrefix("http") { open(url) }
        else if let url = status.cardURL { open(url) }
        else { openBoard() }
    }

    @objc private func openLog() {
        NSWorkspace.shared.open(root.appendingPathComponent("logs/run.log"))
    }

    @objc private func loginClicked() { inTerminal("login", "claude auth login") }
    @objc private func tokenClicked() { inTerminal("setup-token", "claude setup-token") }

    /// Выполнить команду в настоящем терминале.
    ///
    /// Именно в терминале, а не через Process: вход в claude интерактивный — он
    /// открывает браузер и ждёт, что человек вернётся. Делаем это .command-файлом,
    /// который открывает Finder: так не нужны Apple Events и разрешение «управлять
    /// Терминалом», которое иначе спросят при первом же клике.
    ///
    /// Шебанг с `-l` не для красоты: без логин-оболочки в PATH нет ни homebrew,
    /// ни того, куда человек поставил claude.
    private func inTerminal(_ name: String, _ command: String) {
        let file = root.appendingPathComponent("state/\(name).command")
        let script = """
        #!/bin/zsh -l
        # Файл сделан «Фабрикой» — можно удалять.
        \(command)
        echo
        echo "Готово. Окно можно закрыть."
        """
        do {
            try FileManager.default.createDirectory(
                at: root.appendingPathComponent("state"),
                withIntermediateDirectories: true)
            try script.write(to: file, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o755],
                                                  ofItemAtPath: file.path)
            NSWorkspace.shared.open(file)
        } catch {
            lastError = "не смог открыть терминал: \(error.localizedDescription)"
            redraw()
        }
    }

    // MARK: - мелочи

    private func open(_ string: String) {
        if let url = URL(string: string) { NSWorkspace.shared.open(url) }
    }

    private func shell(_ command: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = ["-c", command]
        try? process.run()
        process.waitUntilExit()
    }

    private func shellQuote(_ path: String) -> String {
        "'" + path.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    private func truncate(_ text: String, _ limit: Int) -> String {
        text.count <= limit ? text : String(text.prefix(limit - 1)) + "…"
    }

    private func disabled(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    private func action(_ title: String, _ selector: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: selector, keyEquivalent: "")
        item.target = self
        return item
    }
}

extension Fabrica: NSMenuDelegate {
    // состояние могло измениться, пока меню было закрыто
    func menuWillOpen(_ menu: NSMenu) {
        readStatusFile()
        redraw()
    }
}

@main
enum Main {
    // делегат держим статически: NSApplication.delegate — слабая ссылка
    static let delegate = Fabrica()

    static func main() {
        let app = NSApplication.shared
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
    }
}
