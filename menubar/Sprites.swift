// Человечек для меню-бара: работает, спит, вопрошает, паникует.
//
// Рисуется вектором в системе координат 22×18 и масштабируется под нужный размер,
// поэтому на retina выглядит гладко (пиксель-арт в 18pt превращался в кляксу).
// Картинка шаблонная (isTemplate), так что система сама красит её под светлое
// и тёмное меню и подсвечивает при клике.
//
// Иконка статичная: одна поза на состояние, ничего не дёргается.
// Посмотреть все позы разом: ./menubar/preview.sh

import AppKit

enum Mood {
    case working   // агент работает — человечек стучит по клавиатуре
    case sleeping  // делать нечего — спит, над головой «z»
    case asking    // ждёт ответа на вопрос — над головой «?»
    case alert     // прошлый прогон свалился — «!»
}

enum Sprites {

    /// Холст, в котором нарисован человечек. Ширина больше высоты: слева фигурка,
    /// справа место под значок настроения, иначе они наползают друг на друга.
    static let canvas = CGSize(width: 22, height: 18)

    // MARK: - сборка картинки

    static func image(_ mood: Mood, height: CGFloat = 18) -> NSImage {
        let scale = height / canvas.height
        let size = NSSize(width: canvas.width * scale, height: height)

        let image = NSImage(size: size, flipped: false) { _ in
            let context = NSGraphicsContext.current
            context?.saveGraphicsState()
            let transform = NSAffineTransform()
            transform.scale(by: scale)
            transform.concat()

            draw(mood: mood)

            context?.restoreGraphicsState()
            return true
        }
        image.isTemplate = true
        return image
    }

    // MARK: - собственно рисование (координаты сверху вниз, как удобно человеку)

    private static func draw(mood: Mood) {
        NSColor.black.setFill()

        switch mood {
        case .working:  drawWorking()
        case .sleeping: drawSleeping()
        case .asking:   drawFigure(); glyph("?", size: 12, x: 13.2, top: 2.4, alpha: 1)
        case .alert:    drawFigure(); glyph("!", size: 12, x: 13.2, top: 2.4, alpha: 1)
        }
    }

    /// Печатает: одна рука занесена над клавиатурой, другая уже опустилась.
    private static func drawWorking() {
        drawFigure(arms: false)
        arm(x: 0.4, top: 11.2)
        arm(x: 10.1, top: 12.8)

        // стол во всю ширину — заодно уравновешивает иконку там, где нет значка справа
        rounded(x: -1, top: 15.6, width: canvas.width + 2, height: 1.7, radius: 0.7)
    }

    /// Спит: глаза закрыты, голова клонится вбок, над головой «z».
    private static func drawSleeping() {
        drawFigure(bob: 0.4, tilt: -0.09, eyes: .closed)
        glyph("z", size: 7.5, x: 13.4, top: 5.5, alpha: 1)
        glyph("z", size: 5.5, x: 16.8, top: 2.6, alpha: 0.75)
    }

    private enum Eyes { case open, closed }

    /// Голова, плечи и (по желанию) руки по бокам.
    private static func drawFigure(bob: CGFloat = 0,
                                   tilt: CGFloat = 0,
                                   eyes: Eyes = .open,
                                   arms: Bool = true) {
        let context = NSGraphicsContext.current
        context?.saveGraphicsState()

        if tilt != 0 {
            // наклоняем вокруг шеи, а не вокруг угла холста
            let pivot = NSPoint(x: 6, y: canvas.height - 9)
            let t = NSAffineTransform()
            t.translateX(by: pivot.x, yBy: pivot.y)
            t.rotate(byRadians: tilt)
            t.translateX(by: -pivot.x, yBy: -pivot.y)
            t.concat()
        }

        // туловище (нижний край уходит за холст — получается погрудный портрет)
        rounded(x: 2.2, top: 10.6 + bob, width: 7.6, height: 8, radius: 2.6)
        // голова
        rounded(x: 1, top: 1.2 + bob, width: 10, height: 8.6, radius: 3.4)

        if arms {
            arm(x: 0.4, top: 11.6 + bob)
            arm(x: 10.1, top: 11.6 + bob)
        }

        // глаза — дырки в силуэте, так читаемее любых точек поверх
        context?.compositingOperation = .destinationOut
        NSColor.black.setFill()
        switch eyes {
        case .open:
            circle(centerX: 3.9, centerTop: 5.4 + bob, diameter: 2.1)
            circle(centerX: 8.1, centerTop: 5.4 + bob, diameter: 2.1)
        case .closed:
            rounded(x: 2.85, top: 5.0 + bob, width: 2.1, height: 0.95, radius: 0.47)
            rounded(x: 7.05, top: 5.0 + bob, width: 2.1, height: 0.95, radius: 0.47)
        }
        context?.compositingOperation = .sourceOver
        NSColor.black.setFill()

        context?.restoreGraphicsState()
    }

    private static func arm(x: CGFloat, top: CGFloat) {
        rounded(x: x, top: top, width: 1.5, height: 4.4, radius: 0.75)
    }

    // MARK: - примитивы

    private static func rounded(x: CGFloat, top: CGFloat,
                                width: CGFloat, height: CGFloat, radius: CGFloat) {
        let rect = NSRect(x: x, y: canvas.height - top - height, width: width, height: height)
        NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius).fill()
    }

    private static func circle(centerX: CGFloat, centerTop: CGFloat, diameter: CGFloat) {
        let rect = NSRect(x: centerX - diameter / 2,
                          y: canvas.height - centerTop - diameter / 2,
                          width: diameter, height: diameter)
        NSBezierPath(ovalIn: rect).fill()
    }

    private static func glyph(_ text: String, size: CGFloat,
                              x: CGFloat, top: CGFloat, alpha: CGFloat) {
        guard alpha > 0.05 else { return }
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: size, weight: .heavy),
            .foregroundColor: NSColor.black.withAlphaComponent(min(1, max(0, alpha))),
        ]
        let string = NSAttributedString(string: text, attributes: attributes)
        let bounds = string.size()
        string.draw(at: NSPoint(x: x, y: canvas.height - top - bounds.height))
    }
}
