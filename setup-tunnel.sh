#!/bin/bash
# AI Team Manager — Cloudflare 고정 터널 설정
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo ""
echo -e "${CYAN}AI Team Manager — 고정 URL 설정${NC}"
echo "================================="

# 1. cloudflared 설치
if ! command -v cloudflared &>/dev/null; then
  echo -e "${YELLOW}cloudflared 설치 중...${NC}"
  brew install cloudflared
else
  echo -e "${GREEN}✓ cloudflared 이미 설치됨${NC}"
fi

# 2. Cloudflare 로그인 (브라우저 자동 열림)
echo ""
echo -e "${YELLOW}Cloudflare 로그인 (브라우저가 열립니다)...${NC}"
cloudflared tunnel login

# 3. 터널 생성 (이미 있으면 스킵)
TUNNEL_NAME="ai-team-manager"
if cloudflared tunnel list 2>/dev/null | grep -q "$TUNNEL_NAME"; then
  echo -e "${GREEN}✓ 터널 '$TUNNEL_NAME' 이미 존재${NC}"
else
  echo -e "${YELLOW}터널 생성 중...${NC}"
  cloudflared tunnel create "$TUNNEL_NAME"
fi

# 4. 터널 토큰 가져오기
echo ""
echo -e "${YELLOW}터널 토큰 가져오는 중...${NC}"
TOKEN=$(cloudflared tunnel token "$TUNNEL_NAME" 2>/dev/null)

if [ -z "$TOKEN" ]; then
  echo "토큰 가져오기 실패. 수동으로 실행해주세요:"
  echo "  cloudflared tunnel token $TUNNEL_NAME"
  exit 1
fi

# 5. .env 업데이트
ENV_FILE="$(dirname "$0")/.env"
if grep -q "CLOUDFLARE_TUNNEL_TOKEN" "$ENV_FILE"; then
  # 기존 값 교체
  sed -i '' "s|CLOUDFLARE_TUNNEL_TOKEN=.*|CLOUDFLARE_TUNNEL_TOKEN=$TOKEN|" "$ENV_FILE"
else
  echo "" >> "$ENV_FILE"
  echo "CLOUDFLARE_TUNNEL_TOKEN=$TOKEN" >> "$ENV_FILE"
fi
echo -e "${GREEN}✓ .env 업데이트 완료${NC}"

# 6. 터널 URL 확인
TUNNEL_ID=$(cloudflared tunnel list 2>/dev/null | grep "$TUNNEL_NAME" | awk '{print $1}')
FIXED_URL="https://${TUNNEL_ID}.cfargotunnel.com"

echo ""
echo -e "${GREEN}✅ 설정 완료!${NC}"
echo ""
echo "고정 URL: ${CYAN}$FIXED_URL${NC}"
echo ""
echo "도메인 연결하려면 (선택):"
echo "  cloudflared tunnel route dns $TUNNEL_NAME yourdomain.com"
echo ""
echo "적용하려면:"
echo "  docker compose restart tunnel-web"
echo ""

# 7. 바로 재시작할지 물어보기
read -p "지금 바로 적용할까요? (y/n) " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
  cd "$(dirname "$0")"
  docker compose restart tunnel-web
  echo -e "${GREEN}✓ 터널 재시작 완료${NC}"
  echo "잠시 후 ${CYAN}$FIXED_URL${NC} 으로 접속하세요."
fi
