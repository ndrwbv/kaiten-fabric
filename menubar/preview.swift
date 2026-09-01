// Рендерит все кадры человечка в один PNG, чтобы посмотреть на них глазами.
// Запуск: ./menubar/preview.sh

import AppKit

@main
enum Preview {
    static func main() {
        let zoom: CGFloat = 8            // во сколько раз увеличить
        let actual: CGFloat = 18         // реальная высота иконки в меню-баре
        let pad: CGFloat = 14
        let labelHeight: CGFloat = 18

        let moods: [(String, Mood)] = [
            ("working", .working),
            ("sleeping", .sleeping),
            ("asking", .asking),
            ("epicAsking", .epicAsking),
            ("alert", .alert),
        ]

        let bigH = actual * zoom
        let bigW = bigH * Sprites.canvas.width / Sprites.canvas.height

        let width = pad + bigW + pad + 90
        let height = pad + CGFloat(moods.count) * (bigH + labelHeight + pad)

        let out = NSImage(size: NSSize(width: width, height: height))
        out.lockFocus()

        NSColor.white.setFill()
        NSRect(x: 0, y: 0, width: width, height: height).fill()

        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedSystemFont(ofSize: 12, weight: .medium),
            .foregroundColor: NSColor.black,
        ]

        for (row, mood) in moods.enumerated() {
            let top = height - pad - CGFloat(row) * (bigH + labelHeight + pad)
            mood.0.draw(at: NSPoint(x: pad, y: top - labelHeight + 3), withAttributes: attrs)

            let y = top - labelHeight - bigH
            NSColor(white: 0.95, alpha: 1).setFill()
            NSRect(x: pad, y: y, width: bigW, height: bigH).fill()
            Sprites.image(mood.1, height: bigH)
                .draw(in: NSRect(x: pad, y: y, width: bigW, height: bigH))

            // натуральная величина: как это будет в меню-баре, светлое и тёмное
            let realW = actual * Sprites.canvas.width / Sprites.canvas.height
            let stripX = pad + bigW + pad
            for (i, background) in [NSColor.white, NSColor.black].enumerated() {
                let box = NSRect(x: stripX, y: top - labelHeight - bigH / 2 - 14 + CGFloat(i) * 28,
                                 width: realW + 12, height: 24)
                background.setFill()
                NSBezierPath(roundedRect: box, xRadius: 4, yRadius: 4).fill()

                let icon = Sprites.image(mood.1, height: actual)
                let tinted = NSImage(size: icon.size, flipped: false) { rect in
                    (i == 0 ? NSColor.black : NSColor.white).set()
                    rect.fill(using: .sourceOver)
                    icon.draw(in: rect, from: .zero, operation: .destinationIn, fraction: 1)
                    return true
                }
                tinted.draw(in: NSRect(x: box.minX + 6, y: box.minY + 3,
                                       width: realW, height: actual))
            }
        }

        out.unlockFocus()

        let path = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "preview.png"
        guard let tiff = out.tiffRepresentation,
              let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:])
        else { fatalError("не смог собрать PNG") }
        try! png.write(to: URL(fileURLWithPath: path))
        print("готово: \(path)")
    }
}
