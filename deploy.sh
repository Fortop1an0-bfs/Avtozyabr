#!/usr/bin/env bash
# deploy.sh — запускается на сервере: bash deploy.sh
set -euo pipefail

echo "=== Avtozyabr deploy ==="

# 1. Установить Docker если нет
if ! command -v docker &>/dev/null; then
    echo ">>> Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
fi

# 2. Клонировать / обновить репозиторий
REPO="https://github.com/Fortop1an0-bfs/Avtozyabr.git"
DIR="/opt/avtozyabr"

if [ -d "$DIR/.git" ]; then
    echo ">>> Pulling latest..."
    git -C "$DIR" pull origin main
else
    echo ">>> Cloning repo..."
    git clone "$REPO" "$DIR"
fi

cd "$DIR"

# 3. Создать .env если не существует
if [ ! -f .env ]; then
    echo ">>> Creating .env..."
    cp .env.example .env
fi

# 4. Создать директорию для данных (cookies и пр.)
mkdir -p data

echo ">>> .env ready. Запускаем..."

# 5. Поднять сервисы
docker compose pull postgres 2>/dev/null || true
docker compose up -d --build

echo ""
echo "=== Готово ==="
echo "Логи: docker compose logs -f bot"
echo "Статус: docker compose ps"
