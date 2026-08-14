# AI Dev Team — 자율 AI 개발팀 오케스트레이터

대표님의 지시 한 마디로 PM → Design → Architect → Implement(OpenDevin) → QA → Release 파이프라인이 자동으로 실행됩니다.

## 아키텍처

```
[Web UI :3000]  ←WebSocket→  [Orchestrator :8000]  ←Redis Streams→  [Agents]
                                                                      ├── PM
                                                                      ├── Designer
                                                                      ├── Architect
                                                                      ├── OpenDevin (Implement) :3001
                                                                      ├── QA
                                                                      ├── AutoTest
                                                                      └── Release
```

## 빠른 시작

### 1. 사전 요구사항

- Docker Desktop (Mac: https://www.docker.com/products/docker-desktop/)
- Anthropic API 키

### 2. 설정

```bash
# 저장소 클론
git clone <your-repo> ai-dev-team
cd ai-dev-team

# 환경변수 설정
cp .env.example .env
# .env 파일을 열어 ANTHROPIC_API_KEY 입력
```

### 3. 실행

```bash
docker compose up --build
```

잠시 후 브라우저에서:
- **Web UI**: http://localhost:3000
- **OpenDevin UI** (디버깅): http://localhost:3001
- **API 문서**: http://localhost:8000/docs

### 4. 사용

Web UI 채팅창에 지시사항 입력:
```
부상자 운동앱 로그인 화면과 온보딩 플로우 만들어줘
```

파이프라인이 자동 시작됩니다. Release 스테이지 전 승인 버튼이 나타납니다.

---

## 파일 구조

```
ai-dev-team/
├── docker-compose.yml          # 전체 서비스 정의
├── .env.example                # 환경변수 템플릿
│
├── orchestrator/               # FastAPI 오케스트레이터
│   ├── main.py                 # WebSocket + REST API
│   ├── queue/redis_client.py   # Redis Streams 래퍼
│   └── workflows/pipeline.py  # 파이프라인 스테이지 정의
│
├── agents/base/                # 모든 에이전트 공유 이미지
│   └── agent.py               # 역할별 Claude API 호출
│
└── web/                        # Next.js Web UI
    └── app/page.tsx            # 채팅 + 에이전트 대시보드
```

---

## 파이프라인 스테이지

| 스테이지 | 에이전트 | 설명 |
|---------|--------|------|
| planning | PM | PRD 작성, 마일스톤 정의 |
| design | Designer + Architect | UX 스펙 + 시스템 설계 (병렬) |
| implement | OpenDevin | 실제 코드 작성 |
| qa | QA | 테스트 케이스, 버그 리포트 |
| autotest | AutoTest | 자동 테스트 실행 |
| **[승인]** | 대표님 | Web UI에서 클릭 |
| release | Release | TestFlight / Play Beta 배포 |

---

## 산출물 위치

각 에이전트의 산출물은 Docker volume `shared-workspace`에 저장됩니다.

```bash
# 산출물 확인
docker exec -it ai-dev-team-orchestrator-1 ls /workspace/<project_id>/
```

---

## 커스터마이징

### 에이전트 프롬프트 수정

`agents/base/agent.py`의 `ROLE_PROMPTS` 딕셔너리를 수정하세요.

### 파이프라인 순서 변경

`orchestrator/workflows/pipeline.py`의 `PIPELINE_DEFINITION`을 수정하세요.

### 승인 단계 추가/제거

`Stage(..., requires_approval=True)`로 승인 필요 스테이지를 지정합니다.

---

## 로그 확인

```bash
# 전체 로그
docker compose logs -f

# 특정 서비스
docker compose logs -f agent-pm
docker compose logs -f orchestrator
```
