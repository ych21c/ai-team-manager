"""
Orchestrator — FastAPI + WebSocket

대표님 Web UI ↔ Orchestrator ↔ Redis ↔ Agents

엔드포인트:
  WS  /ws                         실시간 채팅 + 에이전트 상태
  POST /projects                  새 프로젝트 생성 (팀 자동 스폰)
  GET  /projects                  프로젝트 목록
  GET  /projects/{id}             프로젝트 상태
  POST /projects/{id}/approve/{stage}  스테이지 승인
  DELETE /projects/{id}           프로젝트 + 팀 종료
"""
import asyncio
import html
import json
import re
import shutil
import sys
import time
import uuid
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from redis_queue.redis_client import RedisQueue
from workflows.pipeline import Pipeline, StageStatus
from team_spawner import TeamSpawner
from atlassian_client import (
    sync_pm_output, update_jira_status, add_jira_comment, link_pr_to_jira,
    add_jira_remote_link, create_confluence_page, update_confluence_page,
    create_jira_stories, parse_pm_requirements, DOMAIN,
)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# 디자인 목업 링크를 Jira 코멘트에 남길 때 쓰는 외부 접속 기준 URL. web/server.js가
# "/"(SPA)와 "/_next/*" 말고는 전부 orchestrator로 그대로 프록시하므로
# /design-file/*, /recordings/* 등 이 파일의 라우트는 이 도메인으로 바로
# 열린다(ngrok 터널이 web:3000만 뚫고 있음 — docker-compose.yml의 tunnel-web 참고).
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://thrive-estate-vindicate.ngrok-free.dev")

# implement(OpenHands, 무거움)/autotest(GitHub CI 폴러)/qa(Flutter SDK+gcloud,
# 무거움)는 프로젝트별로 격리할 필요가 없어 docker-compose의 정적 싱글턴
# 서비스로 유지한다. 나머지 역할만 TeamSpawner가 프로젝트별 컨테이너로 격리해서 띄운다.
GLOBAL_SHARED_AGENTS = {"implement", "autotest", "qa"}

# implement 스테이지만 대상 — QA는 코드가 실제로 요구사항을 만족하는지 검증하는
# 안전장치라 항상 컨테이너 에이전트(정상 토큰 과금 경로)로 돌아야 한다는 게
# 명시적 결정. project_manual_implement[pid]가 True인 프로젝트는 implement
# 태스크를 큐(redis.send_task) 대신 MANUAL_TASKS_DIR에 파일로만 써놓고 사람
# (Claude Code 세션)이 대신 코딩하게 한다 — API 토큰 과금 없이 이미 구독 중인
# 세션으로 처리하기 위한 비용 절감용 우회. 프로젝트마다, 그리고 그때그때
# (컨테이너 재시작 없이) 켜고 끌 수 있어야 해서 .env 전역 플래그가 아니라
# 프로젝트별 런타임 상태로 둔다 — POST /projects/{id}/manual-implement로 토글.
# 완료되면 POST .../manual-result로 통상적인 stage_completed 이벤트와 동일하게
# 넘겨서, 이후 로직(재시도 라우팅/토큰 집계/jira 코멘트 등)은 그대로 재사용한다.
project_manual_implement: dict[str, bool] = {}
MANUAL_TASKS_DIR = "/workspace/manual_tasks"

# QA 자신이 생성한 시나리오 테스트 코드가 컴파일 자체가 안 되는 경우(예: Flutter
# Finder엔 없는 `.or()` 호출) — agents/qa_testlab/run.py가 자체 수정을
# MAX_QA_BUILD_FIX_ROUNDS번 시도해도 못 고치면, project_manual_qa_build[pid]가
# True일 때만 Implement가 아니라 사람(Claude Code 세션)에게 직접 넘긴다(QA
# manual-result로 완료 보고 — implement와 동일한 우회를 stage="qa"에 재사용).
# False면 QA는 그 라운드를 건너뛸 뿐 Implement에 재작업을 요청하지 않는다(앱
# 코드는 이미 맞을 수 있는 QA 자신의 문제라서). implement처럼 값 자체는 프로젝트별
# 런타임 토글이지만, 실제 판단(자체 수정 시도/실패 감지)은 qa 컨테이너 안에서
# 일어나므로 이 값은 dispatch 시점에 task payload로 실어 보낸다(advance_pipeline/
# _retry_qa_with_feedback 참고) — orchestrator가 QA 실행 중간에 개입할 수 없어서다.
project_manual_qa_build: dict[str, bool] = {}

# ── 전역 상태 ────────────────────────────────────────────────────────
projects:       dict[str, Pipeline] = {}
project_names:  dict[str, str]     = {}   # project_id → 표시명
project_repos:  dict[str, str]     = {}   # project_id → github repo (owner/repo)
project_jira:   dict[str, dict]    = {}   # project_id → { epic, stories, confluence_url }
project_docs:   dict[str, dict]    = {}   # project_id → { overview, architecture, history:[...], confluence_page_id }
project_messages: dict[str, list[dict]] = {}   # project_id → 채팅 메시지 이력 (새로고침 시 복원용)
MAX_STORED_MESSAGES = 300   # 프로젝트당 메모리 상한
# project_id → agent → phase_id → {label, status, detail, ts} — web/app/page.tsx
# PhaseTimeline이 그리는 단계별 칩의 서버 쪽 사본. agent_phase 이벤트는 원래
# 연결된 WS 클라이언트에게만 방송되고 어디에도 저장되지 않아서, 새로고침하거나
# 탭을 새로 열면(WS는 재연결돼도) 그 세션이 연결되기 전에 지나간 단계들은
# 영영 복원이 안 돼 QA 카드에 칩이 하나도 안 보이는 문제가 있었다(lastMessage는
# project_messages로 이미 복원되는데 phases만 빠져있었음). 여기 저장해뒀다가
# _make_project_info로 내려주면 프론트가 최초 로드 때 seedAgentsFromMessages와
# 같은 방식으로 칩을 복원한다.
project_phases: dict[str, dict[str, dict[str, dict]]] = {}
# project_id → {"by_sprint": {"1": {"planning": {input_tokens,output_tokens,cost_usd}, "design": {...}, ...}, "2": {...}},
#               "pre_migration": {input_tokens,output_tokens,cost_usd} | None}
# 스프린트(전체 재기획 회차) × 스테이지 단위로 누적한다 — outputs["input_tokens"/
# "output_tokens"/"cost_usd"]는 "이번 실행 1회분"이라 Run으로 재실행하면 merge 시
# 덮어써진다(pipeline.py mark_completed)지만, 여기 누적치는 사라지지 않는다.
# 게이트 노드 배지는 "이번 실행", 탭 헤더는 이 누적치의 파생 합계(_derive_lifetime_totals)를
# 쓴다. "pre_migration"은 이 스프린트/스테이지 분리 기능 도입 전 이미 쌓여있던
# 평면 누적치를 옮겨 담는 자리 — 진짜 "1번 스프린트 비용"이 아니라서 가짜
# 스프린트 밑에 끼워넣지 않고 형제 필드로 분리한다(_load_all_projects 참고).
project_token_totals: dict[str, dict] = {}
# project_id → { app_name, app_identifier, language, app_version, environment,
# platforms: ["ios","android"], host_workspace_path } — 스프린트 화면 배포 카드에서
# 사람이 웹으로 편집하는 값들. App Store Connect/Google Play 자격증명은 여기 안
# 들어간다 — host_workspace_path가 가리키는 프로젝트 레포 자신의 fastlane/.env가
# 이미 그걸 갖고 있고 deploy_runner가 그 디렉토리에서 fastlane을 그대로 실행하므로
# ai-dev-team은 자격증명을 아예 저장·전송하지 않는다.
project_deploy_config: dict[str, dict] = {}
# project_id → { status: idle|running|success|failed, started_at, finished_at,
# build_number, log_tail, error } — 배포 1회 실행 결과. build_number는 사람이
# 편집 못 하는 자동 증가값이라 여기 결과로만 보여준다.
project_deploy_status: dict[str, dict] = {}
# 빌드(Xcode 필요)는 orchestrator의 Linux Docker 컨테이너 안에서 못 돌려서, 이
# Mac 호스트에서 네이티브로 도는 scripts/deploy_runner.py를 호출한다.
# host.docker.internal은 Docker Desktop for Mac이 기본 제공하는 호스트 DNS.
DEPLOY_RUNNER_URL = os.getenv("DEPLOY_RUNNER_URL", "http://host.docker.internal:8765")
qa_retry_counts: dict[str, int] = {}   # project_id → QA/AutoTest가 Implement에 재작업 요청한 횟수 (합산)
# _save_project/_load_all_projects가 이 값도 STATE_DIR에 함께 영속화한다 — 예전엔
# 메모리에만 있어서 orchestrator/main.py를 바인드 마운트로 라이브 편집할 때마다
# (예: self-improve 프로젝트가 자기 자신의 소스를 고치는 동안) uvicorn --reload가
# 프로세스를 재시작해서 이 카운터가 조용히 0으로 리셋됐다. 그러면 recoveryfit
# 프로젝트가 같은 지점(스플래시→랜딩 전환 시나리오)에서 몇 시간째 반복 실패해도
# "1/3"만 계속 찍히고 MAX_QA_RETRIES에 절대 도달하지 못해 자동 정지 가드가
# 사실상 무력화됐다(실제로 재현 — 사람이 수동으로 취소할 때까지 안 멈췄음).
MAX_QA_RETRIES = 3   # 이 횟수를 넘으면 재시도 없이 실패 처리하고 파이프라인을 멈춤 (무한루프 방지)
chat_triage_in_flight: dict[str, float] = {}   # project_id → PM triage 태스크를 보낸 시각
CHAT_TRIAGE_TIMEOUT_SEC = 120   # 이 시간이 지나면 stale로 보고 새 triage를 다시 보낸다 (에이전트 크래시 등으로 stage_completed가 영영 안 올 경우 대비)
ws_clients:     set[WebSocket]     = set()
redis   = RedisQueue(os.getenv("REDIS_URL", "redis://localhost:6379"))
spawner = TeamSpawner()


def _story_link(project_id: str, key: str, identifier: str) -> str:
    """웹 채팅에 이슈 키만 툭 던지지 않고 "[identifier] [KEY: 실제 제목](Jira URL)"
    형태로 만든다 — web/app/page.tsx의 linkifyContent가 마크다운 [text](url)를
    클릭 가능한 링크로 바꿔주므로 프런트는 안 건드리고 여기서 라벨만 채운다.
    identifier(spec/design/impl/qa/bug/release 등)를 링크 텍스트 "안"에 대괄호로
    같이 넣으면 linkifyContent의 정규식(\\[[^\\]]+\\]\\([^)]+\\))이 첫 "]" 뒤에
    "("가 안 와서 매칭이 깨지므로, 링크 밖에 별도 접두어로 붙인다."""
    title = project_jira.get(project_id, {}).get("story_titles", {}).get(key, "")
    text = f"{key}: {title}" if title else key
    link = f"[{text}](https://{DOMAIN}/browse/{key})" if DOMAIN else text
    return f"[{identifier}] {link}"


def _story_link_list(project_id: str, keys: list[str], identifier: str) -> str:
    return ", ".join(_story_link(project_id, key, identifier) for key in keys)


def _story_plain(project_id: str, key: str, identifier: str) -> str:
    """_story_link와 같은 라벨이지만 마크다운 링크 없이 순수 텍스트로 — Confluence
    히스토리(_render_project_doc_html)는 마크다운을 해석하지 않고 그대로
    HTML 이스케이프해서 보여주므로, 거기 들어갈 텍스트는 이 버전을 쓴다."""
    title = project_jira.get(project_id, {}).get("story_titles", {}).get(key, "")
    text = f"{key}: {title}" if title else key
    return f"[{identifier}] {text}"


# 기본 프로젝트 초기화 (서버 시작 시)
DEFAULT_PROJECT_ID   = "self-improve"
DEFAULT_PROJECT_NAME = "AI Team Manager 자가 개선"
SELF_IMPROVE_INSTRUCTION = """
이 프로젝트는 AI Team Manager 시스템 자체를 개선하는 자가 개선 프로젝트입니다.

소스코드 위치: /workspace/ai-team-manager/
주요 파일:
  - orchestrator/main.py       : FastAPI 오케스트레이터
  - orchestrator/team_spawner.py: Docker 팀 스폰 로직
  - agents/base/agent.py       : 에이전트 실행 엔진
  - web/app/page.tsx            : Web UI (Next.js)
  - docker-compose.yml          : 전체 서비스 구성

개선 목표:
  1. 사용성 향상 (UI/UX 개선)
  2. 에이전트 성능 개선 (프롬프트 최적화)
  3. 새로운 기능 추가
  4. 버그 수정

코드 수정 후 /api/restart 엔드포인트를 호출하면 해당 서비스가 재시작됩니다.
"""


# ── 파일 영속화 ──────────────────────────────────────────────────────
# projects/project_names/... 는 원래 파이썬 메모리에만 있어서 orchestrator가
# 재시작되면(코드 수정 후 --reload 등) 프로젝트가 통째로 사라지는 문제가
# 있었다 (recoverfit 프로젝트가 사라진 사고). STATE_DIR에 프로젝트당 스냅샷
# 파일 하나를 두고 시작할 때 복원한다. CHATLOG_DIR은 복원용이 아니라 순수
# 열람/디버깅용 — 날짜별 파일로 나눠서 언제 무슨 대화가 오갔는지 보기 쉽게.
STATE_DIR   = "/workspace/state"
CHATLOG_DIR = "/workspace/chat_logs"
PERSIST_EVENT_TYPES = {"agent_message", "stage_update", "project_added", "project_updated"}


def _append_chat_log(project_id: str, msg: dict):
    day = time.strftime("%Y-%m-%d", time.localtime(msg["ts"] / 1000))
    d = f"{CHATLOG_DIR}/{project_id}"
    os.makedirs(d, exist_ok=True)
    with open(f"{d}/{day}.jsonl", "a") as f:
        f.write(json.dumps(msg, ensure_ascii=False) + "\n")


def _store_message(project_id: str, frm: str, content: str):
    msg = {"id": uuid.uuid4().hex[:12], "from": frm, "content": content, "ts": int(time.time() * 1000)}
    lst = project_messages.setdefault(project_id, [])
    lst.append(msg)
    if len(lst) > MAX_STORED_MESSAGES:
        del lst[: len(lst) - MAX_STORED_MESSAGES]
    _append_chat_log(project_id, msg)


def _clear_phases(pid: str, agent_names: list[str]):
    """새 라운드가 시작될 때(stage_update running) 지난 라운드 칩을 지운다 —
    web/app/page.tsx의 agent_phase 핸들러(같은 이름의 로직)와 타이밍을 맞춰야,
    새로고침 직후 복원되는 칩이 이전 라운드 것과 섞이지 않는다."""
    phases = project_phases.get(pid)
    if not phases:
        return
    for name in agent_names:
        phases.pop(name, None)


def _save_project(pid: str):
    """프로젝트 현재 스냅샷(이름/레포/스테이지 상태/메시지/지라)을 파일로 저장 —
    재시작 복원용. 상태가 바뀔 때마다 전체를 덮어쓰는 방식이라 규모가 커지면
    비효율적이지만, 지금 트래픽 규모에선 충분히 가볍다."""
    if pid not in projects:
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    data = {
        **_make_project_info(pid),  # instruction도 이제 여기 포함됨
        "jira": project_jira.get(pid, {}),
        "doc":  project_docs.get(pid, {}),
        "qa_retry_count": qa_retry_counts.get(pid, 0),
        # _make_project_info()의 "token_totals"는 API/WS용 파생 뷰({lifetime, by_sprint})라
        # 그대로 저장하면 재시작 후 _load_all_projects가 그걸 원본으로 착각해
        # 이중 파생/오염된다 — 여기서 내부 저장 모양({by_sprint, pre_migration})으로 덮어쓴다.
        "token_totals": project_token_totals.get(pid, {"by_sprint": {}, "pre_migration": None}),
    }
    with open(f"{STATE_DIR}/{pid}.json", "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _delete_project_file(pid: str):
    path = f"{STATE_DIR}/{pid}.json"
    if os.path.exists(path):
        os.remove(path)


def _load_all_projects():
    if not os.path.isdir(STATE_DIR):
        return
    for fname in sorted(os.listdir(STATE_DIR)):
        if not fname.endswith(".json"):
            continue
        pid = fname[:-5]
        try:
            with open(f"{STATE_DIR}/{fname}") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[state] {fname} 로드 실패: {e}")
            continue

        pipeline = Pipeline(pid, data.get("instruction", ""), sprint=data.get("sprint", 1))
        for stage_name, stage_data in (data.get("stages") or {}).items():
            if stage_name not in pipeline.stages:
                continue
            try:
                pipeline.stages[stage_name].status = StageStatus(stage_data.get("status", "pending"))
            except ValueError:
                pass
            pipeline.stages[stage_name].outputs = stage_data.get("outputs") or {}
            pipeline.stages[stage_name].approved = bool(stage_data.get("approved", False))
            pipeline.stages[stage_name].agents_done = list(stage_data.get("agents_done") or [])
            pipeline.stages[stage_name].current_task = dict(stage_data.get("current_task") or {})

        projects[pid]      = pipeline
        project_names[pid] = data.get("name", pid)
        if data.get("repo"):
            project_repos[pid] = data["repo"]
        if data.get("messages"):
            project_messages[pid] = data["messages"]
        if data.get("jira"):
            project_jira[pid] = data["jira"]
        if data.get("doc"):
            project_docs[pid] = data["doc"]
        if data.get("token_totals"):
            project_token_totals[pid] = _migrate_token_totals(data["token_totals"])
        if data.get("qa_retry_count"):
            qa_retry_counts[pid] = data["qa_retry_count"]
        if data.get("manual_implement"):
            project_manual_implement[pid] = True
        if data.get("manual_qa_build"):
            project_manual_qa_build[pid] = True
        if data.get("deploy_config"):
            project_deploy_config[pid] = data["deploy_config"]
        if data.get("deploy_status"):
            project_deploy_status[pid] = data["deploy_status"]
        print(f"[state] 복원됨: {pid} ({project_names[pid]})")


# ── WebSocket 브로드캐스트 ───────────────────────────────────────────
async def broadcast(event: dict):
    etype = event.get("type")
    pid   = event.get("project_id")

    # agent_message는 여기서 한 번에 가로채 저장해둔다 — 새로고침으로 WS가
    # 재연결되면 그동안 브라우저 메모리에만 있던 채팅 기록이 통째로 사라지던
    # 문제를 막기 위함 (init 이벤트에 실어서 복원).
    if etype == "agent_message" and pid:
        _store_message(pid, event.get("agent", ""), event.get("content", ""))

    if pid and etype in PERSIST_EVENT_TYPES and pid in projects:
        _save_project(pid)

    dead = set()
    for ws in ws_clients:
        try:
            await ws.send_json(event)
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)


# ── 에이전트 이벤트 폴링 루프 ───────────────────────────────────────
async def event_loop():
    """이 루프가 죽으면(예외 전파) 그 순간부터 에이전트 이벤트를 영원히 못 받는다
    (재시작 로직이 없었음 — 실제로 reload 도중 Redis 커넥션이 끊기면서
    조용히 죽어 그 이후 이벤트를 놓친 사고가 있었다). 절대 죽지 않도록
    바깥을 try/except로 감싸고, 연결 에러면 클라이언트를 새로 맺는다."""
    while True:
        try:
            await redis.ensure_events_group()
            while True:
                events = await redis.poll_events()
                for event in events:
                    msg_id = event.pop("_msg_id")
                    await handle_agent_event(event)
                    await redis.ack_event(msg_id)
                await asyncio.sleep(0.2)
        except Exception as e:
            print(f"[event_loop] 에러 — 재연결 후 계속: {e}", file=sys.stderr)
            await redis.reset_connection()
            await asyncio.sleep(1)


def _accumulate_token_usage(project_id: str, sprint: int, stage_name: str, outputs: dict):
    """이번 실행분 토큰/비용을 프로젝트의 {스프린트: {스테이지: 누적치}}에 더한다.
    outputs에 토큰 필드가 없는 스테이지(autotest — LLM 호출 없음, PR 생성 실패 등
    조기 종료 경로)는 조용히 건너뛴다. Run으로 같은 스테이지를 여러 번 재실행해도
    (outputs는 매번 덮어써짐) 누적치는 사라지지 않도록 여기서만 더한다. design처럼
    에이전트 둘(designer+architect)이 같은 stage_name으로 각자 완료 보고하는
    경우도 자연스럽게 한 버킷에 합산된다."""
    in_tok  = outputs.get("input_tokens")
    out_tok = outputs.get("output_tokens")
    if not in_tok and not out_tok:
        return
    by_sprint = project_token_totals.setdefault(project_id, {"by_sprint": {}, "pre_migration": None})["by_sprint"]
    totals = by_sprint.setdefault(str(sprint), {}).setdefault(
        stage_name, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    )
    totals["input_tokens"]  += in_tok or 0
    totals["output_tokens"] += out_tok or 0
    totals["cost_usd"]      += outputs.get("cost_usd") or 0.0


def _derive_lifetime_totals(pid: str) -> dict:
    """pre_migration(있으면) + 모든 스프린트/스테이지 누적치를 합산한 평생 총합.
    저장은 스프린트×스테이지 단위로 하고, 이 합계는 API/WS로 나갈 때만 파생한다
    (두 군데에 따로 저장하면 서로 어긋날 수 있어서 단일 소스로 유지)."""
    data = project_token_totals.get(pid, {})
    total = dict(data.get("pre_migration") or {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
    total.setdefault("input_tokens", 0)
    total.setdefault("output_tokens", 0)
    total.setdefault("cost_usd", 0.0)
    for stages in (data.get("by_sprint") or {}).values():
        for stage_totals in stages.values():
            total["input_tokens"]  += stage_totals.get("input_tokens", 0)
            total["output_tokens"] += stage_totals.get("output_tokens", 0)
            total["cost_usd"]      += stage_totals.get("cost_usd", 0.0)
    return total


def _migrate_token_totals(stored: dict) -> dict:
    """저장된 token_totals가 이 기능(스프린트×스테이지 분리) 도입 전의 옛
    평면 모양({input_tokens, output_tokens, cost_usd})이면 새 모양으로 감싼다.
    옛 모양은 최상위에 "input_tokens" 키가 직접 있는 걸로 판별한다 — 새 모양은
    항상 "by_sprint"/"pre_migration"만 최상위에 있으므로 이 판별은 멱등이다
    (이미 새 모양이면 그대로 통과)."""
    if "input_tokens" in stored:
        return {"by_sprint": {}, "pre_migration": stored}
    return stored


async def handle_agent_event(event: dict):
    project_id = event.get("project_id")
    agent_name = event.get("agent")
    event_type = event.get("type")
    pipeline   = projects.get(project_id)
    if not pipeline:
        return

    if event_type == "message":
        await broadcast({
            "type": "agent_message",
            "project_id": project_id,
            "agent": agent_name,
            "content": event.get("content", ""),
        })
    elif event_type == "stage_completed":
        stage_name = event.get("stage")
        outputs    = event.get("outputs", {})

        # chat_triage는 Pipeline.stages에 실재하는 스테이지가 아니라(PM에게 채팅
        # 후속 요청을 검토시키기 위한 가짜 스테이지) mark_completed()로 보내면
        # KeyError가 난다 — 정식 파이프라인 진행 로직에 닿기 전에 여기서 가로챈다.
        if stage_name == "chat_triage":
            await _handle_chat_triage_result(pipeline, project_id, outputs)
            return

        _accumulate_token_usage(project_id, pipeline.sprint, stage_name, outputs)

        # QA든 AutoTest(CI)든, 실패했는데 구체적인 피드백(needs_rework)이 있으면
        # 사람이 볼 것도 없이 Implement에 바로 재작업을 요청한다 — 예전엔 AutoTest는
        # "실패한 체크: analyze-and-test"라는 요약만 남기고 무조건 멈췄었다. 두
        # 스테이지가 같은 재시도 예산(qa_retry_counts/MAX_QA_RETRIES)을 공유해서
        # "QA 3번 + AutoTest 3번"처럼 재시도가 배로 불어나지 않게 한다.
        if stage_name in ("qa", "autotest") and not outputs.get("passed", True) and outputs.get("needs_rework"):
            await _route_needs_rework_or_fail(pipeline, project_id, stage_name, outputs)
            return

        # AutoTest 실패(구체적 피드백 없음 — 예: CI 대기 시간 초과) → COMPLETED로
        # 전이시키지 않고 파이프라인 진행을 여기서 멈춘다 (release는 autotest에
        # 의존하므로 FAILED 상태에서는 절대 준비되지 않음)
        if stage_name == "autotest" and not outputs.get("passed", True):
            pipeline.mark_failed(stage_name, outputs)
            await broadcast({"type": "stage_update", "project_id": project_id, "stage": stage_name, "status": "failed"})
            await broadcast({
                "type": "agent_message", "project_id": project_id, "agent": "system",
                "content": f"❌ AutoTest 실패 — 자동 병합 중단.\n{outputs.get('summary', '')}",
            })
            for story in project_jira.get(project_id, {}).get("stories", []):
                await add_jira_comment(story, f"❌ AutoTest(CI) 실패 — 병합 보류.\n{outputs.get('summary', '')}")
            await _add_history(project_id, f"❌ AutoTest(CI) 실패: {outputs.get('summary', '')}")
            return

        pipeline.mark_completed(stage_name, outputs, agent_name=agent_name)
        # design처럼 에이전트 여럿(designer+architect)이 한 스테이지를 나눠 맡는
        # 경우, 방금 건 그중 한 명뿐일 수 있다 — 그래도 이 에이전트의 산출물/
        # agents_done은 프론트가 바로 반영할 수 있게 항상 전체 스냅샷을 보낸다.
        # "stage_update"(completed)는 stage.agents 전원이 끝났을 때만 보내서,
        # 다음 게이트(승인 등)가 나머지 에이전트를 기다리지 않고 열리지 않게 한다.
        await broadcast({"type": "project_updated", "project_id": project_id, "projects": {pid: _make_project_info(pid) for pid in projects}})
        stage_now_complete = pipeline.stages[stage_name].status == StageStatus.COMPLETED
        if stage_now_complete:
            await broadcast({"type": "stage_update", "project_id": project_id, "stage": stage_name, "status": "completed"})

        # PM 완료 → Jira Epic/Story 등록 (이미 있으면 새로 안 만들고 그대로 유지 —
        # 사람 팀 프로젝트처럼 링크가 계속 붙어있어야 하므로) + Confluence 문서 갱신
        if stage_name == "planning":
            pname = project_names.get(project_id, project_id)
            pm_text = outputs.get("summary", pipeline.instruction)
            existing = project_jira.get(project_id, {})

            if existing.get("epic"):
                # 재기획/재작업 — Epic은 최상위로 그대로 유지하고 갱신 코멘트를 남기되,
                # 이번 재기획에서 추가된 요구사항은 기존 Epic 아래 별도 이슈로 만든다
                # (전에는 코멘트만 남기고 실제 구현 단위 이슈가 하나도 안 생겨서
                # 무슨 작업이 진행 중인지 Jira만 보고는 알 수 없었다).
                await add_jira_comment(existing["epic"], f"📋 PRD 갱신:\n{pm_text}")
                new_records = await _sync_new_requirements_to_epic(project_id, pm_text)
                if new_records:
                    lines = "\n".join(
                        f"• {_story_link(project_id, r['key'], 'spec')}" for r in new_records
                    )
                    await broadcast({
                        "type": "agent_message", "project_id": project_id, "agent": "system",
                        "content": f"📋 [Sprint {pipeline.sprint}] 기존 Epic({existing['epic']}: {pname}) 아래 신규 이슈 {len(new_records)}건 생성\n{lines}",
                    })
                else:
                    await broadcast({
                        "type": "agent_message", "project_id": project_id, "agent": "system",
                        "content": f"📋 기존 Jira 연결 유지 — Epic: [spec] [{existing['epic']}: {pname}](https://{DOMAIN}/browse/{existing['epic']}) (새로 만들 이슈 없음, 갱신 코멘트만 추가)",
                    })
            else:
                atlassian = await sync_pm_output(pname, pm_text)
                if atlassian.get("epic"):
                    project_jira[project_id] = atlassian
                    story_list = _story_link_list(project_id, atlassian.get("stories", []), "spec")
                    await broadcast({
                        "type": "agent_message", "project_id": project_id, "agent": "system",
                        "content": (
                            f"📋 [Sprint {pipeline.sprint}] Jira 등록 완료\n"
                            f"• Epic: [spec] [{atlassian['epic']}: {pname}]({atlassian['jira_url']})\n"
                            f"• Stories: {story_list}"
                        ),
                    })

            _doc(project_id)["overview"] = pm_text
            await _add_history(project_id, "📋 기획(Planning) 완료")
            jira_now = project_jira.get(project_id, {})
            if jira_now.get("confluence_url"):
                await broadcast({
                    "type": "agent_message", "project_id": project_id, "agent": "system",
                    "content": f"📄 프로젝트 문서: {jira_now['confluence_url']}",
                })

        # 설계 완료 → 아키텍처 섹션 갱신
        elif stage_name == "design":
            if agent_name == "architect":
                _doc(project_id)["architecture"] = outputs.get("architecture_summary", "")
            if agent_name == "designer" and outputs.get("design_preview"):
                await broadcast({
                    "type": "agent_message", "project_id": project_id, "agent": "system",
                    "content": "🎨 디자인 목업이 준비됐습니다 — 헤더의 🎨 디자인 버튼에서 확인하세요.",
                })
            await _add_history(project_id, f"🎨 설계({agent_name}) 완료")

        # Implement 완료 → 첫 번째 Story "In Progress" + PR 링크 코멘트 + 문서 히스토리
        elif stage_name == "implement":
            jira = project_jira.get(project_id, {})
            stories = jira.get("stories", [])
            pr_url = outputs.get("pr_url", "")
            # scenario_key로 범위가 좁혀진 재작업(예: 채팅 트리아지가 이슈 하나만
            # 지목한 경우)이면 그 이슈에만 코멘트를 단다 — 예전엔 항상 stories[0]
            # 하나에만 달아서, 두 번째 이후 이슈로 좁혀진 작업의 PR 링크가 엉뚱한
            # (또는 이미 끝난) 첫 번째 이슈에 계속 쌓이는 문제가 있었다. 범위 제한이
            # 없는 전체 작업(초기 구현 등)은 그 PR이 모든 스토리에 다 영향을 주므로
            # 스토리 전체에 단다.
            target_stories = _implement_jira_comment_targets(outputs.get("scenario_keys"), stories)
            comment = f"🤖 구현 완료 — PR: {pr_url}" if pr_url else f"⚠️ 구현 단계 완료했지만 PR 생성 실패: {outputs.get('summary', '')}"
            for story in target_stories:
                target = _stage_issue_target(project_id, story, "implement")
                await update_jira_status(target, "In Progress")
                await add_jira_comment(target, comment)
            await _add_history(project_id, f"🤖 구현 완료 — PR: {pr_url}" if pr_url else "⚠️ 구현 완료했지만 PR 생성 실패")

        # QA 완료 → PR 링크 + 결과 코멘트 (통과 여부와 무관하게 무조건 Done으로
        # 바꾸던 버그를 고침 — 실패면 진행 상황만 남기고 상태는 그대로 둔다.
        # 최종 Done 전환은 release 완료 시점으로 옮김)
        elif stage_name == "qa":
            jira = project_jira.get(project_id, {})
            stories = jira.get("stories", [])
            pr_url  = outputs.get("pr_url", "")
            repo    = project_repos.get(project_id, "")
            passed  = outputs.get("passed", True)
            # QA 녹화 영상은 매 라운드 같은 경로(qa_recording.mp4)에 덮어써져서,
            # 그대로 링크만 걸면 다음 라운드가 시작되는 순간 링크가 이번 라운드
            # 영상이 아니라 최신 영상을 가리키게 된다 — 그래서 스프린트 태그가
            # 붙은 이력 파일로 먼저 복사해두고(_archive_qa_recording, design/
            # history와 같은 패턴), 그 고정된 버전을 링크한다. 웹에서도 같은
            # 파일을 /recordings/{project_id}/history/{version}으로 그대로 열어볼
            # 수 있다(collect_outputs가 산출물 패널에 스프린트별로 나열).
            video_ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
            video_version = _archive_qa_recording(project_id, pipeline.sprint, video_ts)
            for story in stories:
                target = _stage_issue_target(project_id, story, "qa")
                if pr_url:
                    await link_pr_to_jira(target, pr_url, repo)
                icon = "✅" if passed else "⚠️"
                await add_jira_comment(target, f"{icon} QA {'통과' if passed else '이슈 발견'}: {outputs.get('summary', '')}")
                if video_version:
                    await add_jira_remote_link(
                        target,
                        f"{PUBLIC_BASE_URL}/recordings/{project_id}/history/{video_version}",
                        f"QA 녹화 (Sprint {pipeline.sprint})",
                    )
            if stories:
                story_list = _story_link_list(project_id, stories, "qa" if passed else "bug")
                await broadcast({
                    "type": "agent_message",
                    "project_id": project_id,
                    "agent": "system",
                    "content": f"{'✅' if passed else '⚠️'} Jira Stories에 QA 결과 기록: {story_list}",
                })
            await _add_history(project_id, f"{'✅' if passed else '⚠️'} QA {'통과' if passed else '이슈 발견'}: {outputs.get('summary', '')}")

        # AutoTest 통과 → PR을 main에 자동 병합 + Jira 코멘트
        elif stage_name == "autotest":
            repo      = project_repos.get(project_id, "")
            pr_number = outputs.get("pr_number")
            merge_result = False
            if repo and pr_number:
                merge_result = await merge_pull_request(repo, pr_number)
                if merge_result == "conflict":
                    # main이 그새 앞서가서 충돌났다 — Implement가 그 브랜치를 이어받아
                    # 직접 rebase/merge로 풀게 한다. (counter-app에서 실제로 겪은
                    # 상황: 사람이 매번 로컬에서 수동으로 풀어야 했다.)
                    await _route_needs_rework_or_fail(pipeline, project_id, stage_name, {
                        "passed": False,
                        "summary": f"PR #{pr_number} 병합 충돌",
                        "feedback": (
                            f"PR #{pr_number}이 main과 충돌해서 자동 병합이 안 됩니다. "
                            f"브랜치에서 main을 merge(또는 rebase)해서 충돌을 해결하고 다시 push해주세요."
                        ),
                    })
                    return
                await broadcast({
                    "type": "agent_message", "project_id": project_id, "agent": "system",
                    "content": (
                        f"✅ AutoTest 통과 → PR #{pr_number} main에 자동 병합 완료"
                        if merge_result is True else
                        f"⚠️ AutoTest는 통과했지만 PR #{pr_number} 자동 병합에 실패했습니다 — 수동으로 확인해주세요."
                    ),
                })
            merged = merge_result is True
            for story in project_jira.get(project_id, {}).get("stories", []):
                await add_jira_comment(story,
                    "✅ AutoTest(CI) 통과 + PR main 병합 완료" if merged else "✅ AutoTest(CI) 통과 (병합은 수동 확인 필요)")
            await _add_history(project_id, "✅ AutoTest(CI) 통과 + PR 병합 완료" if merged else "✅ AutoTest(CI) 통과 (병합 수동 확인 필요)")

        # Release 완료 → Story 전부 Done으로 전환 (실제 작업이 끝났다고 볼 수 있는 시점)
        elif stage_name == "release":
            jira = project_jira.get(project_id, {})
            stories = jira.get("stories", [])
            for story in stories:
                await update_jira_status(story, "Done")
                await add_jira_comment(story, f"🚀 릴리즈 완료: {outputs.get('summary', '')}")
            if stories:
                story_list = _story_link_list(project_id, stories, "release")
                await broadcast({
                    "type": "agent_message", "project_id": project_id, "agent": "system",
                    "content": f"🚀 릴리즈 완료 — Jira Stories Done 처리: {story_list}",
                })
            await _add_history(project_id, f"🚀 릴리즈 완료: {outputs.get('summary', '')}")

        if stage_now_complete:
            await advance_pipeline(pipeline)
    elif event_type == "stage_failed":
        # agents/base/agent.py의 process_task가 예외를 던지면(크레딧 소진, 네트워크
        # 에러 등) 예전엔 에이전트 로컬 로그에만 찍히고 orchestrator는 전혀 몰라서
        # 스테이지가 "running"에 영원히 멈춰있었다(recoveryfit에서 20시간 넘게
        # 방치된 채 재현됨 — 사람이 컨테이너 로그를 직접 뒤져야만 알 수 있었음).
        # 이제 agent가 실패를 명시적으로 보고하면 즉시 실패로 반영해서 사람이
        # 기다리지 않고 바로 재실행/폐기할 수 있게 한다.
        stage_name = event.get("stage")
        error = event.get("error", "알 수 없는 오류")
        if stage_name in pipeline.stages:
            pipeline.mark_failed(stage_name, {"agent": agent_name, "error": error})
            await broadcast({"type": "stage_update", "project_id": project_id, "stage": stage_name, "status": "failed"})
            await broadcast({
                "type": "agent_message", "project_id": project_id, "agent": "system",
                "content": f"❌ '{stage_name}' 실행 중 오류로 실패했습니다 ({agent_name}): {error}",
            })
            await _add_history(project_id, f"❌ '{stage_name}' 실패({agent_name}): {error}")
            await broadcast({
                "type": "project_updated", "project_id": project_id,
                "projects": {project_id: _make_project_info(project_id)},
            })
    elif event_type == "progress":
        await broadcast({
            "type": "agent_progress",
            "project_id": project_id,
            "agent": agent_name,
            "progress": event.get("progress", 0),
            "message": event.get("message", ""),
        })
    elif event_type == "agent_phase":
        # QA(그리고 향후 다른 에이전트)가 "빌드 시작/성공, 코드 자체 수정,
        # 실기기 테스트 제출/대기" 같은 세부 작업 단위를 그대로 브라우저에
        # 넘긴다 — 웹은 이걸 스크롤되는 채팅이 아니라 에이전트 카드에 고정된
        # 단계별 마커로 그린다(web/app/page.tsx의 agent_phase 핸들러).
        phase_id = event.get("phase", "")
        # project_messages와 동일하게 서버에도 최신 상태를 남겨둔다 — 그래야
        # 이 이벤트가 지나간 뒤에 새로 연결된 탭도 _make_project_info를 통해
        # 지금까지의 칩을 그대로 복원할 수 있다(위 project_phases 주석 참고).
        project_phases.setdefault(project_id, {}).setdefault(agent_name, {})[phase_id] = {
            "label": event.get("label", ""), "status": event.get("status", ""),
            "detail": event.get("detail", ""), "ts": int(time.time() * 1000),
        }
        await broadcast({
            "type": "agent_phase",
            "project_id": project_id,
            "agent": agent_name,
            "stage": event.get("stage", ""),
            "phase": phase_id,
            "label": event.get("label", ""),
            "status": event.get("status", ""),
            "detail": event.get("detail", ""),
        })


async def setup_git_repo(pipeline: Pipeline) -> str | None:
    """Implement 스테이지 시작 전 GitHub 레포 생성 + 로컬 git 초기화 + 초기 커밋."""
    pid  = pipeline.project_id
    name = project_names.get(pid, pid)

    # 이미 레포 있으면 스킵
    if project_repos.get(pid):
        return project_repos[pid]

    if not GITHUB_TOKEN or not GITHUB_OWNER:
        return None

    await broadcast({"type": "agent_message", "project_id": pid, "agent": "system",
                     "content": "🔧 GitHub 레포지토리 생성 중..."})

    # 레포 이름: 프로젝트명 기반
    safe_name = name.lower().replace(" ", "-")
    github_url = await create_github_repo(safe_name, f"AI Team Manager — {name}")

    if not github_url:
        return None

    project_repos[pid] = f"{GITHUB_OWNER}/{safe_name}"

    # 워크스페이스에 git 초기화 + PM/Design 산출물 초기 커밋
    workspace = f"/workspace/{pid}"
    os.makedirs(workspace, exist_ok=True)
    repo_url = f"https://{GITHUB_TOKEN}@github.com/{GITHUB_OWNER}/{safe_name}.git"

    import subprocess
    cmds = [
        f"git -C {workspace} init",
        f"git -C {workspace} config user.email 'ai-team-manager@bot'",
        f"git -C {workspace} config user.name 'AI Team Manager'",
        f"git -C {workspace} remote add origin {repo_url}",
        f"git -C {workspace} add -A",
        f"git -C {workspace} commit -m 'init: PM & Design 산출물'",
        f"git -C {workspace} branch -M main",
        f"git -C {workspace} push -u origin main",
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd.split(), capture_output=True, timeout=30)
        except Exception as e:
            print(f"[git] {cmd[:40]}... 실패: {e}")

    await broadcast({"type": "agent_message", "project_id": pid, "agent": "system",
                     "content": f"✅ GitHub 레포 준비 완료: https://github.com/{GITHUB_OWNER}/{safe_name}"})

    return project_repos[pid]


STAGE_LABELS = {
    "design": "🎨 설계", "implement": "🤖 구현", "qa": "🧪 QA",
    "autotest": "✅ AutoTest(CI)", "release": "🚀 릴리즈",
}


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime())


def _doc(pid: str) -> dict:
    return project_docs.setdefault(pid, {"overview": "", "architecture": "", "history": [], "confluence_page_id": None})


def _esc(text: str) -> str:
    return html.escape(text or "(아직 없음)").replace("\n", "<br/>")


def _render_project_doc_html(pid: str) -> str:
    """프로젝트 하나당 Confluence 페이지 하나 — 개요/아키텍처/리소스 링크/개발
    히스토리를 마일스톤마다 갱신해서, 사람 팀이 위키 한 페이지에 프로젝트
    전반을 정리해두는 것과 같은 역할을 하게 한다."""
    doc  = _doc(pid)
    jira = project_jira.get(pid, {})
    repo = project_repos.get(pid, "")

    resources = []
    if repo:
        resources.append(f'<li>GitHub 레포: <a href="https://github.com/{repo}">{repo}</a></li>')
    if jira.get("jira_url"):
        resources.append(
            f'<li>Jira Epic: <a href="{jira["jira_url"]}">{jira.get("epic", "")}</a> '
            f'({len(jira.get("stories", []))}개 Story)</li>'
        )
    resources_html = "".join(resources) or "<li>(아직 없음)</li>"
    history_html = "".join(f"<li>{_esc(h)}</li>" for h in doc["history"][-50:]) or "<li>(아직 없음)</li>"

    return (
        f"<h1>개요</h1><p>{_esc(doc['overview'])}</p>"
        f"<h1>아키텍처</h1><p>{_esc(doc['architecture'])}</p>"
        f"<h1>리소스 링크</h1><ul>{resources_html}</ul>"
        f"<h1>개발 히스토리</h1><ul>{history_html}</ul>"
    )


async def _sync_confluence_doc(pid: str):
    doc = _doc(pid)
    pname = project_names.get(pid, pid)
    body = _render_project_doc_html(pid)
    if doc.get("confluence_page_id"):
        await update_confluence_page(doc["confluence_page_id"], pname, body)
    else:
        result = await create_confluence_page(pname, body)
        if result:
            doc["confluence_page_id"] = result["id"]
            project_jira.setdefault(pid, {})["confluence_url"] = result["url"]


async def _add_history(pid: str, entry: str):
    _doc(pid)["history"].append(f"{_now_str()} — {entry}")
    await _sync_confluence_doc(pid)


async def _jira_stage_started(project_id: str, stage_name: str):
    """스테이지가 시작될 때마다 관련 Story에 코멘트를 남겨서 Jira만 봐도
    전체 진행 상황을 추적할 수 있게 한다 (planning은 아직 Epic/Story가
    없는 시점이라 대상에서 제외)."""
    label = STAGE_LABELS.get(stage_name)
    if not label:
        return
    stories = project_jira.get(project_id, {}).get("stories", [])
    for story in stories:
        target = _stage_issue_target(project_id, story, stage_name)
        await add_jira_comment(target, f"{label} 단계 시작")


STAGE_LABELS_KO = {"qa": "QA", "autotest": "AutoTest(CI)"}


async def _route_needs_rework_or_fail(pipeline: Pipeline, project_id: str, stage_name: str, outputs: dict):
    """QA/AutoTest 실패를 Implement 재작업으로 돌릴지, 예산을 다 써서 멈추고
    사람을 부를지 결정한다. 두 스테이지가 같은 카운터(qa_retry_counts)를
    공유해서, 어느 쪽에서 실패가 반복되든 총 MAX_QA_RETRIES회로 묶인다 —
    "QA 3번 + AutoTest 3번"처럼 재시도가 배로 불어나는 걸 막는다.

    예산이 소진됐을 때, 이 프로젝트가 manual_implement(외부 세션 코딩 우회)를
    켜둔 상태라면 그냥 멈추고 사람이 API를 수동 호출하길 기다리지 않는다 —
    manual_implement가 켜져 있다는 건 이미 "이 프로젝트의 implement는 컨테이너
    대신 외부 세션이 처리한다"는 명시적 선택이므로, 예산 소진도 같은 경로로
    묶어서 MANUAL_TASKS_DIR에 태스크 파일만 쓰고(_retry_implement_with_feedback→
    _send_task_or_manual) 카운터를 리셋해 계속 진행한다. manual_implement가
    꺼진 프로젝트만 기존처럼 FAILED로 멈추고 사람의 수동 개입(retry-implement
    API)을 기다린다."""
    label = STAGE_LABELS_KO.get(stage_name, stage_name)
    count = qa_retry_counts.get(project_id, 0)
    feedback = outputs.get("feedback", outputs.get("summary", ""))

    if count < MAX_QA_RETRIES:
        qa_retry_counts[project_id] = count + 1
        await broadcast({
            "type": "agent_message", "project_id": project_id, "agent": "system",
            "content": f"🔁 {label} 실패 — Implement에 재작업 요청 ({count + 1}/{MAX_QA_RETRIES})\n{feedback}",
        })
        for story in project_jira.get(project_id, {}).get("stories", []):
            await add_jira_comment(story, f"🔁 {label} 재작업 요청 ({count + 1}/{MAX_QA_RETRIES}): {feedback}")
        await _add_history(project_id, f"🔁 {label} 재작업 요청 ({count + 1}/{MAX_QA_RETRIES}): {feedback}")
        await _retry_implement_with_feedback(pipeline, feedback)
    elif project_manual_implement.get(project_id, False):
        qa_retry_counts[project_id] = 0
        await broadcast({
            "type": "agent_message", "project_id": project_id, "agent": "system",
            "content": f"🖐 {label} 재작업을 {MAX_QA_RETRIES}회 시도했지만 여전히 실패 — 외부 세션(manual-agent-work) 작업 요청으로 전환합니다.\n{feedback}",
        })
        for story in project_jira.get(project_id, {}).get("stories", []):
            await add_jira_comment(story, f"🖐 {label} 재작업 {MAX_QA_RETRIES}회 실패 — 외부 세션 작업 요청으로 전환: {feedback}")
        await _add_history(project_id, f"🖐 {label} 재작업 {MAX_QA_RETRIES}회 실패 — 외부 세션 작업 요청으로 전환: {feedback}")
        await _retry_implement_with_feedback(pipeline, feedback)
    else:
        pipeline.mark_failed(stage_name, outputs)
        await broadcast({"type": "stage_update", "project_id": project_id, "stage": stage_name, "status": "failed"})
        await broadcast({
            "type": "agent_message", "project_id": project_id, "agent": "system",
            "content": f"❌ {label} 재작업을 {MAX_QA_RETRIES}회 시도했지만 여전히 실패합니다 — 자동 진행을 멈춥니다. 수동 확인이 필요합니다.",
        })
        for story in project_jira.get(project_id, {}).get("stories", []):
            await add_jira_comment(story, f"❌ {label} 재작업 {MAX_QA_RETRIES}회 실패 — 수동 확인 필요.\n{feedback}")
        await _add_history(project_id, f"❌ {label} 재작업 {MAX_QA_RETRIES}회 실패 — 수동 확인 필요: {feedback}")


async def _dispatch_chat_triage(pipeline: Pipeline, user_message: str):
    """이미 지나간 단계에 대한 채팅 후속 요청을 사람이 retry-design/retry-implement
    API를 직접 호출하지 않아도 되게, PM에게 검토를 맡겨 자동으로 알맞은 단계를
    다시 돌리기 위한 트리아지 태스크. 새 컨테이너를 띄우지 않고 이미 떠 있는
    프로젝트 전용 pm 큐로 보낸다(Docker 메모리 제약 때문에 에이전트를 늘리지 않음)."""
    pid = pipeline.project_id
    chat_triage_in_flight[pid] = time.time()
    context = {
        name: s.outputs
        for name, s in pipeline.stages.items()
        if s.status == StageStatus.COMPLETED
    }
    context["stage_status"] = {name: s.status.value for name, s in pipeline.stages.items()}
    if project_repos.get(pid):
        context["github_repo"] = project_repos[pid]
    # 기존 Jira 이슈(시나리오) 목록을 넘겨서, PM이 이번 요청이 그 중 하나를
    # 고치는 건지 완전히 새로운 화면/기능인지 판단할 수 있게 한다 — 이게 있어야
    # 재작업을 "이슈 하나"로 좁히거나 새 이슈를 만들지 판단 가능.
    existing_issues = _existing_issues_context(pid)
    if existing_issues:
        context["existing_issues"] = existing_issues
    await redis.send_task("pm", pid, {
        "project_id": pid,
        "stage": "chat_triage",
        "instruction": user_message,
        "context": context,
        "github_repo": project_repos.get(pid, ""),
    })


async def _create_ad_hoc_jira_story(project_id: str, title: str) -> str | None:
    """채팅 트리아지가 "기존 어느 이슈와도 안 맞는 완전히 새로운 화면/기능"이라고
    판단했을 때, 최초 기획 단계의 create_jira_stories를 그대로 재사용해서 이슈
    하나를 이 프로젝트의 기존 Epic 아래에 추가로 만든다. epic/jira_url/
    confluence_url 등 project_jira의 나머지 필드는 건드리지 않고 stories/
    story_titles에만 append한다."""
    jira = project_jira.setdefault(project_id, {})
    epic = jira.get("epic")
    if not epic:
        return None
    pname = project_names.get(project_id, project_id)
    records = await create_jira_stories(epic, pname, [title])
    if not records:
        return None
    key = records[0]["key"]
    jira.setdefault("stories", []).append(key)
    jira.setdefault("story_titles", {})[key] = records[0]["title"]
    if records[0].get("subtasks"):
        jira.setdefault("story_subtasks", {})[key] = records[0]["subtasks"]
    sprint = projects[project_id].sprint if project_id in projects else 1
    await broadcast({
        "type": "agent_message", "project_id": project_id, "agent": "system",
        "content": f"📋 [Sprint {sprint}] 새 Jira 이슈 생성: {_story_link(project_id, key, 'spec')}",
    })
    return key


async def _sync_new_requirements_to_epic(project_id: str, pm_text: str) -> list[dict]:
    """이미 Epic이 있는 프로젝트가 재기획되어 PRD에 요구사항이 추가됐을 때
    (예: "앱 시작 화면 디자인이 없네" 같은 채팅 요청으로 planning이 통째로
    다시 돈 경우), 그 새 요구사항들을 기존 Epic 하나에 뭉뚱그려 코멘트로만
    남기지 않고 각각 별도 Jira 이슈(스토리)로 만든다 — Epic은 최상위 그대로
    두되 그 내부 구현 단위는 사람 팀처럼 이슈별로 추적되게 하기 위함.

    같은 요구사항(REQ ID)에 대해 중복 이슈가 쌓이지 않도록 project_jira[pid]
    ["synced_req_ids"]에 이미 이슈화한 REQ ID를 기록해두고 건너뛴다 — PM이
    재작업마다 전체 요구사항을 다시 나열할 수도 있기 때문."""
    jira = project_jira.setdefault(project_id, {})
    epic = jira.get("epic")
    if not epic:
        return []

    pname = project_names.get(project_id, project_id)
    _, requirements = parse_pm_requirements(pm_text, pname)

    synced_ids = set(jira.setdefault("synced_req_ids", []))
    new_requirements = []
    for req in requirements:
        req_id = req.get("id") if isinstance(req, dict) else req
        if req_id in synced_ids:
            continue
        new_requirements.append(req)
        synced_ids.add(req_id)

    if not new_requirements:
        return []

    records = await create_jira_stories(epic, pname, new_requirements)
    if not records:
        return []

    jira["synced_req_ids"] = list(synced_ids)
    for r in records:
        jira.setdefault("stories", []).append(r["key"])
        jira.setdefault("story_titles", {})[r["key"]] = r["title"]
        if r.get("subtasks"):
            jira.setdefault("story_subtasks", {})[r["key"]] = r["subtasks"]

    return records


async def _handle_chat_triage_result(pipeline: Pipeline, project_id: str, outputs: dict):
    """PM의 트리아지 결정을 받아 실제로 알맞은 단계를 재실행한다 — 기존
    _retry_design_with_feedback/_retry_implement_with_feedback를 그대로 재사용해서
    로직을 중복시키지 않는다(승인 게이트 리셋 등 기존 동작을 그대로 물려받음).

    PM이 판단한 target(기존 이슈 key | "new" | None)을 실제 scenario_key로
    해석한다 — target이 "new"면 새 Jira 이슈를 먼저 만들고, 존재하지 않는 키를
    가리키면(PM이 잘못 판단했거나 project_jira 상태를 못 봤을 수 있으므로) 안전하게
    scenario_key=None(전체 재작업)으로 폴백한다. agent.py의 parse_triage_decision은
    project_jira를 모르기 때문에 이 검증은 여기서만 할 수 있다."""
    chat_triage_in_flight.pop(project_id, None)
    scope           = outputs.get("scope", "none")
    feedback        = outputs.get("feedback", "")
    reply           = outputs.get("reply", "")
    target          = outputs.get("target")
    new_story_title = outputs.get("new_story_title", "")

    scenario_key = None
    if target == "new" and new_story_title:
        scenario_key = await _create_ad_hoc_jira_story(project_id, new_story_title)
    elif target and target in project_jira.get(project_id, {}).get("stories", []):
        scenario_key = target

    if scope == "design" and feedback:
        scope_note = f" ({_story_link(project_id, scenario_key, 'design')}만)" if scenario_key else ""
        scope_note_plain = f" ({_story_plain(project_id, scenario_key, 'design')}만)" if scenario_key else ""
        await broadcast({
            "type": "agent_message", "project_id": project_id, "agent": "system",
            "content": f"🔁 PM 판단: 디자인 재작업이 필요합니다{scope_note} — designer/architect부터 다시 시작합니다.\n{feedback}",
        })
        await _add_history(project_id, f"🔁 채팅 요청 → 디자인 재작업{scope_note_plain}: {feedback}")
        await _retry_design_with_feedback(pipeline, feedback, [scenario_key] if scenario_key else None)
    elif scope == "implement" and feedback:
        scope_note = f" ({_story_link(project_id, scenario_key, 'impl')}만)" if scenario_key else ""
        scope_note_plain = f" ({_story_plain(project_id, scenario_key, 'impl')}만)" if scenario_key else ""
        await broadcast({
            "type": "agent_message", "project_id": project_id, "agent": "system",
            "content": f"🔁 PM 판단: 구현 수정이 필요합니다{scope_note} — Implement에 재작업을 요청합니다.\n{feedback}",
        })
        await _add_history(project_id, f"🔁 채팅 요청 → 구현 재작업{scope_note_plain}: {feedback}")
        await _retry_implement_with_feedback(pipeline, feedback, [scenario_key] if scenario_key else None)
    else:
        await broadcast({
            "type": "agent_message", "project_id": project_id, "agent": "system",
            "content": reply or "PM이 검토했지만 추가로 다시 진행할 단계가 없다고 판단했습니다.",
        })


async def _retry_design_with_feedback(pipeline: Pipeline, feedback: str, scenario_keys: list[str] | None = None):
    """디자인 목업이 마음에 안 들거나 화면을 더 추가하고 싶을 때,
    사람이 피드백과 함께 디자인부터 다시 돌리는 수동 개입용 엔드포인트.
    design뿐 아니라 그 위에서 이미 진행된 implement/qa/autotest도 새 디자인을
    반영해서 다시 만들어야 하므로 함께 PENDING으로 되돌린다. implement의
    approved 플래그도 초기화한다 — 안 그러면 새 목업을 사람이 다시 확인하지
    않은 채 구현이 바로 시작돼버려 승인 게이트를 둔 의미가 없어진다.
    pipeline.instruction 자체는 보존하고(원본 PRD 유지) 이번 재작업 피드백만 덧붙인다.

    scenario_keys가 주어지고 그중 실제 존재하는 Jira 이슈 키가 하나 이상이면,
    design 스테이지를 "이 키들만" 다시 만들도록 좁힌다(scenario_scope) — 그래서
    design.outputs를 통째로 비우지 않는다(다른 시나리오들의 요약을 날리면 안
    되므로). 존재하는 키가 하나도 없거나 안 주어지면 예전처럼 스테이지 전체를
    초기화한다."""
    story_titles = project_jira.get(pipeline.project_id, {}).get("story_titles", {})
    scoped_keys = [k for k in (scenario_keys or []) if k in story_titles]
    scoped = bool(scoped_keys)

    design_stage = pipeline.stages["design"]
    design_stage.status = StageStatus.PENDING
    # design 자체가 승인 게이트(PM→Design)를 갖게 된 뒤에도, 이 재시도는 "design을
    # 직접 다시 돌려달라"는 명시적 사람 개입이지 PM 단계로 되돌아가는 게 아니다.
    # approved를 건드리지 않으면 advance_pipeline이 PM→Design 게이트를 다시 띄워
    # 정작 design 재작업 자체는 큐에 안 들어가는 버그가 생긴다.
    design_stage.approved = True
    if scoped:
        design_stage.scenario_scope = [{"key": k, "title": story_titles[k]} for k in scoped_keys]
    else:
        design_stage.outputs = {}
        design_stage.scenario_scope = None

    # implement/qa/autotest는 새 디자인으로 다시 돌아야 하지만, 이전 라운드가
    # 아직 머지 전(autotest가 COMPLETED까지 못 감)이라면 implement의 branch/
    # pr_number는 지우지 않고 남겨둔다 — advance_pipeline이 implement를 다시
    # 돌릴 때 이 branch를 retry_branch로 넘겨서(QA 실패 재시도 경로인
    # _retry_implement_with_feedback와 동일한 재사용 규칙) 매번 새 브랜치+새
    # PR을 또 만들지 않고 기존 PR을 이어서 고치게 한다. 예전엔 여기서 무조건
    # outputs를 비웠어서, "디자인 그대로 재실행"만 해도 PR이 계속 새로 쌓이고
    # 아무것도 머지되지 않는 문제가 있었다(recoveryfit에서 실제 재현: PR #10
    # → #12 → #14가 전부 동일 내용인데 하나도 머지 안 됨 — QA 재시도가
    # "이전 시도(브랜치: 알 수 없음)"라고 보고할 정도로 branch 정보 자체가
    # 여기서 날아갔었다). 반대로 이전 라운드가 이미 autotest까지 끝나 머지된
    # 뒤라면 그 브랜치는 이미 죽은(main에 합쳐진) 브랜치이므로 재사용하지 않고
    # 새로 만든다(안전).
    prior_round_merged = pipeline.stages["autotest"].status == StageStatus.COMPLETED

    for name in ("implement", "qa", "autotest"):
        pipeline.stages[name].status = StageStatus.PENDING
        if name == "implement" and not prior_round_merged:
            continue
        pipeline.stages[name].outputs = {}
    pipeline.stages["implement"].approved = False

    tag = f"[디자인 재작업 요청 - {', '.join(scoped_keys)}]" if scoped else "[디자인 재작업 요청]"
    pipeline.instruction += f"\n\n{tag} {feedback}"
    await advance_pipeline(pipeline)


async def _retry_planning_with_feedback(
    pipeline: Pipeline, feedback: str, full_rewrite: bool, jira_issue: str | None = None
):
    """PM이 만든 기획 자체를 다시 짜야 할 때(요구사항이 바뀌었거나 완전히 새로
    시작해야 할 때) planning부터 전체 파이프라인을 되돌리는 수동 개입용 함수.
    design 재작업보다 상위 단계라 design 이하 전부와 implement/release의
    approved 플래그까지 모두 초기화해야 한다 — 안 그러면 옛 요구사항 기준으로
    이미 승인된 상태가 새 요구사항에 그대로 남아있게 된다.
    full_rewrite=True면 pipeline.instruction 자체를 새 내용으로 교체하고
    (완전히 다른 요구사항으로 바뀌는 경우), False면 기존 PRD에 이번 변경사항만
    덧붙인다(기존 범위 안에서 특정 요청/이슈 하나만 바뀌는 경우). jira_issue가
    있으면 어떤 이슈에 대한 변경인지 추적할 수 있게 해당 스토리에도 코멘트를
    남긴다(planning 완료 후 붙는 Epic 코멘트와는 별개).

    이 시점이 "전체 파이프라인 실행 회차"가 바뀌는 유일한 지점이라 pipeline.sprint를
    여기서 증가시킨다 — 이후 이번 라운드에서 새로 생성되는 이슈/디자인 산출물에
    Sprint 번호를 태그로 붙여서, mtime만으로 짐작하던 "이번 라운드 결과물"을
    명시적으로 구분할 수 있게 한다."""
    pipeline.sprint += 1
    for name in ("planning", "design", "implement", "qa", "autotest", "release"):
        pipeline.stages[name].status = StageStatus.PENDING
        pipeline.stages[name].outputs = {}
    # design/autotest도 이제 승인 게이트를 가지므로(design/implement/autotest/release
    # 전부) 재기획 시 전부 다시 승인받게 한다 — 안 그러면 옛 요구사항 기준으로 이미
    # 승인된 상태가 새 요구사항에 그대로 남아 게이트를 건너뛰게 된다.
    pipeline.stages["design"].approved = False
    pipeline.stages["implement"].approved = False
    pipeline.stages["autotest"].approved = False
    pipeline.stages["release"].approved = False

    if full_rewrite:
        pipeline.instruction = feedback
    else:
        tag = f" - {jira_issue}" if jira_issue else ""
        pipeline.instruction += f"\n\n[요구사항 변경 요청{tag}] {feedback}"

    if jira_issue:
        await add_jira_comment(jira_issue, f"📝 요구사항 변경 요청: {feedback}")

    await advance_pipeline(pipeline)


async def _send_task_or_manual(pipeline: Pipeline, agent_name: str, stream_project_id: str | None, stage_name: str, task: dict):
    """redis.send_task의 공용 앞단 — project_manual_implement[pid]가 켜진 프로젝트의
    implement 스테이지는 컨테이너 큐 대신 MANUAL_TASKS_DIR에 태스크 파일만 쓰고
    사람(Claude Code 세션)이 완료 후 POST .../manual-result로 보고하게 한다.
    QA는 대상이 아니다(항상 컨테이너 에이전트로 검증) — 그래서 stage_name이
    "implement"인지 먼저 확인한다. advance_pipeline의 최초 디스패치뿐 아니라
    _retry_implement_with_feedback(QA 실패 후 재작업 등)도 전부 이 함수를
    거쳐야 한다 — 재시도 라운드가 API 과금이 가장 자주 발생하는 경로라 거기를
    놓치면 비용 절감 효과가 거의 없다.

    디스패치 방식과 무관하게(큐로 나가든 수동 파일로 쓰이든) 항상 이 에이전트가
    "지금 실제로 뭘 지시받았는지"를 stage.current_task에 스냅샷으로 남긴다 —
    플로우차트 탭이 원시 로그 대신 구조화된 태스크 상세를 보여주는 용도."""
    pipeline.stages[stage_name].current_task[agent_name] = {
        "instruction": task.get("instruction", ""),
        "dispatched_at": time.time(),
        "manual": stage_name == "implement" and project_manual_implement.get(task["project_id"], False),
    }
    if stage_name == "implement" and project_manual_implement.get(task["project_id"], False):
        os.makedirs(MANUAL_TASKS_DIR, exist_ok=True)
        task_path = f"{MANUAL_TASKS_DIR}/{task['project_id']}_{stage_name}_{agent_name}_{int(time.time())}.json"
        with open(task_path, "w") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)
        print(f"[manual] '{stage_name}'({agent_name}) 태스크 큐 대신 파일로 기록: {task_path}")
        await broadcast({
            "type": "agent_message", "project_id": task["project_id"], "agent": "system",
            "content": f"🖐 '{stage_name}' 스테이지는 수동 처리 모드 — 태스크가 {task_path}에 대기 중입니다.",
        })
        return
    await redis.send_task(agent_name, stream_project_id, task)


_NO_BLIND_REVERT_GUIDANCE = (
    "중요: 테스트가 실패한다고 무조건 앱 코드를 옛날 상태로 되돌리지 마세요. "
    "`git log --oneline -5`로 최근 커밋 메시지를 먼저 확인해서, 최근에 의도적으로 "
    "디자인/동작을 바꾼 거라면(예: 배경색·버튼 종류·레이아웃 변경 커밋이 최근에 있음) "
    "실패 원인은 그 변경을 못 따라간 오래된 테스트 쪽일 가능성이 높습니다 — 그럴 땐 앱 "
    "코드가 아니라 테스트를 최신 동작에 맞게 갱신하세요. 반대로 그런 의도적 변경 이력이 "
    "없다면 앱 코드 쪽 버그일 가능성이 높습니다.\n\n"
    "그리고 이것도 중요합니다 — QA는 매 라운드 integration_test/scenario_test.dart를 "
    "LLM으로 처음부터 새로 생성합니다. 그러니 실패 원인이 '최근 의도적 설계 변경을 "
    "테스트가 못 따라감'도 아니고 '앱이 요구사항을 실제로 안 지킴'도 아니라, 테스트 코드 "
    "자체의 기술적인 가정 실수인 경우가 있습니다 — 예: 위젯 타입을 잘못 추측(실제론 "
    "Icon인데 CircleAvatar를 찾는다든지), RichText/Text.rich 안의 TextSpan 조각을 "
    "find.text()로 정확히 매칭하려 함(RichText는 findRichText: true 없이는 아예 안 "
    "잡힘), 비동기 초기화 타이밍을 감안 안 한 pump 방식 등. 이런 경우 앱 코드(lib/**)는 "
    "절대 건드리지 마세요 — 그 라운드의 특정 테스트 문구에 맞춰 앱 위젯 구조를 바꿔봐야 "
    "다음 QA 라운드가 완전히 다른 테스트를 새로 생성하면 그 변경은 무의미해지고, 오히려 "
    "불필요한 앱 코드 변경만 쌓입니다. 이 경우엔 integration_test/scenario_test.dart만 "
    "고치고(다른 시나리오는 건드리지 말 것), `flutter test`/`xvfb-run flutter test`로 "
    "실제로 통과하는지 반드시 실행해서 확인한 뒤 커밋하세요."
)


async def _retry_implement_with_feedback(pipeline: Pipeline, feedback: str, scenario_keys: list[str] | None = None):
    """QA는 결과물(APK 등)을 받아서 테스트만 한다 — 테스트할 결과물 자체가
    없으면(pubspec.yaml 없음, 빌드 실패, APK 못 찾음 등 needs_rework) QA가
    직접 고치는 게 아니라 Implement에 구체적인 이유와 함께 재작업을 요청한다.
    pipeline.instruction 자체는 안 건드리고(다른 재실행에 영향 없게) 이번
    태스크에만 피드백을 덧붙여서 보낸다.

    scenario_keys가 주어지고 그중 실제 존재하는 Jira 이슈 키가 하나 이상이면,
    누적된 pipeline.instruction 전체 대신 "이 화면/기능들만" 범위로 좁힌
    instruction을 보낸다 — implement 컨테이너(agents/implement_openhands/run.py)가
    이 범위 제한 문구를 보고 design/applied의 다른 시나리오는 안 건드리도록
    유도한다(강제는 아님 — OpenHands는 자유도 높은 코딩 에이전트라 프롬프트로
    유도하는 것 이상은 못 함)."""
    pid = pipeline.project_id
    # 실패했던 브랜치를 기억해뒀다가 그대로 이어서 고치게 한다 — 지우기 전에 먼저 챙긴다.
    prior_branch = pipeline.stages["implement"].outputs.get("branch")

    pipeline.stages["implement"].status = StageStatus.PENDING
    pipeline.stages["implement"].outputs = {}
    pipeline.stages["qa"].status = StageStatus.PENDING
    pipeline.stages["qa"].outputs = {}

    context = {
        name: s.outputs
        for name, s in pipeline.stages.items()
        if s.status == StageStatus.COMPLETED
    }
    if project_repos.get(pid):
        context["github_repo"] = project_repos[pid]
    if prior_branch:
        context["retry_branch"] = prior_branch

    story_titles = project_jira.get(pid, {}).get("story_titles", {})
    scoped_keys = [k for k in (scenario_keys or []) if k in story_titles]
    scoped = bool(scoped_keys)

    if scoped:
        context["scenario_keys"] = scoped_keys
        scope_desc = ", ".join(f"{k} - {story_titles[k]}" for k in scoped_keys)
        files_desc = ", ".join(f"design/applied/{k}.html" for k in scoped_keys)
        instruction = (
            f"[범위 제한: {scope_desc}] {feedback}\n\n"
            f"{files_desc}에 해당하는 화면/기능만 반영하세요. "
            f"다른 화면/기능은 이미 반영돼 있으니 절대 건드리지 마세요.\n\n"
            f"{_NO_BLIND_REVERT_GUIDANCE}"
        )
    else:
        instruction = (
            f"{pipeline.instruction}\n\n"
            f"[QA/AutoTest 재작업 요청] 이전 시도(브랜치: {prior_branch or '알 수 없음'})에서 다음 문제로 "
            f"테스트/CI를 통과하지 못했습니다 — 처음부터 다시 만들지 말고 이미 작성된 코드를 그대로 "
            f"둔 채 아래 문제만 구체적으로 고치거나 부족한 부분만 추가하세요:\n{feedback}\n\n"
            f"{_NO_BLIND_REVERT_GUIDANCE}"
        )

    pipeline.mark_running("implement")
    _clear_phases(pid, ["implement"])
    await broadcast({"type": "stage_update", "project_id": pid, "stage": "implement", "status": "running"})
    await _send_task_or_manual(pipeline, "implement", None, "implement", {
        "project_id": pid,
        "stage": "implement",
        "instruction": instruction,
        "context": context,
        "github_repo": project_repos.get(pid, ""),
    })


def _stage_issue_target(project_id: str, story_key: str, stage: str) -> str:
    """story_key에 대해 이 stage(design/implement/qa)가 실제로 코멘트/상태를
    남겨야 할 Jira 이슈를 반환한다 — 있으면 그 stage 전용 하위 작업(Subtask),
    없으면 story 자신에 폴백한다(하위 작업 도입 전에 만들어진 스토리이거나
    생성이 실패한 경우, 또는 autotest/release처럼 애초에 하위 작업이 없는
    stage). 이게 없으면 design/impl/qa 코멘트가 전부 스토리 하나에 뒤섞여
    Jira만 보고는 단계별 진행 상황을 구분할 수 없었다."""
    subtasks = project_jira.get(project_id, {}).get("story_subtasks", {}).get(story_key, {})
    return subtasks.get(stage, story_key)


def _archive_qa_recording(project_id: str, sprint: int, ts: str) -> str | None:
    """이번 라운드 QA 녹화(qa_recording.mp4, 매 라운드 같은 경로에 덮어써짐)를
    스프린트 태그가 붙은 이력 파일로 복사해서 남긴다 — design/history와 같은
    패턴(publish_design 참고). 이게 없으면 지난 스프린트 영상은 다음 라운드가
    시작되는 순간 덮어써져서 웹에서도 Jira 링크로도 다시 못 본다.
    반환: "sprint3_20260818T120000" 같은 버전 문자열, 원본이 없으면 None."""
    src = f"/workspace/{project_id}/qa_recording.mp4"
    if not os.path.exists(src):
        return None
    hist_dir = f"/workspace/{project_id}/qa/history"
    os.makedirs(hist_dir, exist_ok=True)
    version = f"sprint{sprint}_{ts}"
    shutil.copyfile(src, f"{hist_dir}/{version}.mp4")
    return version


def _existing_issues_context(project_id: str) -> list[dict]:
    """이 프로젝트에 이미 만들어진 Jira 스토리 목록을 [{"key","title"}, ...]로 반환.
    PM이 재기획(planning 재실행)이나 채팅 트리아지를 할 때 이미 있는 이슈를 보고
    판단할 수 있게 넘기는 용도 — 안 보이면 매 스프린트 같은 요구사항을 다른 문구로
    다시 써서 _sync_new_requirements_to_epic의 텍스트 기반 dedup을 통과해 중복
    이슈가 쌓이는 문제가 있었다."""
    jira = project_jira.get(project_id, {})
    if not jira.get("stories"):
        return []
    story_titles = jira.get("story_titles", {})
    return [{"key": key, "title": story_titles.get(key, key)} for key in jira["stories"]]


def _completed_context(pipeline: Pipeline) -> dict:
    """완료된 모든 스테이지의 outputs를 스테이지명으로 묶어 다음 태스크 context로
    쓴다 — advance_pipeline(main.py:899-906 부근)이 쓰는 것과 같은 구성."""
    ctx = {
        name: s.outputs
        for name, s in pipeline.stages.items()
        if s.status == StageStatus.COMPLETED
    }
    if project_repos.get(pipeline.project_id):
        ctx["github_repo"] = project_repos[pipeline.project_id]
    return ctx


async def _retry_qa_with_feedback(pipeline: Pipeline, feedback: str):
    """플로우차트 탭의 Implement→QA는 게이트가 없어 자동으로 넘어가지만, QA 자체를
    (같은 구현물로) 다시 돌리고 싶을 때 쓰는 수동 개입용 함수. QA가 바뀌면 그 결과에
    의존하는 autotest도 다시 돌아야 하므로 함께 초기화한다. pipeline.instruction은
    안 건드리고(다른 재실행에 영향 없게) 이번 태스크에만 피드백을 덧붙인다."""
    pid = pipeline.project_id
    pipeline.stages["qa"].status = StageStatus.PENDING
    pipeline.stages["qa"].outputs = {}
    pipeline.stages["autotest"].status = StageStatus.PENDING
    pipeline.stages["autotest"].outputs = {}
    pipeline.stages["autotest"].approved = False

    context = _completed_context(pipeline)
    instruction = pipeline.instruction
    if feedback:
        instruction += f"\n\n[QA 재실행 요청] {feedback}"

    pipeline.mark_running("qa")
    _clear_phases(pid, ["qa"])
    await broadcast({"type": "stage_update", "project_id": pid, "stage": "qa", "status": "running"})
    await _send_task_or_manual(pipeline, "qa", None, "qa", {
        "project_id": pid, "stage": "qa", "instruction": instruction,
        "context": context, "github_repo": project_repos.get(pid, ""),
        "manual_qa_build_fix": project_manual_qa_build.get(pid, False),
    })


async def _retry_autotest_with_feedback(pipeline: Pipeline, feedback: str):
    """QA→AutoTest 게이트에서 "No"를 눌러 오토테스트(CI)만 다시 돌릴 때 쓰는 함수.
    autotest는 브랜치/커밋을 그대로 두고 CI만 재확인하는 성격이라 free-text
    피드백은 참고용으로만 instruction에 덧붙인다."""
    pid = pipeline.project_id
    pipeline.stages["autotest"].status = StageStatus.PENDING
    pipeline.stages["autotest"].outputs = {}

    context = _completed_context(pipeline)
    instruction = pipeline.instruction
    if feedback:
        instruction += f"\n\n[AutoTest 재실행 요청] {feedback}"

    pipeline.mark_running("autotest")
    _clear_phases(pid, ["autotest"])
    await broadcast({"type": "stage_update", "project_id": pid, "stage": "autotest", "status": "running"})
    await _send_task_or_manual(pipeline, "autotest", None, "autotest", {
        "project_id": pid, "stage": "autotest", "instruction": instruction,
        "context": context, "github_repo": project_repos.get(pid, ""),
    })


async def _retry_release_with_feedback(pipeline: Pipeline, feedback: str):
    """AutoTest→Release 게이트에서 "No"를 눌러 릴리즈만 다시 돌릴 때 쓰는 함수.
    release는 implement/qa/autotest처럼 전역 싱글턴이 아니라 TeamSpawner가 프로젝트마다
    격리해서 띄우는 컨테이너이므로(GLOBAL_SHARED_AGENTS에 없음) stream_project_id를
    None이 아니라 project_id로 넘겨야 큐가 프로젝트별로 분리된다."""
    pid = pipeline.project_id
    pipeline.stages["release"].status = StageStatus.PENDING
    pipeline.stages["release"].outputs = {}

    context = _completed_context(pipeline)
    instruction = pipeline.instruction
    if feedback:
        instruction += f"\n\n[Release 재실행 요청] {feedback}"

    pipeline.mark_running("release")
    _clear_phases(pid, ["release"])
    await broadcast({"type": "stage_update", "project_id": pid, "stage": "release", "status": "running"})
    await _send_task_or_manual(pipeline, "release", pid, "release", {
        "project_id": pid, "stage": "release", "instruction": instruction,
        "context": context, "github_repo": project_repos.get(pid, ""),
    })


def _reset_stage_cascade(pipeline: Pipeline, stage_name: str):
    """플로우차트 탭의 "폐기" 버튼 — stage_name과 그 이후로 의존하는 모든 스테이지를
    PENDING/빈 outputs/미승인으로 되돌린다(기존 _retry_planning_with_feedback/
    _retry_design_with_feedback의 cascade 리셋과 동일 패턴). advance_pipeline은
    일부러 호출하지 않는다 — 그래야 파이프라인이 그 자리에 멈춘 채로, 프론트가
    stages 상태만 보고 "직전에 완료된 스테이지"의 결정 블록을 다시 활성 상태로
    그릴 수 있다(별도의 '커서' 개념을 서버에 만들지 않기 위함)."""
    order = ["planning", "design", "implement", "qa", "autotest", "release"]
    idx = order.index(stage_name)
    for name in order[idx:]:
        stage = pipeline.stages[name]
        stage.status = StageStatus.PENDING
        stage.outputs = {}
        stage.approved = False


def _cancel_running_stage(pipeline: Pipeline, stage_name: str):
    """"취소" 버튼 — 아직 실행 중(running)인 스테이지를 되돌린다. _reset_stage_cascade와
    달리 대상 스테이지 자체의 outputs/agents_done/approved는 보존한다 — design처럼
    에이전트가 여럿(designer+architect)인 스테이지에서 한쪽만 안 끝났는데 취소하면,
    이미 끝낸 쪽의 산출물까지 날리고 처음부터 다시 돌게 만들던 문제가 있었다
    (recoveryfit에서 실제 재현: architect는 이미 끝났는데 취소→재승인하면 architect
    까지 처음부터 다시 돎). keep_agents_done을 세워두면 advance_pipeline이 다음번
    이 스테이지를 돌릴 때 agents_done에 이미 있는 에이전트는 건너뛰고 나머지만
    태스크를 보낸다. approved는 건드리지 않아도 된다 — gateIsPending(프론트)이
    보는 건 status뿐이라 PENDING이면 어차피 게이트가 다시 뜨고, "Yes"를 누르면
    advance_pipeline이 (approve() 자체는 상태가 WAITING이 아니라 no-op이어도)
    무조건 실행돼 이어서 진행된다. 다운스트림 스테이지는 어차피 아직 시작 전이라
    (design도 안 끝났으므로) _reset_stage_cascade와 동일하게 완전히 초기화한다."""
    order = ["planning", "design", "implement", "qa", "autotest", "release"]
    idx = order.index(stage_name)
    stage = pipeline.stages[stage_name]
    stage.status = StageStatus.PENDING
    stage.keep_agents_done = True
    for name in order[idx + 1:]:
        downstream = pipeline.stages[name]
        downstream.status = StageStatus.PENDING
        downstream.outputs = {}
        downstream.approved = False


async def advance_pipeline(pipeline: Pipeline):
    # 프로젝트 전용 팀(pm/designer/architect/release) 컨테이너가 없으면(수동으로
    # 지웠다가 재기동을 깜빡한 경우 등) 여기서 항상 먼저 보장한다 — 실제로 오늘
    # 컨테이너를 지워놓고 approve만 보내서 태스크가 큐에 쌓인 채 아무도 안
    # 받아가는 사고가 있었다. spawn_team은 이미 떠 있으면 그대로 재사용하는
    # 멱등 함수라 매번 불러도 안전하다.
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, spawner.spawn_team, pipeline.project_id, ANTHROPIC_API_KEY)

    for stage in pipeline.get_ready_stages():
        if stage.requires_approval and not stage.approved:
            pipeline.mark_waiting_approval(stage.name)
            approval_msg = (
                "디자인 목업을 확인하고 승인해주세요 (헤더의 🎨 디자인 버튼). "
                "승인해야 구현이 시작됩니다."
                if stage.name == "implement"
                else f"'{stage.name}' 스테이지를 시작하려면 승인이 필요합니다."
            )
            await broadcast({
                "type": "approval_required",
                "project_id": pipeline.project_id,
                "stage": stage.name,
                "message": approval_msg,
            })
            return

        # implement 시작 전 → 레포 자동 생성
        if stage.name == "implement" and not project_repos.get(pipeline.project_id):
            await setup_git_repo(pipeline)

        # 직접 의존성뿐 아니라 완료된 모든 스테이지의 산출물을 전달 —
        # 예: autotest는 qa에만 의존하지만 implement가 만든 branch/pr_number가 필요함
        context = {
            name: s.outputs
            for name, s in pipeline.stages.items()
            if s.status == StageStatus.COMPLETED
        }
        # 레포 정보를 context에 포함 → Implement Agent가 활용
        if project_repos.get(pipeline.project_id):
            context["github_repo"] = project_repos[pipeline.project_id]

        # design 재작업 등으로 implement가 "머지 전" 상태에서 다시 도는 경우,
        # _retry_design_with_feedback가 지우지 않고 남겨둔 branch를 넘겨서 새
        # PR을 또 만들지 않고 기존 PR을 이어서 고치게 한다 — QA 실패 재시도
        # 경로(_retry_implement_with_feedback)와 동일한 재사용 규칙.
        if stage.name == "implement":
            prior_branch = stage.outputs.get("branch")
            if prior_branch:
                context["retry_branch"] = prior_branch

        # planning(PM) 재실행 시 이미 만들어둔 Jira 이슈를 보여준다 — 안 보이면 PM이
        # 매 스프린트 같은 요구사항을 다른 문구로 다시 써서, 텍스트 기반 dedup
        # (_sync_new_requirements_to_epic)을 통과해 중복 이슈가 쌓인다.
        if stage.name == "planning":
            existing_issues = _existing_issues_context(pipeline.project_id)
            if existing_issues:
                context["existing_issues"] = existing_issues

        # design 스테이지는 Jira 스토리 단위로 목업을 나눠 만들어야 하므로 시나리오
        # 목록을 넘긴다. Jira 연동이 꺼져 있거나 스토리가 없으면 "main" 시나리오
        # 하나로 폴백 — 예전처럼 화면 전체를 목업 하나로 만드는 동작과 동일해짐.
        # scenario_scope가 있으면(재작업 요청이 특정 이슈 하나만 겨냥한 경우)
        # 전체 목록 대신 그 시나리오 하나만 넘긴다 — designer가 그것만 만들고,
        # publish_design도 그 키만 건드리므로 나머지 화면은 그대로 남는다.
        # 1회성 힌트라 여기서 소비하고 바로 비운다(다음 실행에 안 새게).
        if stage.name == "design":
            if stage.scenario_scope:
                scenarios = stage.scenario_scope
                stage.scenario_scope = None
            else:
                jira = project_jira.get(pipeline.project_id, {})
                story_titles = jira.get("story_titles", {})
                scenarios = [
                    {"key": key, "title": story_titles.get(key, key)}
                    for key in jira.get("stories", [])
                ] or [{"key": "main", "title": "전체 화면"}]
            context["scenarios"] = scenarios

        pipeline.mark_running(stage.name)
        _clear_phases(pipeline.project_id, stage.agents)
        await broadcast({"type": "stage_update", "project_id": pipeline.project_id, "stage": stage.name, "status": "running"})
        await _jira_stage_started(pipeline.project_id, stage.name)

        for agent_name in stage.agents:
            # mark_running이 keep_agents_done으로 agents_done을 보존해준 경우(취소 후
            # 재승인) 이미 끝낸 에이전트는 건너뛴다 — 안 그러면 design처럼 에이전트가
            # 여럿인 스테이지에서 한쪽만 취소했는데 이미 끝난 다른 쪽까지 다시 돈다
            # (recoveryfit에서 실제 재현: architect는 이미 끝났는데 취소하니 처음부터
            # 다시 돎). 평소(agents_done이 매번 비워지는 정상 재실행)엔 항상 빈
            # 리스트라 이 조건이 아무것도 걸러내지 않는다.
            if agent_name in stage.agents_done:
                continue

            # implement/autotest는 전역 공유 싱글턴(docker-compose 정적 서비스) — 큐도 전역.
            # 나머지(pm/designer/architect/qa/release)는 TeamSpawner가 프로젝트별로 격리해서
            # 띄우므로 큐도 프로젝트별로 분리해야 서로 태스크를 중복으로 가져가지 않는다.
            stream_project_id = None if agent_name in GLOBAL_SHARED_AGENTS else pipeline.project_id
            task_payload = {
                "project_id": pipeline.project_id,
                "stage": stage.name,
                "instruction": pipeline.instruction,
                "context": context,
                "github_repo": project_repos.get(pipeline.project_id, ""),
            }
            if stage.name == "qa":
                task_payload["manual_qa_build_fix"] = project_manual_qa_build.get(pipeline.project_id, False)
            await _send_task_or_manual(pipeline, agent_name, stream_project_id, stage.name, task_payload)


def _make_project_info(pid: str) -> dict:
    p = projects[pid]
    return {
        **p.summary(),
        # 플로우차트 탭의 PM 노드 Input(스프린트 지시사항 편집)에 필요 — 예전엔
        # _save_project가 디스크 스냅샷에만 따로 붙여서, 재시작 복원 전에는
        # 프론트가 살아있는 API로 이 값을 받을 방법이 없었다.
        "instruction": p.instruction,
        "name":     project_names.get(pid, pid),
        "repo":     project_repos.get(pid, ""),
        "messages": project_messages.get(pid, []),
        "phases":   project_phases.get(pid, {}),
        "jira":     project_jira.get(pid, {}),
        "token_totals": {
            "lifetime": _derive_lifetime_totals(pid),
            "by_sprint": project_token_totals.get(pid, {}).get("by_sprint", {}),
        },
        "deploy_config": project_deploy_config.get(pid, {}),
        "deploy_status": project_deploy_status.get(pid, {"status": "idle"}),
        "manual_implement": project_manual_implement.get(pid, False),
        "manual_qa_build": project_manual_qa_build.get(pid, False),
    }


# ── FastAPI ──────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 디스크에 저장된 프로젝트 스냅샷 복원 (재시작해도 사라지지 않게)
    _load_all_projects()

    # 기본 프로젝트는 없으면 새로 등록
    if DEFAULT_PROJECT_ID not in projects:
        projects[DEFAULT_PROJECT_ID]      = Pipeline(DEFAULT_PROJECT_ID, SELF_IMPROVE_INSTRUCTION)
        project_names[DEFAULT_PROJECT_ID] = DEFAULT_PROJECT_NAME
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, spawner.spawn_team, DEFAULT_PROJECT_ID, ANTHROPIC_API_KEY)

    asyncio.create_task(event_loop())
    yield
    await redis.close()

app = FastAPI(title="AI Team Manager", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


# ── WebSocket ────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    await ws.send_json({
        "type": "init",
        "projects": {pid: _make_project_info(pid) for pid in projects},
    })
    try:
        while True:
            data = await ws.receive_json()
            await handle_ws_message(ws, data)
    except WebSocketDisconnect:
        ws_clients.discard(ws)


async def handle_ws_message(ws: WebSocket, data: dict):
    msg_type = data.get("type")

    if msg_type == "instruction":
        project_id  = data.get("project_id") or str(uuid.uuid4())[:8]
        instruction = data.get("content", "")
        # 사용자 메시지는 agent_message 브로드캐스트를 안 타므로 여기서 직접 저장
        # (프론트가 이미 낙관적으로 화면에 그려서 다시 브로드캐스트하지는 않음).
        _store_message(project_id, "user", instruction)
        is_new = project_id not in projects
        if is_new:
            projects[project_id] = Pipeline(project_id, instruction)
        else:
            # 통째로 덮어쓰면 최초 PRD(instruction)가 사라진다 — 실제로 이렇게
            # 날아간 적이 있었다. 기존 프로젝트에 온 채팅은 원본 위에 추가
            # 요청으로만 덧붙인다.
            projects[project_id].instruction += f"\n\n[추가 요청] {instruction}"
        # 팀이 아직 없거나(첫 대화) 인액티브 버튼으로 정지돼 있던 경우 여기서 재기동
        # (spawn_team은 이미 실행 중이면 그대로 두는 멱등 함수).
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, spawner.spawn_team, project_id, ANTHROPIC_API_KEY)
        await broadcast({"type": "project_updated", "project_id": project_id, "projects": {pid: _make_project_info(pid) for pid in projects}})

        if not is_new and not projects[project_id].get_ready_stages():
            # design처럼 이미 completed로 지나간 스테이지는 채팅만으론 자동으로
            # 다시 돌지 않는다(get_ready_stages는 PENDING만 봄). 예전엔 여기서
            # 그냥 조용히 아무 일도 안 일어나서 사용자가 응답이 없다고 오해했고,
            # 그 다음엔 사람이 retry-design/retry-implement API를 직접 호출하라고만
            # 안내했다 — 이제는 PM에게 검토를 맡겨 알맞은 단계를 자동으로 다시
            # 돌리는 chat_triage 태스크를 보낸다.
            pipeline = projects[project_id]
            in_flight_ts = chat_triage_in_flight.get(project_id)
            if in_flight_ts and (time.time() - in_flight_ts) < CHAT_TRIAGE_TIMEOUT_SEC:
                # 이미 PM이 검토 중일 때 또 보내면 서로 다른 결정이 경합할 수 있어서
                # 새로 보내지 않는다 — 이번 메시지는 이미 위에서 instruction에
                # 덧붙였으니 진행 중인 검토(또는 다음 검토)에 자연히 반영된다.
                await broadcast({
                    "type": "agent_message", "project_id": project_id, "agent": "system",
                    "content": "PM이 방금 요청을 검토 중이에요 — 이번 메시지도 참고해서 함께 반영할게요.",
                })
            else:
                await broadcast({
                    "type": "agent_message", "project_id": project_id, "agent": "system",
                    "content": "🤔 PM이 요청을 검토 중입니다 — 디자인/구현 중 어디를 다시 손봐야 할지 확인하고 있어요.",
                })
                await _dispatch_chat_triage(pipeline, instruction)
        else:
            await advance_pipeline(projects[project_id])

    elif msg_type == "approve":
        p = projects.get(data.get("project_id"))
        if p:
            p.approve(data.get("stage"))
            await advance_pipeline(p)


# ── REST ─────────────────────────────────────────────────────────────
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_OWNER = os.getenv("GITHUB_REPO", "")   # owner만 (레포명은 프로젝트별 자동)


async def create_github_repo(repo_name: str, description: str) -> str | None:
    """GitHub API로 레포 생성. 성공하면 URL 반환."""
    if not GITHUB_TOKEN or not GITHUB_OWNER:
        return None
    # 레포명: 영문/숫자/하이픈만 허용 (한글 등 비ASCII 제거)
    import re
    safe_name = repo_name.lower().replace(" ", "-").replace("_", "-")
    safe_name = re.sub(r"[^a-z0-9\-]", "", safe_name)  # 비ASCII 제거
    safe_name = re.sub(r"-+", "-", safe_name).strip("-")  # 연속 하이픈 정리
    if not safe_name:
        safe_name = f"project-{uuid.uuid4().hex[:6]}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.github.com/user/repos",
                headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github+json",
                },
                json={
                    "name": safe_name,
                    "description": description,
                    "private": False,
                    "auto_init": True,          # README 자동 생성
                    "gitignore_template": "Python",
                },
            )
            if r.status_code in (201, 422):   # 422 = 이미 존재
                data = r.json()
                repo_url = data.get("html_url") or f"https://github.com/{GITHUB_OWNER}/{safe_name}"
                print(f"[github] 레포: {repo_url}")
                return repo_url
    except Exception as e:
        print(f"[github] 레포 생성 실패: {e}")
    return None


async def clone_existing_repo(pid: str, github_repo: str) -> bool:
    """기존 레포를 워크스페이스 루트에 clone. 신규 레포(setup_git_repo)와 동일하게
    /workspace/{project_id} 자체가 레포 루트가 되도록 레이아웃을 맞춘다."""
    if not GITHUB_TOKEN:
        print(f"[github] GITHUB_TOKEN 없음 — {github_repo} clone 스킵")
        return False
    workspace = f"/workspace/{pid}"
    os.makedirs(workspace, exist_ok=True)
    repo_url = f"https://{GITHUB_TOKEN}@github.com/{github_repo}.git"
    import subprocess
    try:
        result = subprocess.run(
            ["git", "clone", repo_url, workspace],
            capture_output=True, timeout=120,
        )
        if result.returncode != 0:
            print(f"[github] {github_repo} clone 실패: {result.stderr.decode(errors='replace')[:300]}")
            return False
        print(f"[github] 기존 레포 clone 완료: {github_repo} → {workspace}")
        return True
    except Exception as e:
        print(f"[github] {github_repo} clone 실패: {e}")
        return False


async def merge_pull_request(repo: str, pr_number: int) -> bool | str:
    """AutoTest 통과 후 PR을 main에 squash merge.
    반환: True(병합 성공) / "conflict"(main과 충돌 — Implement가 풀어야 함) / False(그 외 실패)."""
    if not GITHUB_TOKEN:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.put(
                f"https://api.github.com/repos/{repo}/pulls/{pr_number}/merge",
                headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github+json",
                },
                json={"merge_method": "squash"},
            )
            if r.status_code == 200:
                print(f"[github] PR #{pr_number} 병합 완료: {repo}")
                return True
            print(f"[github] PR #{pr_number} 병합 실패 ({r.status_code}): {r.text[:300]}")
            if r.status_code == 405 and "conflict" in r.text.lower():
                return "conflict"
            return False
    except Exception as e:
        print(f"[github] PR #{pr_number} 병합 실패: {e}")
        return False


async def create_pull_request(repo: str, branch: str, title: str, body: str) -> int | None:
    """새 브랜치 → main으로 향하는 PR 생성. 성공 시 PR 번호 반환 (design 스테이지가
    시나리오별 목업을 머지하기 전 단계로 사용 — 그 뒤 바로 merge_pull_request 호출)."""
    if not GITHUB_TOKEN:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://api.github.com/repos/{repo}/pulls",
                headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github+json",
                },
                json={"title": title, "head": branch, "base": "main", "body": body},
            )
            if r.status_code == 201:
                return r.json().get("number")
            print(f"[github] PR 생성 실패 ({r.status_code}): {r.text[:300]}")
    except Exception as e:
        print(f"[github] PR 생성 실패: {e}")
    return None


class NewProject(BaseModel):
    name: str
    instruction: str = ""
    project_id: str | None = None
    github_repo: str | None = None   # "owner/repo" 형식 or None → 자동 생성

@app.post("/projects")
async def create_project(body: NewProject):
    pid  = body.project_id or str(uuid.uuid4())[:8]
    name = body.name or pid

    pipeline = Pipeline(pid, body.instruction or f"{name} 개발")
    projects[pid]      = pipeline
    project_names[pid] = name

    # GitHub 레포 처리
    github_url = None
    if body.github_repo:
        # 기존 레포 연결 (owner/repo 형식) — 실제 코드를 워크스페이스로 clone
        project_repos[pid] = body.github_repo
        github_url = f"https://github.com/{body.github_repo}"
        print(f"[github] 기존 레포 연결: {github_url}")
        await clone_existing_repo(pid, body.github_repo)
    elif GITHUB_TOKEN and GITHUB_OWNER:
        # 레포 자동 생성 — 여기서 바로 워크스페이스에 clone까지 해둬야 한다.
        # 안 그러면 나중에 planning/design이 pm_output.md 등을 워크스페이스에
        # 먼저 써놓고, implement가 그제서야 clone을 시도하면서 "디렉토리가
        # 비어있지 않다"는 이유로 항상 실패하는 버그가 있었다 (실제로 발견됨).
        safe_name = name.lower().replace(" ", "-")
        github_url = await create_github_repo(
            repo_name=safe_name,
            description=f"AI Team Manager — {name}",
        )
        if github_url:
            project_repos[pid] = f"{GITHUB_OWNER}/{safe_name}"
            await clone_existing_repo(pid, project_repos[pid])

    # 에이전트 팀 스폰 (백그라운드) — pm/designer/architect/qa/release만 (implement/autotest는 전역 싱글턴)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, spawner.spawn_team, pid, ANTHROPIC_API_KEY)

    await broadcast({
        "type": "project_added",
        "project_id": pid,
        "projects": {p: _make_project_info(p) for p in projects},
        "github_url": github_url,
    })
    return {"project_id": pid, "name": name, "github_url": github_url, "repo": project_repos.get(pid, "")}

@app.get("/projects")
async def list_projects():
    return [_make_project_info(pid) for pid in projects]

@app.get("/projects/{project_id}")
async def get_project(project_id: str):
    if project_id not in projects:
        raise HTTPException(404)
    return _make_project_info(project_id)

@app.get("/github/repos")
async def list_github_repos():
    """GITHUB_OWNER 소유 레포 중 아직 프로젝트로 연결되지 않은 것만 반환
    (사이드바 '내 GitHub 레포' 섹션 — 기존 레포를 클릭해서 바로 연결할 수 있게)."""
    if not GITHUB_TOKEN:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.github.com/user/repos",
                headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
                params={"per_page": 100, "sort": "updated", "affiliation": "owner"},
            )
            if r.status_code != 200:
                return []
            data = r.json()
    except Exception as e:
        print(f"[github] 레포 목록 조회 실패: {e}")
        return []

    connected = set(project_repos.values())
    return [
        {
            "full_name": repo["full_name"],
            "name":      repo["name"],
            "private":   repo["private"],
            "updated_at": repo["updated_at"],
            "html_url":  repo["html_url"],
        }
        for repo in data
        if repo["full_name"] not in connected
    ]

class GateApprove(BaseModel):
    # 플로우차트 탭의 "Go"(디자이너 등 추가 입력박스가 있는 승인) — 채워져 있으면
    # 다음 스테이지가 시작되기 전에 pipeline.instruction에 덧붙인다. "Yes"는 body
    # 없이 이 엔드포인트를 그대로 호출하면 됨(기존 동작과 동일).
    extra_input: str | None = None

@app.post("/projects/{project_id}/approve/{stage_name}")
async def approve_stage(project_id: str, stage_name: str, body: GateApprove = GateApprove()):
    p = projects.get(project_id)
    if not p:
        raise HTTPException(404)
    if body.extra_input:
        p.instruction += f"\n\n[{stage_name} 추가 요청] {body.extra_input}"
    p.approve(stage_name)
    await advance_pipeline(p)
    await _add_history(project_id, f"✓ '{stage_name}' 게이트 승인" + (f" (추가 요청: {body.extra_input})" if body.extra_input else ""))
    return {"ok": True}


class RetryFeedback(BaseModel):
    feedback: str
    # 지정하면 이 Jira 이슈 키(예: "ATM-5") 하나만 재작업 — 없으면(None) 예전처럼
    # 스테이지 전체(모든 시나리오/전체 앱)를 다시 돈다.
    scenario_key: str | None = None

@app.post("/projects/{project_id}/retry-design")
async def retry_design(project_id: str, body: RetryFeedback):
    """디자인 목업이 사라졌거나 마음에 안 들 때 사람이 피드백과 함께 디자인부터
    다시 돌리는 수동 개입용 엔드포인트. 채팅으로 온 요청은 이미 completed된
    스테이지를 자동으로 재실행하지 않으므로(get_ready_stages는 PENDING만 봄)
    이 엔드포인트를 통해 명시적으로 트리거해야 한다."""
    p = projects.get(project_id)
    if not p:
        raise HTTPException(404)
    await _retry_design_with_feedback(p, body.feedback, [body.scenario_key] if body.scenario_key else None)
    return {"ok": True}


class RetryPlanning(BaseModel):
    feedback: str
    full_rewrite: bool = False
    jira_issue: str | None = None

@app.post("/projects/{project_id}/retry-planning")
async def retry_planning(project_id: str, body: RetryPlanning):
    """요구사항 자체가 바뀌어서 PM 기획부터 전체 파이프라인을 다시 돌려야 할 때
    쓰는 수동 개입용 엔드포인트. get_ready_stages는 PENDING만 보므로(이미
    지나간 스테이지는 저절로 재실행되지 않음) 명시적으로 트리거해야 한다."""
    p = projects.get(project_id)
    if not p:
        raise HTTPException(404)
    await _retry_planning_with_feedback(p, body.feedback, body.full_rewrite, body.jira_issue)
    return {"ok": True}


def _safe_design_key(key: str) -> bool:
    return bool(key) and "/" not in key and ".." not in key


def _implement_jira_comment_targets(scenario_keys: list[str] | None, stories: list[str]) -> list[str]:
    """구현 완료 코멘트/상태 전환을 어느 Jira 이슈(들)에 적용할지 결정한다 —
    scenario_keys로 범위가 좁혀졌고 그중 실제 존재하는 이슈가 있으면 그
    이슈들만, 아니면(범위 제한 없음 또는 알 수 없는 키뿐) 전체 스토리를
    대상으로 한다. 예전엔 항상 stories[0] 하나에만 코멘트를 달아서, 두 번째
    이후 이슈로 좁혀진 재작업의 PR 링크가 (이미 끝난) 첫 번째 이슈에 계속
    쌓이는 문제가 있었다."""
    hits = [k for k in (scenario_keys or []) if k in stories]
    return hits or stories


def _scenarios_with_jira_issue(scenario_keys: list[str], story_keys) -> list[str]:
    """디자인 목업이 적용될 때 Jira 코멘트를 남길 시나리오만 골라낸다 — Jira
    연동이 꺼져 있거나 스토리가 없을 때 쓰는 "main" 같은 폴백 키는 실제 이슈가
    아니므로 코멘트 대상에서 제외해야 한다(안 그러면 add_jira_comment가 존재
    하지 않는 이슈 키로 계속 실패 호출됨)."""
    story_set = set(story_keys)
    return [k for k in scenario_keys if k in story_set]


class DesignPublish(BaseModel):
    github_repo: str
    branch: str
    scenarios: list[str]

@app.post("/projects/{project_id}/design/publish")
async def publish_design(project_id: str, body: DesignPublish):
    """designer 에이전트가 시나리오별 목업을 새 브랜치에 커밋·푸시한 뒤 호출한다.
    PR 생성 + 즉시 스쿼시 머지(사람 승인 없이 디자이너가 직접 반영)까지 여기서
    처리 — GitHub 호출과 apply/history 파일 정리를 한 곳에 모아 로직 중복을 피한다.
    머지 성공 시 design/pending/{key}.html을 design/applied/{key}.html로 옮기고
    design/history/{key}/{timestamp}.html에 스냅샷을 남긴다(과거 버전 조회용).
    머지 실패 시 pending에 그대로 남아 '적용 전'으로 계속 보인다."""
    workspace = f"/workspace/{project_id}"
    scenarios = [k for k in body.scenarios if _safe_design_key(k)]
    sprint = projects[project_id].sprint if project_id in projects else 1

    pr_number = await create_pull_request(
        body.github_repo, body.branch,
        title=f"[sprint {sprint}] design: {project_id} 목업 갱신",
        body=f"시나리오: {', '.join(scenarios) or '(없음)'}",
    )
    if pr_number is None:
        await broadcast({
            "type": "agent_message", "project_id": project_id, "agent": "system",
            "content": "⚠️ 디자인 PR 생성에 실패했습니다 — 목업이 적용 전 상태로 남아 있습니다.",
        })
        return {"merged": False, "pr_number": None}

    merged = await merge_pull_request(body.github_repo, pr_number)
    if merged is True:
        applied_dir = f"{workspace}/design/applied"
        pending_dir = f"{workspace}/design/pending"
        os.makedirs(applied_dir, exist_ok=True)
        ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        version = f"sprint{sprint}_{ts}"
        applied_count = 0
        # 목업이 적용될 때마다 그 시나리오(=Jira 이슈 키)에 링크를 코멘트로 남긴다 —
        # 산출물을 앱 안에서만 보여주면 이 라운드가 최신본으로 덮어써질 때 이전
        # 버전을 볼 방법이 없어진다(design/applied/{key}.html은 항상 최신 하나뿐).
        # design/history/{key}/{ts}.html은 이미 서버에 스냅샷으로 남기고 있으니
        # "최신" 링크와 "이 버전"(스냅샷) 링크를 같이 남겨서, Jira 코멘트 이력
        # 자체가 그 이슈의 디자인 변경 히스토리 역할을 하게 한다. "main"처럼 Jira
        # 연동이 꺼져 있을 때 쓰는 폴백 키는 실제 이슈가 아니므로 건너뛴다.
        commentable = set(_scenarios_with_jira_issue(
            scenarios, project_jira.get(project_id, {}).get("stories", [])
        ))
        for key in scenarios:
            src = f"{pending_dir}/{key}.html"
            if not os.path.exists(src):
                continue
            with open(src, errors="replace") as f:
                content = f.read()
            with open(f"{applied_dir}/{key}.html", "w") as f:
                f.write(content)
            hist_dir = f"{workspace}/design/history/{key}"
            os.makedirs(hist_dir, exist_ok=True)
            with open(f"{hist_dir}/{version}.html", "w") as f:
                f.write(content)
            os.remove(src)
            applied_count += 1
            if key in commentable:
                target = _stage_issue_target(project_id, key, "design")
                await add_jira_comment(target, (
                    f"🎨 디자인 목업 반영 (Sprint {sprint} · 버전 {ts})\n"
                    f"최신: {PUBLIC_BASE_URL}/design-file/{project_id}/applied/{key}\n"
                    f"이 버전: {PUBLIC_BASE_URL}/design-file/{project_id}/history/{key}/{version}"
                ))
        await broadcast({
            "type": "agent_message", "project_id": project_id, "agent": "system",
            "content": f"🎨 디자인이 머지·적용됐습니다 ({applied_count}개 화면) — 헤더의 🎨 디자인 버튼에서 확인하세요.",
        })
    else:
        await broadcast({
            "type": "agent_message", "project_id": project_id, "agent": "system",
            "content": f"⚠️ 디자인 PR #{pr_number} 머지 실패 — 적용 전 상태로 남아 있습니다.",
        })
    return {"merged": merged is True, "pr_number": pr_number}


def _list_design_bucket(base: str, project_id: str, bucket: str, story_titles: dict | None = None) -> list[dict]:
    """design/applied 또는 design/pending 디렉터리를 스캔해 시나리오별 목업 목록을
    만든다. Jira 스토리 제목이 있으면 라벨로 붙인다 — collect_outputs와 같은
    '파일 나열 → mtime 역순 정렬' 패턴이고, base를 인자로 받아 테스트 가능하게 한다."""
    dir_path = f"{base}/design/{bucket}"
    if not os.path.isdir(dir_path):
        return []
    story_titles = story_titles or {}
    items = []
    for fname in os.listdir(dir_path):
        if not fname.endswith(".html"):
            continue
        key = fname[:-5]
        fpath = f"{dir_path}/{fname}"
        items.append({
            "key": key,
            "title": story_titles.get(key, key),
            "url": f"/design-file/{project_id}/{bucket}/{key}",
            "mtime": os.path.getmtime(fpath),
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


@app.get("/design/{project_id}/applied")
async def list_applied_design(project_id: str):
    story_titles = project_jira.get(project_id, {}).get("story_titles", {})
    return {"items": _list_design_bucket(f"/workspace/{project_id}", project_id, "applied", story_titles)}


@app.get("/design/{project_id}/pending")
async def list_pending_design(project_id: str):
    story_titles = project_jira.get(project_id, {}).get("story_titles", {})
    return {"items": _list_design_bucket(f"/workspace/{project_id}", project_id, "pending", story_titles)}


@app.get("/design/{project_id}/history/{key}")
async def list_design_history(project_id: str, key: str):
    if not _safe_design_key(key):
        raise HTTPException(status_code=400, detail="잘못된 시나리오 키")
    base = f"/workspace/{project_id}/design/history/{key}"
    if not os.path.isdir(base):
        return {"items": []}
    items = []
    for fname in os.listdir(base):
        if not fname.endswith(".html"):
            continue
        version = fname[:-5]
        items.append({
            "version": version,
            "url": f"/design-file/{project_id}/history/{key}/{version}",
            "mtime": os.path.getmtime(f"{base}/{fname}"),
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"items": items}


@app.get("/design-file/{project_id}/{bucket}/{key}")
async def get_design_file(project_id: str, bucket: str, key: str):
    if bucket not in ("applied", "pending") or not _safe_design_key(key):
        raise HTTPException(status_code=400, detail="잘못된 요청")
    path = f"/workspace/{project_id}/design/{bucket}/{key}.html"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="디자인 목업이 없습니다")
    return FileResponse(path, media_type="text/html")


@app.get("/design-file/{project_id}/history/{key}/{version}")
async def get_design_history_file(project_id: str, key: str, version: str):
    if not _safe_design_key(key) or not _safe_design_key(version):
        raise HTTPException(status_code=400, detail="잘못된 요청")
    path = f"/workspace/{project_id}/design/history/{key}/{version}.html"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="디자인 버전을 찾을 수 없습니다")
    return FileResponse(path, media_type="text/html")


@app.post("/projects/{project_id}/retry-implement")
async def retry_implement(project_id: str, body: RetryFeedback):
    """qa 자동 재시도(MAX_QA_RETRIES)가 소진돼 FAILED로 멈춘 프로젝트를, 사람이
    직접 확인한 정확한 피드백으로 한 번 더 implement에 재작업 요청할 때 쓰는
    수동 개입용 엔드포인트. qa_retry_counts를 0으로 리셋한다 — 리셋하지 않으면
    카운터가 이미 MAX_QA_RETRIES에 도달해 있어서, 이번에 사람이 원인을 고쳐
    보내도 다음 QA/AutoTest 실패 때 자동 재작업 없이 곧장 다시 FAILED로
    멈춰버려 매번 수동 개입을 반복해야 했다."""
    p = projects.get(project_id)
    if not p:
        raise HTTPException(404)
    qa_retry_counts[project_id] = 0
    await _add_history(project_id, f"🔁 QA 재작업 요청 (수동, retry 예산 소진 후 개입 — retry 카운트 리셋): {body.feedback}")
    await _retry_implement_with_feedback(p, body.feedback, [body.scenario_key] if body.scenario_key else None)
    return {"ok": True}


class StageRerun(BaseModel):
    feedback: str = ""
    # 지정하면 이 Jira 이슈 키들(예: ["ATM-5", "ATM-10"])만 재작업 — 없거나
    # 비어 있으면 스테이지 전체(모든 시나리오)를 다시 돈다. recoveryfit에서
    # 실제 재현: 플로우차트 탭 "Run"으로 화면 1개짜리 재작업을 돌렸는데 이
    # 필드가 아예 없어서 매번 scenario_keys=None으로 넘어가 시나리오 8개가
    # 전부 재생성됐다 — retry-design/retry-implement 엔드포인트는 이미
    # scenario_key를 받는데 이 엔드포인트만 빠져 있었다.
    scenario_keys: list[str] | None = None

_RERUN_NO_CHANGE_TEXT = "(변경 없음 — 같은 내용으로 재실행)"

@app.post("/projects/{project_id}/stage/{stage_name}/rerun")
async def rerun_stage(project_id: str, stage_name: str, body: StageRerun):
    """플로우차트 탭 결정 블록의 "Run" — 입력을 수정했든 안 했든, 그 스테이지를
    같은 자리에서 다시 돌린다(다음 스테이지로 넘어가지 않음). planning/design/
    implement는 기존 retry-* 엔드포인트가 쓰던 헬퍼를 그대로 재사용하고,
    qa/autotest/release는 이번에 추가한 헬퍼로 위임한다."""
    p = projects.get(project_id)
    if not p:
        raise HTTPException(404)
    feedback = body.feedback.strip() or _RERUN_NO_CHANGE_TEXT

    if stage_name == "planning":
        await _retry_planning_with_feedback(p, feedback, full_rewrite=False)
    elif stage_name == "design":
        await _retry_design_with_feedback(p, feedback, body.scenario_keys)
    elif stage_name == "implement":
        await _retry_implement_with_feedback(p, feedback, body.scenario_keys)
    elif stage_name == "qa":
        await _retry_qa_with_feedback(p, body.feedback.strip())
    elif stage_name == "autotest":
        await _retry_autotest_with_feedback(p, body.feedback.strip())
    elif stage_name == "release":
        await _retry_release_with_feedback(p, body.feedback.strip())
    else:
        raise HTTPException(400, f"알 수 없는 스테이지: {stage_name}")

    await _add_history(project_id, f"↺ '{stage_name}' 재실행: {feedback}")
    return {"ok": True}


class ManualImplementToggle(BaseModel):
    enabled: bool


@app.post("/projects/{project_id}/manual-implement")
async def set_manual_implement(project_id: str, body: ManualImplementToggle):
    """implement 스테이지를 사람(Claude Code 세션)이 처리할지 컨테이너 에이전트가
    처리할지 프로젝트별로, 그때그때 토글한다 — .env 전역 플래그였다면 값을 바꿀
    때마다 orchestrator 컨테이너를 재시작해야 해서 이렇게 런타임 상태로 뒀다.
    이미 큐로 나간(또는 이미 파일로 대기 중인) 진행 중 태스크에는 영향 없고,
    다음 디스패치(_send_task_or_manual)부터 적용된다."""
    if project_id not in projects:
        raise HTTPException(404)
    project_manual_implement[project_id] = body.enabled
    _save_project(project_id)
    await _add_history(project_id, f"🖐 implement 수동 처리 모드 {'켜짐' if body.enabled else '꺼짐'}")
    await broadcast({"type": "project_updated", "project_id": project_id, "projects": {pid: _make_project_info(pid) for pid in projects}})
    return {"ok": True, "manual_implement": body.enabled}


class ManualQaBuildToggle(BaseModel):
    enabled: bool


@app.post("/projects/{project_id}/manual-qa-build")
async def set_manual_qa_build(project_id: str, body: ManualQaBuildToggle):
    """QA가 자기 시나리오 테스트 코드의 컴파일 실패를 자체 수정 예산 안에 못 고쳤을
    때, Implement에 넘기지 않고(앱 코드 문제가 아니므로) 사람(Claude Code 세션)에게
    직접 넘길지 프로젝트별로 토글한다. 꺼져 있으면 QA는 그 라운드를 그냥
    건너뛴다. manual_implement와 달리 이 값은 orchestrator가 아니라 qa 컨테이너
    안에서 검사돼야 해서(자체 수정 시도 자체가 그 안에서 일어남) 다음 QA
    dispatch(advance_pipeline/_retry_qa_with_feedback)의 task payload에 실어
    보낸다."""
    if project_id not in projects:
        raise HTTPException(404)
    project_manual_qa_build[project_id] = body.enabled
    _save_project(project_id)
    await _add_history(project_id, f"🖐 QA 테스트 코드 빌드 실패 외부 처리 {'켜짐' if body.enabled else '꺼짐'}")
    await broadcast({"type": "project_updated", "project_id": project_id, "projects": {pid: _make_project_info(pid) for pid in projects}})
    return {"ok": True, "manual_qa_build": body.enabled}


class ManualStageResult(BaseModel):
    agent: str
    outputs: dict


@app.post("/projects/{project_id}/stage/{stage_name}/manual-result")
async def manual_stage_result(project_id: str, stage_name: str, body: ManualStageResult):
    """project_manual_implement로 큐 대신 파일로 넘긴 태스크(_send_task_or_manual
    참고)를 사람이 (Claude Code 세션으로) 직접 처리한 뒤 결과를 넣는 입구. 컨테이너 에이전트가
    STREAM_EVENTS에 stage_completed를 xadd하는 것과 동일한 효과를 내야 재시도
    라우팅(_route_needs_rework_or_fail)/토큰 집계/jira 코멘트 등 기존 완료 처리
    로직을 그대로 재사용할 수 있어서, 새 로직을 만들지 않고 handle_agent_event를
    그대로 호출한다 — 실제 에이전트가 보낸 이벤트와 구분이 안 되는 동일 경로."""
    if project_id not in projects:
        raise HTTPException(404)
    await handle_agent_event({
        "project_id": project_id,
        "agent": body.agent,
        "type": "stage_completed",
        "stage": stage_name,
        "outputs": body.outputs,
    })
    return {"ok": True}


_DISCARDABLE_STAGES = ("design", "implement", "qa", "autotest", "release")

@app.post("/projects/{project_id}/stage/{stage_name}/discard")
async def discard_stage(project_id: str, stage_name: str):
    """플로우차트 탭 결정 블록의 "폐기" — 이 스테이지와 그 이후 전부를 미실행 상태로
    되돌리고 파이프라인을 그 자리에 멈춰둔다(advance_pipeline 호출 안 함). planning은
    첫 스테이지라 되돌아갈 이전 단계가 없으므로 대상에서 제외."""
    if stage_name not in _DISCARDABLE_STAGES:
        raise HTTPException(400, f"'{stage_name}'은(는) 폐기할 수 없습니다")
    p = projects.get(project_id)
    if not p:
        raise HTTPException(404)
    if p.stages[stage_name].status == StageStatus.RUNNING:
        # "취소" — 아직 안 끝난 스테이지를 되돌리는 경우, 이미 끝낸 에이전트(design의
        # designer/architect처럼)의 산출물은 보존한다.
        _cancel_running_stage(p, stage_name)
        await _add_history(project_id, f"⏹ '{stage_name}' 취소 — 이전 단계로 되돌림")
    else:
        _reset_stage_cascade(p, stage_name)
        await _add_history(project_id, f"✗ '{stage_name}' 폐기 — 이전 단계로 되돌림")
    await broadcast({
        "type": "project_updated", "project_id": project_id,
        "projects": {project_id: _make_project_info(project_id)},
    })
    return {"ok": True}


class DeployConfigUpdate(BaseModel):
    app_name: str | None = None
    app_identifier: str | None = None
    language: str | None = None
    app_version: str | None = None
    environment: str | None = None       # test|dev|prod
    platforms: list[str] | None = None   # ["ios","android"] 부분집합
    host_workspace_path: str | None = None

_DEPLOY_ENVIRONMENTS = {"test", "dev", "prod"}
_DEPLOY_PLATFORMS = {"ios", "android"}

@app.put("/projects/{project_id}/deploy/config")
async def update_deploy_config(project_id: str, body: DeployConfigUpdate):
    """스프린트 화면 맨 아래 배포 카드의 필드 편집. App Store Connect/Google Play
    자격증명은 여기 안 들어간다 — host_workspace_path가 가리키는 프로젝트 레포
    자신의 fastlane/.env가 이미 갖고 있고 deploy_runner가 그 디렉토리에서
    fastlane을 그대로 실행하므로 ai-dev-team은 자격증명을 저장·전송하지 않는다.
    넘어온 필드만 덮어쓰는 부분 업데이트."""
    if project_id not in projects:
        raise HTTPException(404)
    if body.environment is not None and body.environment not in _DEPLOY_ENVIRONMENTS:
        raise HTTPException(400, f"environment는 {sorted(_DEPLOY_ENVIRONMENTS)} 중 하나여야 합니다")
    if body.platforms is not None:
        invalid = set(body.platforms) - _DEPLOY_PLATFORMS
        if invalid or not body.platforms:
            raise HTTPException(400, f"platforms는 {sorted(_DEPLOY_PLATFORMS)}의 비어있지 않은 부분집합이어야 합니다")

    cfg = project_deploy_config.setdefault(project_id, {})
    cfg.update(body.model_dump(exclude_unset=True))
    _save_project(project_id)
    await broadcast({
        "type": "project_updated", "project_id": project_id,
        "projects": {project_id: _make_project_info(project_id)},
    })
    return {"ok": True, "deploy_config": cfg}


@app.post("/projects/{project_id}/deploy")
async def trigger_deploy(project_id: str):
    """스프린트 최하단 배포 버튼 — Release 스테이지가 끝난 뒤에만 허용된다. 실제
    빌드(flutter build ipa/appbundle)는 Xcode가 필요해 orchestrator가 도는 Linux
    Docker 컨테이너 안에서는 못 돌리므로, 이 Mac 호스트에서 네이티브로 도는
    scripts/deploy_runner.py를 호출만 하고 바로 리턴한다(빌드가 몇 분~수십 분
    걸림). 실제 결과는 /internal/deploy-callback으로 비동기 통보된다."""
    p = projects.get(project_id)
    if not p:
        raise HTTPException(404)

    release = p.stages.get("release")
    if not release or release.status != StageStatus.COMPLETED:
        raise HTTPException(409, "Release 스테이지가 완료된 뒤에만 배포할 수 있습니다")

    if project_deploy_status.get(project_id, {}).get("status") == "running":
        raise HTTPException(409, "이미 배포가 진행 중입니다")

    cfg = project_deploy_config.get(project_id, {})
    workspace = cfg.get("host_workspace_path")
    if not workspace:
        raise HTTPException(400, "host_workspace_path가 설정돼 있지 않습니다 — 배포 카드에서 먼저 설정하세요")

    project_deploy_status[project_id] = {"status": "running", "started_at": int(time.time() * 1000), "phases": {}}
    _save_project(project_id)
    await broadcast({
        "type": "project_updated", "project_id": project_id,
        "projects": {project_id: _make_project_info(project_id)},
    })

    # host_workspace_path가 아직 clone된 적 없는 빈 디렉토리일 수 있으므로,
    # deploy_runner가 필요하면 스스로 clone할 수 있게 이 프로젝트의 GitHub 레포
    # URL을 같이 넘긴다 — clone_existing_repo(프로젝트 생성 시 clone)와 동일하게
    # GITHUB_TOKEN을 박아 만든다. 연결된 레포가 없거나 토큰이 없으면 None으로
    # 넘어가고, deploy_runner는 workspace가 이미 clone돼 있지 않으면 그때 에러를 낸다.
    github_repo = project_repos.get(project_id)
    repo_url = f"https://{GITHUB_TOKEN}@github.com/{github_repo}.git" if (github_repo and GITHUB_TOKEN) else None

    payload = {
        "project_id": project_id,
        "workspace": workspace,
        "environment": cfg.get("environment", "prod"),
        "platforms": cfg.get("platforms", ["ios", "android"]),
        "app_version": cfg.get("app_version"),
        "app_identifier": cfg.get("app_identifier"),
        "repo_url": repo_url,
        "callback_url": "http://localhost:8000/internal/deploy-callback",
        "progress_url": "http://localhost:8000/internal/deploy-progress",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{DEPLOY_RUNNER_URL}/run", json=payload)
            r.raise_for_status()
    except Exception as e:
        started_at = project_deploy_status[project_id]["started_at"]
        error = (f"배포 러너({DEPLOY_RUNNER_URL})에 연결할 수 없습니다 — 호스트에서 "
                 f"scripts/deploy_runner.py가 실행 중인지 확인하세요. ({e})")
        project_deploy_status[project_id] = {
            "status": "failed", "started_at": started_at,
            "finished_at": int(time.time() * 1000), "error": error,
        }
        _save_project(project_id)
        await broadcast({
            "type": "project_updated", "project_id": project_id,
            "projects": {project_id: _make_project_info(project_id)},
        })
        raise HTTPException(502, error)

    await _add_history(project_id, f"🚀 배포 시작 (env={payload['environment']}, version={payload['app_version']})")
    return {"ok": True}


class DeployCallback(BaseModel):
    project_id: str
    success: bool
    app_version: str | None = None
    build_number: str | None = None
    log_tail: str = ""
    error: str | None = None

@app.post("/internal/deploy-callback")
async def deploy_callback(body: DeployCallback):
    """호스트에서 도는 deploy_runner.py가 빌드+업로드를 끝낸 뒤 결과를 통보하는
    내부 콜백. 외부에 노출되지 않는 경로라 별도 인증 없음(오케스트레이터 8000
    포트 자체가 지금도 무인증인 것과 동일한 보안 수준)."""
    project_id = body.project_id
    if project_id not in projects:
        raise HTTPException(404)

    prev = project_deploy_status.get(project_id, {})
    project_deploy_status[project_id] = {
        "status": "success" if body.success else "failed",
        "started_at": prev.get("started_at"),
        "finished_at": int(time.time() * 1000),
        "app_version": body.app_version,
        "build_number": body.build_number,
        "log_tail": body.log_tail,
        "error": body.error,
        "phases": prev.get("phases", {}),  # 단계별 스텝칩은 이 결과가 온 뒤에도 그대로 남겨서 보여준다
    }
    _save_project(project_id)
    await broadcast({
        "type": "project_updated", "project_id": project_id,
        "projects": {project_id: _make_project_info(project_id)},
    })
    label = f"{body.app_version}+{body.build_number}" if body.success else (body.error or "실패")
    await _add_history(project_id, f"{'✅' if body.success else '❌'} 배포 {'완료' if body.success else '실패'}: {label}")
    return {"ok": True}


class DeployProgress(BaseModel):
    project_id: str
    phase: str
    status: str   # start|success|fail
    label: str
    detail: str = ""

@app.post("/internal/deploy-progress")
async def deploy_progress(body: DeployProgress):
    """deploy_runner.py가 배포 단계(workspace/version/build/fastlane/commit_push)
    하나를 시작·완료할 때마다 보내는 진행 상황 통보 — 배포 카드의 스텝칩을
    채운다. deploy-callback과 동일하게 내부 전용, 무인증."""
    project_id = body.project_id
    if project_id not in projects:
        raise HTTPException(404)

    st = project_deploy_status.setdefault(project_id, {"status": "running"})
    phases = st.setdefault("phases", {})
    phases[body.phase] = {"status": body.status, "label": body.label, "detail": body.detail}
    await broadcast({
        "type": "project_updated", "project_id": project_id,
        "projects": {project_id: _make_project_info(project_id)},
    })
    return {"ok": True}


@app.post("/projects/{project_id}/deactivate")
async def deactivate_project(project_id: str):
    """사이드바 '인액티브' 버튼 — 프로젝트 전용 팀 컨테이너(pm/designer/architect/release)를
    삭제하지 않고 정지만 한다. 산출물은 workspace 볼륨에 그대로 남고, 채팅으로
    다시 지시하면(위 instruction 핸들러) 컨테이너가 재기동돼 이어서 실행된다.
    implement/autotest/qa는 여러 프로젝트가 공유하는 싱글턴이라 여기서 건드리지 않는다."""
    p = projects.get(project_id)
    if not p:
        raise HTTPException(404)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, spawner.pause_team, project_id)
    for stage in p.stages.values():
        if stage.status == StageStatus.RUNNING:
            stage.status = StageStatus.PENDING
    await broadcast({"type": "project_updated", "project_id": project_id, "projects": {pid: _make_project_info(pid) for pid in projects}})
    return {"ok": True}

@app.post("/projects/{project_id}/activate")
async def activate_project(project_id: str):
    """사이드바 '재실행' 버튼 — 정지된(인액티브) 프로젝트의 팀 컨테이너를 새
    채팅 없이 바로 재기동한다. spawn_team은 이미 떠 있으면 그대로 두는 멱등
    함수라 이미 활성 상태인 프로젝트에서 눌려도 안전하다."""
    p = projects.get(project_id)
    if not p:
        raise HTTPException(404)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, spawner.spawn_team, project_id, ANTHROPIC_API_KEY)
    await broadcast({"type": "project_updated", "project_id": project_id, "projects": {pid: _make_project_info(pid) for pid in projects}})
    return {"ok": True}


@app.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    if project_id == DEFAULT_PROJECT_ID:
        raise HTTPException(400, "기본 프로젝트는 삭제할 수 없습니다.")
    projects.pop(project_id, None)
    project_names.pop(project_id, None)
    _delete_project_file(project_id)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, spawner.stop_team, project_id)
    await broadcast({"type": "project_removed", "project_id": project_id})
    return {"ok": True}

@app.get("/health")
async def health():
    return {"status": "ok", "projects": len(projects)}


def _parse_byte_range(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    """HTTP Range 헤더("bytes=START-END"류)를 (start, end) 바이트 오프셋으로
    파싱한다. 헤더가 없거나 문법이 안 맞거나 파일 범위를 벗어나면 None을
    반환해서 호출부가 평범한 200 전체 응답으로 폴백하게 한다."""
    if not range_header or not range_header.startswith("bytes="):
        return None
    spec = range_header[len("bytes="):].split(",")[0].strip()  # 여러 range 중 첫 구간만 지원
    m = re.match(r"^(\d*)-(\d*)$", spec)
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if m.group(1) == "":
        # 접미사 범위: "bytes=-500" → 파일 끝에서 500바이트
        start = max(file_size - int(m.group(2)), 0)
        end = file_size - 1
    else:
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else file_size - 1
    end = min(end, file_size - 1)
    if start > end or start >= file_size:
        return None
    return start, end


def _serve_video_range(path: str, range_header: str | None):
    """파일 경로 하나만 받아 Range 유무에 따라 200 전체 응답 또는 206 부분
    스트리밍 응답을 만든다. 라우트 데코레이터 밖으로 분리해서 /workspace
    마운트 없이도(테스트 환경 등) 임의 경로로 테스트 가능하게 한다."""
    file_size  = os.path.getsize(path)
    byte_range = _parse_byte_range(range_header, file_size)
    if byte_range is None:
        return FileResponse(path, media_type="video/mp4", headers={"Accept-Ranges": "bytes"})

    start, end = byte_range
    length = end - start + 1

    def _iter_chunk():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        _iter_chunk(),
        status_code=206,
        media_type="video/mp4",
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        },
    )


@app.get("/recordings/{project_id}")
async def get_qa_recording(project_id: str, request: Request):
    """QA(Firebase Test Lab) 단계가 남긴 무작위 테스트 녹화 영상을 서빙한다.

    모바일 Chrome의 <video>는 재생/탐색 전에 Range 요청을 보내고, 206 Partial
    Content로 응답하지 않으면 아예 재생을 거부한다 — 데스크톱 Chrome/Safari는
    Range 없이 200 + 전체 파일을 줘도 그냥 재생해버려서 실제로 모바일에서
    겪기 전까진 못 알아챘다(Starlette FileResponse는 Range를 처리하지 않음).
    Range 헤더가 오면 해당 구간만 스트리밍해서 206으로 응답한다."""
    path = f"/workspace/{project_id}/qa_recording.mp4"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="녹화 영상이 없습니다")
    return _serve_video_range(path, request.headers.get("range"))


@app.get("/recordings/{project_id}/history/{version}")
async def get_qa_recording_history(project_id: str, version: str, request: Request):
    """_archive_qa_recording이 남긴 스프린트별 QA 녹화 이력을 서빙한다 — 최신
    qa_recording.mp4와 달리 다음 라운드가 와도 덮어써지지 않는다. Jira
    remotelink와 산출물 패널(collect_outputs)이 이 URL을 그대로 가리킨다."""
    if "/" in version or ".." in version:
        raise HTTPException(status_code=400, detail="잘못된 버전")
    path = f"/workspace/{project_id}/qa/history/{version}.mp4"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="녹화 영상이 없습니다")
    return _serve_video_range(path, request.headers.get("range"))


@app.get("/screenshots/{project_id}/{filename}")
async def get_screenshot(project_id: str, filename: str):
    """QA(Firebase Test Lab)가 Robo 테스트 중 남긴 실제 기기 스크린샷을 서빙."""
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="잘못된 파일명")
    path = f"/workspace/{project_id}/.qa_screenshots/{filename}"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="스크린샷이 없습니다")
    return FileResponse(path, media_type="image/png")


def collect_outputs(base: str, project_id: str) -> list[dict]:
    """디자인 목업/QA 녹화 영상/스크린샷처럼 프로젝트가 남긴 산출물을 파일
    수정시각 기준 최신순으로 모아서 반환 — 웹 UI가 '산출물' 목록을 렌더링할 때
    이거 하나만 호출하면 되게 한다 (어떤 파일이 있는지 프론트가 직접 뒤질 필요 없음).
    FastAPI 핸들러에서 순수 로직만 분리 — base 디렉토리만 넣으면 테스트 가능."""
    items = []

    video_path = f"{base}/qa_recording.mp4"
    if os.path.exists(video_path):
        items.append({
            "type": "video", "label": "QA 동작 녹화 (최신)", "icon": "🎥",
            "url": f"/recordings/{project_id}",
            "mtime": os.path.getmtime(video_path),
        })

    # 스프린트별 QA 녹화 이력(_archive_qa_recording) — 최신 qa_recording.mp4는
    # 다음 라운드에 덮어써지지만, 이 파일들은 스프린트 태그가 붙어 계속 남는다.
    video_hist_dir = f"{base}/qa/history"
    if os.path.isdir(video_hist_dir):
        for fname in os.listdir(video_hist_dir):
            if not fname.endswith(".mp4"):
                continue
            version = fname[:-4]
            m = re.match(r"^sprint(\d+)_", version)
            label = f"QA 녹화 (Sprint {m.group(1)})" if m else f"QA 녹화 ({version})"
            items.append({
                "type": "video", "label": label, "icon": "🎥",
                "url": f"/recordings/{project_id}/history/{version}",
                "mtime": os.path.getmtime(f"{video_hist_dir}/{fname}"),
            })

    shots_dir = f"{base}/.qa_screenshots"
    if os.path.isdir(shots_dir):
        for fname in sorted(os.listdir(shots_dir)):
            if fname.lower().endswith(".png"):
                fpath = f"{shots_dir}/{fname}"
                items.append({
                    "type": "screenshot", "label": f"스크린샷 ({fname})", "icon": "📸",
                    "url": f"/screenshots/{project_id}/{fname}",
                    "mtime": os.path.getmtime(fpath),
                })

    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


@app.get("/outputs/{project_id}")
async def list_outputs(project_id: str):
    return {"items": collect_outputs(f"/workspace/{project_id}", project_id)}


LOG_TAIL_LINES = 300

@app.get("/projects/{project_id}/agent-log/{agent_name}")
async def get_agent_log(project_id: str, agent_name: str):
    """상세 패널의 '로그' 버튼 — 에이전트가 /workspace/logs에 남긴 파일을 읽어 반환.
    implement/autotest/qa는 프로젝트별 컨테이너가 아니라 상시 싱글턴이라
    로그 파일도 프로젝트 구분 없이 공유된다 (shared=True로 프론트에 알려줌)."""
    shared = agent_name in GLOBAL_SHARED_AGENTS
    path = f"/workspace/logs/{agent_name}.log" if shared else f"/workspace/logs/{agent_name}-{project_id}.log"
    if not os.path.exists(path):
        return {"agent": agent_name, "lines": [], "shared": shared}
    with open(path, "r", errors="replace") as f:
        lines = f.readlines()[-LOG_TAIL_LINES:]
    return {"agent": agent_name, "lines": [l.rstrip("\n") for l in lines], "shared": shared}


# ── 자가 개선: 서비스 재시작 ────────────────────────────────────────
class RestartRequest(BaseModel):
    service: str   # "orchestrator" | "web" | "agent-pm" | ...

@app.post("/restart")
async def restart_service(body: RestartRequest):
    """에이전트가 코드 수정 후 해당 서비스를 재시작할 때 호출."""
    allowed = {"web", "orchestrator", "agent-pm", "agent-designer",
               "agent-architect", "agent-qa", "agent-autotest", "agent-release"}
    if body.service not in allowed:
        raise HTTPException(400, f"허용되지 않는 서비스: {body.service}")
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
        # 컨테이너 이름 패턴: ai-dev-team-{service}-1
        container_name = f"ai-dev-team-{body.service}-1"
        c = client.containers.get(container_name)
        c.restart(timeout=10)
        await broadcast({"type": "service_restarted", "service": body.service})
        return {"ok": True, "restarted": body.service}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── 터널 URL ─────────────────────────────────────────────────────────
_tunnel_cache: dict = {}

@app.get("/tunnel-urls")
async def tunnel_urls():
    global _tunnel_cache
    if _tunnel_cache.get("web"):
        return _tunnel_cache

    async def fetch(host: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                r = await client.get(f"http://{host}:20241/quicktunnel")
                if r.status_code == 200:
                    url = r.json().get("url", "")
                    return f"https://{url}" if url and not url.startswith("http") else url
        except Exception:
            pass
        return ""

    web = await fetch("tunnel-web")
    api = await fetch("tunnel-api")
    if web:
        _tunnel_cache = {"web": web, "api": api}
    return _tunnel_cache
