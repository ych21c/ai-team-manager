#!/bin/bash
# AI Dev Team — 원클릭 설치 스크립트 (Mac)
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo ""
echo "🤖 AI Dev Team 설치 시작"
echo "========================="

# ── 1. Homebrew 확인 / 설치 ──────────────────────────────────────────
if ! command -v brew &>/dev/null; then
  echo -e "${YELLOW}Homebrew 설치 중...${NC}"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
else
  echo -e "${GREEN}✓ Homebrew 이미 설치됨${NC}"
fi

# ── 2. Docker Desktop 확인 / 설치 ───────────────────────────────────
if ! command -v docker &>/dev/null; then
  echo -e "${YELLOW}Docker Desktop 설치 중... (약 2분 소요)${NC}"

  # Apple Silicon / Intel 자동 감지
  ARCH=$(uname -m)
  if [ "$ARCH" = "arm64" ]; then
    brew install --cask docker
  else
    brew install --cask docker
  fi

  echo -e "${YELLOW}Docker Desktop을 실행해주세요 (Applications > Docker)${NC}"
  echo "실행 후 엔터를 누르세요..."
  read -r

  # Docker 데몬 대기
  echo "Docker 데몬 시작 대기 중..."
  until docker info &>/dev/null 2>&1; do
    sleep 2
    echo -n "."
  done
  echo ""
else
  echo -e "${GREEN}✓ Docker 이미 설치됨${NC}"
  # 데몬 실행 확인
  if ! docker info &>/dev/null 2>&1; then
    echo -e "${YELLOW}Docker Desktop을 실행해주세요 (Applications > Docker)${NC}"
    echo "실행 후 엔터를 누르세요..."
    read -r
    until docker info &>/dev/null 2>&1; do sleep 2; echo -n "."; done
    echo ""
  fi
fi

echo -e "${GREEN}✓ Docker 준비 완료${NC}"

# ── 3. .env 파일 설정 ────────────────────────────────────────────────
if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo -e "${YELLOW}ANTHROPIC_API_KEY를 입력하세요:${NC}"
  echo -n "  sk-ant-... > "
  read -r API_KEY

  if [ -n "$API_KEY" ]; then
    sed -i '' "s|sk-ant-xxxxxxxxxxxxxxxxxxxx|${API_KEY}|g" .env
    echo -e "${GREEN}✓ API 키 저장됨${NC}"
  else
    echo -e "${RED}API 키를 나중에 .env 파일에 직접 입력하세요${NC}"
  fi
else
  echo -e "${GREEN}✓ .env 이미 존재${NC}"
fi

# ── 4. Docker Compose 실행 ───────────────────────────────────────────
echo ""
echo "컨테이너 빌드 및 시작 중... (첫 실행은 3-5분 소요)"
docker compose up --build -d

echo ""
echo -e "${GREEN}✅ 실행 완료!${NC}"
echo ""
echo "접속 주소:"
echo "  로컬 Web UI   : http://localhost:3000"
echo "  로컬 API 문서  : http://localhost:8000/docs"
echo "  OpenDevin UI  : http://localhost:3001"
echo ""
echo "외부 접속 URL (Cloudflare Tunnel):"
echo "  잠시 후 자동으로 표시됩니다. 확인하려면:"
echo "  docker compose logs tunnel-web 2>&1 | grep trycloudflare"
echo ""
echo "로그 확인: docker compose logs -f"
echo "종료:      docker compose down"
