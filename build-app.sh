#!/usr/bin/env bash
# Собирает менюбар-приложение «Фабрика.app» рядом с этим скриптом.
#
#   ./build-app.sh          # собрать
#   open Фабрика.app        # запустить (иконка появится в правой части меню-бара)
#
# Путь к папке фабрики зашивается в бинарник при сборке, так что приложение
# можно потом утащить хоть в /Applications.
set -euo pipefail
cd "$(dirname "$0")"

ROOT="$(pwd)"
APP="Фабрика.app"
BUILD="menubar/.build"

command -v swiftc >/dev/null || { echo "нужен swiftc (Xcode Command Line Tools)" >&2; exit 1; }

# сносим только собранное приложение: в $BUILD лежит ещё и превью человечка
rm -rf "$APP"
mkdir -p "$BUILD" "$APP/Contents/MacOS"

# путь к фабрике — отдельным сгенерированным файлом, чтобы не городить #if в исходнике
printf 'let bakedRoot = "%s"\n' "${ROOT//\"/\\\"}" > "$BUILD/Root.swift"

swiftc -O -parse-as-library \
    -o "$APP/Contents/MacOS/Fabrica" \
    menubar/Fabrica.swift menubar/Sprites.swift "$BUILD/Root.swift"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Фабрика</string>
    <key>CFBundleDisplayName</key><string>Фабрика</string>
    <key>CFBundleIdentifier</key><string>local.kaiten-fabrica</string>
    <key>CFBundleExecutable</key><string>Fabrica</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <!-- только меню-бар: без иконки в доке и без окна -->
    <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

# без подписи Gatekeeper ругается при первом запуске — подписываем локально
codesign --force --sign - "$APP" 2>/dev/null || true

echo "собрано: $ROOT/$APP"
echo "запустить: open '$APP'"

# Установка в /Applications: с флагом --install, либо автоматически, если приложение
# там уже стоит — иначе после пересборки в /Applications осталась бы старая версия.
INSTALLED="/Applications/$APP"
# Label из ~/Library/LaunchAgents/<label>.plist, если автозапуск настроен
AGENT="${FABRICA_AGENT:-local.kaiten-fabrica}"
[[ -f "$HOME/Library/LaunchAgents/ru.dodo.kaiten-fabrica.plist" ]] && AGENT="ru.dodo.kaiten-fabrica"
if [[ "${1:-}" == "--install" || -d "$INSTALLED" ]]; then
    # приложение может быть запущено — сначала гасим, иначе перезапись даст битый бандл
    pkill -f "$APP/Contents/MacOS/Fabrica" 2>/dev/null || true
    sleep 1
    rm -rf "$INSTALLED"
    cp -R "$APP" "$INSTALLED"
    # копию из папки проекта убираем: две одинаковые иконки в меню-баре — плохая идея
    rm -rf "$APP"
    echo "установлено: $INSTALLED"

    # Приложение мы погасили выше, и само оно не вернётся: в LaunchAgent намеренно нет
    # KeepAlive. Без этого шага пересборка тихо выключала фабрику до следующего входа
    # в систему — а заметить это можно только по пропавшей иконке в меню-баре.
    PLIST="$HOME/Library/LaunchAgents/$AGENT.plist"
    if [[ -f "$PLIST" ]]; then
        # Именно bootout+bootstrap, а не kickstart: launchd запоминает подпись сервиса
        # с момента регистрации, и после пересборки запускает бандл с новой подписью
        # против старой записи — процесс тут же умирает с OS_REASON_CODESIGNING.
        # Перерегистрация обновляет запись, kickstart — нет.
        launchctl bootout "gui/$(id -u)/$AGENT" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null && \
            echo "перезапущено через LaunchAgent"
    else
        open "$INSTALLED" && echo "запущено"
    fi
fi
