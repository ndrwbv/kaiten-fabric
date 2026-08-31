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

// Разведка инбокса дешёвая и короткая, поэтому ходит чаще полного прогона: карточку
// закинули — через несколько минут в ней уже лежит комментарий.
let inboxIntervalChoices: [(title: String, minutes: Int)] = [
    ("Каждые 5 минут", 5),
    ("Каждые 10 минут", 10),
    ("Каждые 15 минут", 15),
    ("Каждые 30 минут", 30),
    ("Выключено", 0),
]

struct Status {
    var phase: String?
    var cardID: Int?
    var cardTitle: String?
    var cardURL: String?
    var returning = false
    var awaitingAnswer = 0
    var pid: Int32?
    var lastOutcome: String?
    var lastTitle: String?
    var lastCardID: Int?
    var lastPR: String?
    var lastURL: String?
    var lastCost: Double?
    var inboxPending = 0
    var epicsWaiting = 0
    var nightWaiting = 0
}

final class Fabrica: NSObject, NSApplicationDelegate {

    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    private let root: URL
    private let defaults = UserDefaults.standard

    private var scheduleTimer: Timer?
    private var inboxTimer: Timer?
    private var pollTimer: Timer?
    private var runner: Process?
    private var nextRun: Date?
    private var nextInboxRun: Date?
    // расписание попало в занятое время — прогон не теряем, а делаем сразу после текущего
    private var pendingRun = false
    private var pendingInboxRun = false
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
            self.startRun(manual: false)
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
            self.startRun(manual: false, inboxOnly: true)
        }
        RunLoop.main.add(timer, forMode: .common)
        inboxTimer = timer
    }

    // MARK: - запуск прогона

    private func startRun(manual: Bool, inboxOnly: Bool = false) {
        guard runner == nil else {
            if manual { NSSound.beep() }
            // по расписанию — не теряем: запустим сразу после текущего прогона
            else if inboxOnly { pendingInboxRun = true } else { pendingRun = true }
            return
        }
        lastError = nil
        // полный прогон сам заходит в инбокс, отдельная разведка после него не нужна
        if !inboxOnly { pendingInboxRun = false }

        let script = "cd \(shellQuote(root.path)) && ./run.sh"
            + (inboxOnly ? " --only-triage" : "")
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
                if proc.terminationStatus != 0 && proc.terminationReason != .uncaughtSignal {
                    self.lastError = "run.sh завершился с кодом \(proc.terminationStatus)"
                }
                self.readStatusFile()
                self.redraw()
                if self.pendingRun || self.pendingInboxRun {
                    let inboxOnly = !self.pendingRun
                    self.pendingRun = false
                    self.pendingInboxRun = false
                    DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
                        self.startRun(manual: false, inboxOnly: inboxOnly)
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
        s.pid = (json["pid"] as? NSNumber)?.int32Value
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
    private func currentMood() -> Mood {
        if runner != nil { return .working }
        if lastError != nil { return .alert }
        if status.awaitingAnswer > 0 { return .asking }
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
        statusItem.button?.toolTip = running
            ? "Фабрика работает: \(status.phase ?? "")"
            : (status.awaitingAnswer > 0
               ? "Агент ждёт ответа: \(status.awaitingAnswer)"
               : "Фабрика")

        menu.removeAllItems()
        menu.addItem(disabled(headline()))
        if running, let title = status.cardTitle, let id = status.cardID {
            let mark = status.returning ? "↩︎ " : ""
            menu.addItem(disabled("   \(mark)#\(id) \(truncate(title, 46))"))
        }
        if let error = lastError {
            menu.addItem(disabled("   ⚠️ \(truncate(error, 50))"))
        }
        if status.awaitingAnswer > 0 {
            let item = action("Ждёт твоего ответа: \(status.awaitingAnswer)", #selector(openBoard))
            item.toolTip = "Ответь комментарием в карточке — фабрика возьмёт её сама"
            menu.addItem(item)
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

        // Эпик в блокере не двигается сам и ничем о себе не напоминает: без этой
        // строки он молча висит, пока кто-нибудь случайно не откроет доску.
        if status.epicsWaiting > 0 {
            let item = action("Эпики ждут тебя: \(status.epicsWaiting)", #selector(openBoard))
            item.toolTip = "Сними блокер в карточке эпика — фабрика продолжит с того же места"
            menu.addItem(item)
        }

        menu.addItem(.separator())
        if running {
            menu.addItem(action("Остановить прогон", #selector(stopClicked)))
        } else {
            menu.addItem(action("Проверить доску сейчас", #selector(runClicked)))
            menu.addItem(action("Разобрать инбокс сейчас", #selector(inboxClicked)))
        }

        menu.addItem(intervalMenu(title: "Расписание", choices: intervalChoices,
                                  current: intervalMinutes, next: nextRun,
                                  selector: #selector(intervalClicked(_:)),
                                  hint: "Полный прогон: ревью, работа и разведка инбокса"))
        menu.addItem(intervalMenu(title: "Разведка инбокса", choices: inboxIntervalChoices,
                                  current: inboxIntervalMinutes, next: nextInboxRun,
                                  selector: #selector(inboxIntervalClicked(_:)),
                                  hint: "Только инбокс: посмотреть новые карточки и отписаться"))

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

        menu.addItem(.separator())
        menu.addItem(action("Выйти", #selector(quitClicked)))
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
        if runner != nil {
            return "Работает: \(status.phase ?? "запускаюсь")"
        }
        guard intervalMinutes > 0 else { return "Расписание выключено" }
        guard let next = nextRun else { return "Ждёт" }
        return "Ждёт, следующая проверка в \(clock(next))"
    }

    // MARK: - действия

    @objc private func runClicked() { startRun(manual: true) }
    @objc private func inboxClicked() { startRun(manual: true, inboxOnly: true) }
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
