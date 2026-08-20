---
name: manual-agent-work
description: ai-dev-team의 implement/qa 스테이지를 컨테이너 에이전트(API 과금) 대신 이 세션이 직접 처리한다. MANUAL_TASKS_DIR에 쌓인 대기 태스크를 확인하고, 실제 코딩/QA 작업을 수행한 뒤 완료를 보고한다.
---

# 배경

`orchestrator/main.py`의 `MANUAL_STAGES`(`.env`)에 든 스테이지는 큐(redis)로
컨테이너 에이전트에 안 보내고 `/workspace/manual_tasks/`에 태스크 파일만 쓴다
(`_send_task_or_manual` 헬퍼 — `advance_pipeline`과 4개
`_retry_*_with_feedback` 전부 여기를 거침). API 토큰 과금 없이, 이미 구독
중인 이 세션이 implement/qa 작업을 대신 하기 위한 비용 절감용 우회다.

`/workspace`는 `shared-workspace`라는 docker 네임드 볼륨이라 호스트에서 직접
경로로 접근할 수 없다 — 항상 `docker exec`로 컨테이너 안에서 읽고 쓴다.

# 절차

## 1. 대기 중인 태스크 확인

```
docker exec ai-dev-team-orchestrator-1 sh -c "ls -la /workspace/manual_tasks/*.json 2>/dev/null"
```

없으면 "처리할 태스크 없음"으로 보고하고 끝낸다. 파일명 형식은
`{project_id}_{stage}_{agent_name}_{unix_timestamp}.json` — timestamp가
작은(오래된) 것부터 순서대로 처리한다. 인자로 project_id가 주어졌으면 그
프로젝트 것만 필터링한다.

## 2. 태스크 읽기

```
docker exec ai-dev-team-orchestrator-1 cat /workspace/manual_tasks/<파일명>
```

JSON에 `project_id`, `stage`("implement" 또는 "qa"), `instruction`(이번
라운드 지시), `context`(완료된 이전 스테이지들의 outputs), `github_repo`가
들어있다.

## 3. 실제 작업 수행 — stage별로 다름

작업 방식/규칙을 새로 지어내지 말고, 실제 컨테이너 에이전트가 뭘 하는지
**먼저 해당 소스를 읽고** 그대로 따라 한다 — 이 프로젝트가 실전에서 겪은
실패 패턴들이 그 안에 이미 규칙으로 녹아있다(예: qa의
`_VERIFY_SCENARIOS_RULES`).

### stage == "implement"

- 참고 소스: `agents/implement_openhands/run.py` (프롬프트/워크플로), 특히
  `outputs` 스키마는 `stage_completed` emit 지점(파일 내 "outputs = {"로 검색)을
  확인.
- 워크스페이스: `/workspace/{project_id}` — 이미 implement 결과물(브랜치 등)이
  있으면 `context`의 `retry_branch`/기존 `implement.branch`를 이어서 쓴다
  (처음부터 새로 만들지 않음 — `_NO_BLIND_REVERT_GUIDANCE`와 같은 취지).
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

### stage == "qa"

- 참고 소스: `agents/qa_testlab/run.py`의 `verify_scenarios` 함수와
  `_VERIFY_SCENARIOS_RULES` 상수(반드시 그 규칙을 그대로 지켜서 시나리오 테스트
  Dart 코드를 작성 — Finder에 `.or()`/`.and()` 같은 존재하지 않는 메서드를
  쓰는 등 컴파일 자체가 깨지는 코드를 쓰면 전체 시나리오가 실패로 잡힌다).
- 워크스페이스: `/workspace/{project_id}-qa-clone` (implement 트리와 분리된
  QA 전용 클론 — `SCENARIO_TEST_FILE = "integration_test/scenario_test.dart"`).
- 실행 환경(Flutter SDK, xvfb)은 `ai-dev-team-agent-qa-1` 컨테이너에 있다 —
  `docker exec ai-dev-team-agent-qa-1 xvfb-run -a flutter test
  integration_test/scenario_test.dart` 형태로 실제로 돌려서 통과 여부를
  판정한다(정적 리뷰만으로 판단하지 말 것 — `verify_scenarios`의 docstring이
  이 이유를 설명함).
- 완료 outputs 예시(run.py의 "outputs": {" 지점 참고):
  - 통과: `{"agent": "qa", "passed": true, "summary": "..."}`
  - 실패(구현 재작업 필요): `{"agent": "qa", "passed": false, "needs_rework": true, "feedback": "구체적인 실패 원인"}`

## 4. 완료 보고

```
curl -s -X POST http://localhost:8000/projects/{project_id}/stage/{stage}/manual-result \
  -H "Content-Type: application/json" \
  -d '{"agent": "implement 또는 qa", "outputs": { ... }}'
```

(orchestrator는 docker-compose에서 8000 포트를 호스트에 그대로 열어두므로
`docker exec` 없이 호스트에서 바로 curl 가능.) 이 엔드포인트는 컨테이너
에이전트가 보낸 것과 동일하게 `handle_agent_event`를 태워서 재시도 라우팅
(`_route_needs_rework_or_fail`)/토큰 집계/jira 코멘트를 그대로 처리한다 —
`outputs`를 stage가 기대하는 스키마에 맞게 채우는 게 중요하다(3단계 참고).

## 5. 처리한 태스크 파일 정리

```
docker exec ai-dev-team-orchestrator-1 rm /workspace/manual_tasks/<파일명>
```

지우지 않으면 재실행 시 같은 태스크를 또 처리하려고 시도하니 완료 보고 후
반드시 지운다.

# 주의

- 태스크 여러 개가 쌓여 있어도 한 프로젝트의 같은 스테이지를 동시에 두 번
  처리하지 않는다 — 오래된 것부터 순서대로.
- `MANUAL_STAGES`가 꺼진(`.env`에 없는) 스테이지는 이 스킬이 관여할 필요가
  없다 — 평소처럼 컨테이너 에이전트가 자동 처리한다.
- implement/qa 컨테이너(`ai-dev-team-agent-implement-1`,
  `ai-dev-team-agent-qa-1`)는 MANUAL_STAGES가 켜져 있어도 계속 떠 있다(그냥
  redis 큐가 비어서 할 일이 없을 뿐) — `docker exec`로 그 툴체인을 그대로
  빌려 쓰는 용도로 남겨둔 것이니 굳이 내리지 않는다.
