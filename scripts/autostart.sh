#!/bin/zsh
# 로그인 시 실행: Docker Desktop이 뜰 때까지 대기 후 전체 스택 기동
set -e

PROJECT_DIR="/Volumes/External/Dev/Development/ai-dev-team"
LOG_FILE="$PROJECT_DIR/scripts/autostart.log"
DOCKER_BIN="/usr/local/bin/docker"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] autostart 시작" >> "$LOG_FILE"

open -ga Docker

# Docker 데몬이 응답할 때까지 최대 5분 대기
for i in $(seq 1 60); do
  if "$DOCKER_BIN" info >/dev/null 2>&1; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Docker 데몬 준비됨 (${i}번째 시도)" >> "$LOG_FILE"
    break
  fi
  sleep 5
done

if ! "$DOCKER_BIN" info >/dev/null 2>&1; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Docker 데몬이 5분 내에 준비되지 않아 중단" >> "$LOG_FILE"
  exit 1
fi

cd "$PROJECT_DIR"
"$DOCKER_BIN" compose up -d >> "$LOG_FILE" 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] docker compose up -d 완료" >> "$LOG_FILE"
