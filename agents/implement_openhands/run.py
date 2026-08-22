"""
Implement Agent — OpenHands SDK 기반 실제 코딩 에이전트.

agents/base/agent.py와 달리 텍스트만 생성하는 게 아니라, 실제로
레포를 clone/checkout하고 파일을 수정하고 커밋/push까지 수행한다.

흐름:
  1. Redis에서 implement 태스크 수신 (github_repo, instruction)
  2. 워크스페이스에 레포가 없으면 clone (오케스트레이터가 이미 clone 해뒀으면 스킵)
  3. 새 브랜치 생성
  4. OpenHands SDK Conversation으로 실제 코드 작업 수행 (TerminalTool + FileEditorTool)
  5. 변경사항이 있으면 커밋 → 브랜치 push → GitHub PR 생성
  6. stage_completed 이벤트에 branch/pr_number/pr_url/head_sha를 실어 보냄
     → 이후 AutoTest 스테이지가 이 정보로 CI 결과를 추적
"""
import asyncio
import json
import os
import sys
import uuid

import httpx
import redis.asyncio as aioredis
from pydantic import SecretStr

from openhands.sdk import LLM, Agent, Conversation, Tool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.task_tracker import TaskTrackerTool
from openhands.tools.terminal import TerminalTool

from build_apk import build_and_handoff_apk, _extract_issue_lines, _has_analyze_errors
from git_workspace import run, ensure_git_workspace, find_conflict_marker_files
from prompt_helpers import mockup_guidance, NO_EMULATOR_GUIDANCE

REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379")
AGENT_NAME   = "implement"
API_KEY      = os.getenv("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
LLM_MODEL    = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
# OpenHands가 작업을 마쳤다고 보고해도 flutter analyze 에러가 남아있을 수 있어서
# (프롬프트 지시는 강제가 아니라 권고일 뿐), 결정적으로 analyze→수정 라운드를
# 돈다. 에이전트가 못 고치는 에러도 있을 수 있으니 무한 루프 방지용 상한이
# 필요하다 — 사용자 확인: 10라운드, 다 써도 안 고쳐지면 진행하지 않고
# stage_failed로 멈춘다("성공해야 넘어가는거 아냐?").
MAX_SELF_FIX_ROUNDS = 10

STREAM_INBOX  = f"agent:{AGENT_NAME}:inbox"
STREAM_EVENTS = "orchestrator:events"


class _Tee:
    """stdout/stderr를 컨테이너 로그와 /workspace/logs 파일에 동시에 남긴다
    (나중에 문제 분석용 — 이 에이전트는 상시 싱글턴이라 재시작돼도 이력이 남아야 함)."""
    def __init__(self, stream, path: str):
        self._stream = stream
        self._file = open(path, "a", buffering=1)

    def write(self, data):
        self._stream.write(data)
        self._file.write(data)

    def flush(self):
        self._stream.flush()
        self._file.flush()

    def __getattr__(self, name):
        # isatty()/fileno() 등을 OpenHands SDK(rich 등)가 호출해서, write/flush
        # 말고는 원본 스트림으로 그대로 위임해야 한다.
        return getattr(self._stream, name)


def _setup_file_log(name: str):
    os.makedirs("/workspace/logs", exist_ok=True)
    path = f"/workspace/logs/{name}.log"
    if os.path.exists(path) and os.path.getsize(path) > 5_000_000:
        os.replace(path, path + ".1")
    sys.stdout = _Tee(sys.stdout, path)
    sys.stderr = _Tee(sys.stderr, path)


_setup_file_log(AGENT_NAME)


async def emit(r: aioredis.Redis, event: dict):
    await r.xadd(STREAM_EVENTS, {"payload": json.dumps(event)})


async def open_pull_request(repo: str, branch: str, title: str, body_text: str) -> dict | None:
    """이 브랜치로 이미 열린 PR이 있으면(재작업 재시도) 새로 만들지 않고 그걸
    그대로 재사용한다 — 재시도마다 PR #5, #6, #7...로 쌓이는 걸 막기 위함."""
    if not GITHUB_TOKEN:
        return None
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://api.github.com/repos/{repo}/pulls",
            headers=headers,
            json={"title": title, "head": branch, "base": "main", "body": body_text},
        )
        if r.status_code == 201:
            return r.json()
        if r.status_code == 422:  # 이미 이 브랜치로 열린 PR 있음
            owner = repo.split("/")[0]
            existing = await client.get(
                f"https://api.github.com/repos/{repo}/pulls",
                headers=headers,
                params={"head": f"{owner}:{branch}", "state": "open"},
            )
            if existing.status_code == 200 and existing.json():
                return existing.json()[0]
        print(f"[implement] PR 생성 실패 ({r.status_code}): {r.text[:300]}")
        return None


CI_WORKFLOW = """name: validation
on: [pull_request]
jobs:
  analyze-and-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: subosito/flutter-action@v2
        with:
          flutter-version: '3.41.6'
      - run: flutter pub get
      - run: flutter analyze
      - run: flutter test
      - name: Run QA scenario integration tests (if present)
        run: |
          if [ -d integration_test ]; then
            flutter test integration_test/
          fi
"""


def ensure_ci_workflow(workspace: str) -> bool:
    """AutoTest 스테이지는 GitHub Actions 체크 결과를 폴링해서 통과해야 자동 병합한다.
    이 워크플로 파일이 레포에 없으면 체크 자체가 하나도 안 생겨서 20분 타임아웃
    →실패 →병합 안 됨으로 항상 끝나버린다. OpenHands 지시만 믿지 않고(놓칠 수 있음)
    여기서 결정적으로 항상 만들어 넣는다 — 이미 있으면 손대지 않는다."""
    path = f"{workspace}/.github/workflows/validation.yml"
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(CI_WORKFLOW)
    return True


def run_openhands_task(workspace: str, instruction: str, project_id: str, is_retry: bool, scenario_keys: list[str] | None = None) -> tuple[int, dict, str]:
    """OpenHands Conversation을 (동기적으로) 실행. 블로킹 호출이므로 to_thread로 감싸서 호출한다.
    반환값의 두 번째 원소는 이번 실행의 토큰/비용(OpenHands SDK의 llm.metrics가 이미 누적
    집계 — 플로우차트 탭 게이트에 표시하기 위함). 세 번째 원소는 MAX_SELF_FIX_ROUNDS를
    다 써도 남아있는 flutter analyze 에러(성공 시 빈 문자열) — 호출부가 이걸로
    커밋/PR 진행 여부를 결정한다."""
    llm = LLM(usage_id="implement", model=f"anthropic/{LLM_MODEL}", api_key=SecretStr(API_KEY))
    agent = Agent(
        llm=llm,
        tools=[
            Tool(name=TerminalTool.name),
            Tool(name=FileEditorTool.name),
            Tool(name=TaskTrackerTool.name),
        ],
    )
    events: list = []
    # persistence_dir을 워크스페이스(git 체크아웃) 밖에 둬야 나중에 git add -A로
    # OpenHands 내부 상태 파일이 사용자 레포에 잘못 커밋되는 걸 막을 수 있다.
    persistence_dir = f"/workspace/.openhands_state/{project_id}"
    conversation = Conversation(
        agent=agent, workspace=workspace, callbacks=[events.append],
        persistence_dir=persistence_dir,
    )
    retry_note = (
        "지금 워크스페이스엔 이전 시도에서 작성한 코드가 이미 그대로 있습니다 — "
        "처음부터 다시 만들지 마세요. 먼저 기존 파일들을 읽어서 뭐가 이미 있는지 "
        "파악한 뒤, 아래 피드백에서 지적된 부족한/틀린 부분만 고치거나 추가하세요.\n\n"
        if is_retry else ""
    )
    try:
        conversation.send_message(
            f"{retry_note}{instruction}\n\n"
            f"작업 공간의 design/applied/ 디렉터리에 시나리오(Jira 스토리)별 HTML 와이어프레임 "
            f"목업이 있고, designer_output.md(화면/컴포넌트 스펙), architect_output.md(기술 스택/ "
            f"모듈 구조/데이터 모델)가 있으면 먼저 읽어보세요 — 특히 architect_output.md의 "
            f"module_structure는 architect가 정한 폴더/파일 구조와 책임 분담이니 임의로 다른 "
            f"구조로 만들지 말고 그대로 따르세요. design/applied/*.html이 있으면 그 레이아웃/ "
            f"색상/버튼 배치를 최대한 그대로 Flutter 위젯으로 재현하세요 — {mockup_guidance(scenario_keys)} "
            f"이 컨테이너엔 Flutter/Android SDK가 설치돼 있습니다. Flutter 프로젝트가 아직 없으면 "
            f"android/ 밑 Gradle 파일들을 직접 타이핑하지 말고 반드시 `flutter create .`로 "
            f"골격을 생성한 뒤 그 위에 코드를 작성하세요 (손으로 만든 Gradle 설정은 버전이 "
            f"미묘하게 안 맞아 빌드가 반복 실패하는 원인이 됩니다). "
            f"작업을 마치기 전에 `flutter analyze`와 `flutter build apk --debug`를 직접 실행해서 "
            f"실제로 빌드되는 것까지 확인하고, 실패하면 그 자리에서 고치세요. "
            f"{NO_EMULATOR_GUIDANCE} "
            f"작업을 완료했으면 변경사항을 정리하고 멈추세요. "
            f"git commit/push는 직접 하지 마세요 (다른 프로세스가 처리합니다). "
            f"git merge/pull/rebase/cherry-pick도 직접 실행하지 마세요 — 브랜치는 "
            f"이미 최신 상태로 준비되어 있고, 실제로 충돌이 나면 그 컨플릭트 마커가 "
            f"그대로 커밋돼 CI가 깨지는 사고가 있었습니다."
        )
        conversation.run()

        # OpenHands에게 "flutter analyze/build 확인하고 고치라"고 프롬프트로
        # 지시했지만 강제는 아니라 실제로 안 했을 수 있다 — 여기서 결정적으로
        # analyze→수정 라운드를 돌려서 실제로 에러가 없어질 때까지(또는 상한
        # 소진까지) 강제한다. send_message()는 conversation의 실행 상태를
        # IDLE로 되돌리므로 같은 Conversation에서 반복 호출이 SDK가 의도한
        # 멀티턴 사용법이고, llm.metrics는 Conversation 생애 동안 누적되므로
        # 여러 라운드의 토큰/비용이 최종 token_usage에 그대로 합산된다.
        unresolved = ""
        for _ in range(MAX_SELF_FIX_ROUNDS):
            analyze = run(["flutter", "analyze"], cwd=workspace, timeout=180)
            analyze_text = f"{analyze.stdout}\n{analyze.stderr}"
            if not _has_analyze_errors(analyze_text):
                unresolved = ""
                break
            unresolved = _extract_issue_lines(analyze_text) or analyze_text[-2000:]
            conversation.send_message(
                f"`flutter analyze` 결과 아직 고쳐야 할 에러가 있습니다:\n{unresolved}\n"
                f"위 에러를 고치세요."
            )
            conversation.run()
        else:
            unresolved = unresolved or "(원인 불명 — flutter analyze 반복 실패)"
    finally:
        conversation.close()
    usage = llm.metrics.accumulated_token_usage
    token_usage = {
        "input_tokens":  usage.prompt_tokens,
        "output_tokens": usage.completion_tokens,
        "cost_usd":      llm.metrics.accumulated_cost,
    }
    return len(events), token_usage, unresolved


async def process_task(r: aioredis.Redis, task: dict):
    project_id   = task.get("project_id", "")
    stage        = task.get("stage")
    instruction  = task.get("instruction", "")
    github_repo  = task.get("github_repo", "")
    context      = task.get("context", {})
    retry_branch = context.get("retry_branch")
    scenario_keys = context.get("scenario_keys")
    workspace    = f"/workspace/{project_id}"

    await emit(r, {
        "type": "message", "project_id": project_id, "agent": AGENT_NAME,
        "content": f"[Implement] '{stage}' 시작 — 레포: {github_repo or '(없음)'}",
    })

    if not github_repo:
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": "⚠️ 연결된 GitHub 레포가 없어 구현을 건너뜁니다."})
        await emit(r, {"type": "stage_completed", "project_id": project_id, "agent": AGENT_NAME,
                        "stage": stage, "outputs": {"agent": AGENT_NAME, "summary": "레포 없음 — 스킵"}})
        return

    os.makedirs(workspace, exist_ok=True)

    repo_url = f"https://{GITHUB_TOKEN}@github.com/{github_repo}.git"
    ok, detail = ensure_git_workspace(workspace, repo_url)
    if not ok:
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": f"❌ 레포 clone 실패: {detail}"})
        return

    run(["git", "config", "user.email", "ai-team-manager@bot"], cwd=workspace)
    run(["git", "config", "user.name", "AI Team Manager"], cwd=workspace)
    run(["git", "fetch", "origin"], cwd=workspace, timeout=60)

    if retry_branch:
        # 재작업 요청 — 처음부터 새로 만들지 않고 실패했던 브랜치를 그대로 이어서
        # 고친다 (전엔 매번 main에서 새 브랜치를 파서 이전 작업이 통째로 버려지고
        # 매번 처음부터 재구현하는 버그가 있었음).
        branch = retry_branch
        checkout = run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=workspace)
        if checkout.returncode != 0:
            await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                            "content": f"⚠️ 기존 브랜치({branch}) 체크아웃 실패, main에서 새로 시작합니다: {checkout.stderr[:200]}"})
            retry_branch = None

    if not retry_branch:
        branch = f"ai-implement/{project_id}-{uuid.uuid4().hex[:6]}"
        run(["git", "checkout", "main"], cwd=workspace)
        run(["git", "pull", "origin", "main"], cwd=workspace, timeout=60)
        checkout = run(["git", "checkout", "-b", branch], cwd=workspace)
        if checkout.returncode != 0:
            await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                            "content": f"❌ 브랜치 생성 실패: {checkout.stderr[:300]}"})
            return

    await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                    "content": f"🤖 OpenHands 실행 중... (브랜치: {branch})"})

    try:
        event_count, token_usage, unresolved = await asyncio.to_thread(run_openhands_task, workspace, instruction, project_id, bool(retry_branch), scenario_keys)
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": f"✅ OpenHands 작업 완료 ({event_count}개 이벤트)"})
    except Exception as e:
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": f"❌ OpenHands 실행 오류: {e}"})
        return

    if unresolved:
        # MAX_SELF_FIX_ROUNDS를 다 써도 flutter analyze 에러가 안 없어졌다 —
        # 커밋/푸시/PR을 진행하지 않고 여기서 멈춘다("성공해야 넘어가는거 아냐?").
        # agents/base/agent.py의 build_stage_failed_event와 동일한 이벤트
        # shape을 직접 구성해서 보낸다 — implement/qa/autotest는 그 헬퍼를
        # 공유하지 않는 독립 빌드 컨텍스트라서.
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": f"❌ flutter analyze 에러를 {MAX_SELF_FIX_ROUNDS}라운드 안에 못 고쳤습니다 — 중단.\n{unresolved}"})
        await emit(r, {"type": "stage_failed", "project_id": project_id, "agent": AGENT_NAME,
                        "stage": stage, "error": unresolved})
        return

    if ensure_ci_workflow(workspace):
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": "🔧 CI 워크플로(.github/workflows/validation.yml) 없어서 자동 추가 — AutoTest/자동 병합이 동작하려면 필요"})

    diff = run(["git", "status", "--porcelain"], cwd=workspace)
    if not diff.stdout.strip():
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": "ℹ️ 변경된 파일이 없습니다 — PR 생략."})
        await emit(r, {"type": "stage_completed", "project_id": project_id, "agent": AGENT_NAME,
                        "stage": stage, "outputs": {"agent": AGENT_NAME, "summary": "변경사항 없음", **token_usage}})
        return

    # QA가 같은 소스를 처음부터 다시 빌드하지 않도록, Implement가 결정적으로
    # (LLM 지시가 아니라 이 코드로) 한 번 더 빌드해서 고정 경로에 남긴다.
    # 실패해도 PR 생성 자체는 막지 않는다 — build_ok=False를 outputs에 실어
    # 보내면 QA가 이걸 보고 자기 빌드로 폴백한다(process_task의 기존 방어선).
    build_ok, build_detail, apk_path = await asyncio.to_thread(build_and_handoff_apk, workspace, project_id)
    if build_ok:
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": f"📦 APK 빌드 완료 — QA가 재빌드 없이 이 파일을 그대로 검증합니다: {apk_path}"})
    else:
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": f"⚠️ 자체 빌드 검증 실패 — QA가 자체 빌드로 폴백해서 검증합니다: {build_detail[:300]}"})

    conflict_files = find_conflict_marker_files(workspace, diff.stdout)
    if conflict_files:
        # 실제 진행 중인 merge가 있었다면 워크스페이스를 깨끗하게 되돌려서
        # 다음 재시도가 이 라운드의 반쯤 풀린 merge 상태를 이어받지 않게 한다
        # (실패해도 무시 — merge 중이 아니었을 수도 있음).
        run(["git", "merge", "--abort"], cwd=workspace)
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": f"❌ 커밋 전 git 컨플릭트 마커 발견 — 커밋을 막았습니다: {', '.join(conflict_files[:10])}"})
        await emit(r, {"type": "stage_failed", "project_id": project_id, "agent": AGENT_NAME,
                        "stage": stage,
                        "error": f"OpenHands가 남긴 것으로 보이는 git 컨플릭트 마커가 있어 커밋/PR을 중단했습니다: {', '.join(conflict_files)}"})
        return

    run(["git", "add", "-A"], cwd=workspace)
    run(["git", "commit", "-m", f"AI Implement: {instruction[:60]}"], cwd=workspace)
    push = run(["git", "push", "-u", "origin", branch], cwd=workspace, timeout=120)
    if push.returncode != 0:
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": f"❌ 브랜치 push 실패: {push.stderr[:300]}"})
        return

    head_sha = run(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()
    pr = await open_pull_request(github_repo, branch, f"[AI] {instruction[:60]}", instruction)

    if not pr:
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": f"⚠️ 브랜치는 push했지만 PR 생성에 실패했습니다: {branch}"})
        await emit(r, {"type": "stage_completed", "project_id": project_id, "agent": AGENT_NAME,
                        "stage": stage, "outputs": {"agent": AGENT_NAME, "summary": "PR 생성 실패",
                                                     "branch": branch, "head_sha": head_sha,
                                                     "build_ok": build_ok, "scenario_keys": scenario_keys,
                                                     **token_usage}})
        return

    await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                    "content": f"✅ PR 생성 완료: {pr['html_url']}"})
    await emit(r, {
        "type": "stage_completed", "project_id": project_id, "agent": AGENT_NAME, "stage": stage,
        "outputs": {
            "agent": AGENT_NAME,
            "summary": f"PR #{pr['number']} 생성: {pr['html_url']}",
            "branch": branch,
            "pr_number": pr["number"],
            "pr_url": pr["html_url"],
            "head_sha": head_sha,
            "build_ok": build_ok,
            # 오케스트레이터(main.py)가 이 PR 링크 코멘트를 어느 Jira 이슈에 달지
            # 결정할 때 필요 — 범위가 특정 시나리오(들)로 좁혀진 재작업
            # (scenario_keys 있음)이면 그 이슈들에만, 아니면(None) 전체 스토리에 단다.
            "scenario_keys": scenario_keys,
            **token_usage,
        },
    })


GROUP_NAME = "workers"


async def ensure_group(r: aioredis.Redis):
    """컨슈머 그룹 생성 (최초 1회). id='$'로 시작해 그룹 생성 시점 이후의
    메시지만 받는다 — 재시작마다 스트림 전체를 처음부터 재생하면서 같은
    implement 작업이 반복 실행돼 GitHub에 중복 PR이 여러 번 쌓였던 버그를
    막기 위함 (실제로 한 레포에 동일 내용 PR이 9개 쌓인 적 있음)."""
    try:
        await r.xgroup_create(name=STREAM_INBOX, groupname=GROUP_NAME, id="$", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            raise


async def main():
    if not API_KEY:
        print("[implement] ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    if not GITHUB_TOKEN:
        print("[implement] ERROR: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    print(f"[implement] OpenHands 기반 구현 에이전트 시작 (model={LLM_MODEL})")
    r = await aioredis.from_url(REDIS_URL, decode_responses=True)
    await ensure_group(r)

    while True:
        try:
            results = await r.xreadgroup(
                GROUP_NAME, "implement", {STREAM_INBOX: ">"}, block=500, count=1
            )
            for _, messages in results:
                for msg_id, fields in messages:
                    await process_task(r, json.loads(fields["payload"]))
                    await r.xack(STREAM_INBOX, GROUP_NAME, msg_id)
        except Exception as e:
            print(f"[implement] Error: {e}", file=sys.stderr)
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
