#!/usr/bin/env bash
# Рендерит кадры человечка в PNG, чтобы посмотреть, что получилось.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p menubar/.build
swiftc -O -o menubar/.build/preview menubar/Sprites.swift menubar/preview.swift
menubar/.build/preview "${1:-menubar/.build/preview.png}"
