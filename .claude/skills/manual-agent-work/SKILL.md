---
name: manual-agent-work
description: ai-dev-team의 implement 스테이지를 컨테이너 에이전트(API 과금) 대신 이 세션이 직접 처리한다. MANUAL_TASKS_DIR에 쌓인 대기 태스크를 확인하고, 실제 코딩 작업을 수행한 뒤 완료를 보고한다. QA 검증은 대상이 아니다 — 항상 컨테이너 에이전트가 자동으로 처리한다.
---

# 배경

프로젝트별 런타임 토글(`project_manual_implement[pid]` — 웹 플로우차트 탭의
implement 노드에 있는 "🖐 수동 처리(외부 세션이 코드 작성)" 체크박스, 또는
`POST /projects/{id}/manual-implement`)이 켜진 프로젝트는 implement 태스크를
큐(redis)로 컨테이너 에이전트에 안 보내고 `/workspace/manual_tasks/`에 태스크
파일만 쓴다(`orchestrator/main.py`의 `_send_task_or_manual` 헬퍼 —
`advance_pipeline`과 `_retry_implement_with_feedback` 둘 다 여기를 거침).
API 토큰 과금 없이, 이미 구독 중인 이 세션이 코드 작성을 대신 하기 위한
비용 절감용 우회다.

**QA는 이 우회의 대상이 아니다.** QA는 implement가 실제로 요구사항을
만족했는지 검증하는 안전장치라, 켜져 있든 꺼져 있든 항상 `ai-dev-team-agent-qa-1`
컨테이너가 정상적으로(토큰 과금 경로로) 처리한다 — `_send_task_or_manual`이
`stage_name == "implement"`일 때만 가로채도록 명시적으로 제한돼 있다. 이
세션이 QA 시나리오 테스트를 대신 작성하거나 판정하지 않는다.

`/workspace`는 `shared-workspace`라는 docker 네임드 볼륨이라 호스트에서 직접
경로로 접근할 수 없다 — 항상 `docker exec`로 컨테이너 안에서 읽고 쓴다.

# 절차

## 1. 대기 중인 태스크 확인

```
docker exec ai-dev-team-orchestrator-1 sh -c "ls -la /workspace/manual_tasks/*.json 2>/dev/null"
```

없으면 "처리할 태스크 없음"으로 보고하고 끝낸다. 파일명 형식은
`{project_id}_implement_implement_{unix_timestamp}.json` — timestamp가
작은(오래된) 것부터 순서대로 처리한다. 인자로 project_id가 주어졌으면 그
프로젝트 것만 필터링한다.

## 2. 태스크 읽기

```
docker exec ai-dev-team-orchestrator-1 cat /workspace/manual_tasks/<파일명>
```

JSON에 `project_id`, `stage`(항상 "implement"), `instruction`(이번 라운드
지시 — QA 실패 후 재작업 요청이면 `_retry_implement_with_feedback`이 붙인
"[QA/AutoTest 재작업 요청] ..." 문구가 포함돼 있다), `context`(완료된 이전
스테이지들의 outputs — `retry_branch`가 있으면 그 브랜치를 이어서 쓸 것),
`github_repo`가 들어있다.

## 3. 실제 코딩 수행

작업 방식/규칙을 새로 지어내지 말고, 실제 implement 컨테이너 에이전트가 뭘
하는지 **먼저 소스를 읽고** 그대로 따라 한다 — 이 프로젝트가 실전에서 겪은
실패 패턴들이 그 안에 이미 규칙/가드레일로 녹아있다.

- 참고 소스: `agents/implement_openhands/run.py` (프롬프트/워크플로), 특히
  `outputs` 스키마는 `stage_completed` emit 지점(파일 내 `"outputs = {"` 또는
  `"outputs": {`로 검색)을 확인.
- 워크스페이스: `/workspace/{project_id}` — 이미 implement 결과물(브랜치 등)이
  있으면 `context`의 `retry_branch`/기존 `implement.branch`를 이어서 쓴다
  (처음부터 새로 만들지 않음 — run.py의 `_NO_BLIND_REVERT_GUIDANCE`와 같은 취지:
  테스트 실패라고 무조건 코드를 되돌리지 말고, 최근 커밋이 의도적 변경인지
  먼저 확인).
- 실제 코딩은 `docker exec -it ai-dev-team-agent-implement-1 bash`로 들어가서
  하거나(이 컨테이너가 빌드 툴체인을 갖고 있음), 필요하면
  `docker cp`로 파일을 오가며 로컬 Read/Edit로 고친 뒤 다시 컨테이너에 반영해도
  된다 — 최종적으로 `/workspace/{project_id}` 안의 git 저장소에 커밋/push까지
  끝나 있어야 한다.
- git/PR: `.env`의 `GITHUB_TOKEN`으로 `git push
  https://x-access-token:${TOKEN}@github.com/{github_repo}.git <branch>`,
  PR 생성은 `gh` 없이 GitHub REST API(`POST /repos/{github_repo}/pulls`)로.
- 완료 outputs 예시(실제 필드는 run.py의 emit 지점 참고):
  `{"agent": "implement", "summary": "...", "branch": "...", "pr_number": N, "pr_url": "...", "head_sha": "...", "build_ok": true}`

## 4. 완료 보고

```
curl -s -X POST http://localhost:8000/projects/{project_id}/stage/implement/manual-result \
  -H "Content-Type: application/json" \
  -d '{"agent": "implement", "outputs": { ... }}'
```

(orchestrator는 docker-compose에서 8000 포트를 호스트에 그대로 열어두므로
`docker exec` 없이 호스트에서 바로 curl 가능.) 이 엔드포인트는 컨테이너
에이전트가 보낸 것과 동일하게 `handle_agent_event`를 태워서, implement→qa
게이트가 없는 파이프라인 규칙에 따라 **QA를 자동으로(컨테이너 에이전트로)
바로 이어서 돌린다** — 여기서 QA를 대신 수행하거나 결과를 지어내지 않는다.
`outputs`를 3단계 스키마에 맞게 채우는 게 중요하다(안 맞으면 다음 단계
디스패치가 깨질 수 있음).

## 5. 처리한 태스크 파일 정리

```
docker exec ai-dev-team-orchestrator-1 rm /workspace/manual_tasks/<파일명>
```

지우지 않으면 재실행 시 같은 태스크를 또 처리하려고 시도하니 완료 보고 후
반드시 지운다.

# QA를 지금 바로 검증하고 싶을 때

파이프라인 핸드오프 없이 워크스페이스 코드를 그냥 직접 고쳤거나(이 스킬을
거치지 않고), QA 결과를 당장 확인하고 싶을 뿐이면 — 웹 플로우차트 탭 QA
노드의 "🧪 밸리데이션만 실행" 버튼을 누르거나 아래를 직접 호출한다. 이건
implement 상태와 무관하게 지금 워크스페이스 코드로 QA(컨테이너 에이전트,
정상 토큰 경로)를 즉시 재실행한다 — 이 스킬이 대신 판정하지 않는다:

```
curl -s -X POST http://localhost:8000/projects/{project_id}/stage/qa/rerun \
  -H "Content-Type: application/json" -d '{}'
```

# 주의

- 태스크 여러 개가 쌓여 있어도 한 프로젝트를 동시에 두 번 처리하지 않는다 —
  오래된 것부터 순서대로.
- 토글이 꺼진 프로젝트는 이 스킬이 관여할 필요가 없다 — 평소처럼
  `ai-dev-team-agent-implement-1`이 자동 처리한다.
- `ai-dev-team-agent-implement-1`은 토글이 켜져 있어도 계속 떠 있다(그냥
  redis 큐가 비어서 할 일이 없을 뿐) — `docker exec`로 그 툴체인을 그대로
  빌려 쓰는 용도로 남겨둔 것이니 굳이 내리지 않는다.
