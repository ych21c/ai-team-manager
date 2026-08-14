# AI Team Manager — 프로젝트 현황

> 마지막 업데이트: 2026-06-27  
> 목적: 세션 간 연속성 유지용 작업 상태 정리

---

## 📁 프로젝트 구조

```
~/Development/ai-dev-team/
├── .env                              ← 모든 API 키 (아래 설정 현황 참조)
├── docker-compose.yml                ← 전체 서비스 정의
├── test_integrations.py              ← 통합 테스트 (9개)
├── PROJECT_STATUS.md                 ← 이 파일
│
├── orchestrator/
│   ├── main.py                       ← FastAPI 오케스트레이터 (포트 8000)
│   ├── atlassian_client.py           ← Jira + Confluence 연동
│   ├── team_spawner.py               ← Docker 에이전트 팀 스폰
│   ├── redis_queue/redis_client.py   ← Redis Streams 클라이언트
│   └── workflows/pipeline.py         ← 파이프라인 상태 머신
│
├── agents/base/
│   └── agent.py                      ← 모든 에이전트 공통 실행 엔진
│
├── web/
│   └── app/page.tsx                  ← Next.js Web UI (포트 3000)
│
└── url-notifier/                     ← ngrok URL 콘솔 출력
```

---

## ⚙️ .env 설정 현황

| 키 | 상태 | 값 |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ 설정됨 | `sk-ant-api03-...` (유료 크레딧 필요) |
| `LLM_MODEL` | ✅ | `claude-sonnet-4-6` |
| `GITHUB_TOKEN` | ✅ 설정됨 | `ghp_u4LD...` |
| `GITHUB_REPO` | ✅ | `ych21c` (owner만, 레포명은 프로젝트별 자동 생성) |
| `NGROK_AUTHTOKEN` | ✅ 설정됨 | `3EcLQ...` |
| `ATLASSIAN_EMAIL` | ✅ | `ych21c@gmail.com` |
| `ATLASSIAN_API_TOKEN` | ✅ 설정됨 | `ATATT3x...` |
| `ATLASSIAN_DOMAIN` | ✅ | `ych21c.atlassian.net` |
| `JIRA_PROJECT_KEY` | ✅ | `ATM` |
| `CONFLUENCE_SPACE_KEY` | ✅ | `ATM` |
| `POSTGRES_USER/PASSWORD/DB` | ✅ | `devteam/devteam/devteam` |
| `APPLE_ID` | ❌ 미설정 | `your@email.com` (플레이스홀더) |
| `GOOGLE_PLAY_JSON_KEY_PATH` | ❌ 미설정 | 플레이스홀더 |
| `SLACK_WEBHOOK_URL` | ❌ 미설정 | 플레이스홀더 |

---

## 🏗️ 아키텍처 요약

```
사용자 브라우저 / 모바일
       ↓ WebSocket + REST
   Web UI (Next.js :3000)
       ↓
  Orchestrator (FastAPI :8000)
       ↓ Redis Streams
  ┌────┬────────┬───────────┬────┬──────────┬─────────┐
  PM  Designer Architect  Impl   QA    AutoTest  Release
              (병렬)       (OpenDevin)
       ↓
  Jira Epic/Stories + Confluence PRD
  GitHub Repo (자동 생성)
       ↓
  ngrok → thrive-estate-vindicate.ngrok-free.dev
```

**파이프라인 순서:**
1. `planning` (PM) → 승인 필요 → Jira Epic+Stories + Confluence PRD 자동 생성
2. `design` + `architecture` (병렬)
3. `implement` (OpenDevin) → GitHub 레포 자동 생성 + Jira Story "In Progress"
4. `qa` → Jira Story "Done" + PR 링크
5. `autotest`
6. `release` → `/restart` 호출 (자가 개선 시)

---

## 🤖 에이전트 모델 설정 (토큰 최적화)

| 에이전트 | 모델 | max_tokens |
|---|---|---|
| pm | claude-sonnet-4-6 | 4096 |
| architect | claude-sonnet-4-6 | 3000 |
| implement | claude-sonnet-4-6 | 4096 |
| designer | claude-haiku-4-5-20251001 | 2048 |
| qa | claude-haiku-4-5-20251001 | 1024 |
| autotest | claude-haiku-4-5-20251001 | 512 |
| release | claude-haiku-4-5-20251001 | 512 |

---

## 🧪 통합 테스트 현황 (`python3 test_integrations.py`)

마지막 실행 결과: **5/9 통과**

| # | 테스트 | 상태 | 비고 |
|---|---|---|---|
| 1 | Orchestrator 상태 | ✅ | docker compose up 필요 |
| 2 | Jira 연결 | ✅ | |
| 3 | Jira 프로젝트 (ATM) | ✅ | |
| 4 | **Jira 이슈 생성** | ❌ | **현재 버그** — 아래 참조 |
| 5 | Confluence 연결 | ✅ | |
| 6 | Confluence 페이지 생성 | ✅ | |
| 7 | GitHub 연결 | ✅ | |
| 8 | GitHub 레포 생성 | — | GitHub 연결 성공 시 실행 |
| 9 | 프로젝트 생성 API | — | Orchestrator 실행 시 실행 |

---

## 🐛 현재 버그 (미해결)

### Bug #1 — Jira 이슈 유형 오류 (최우선)
**증상:**
```
[4] Jira 이슈 생성 ❌
생성 실패 (400): {"errorMessages":[],"errors":{"issuetype":"유효한 이슈 유형을 지정하세요"}}
```

**원인:**  
Jira 프로젝트 ATM의 유효한 이슈 유형이 "Story"가 아님.

**수정 필요 파일 2곳:**

1. `orchestrator/atlassian_client.py` 라인 94:
   ```python
   # 현재 (오류)
   "issuetype": {"name": "Story"},
   # 수정 → "Task"로 변경 또는 먼저 이슈 유형 조회
   "issuetype": {"name": "Task"},
   ```

2. `test_integrations.py` 라인 134:
   ```python
   # 현재 (오류)
   "issuetype": {"name": "Story"},
   # 수정
   "issuetype": {"name": "Task"},
   ```

**수정 명령어:**
```bash
cd ~/Development/ai-dev-team
sed -i '' 's/"name": "Story"/"name": "Task"/g' orchestrator/atlassian_client.py test_integrations.py
python3 test_integrations.py
```

**또는** 이슈 유형 동적 조회로 완전히 해결:
```
GET https://{domain}/rest/api/3/issue/createmeta?projectKeys=ATM&expand=projects.issuetypes
```

---

### Bug #2 — Confluence 스페이스 ATM 없음 (낮은 우선순위)
**증상:** 페이지 생성 시 ATM 스페이스가 없어서 개인 스페이스(~ych21c) 사용 중  
**수정:** Confluence에서 ATM 스페이스 직접 생성 필요 (UI 작업)

---

## 📋 다음 할 일 (우선순위 순)

### 즉시 (코드 수정)
- [ ] **Jira "Story" → "Task" 변경** — `atlassian_client.py` + `test_integrations.py`
- [ ] `docker compose restart orchestrator` — 변경 적용
- [ ] `python3 test_integrations.py` — 9/9 확인

### 단기
- [ ] Confluence ATM 스페이스 생성 (ych21c.atlassian.net 에서 직접)
- [ ] PostgreSQL 채팅 히스토리 저장 구현 (현재 메모리만)
- [ ] 직접 대화 모드 — 파이프라인 우회하는 캐주얼 채팅

### 중기
- [ ] Anthropic API 크레딧 확보 → 에이전트 실제 실행 테스트
- [ ] 자가 개선 프로젝트 첫 실행 확인
- [ ] 운동 앱 (부상자용) 프로젝트 시작

---

## 🚀 실행 방법

```bash
# 시작
cd ~/Development/ai-dev-team
docker compose up -d

# 로그 확인
docker compose logs -f orchestrator
docker compose logs -f agent-pm

# 통합 테스트 (Orchestrator 실행 중일 때)
python3 test_integrations.py

# 변경 후 재시작
docker compose restart orchestrator
```

**외부 접속 URL:** https://thrive-estate-vindicate.ngrok-free.dev

---

## 💡 주요 설계 결정 (WHY)

| 결정 | 이유 |
|---|---|
| Redis Streams | 에이전트 간 비동기 메시지 큐, 순서 보장 |
| OpenDevin as Implement Agent | 실제 파일 생성/수정 가능한 코딩 에이전트 |
| ngrok 고정 도메인 | Cloudflare 계정 없이 고정 URL 확보 |
| Sonnet/Haiku 분리 | 복잡 역할(PM/Arch/Impl)은 Sonnet, 단순 역할은 Haiku로 비용 절감 |
| 소스 마운트 (`:ro`) | 자가 개선 시 에이전트가 소스 읽기 가능 |
| `redis_queue/` 폴더명 | Python 내장 `queue` 모듈 충돌 방지 |
| GitHub 레포명 regex | 한글 프로젝트명 → 안전한 영문 레포명 변환 |
| Jira `leadAccountId` | `/myself` API로 먼저 accountId 조회 후 프로젝트 생성 |
