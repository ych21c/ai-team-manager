"""
AutoTest Agent — 실제 테스트를 직접 돌리지 않고, Implement 단계가 올린 PR의
GitHub Actions 체크(.github/workflows/validation.yml: flutter analyze + flutter test)
결과를 폴링해서 pass/fail을 판정한다.

자체 Flutter 실행 환경을 새로 만드는 대신 이미 검증된 CI를 재사용 — 버전 드리프트,
중복 유지보수를 피하고 실제 PR 머지 조건과 동일한 기준으로 판정한다.
"""
import asyncio
import json
import os
import re
import sys
import time

import httpx
import redis.asyncio as aioredis

REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379")
AGENT_NAME   = "autotest"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
POLL_INTERVAL_SEC = 15
MAX_WAIT_SEC       = 20 * 60   # CI가 20분 넘게 걸리면 타임아웃 처리(실패로 간주)

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


def _find_upstream(context: dict, key: str):
    """이전 스테이지들의 outputs를 뒤져서 key를 찾는다 (implement가 만든 branch/pr_number/head_sha 등).
    implement를 최우선으로 본다 — autotest 자신도 이전 라운드 결과에 같은 이름의
    필드(branch/head_sha/pr_number)를 남기는데, 그게 completed 상태로 계속 남아
    있으면 context에 같이 실려서 방금 implement가 만든 새 브랜치를 낡은 값으로
    덮어쓴다(QA에서 실제로 이 사고로 몇 주 된 브랜치를 계속 테스트했었다)."""
    implement_outputs = context.get("implement")
    if isinstance(implement_outputs, dict) and key in implement_outputs:
        return implement_outputs[key]
    for name, stage_outputs in context.items():
        if name != "implement" and isinstance(stage_outputs, dict) and key in stage_outputs:
            return stage_outputs[key]
    return None


async def get_check_status(client: httpx.AsyncClient, repo: str, sha: str) -> tuple[str, str]:
    """GitHub Checks API 조회. 반환: (state, summary). state는 'pending' | 'success' | 'failure'."""
    r = await client.get(
        f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs",
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
    )
    if r.status_code != 200:
        return "pending", f"체크 조회 실패 ({r.status_code})"

    runs = r.json().get("check_runs", [])
    if not runs:
        return "pending", "체크 런 없음 (아직 시작 안 됐을 수 있음)"

    if any(run["status"] != "completed" for run in runs):
        return "pending", f"{len(runs)}개 체크 중 진행중"

    failed = [run["name"] for run in runs if run.get("conclusion") not in ("success", "skipped", "neutral")]
    if failed:
        return "failure", f"실패한 체크: {', '.join(failed)}"
    return "success", f"{len(runs)}개 체크 모두 통과"


def _pick_failed_run(runs: list[dict]) -> dict | None:
    return next((run for run in runs if run.get("conclusion") not in ("success", "skipped", "neutral")), None)


def _extract_job_id(html_url: str) -> str | None:
    """check-run의 html_url(예: https://github.com/o/r/actions/runs/123/job/456)에서
    job id만 뽑는다 — check-run API 자체엔 로그가 없어서 job 로그 API를 따로 불러야 한다."""
    m = re.search(r"/runs/\d+/job/(\d+)", html_url or "")
    return m.group(1) if m else None


async def fetch_failure_detail(client: httpx.AsyncClient, repo: str, sha: str) -> str:
    """실패한 체크런의 실제 로그 마지막 부분을 가져온다 — "실패한 체크: analyze-and-test"
    라는 요약만으로는 Implement가 뭘 고쳐야 하는지 알 수 없어서, 재작업 피드백에
    실제 에러 메시지를 실어 보내기 위함."""
    try:
        r = await client.get(
            f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
        )
        failed_run = _pick_failed_run(r.json().get("check_runs", []))
        if not failed_run:
            return ""
        job_id = _extract_job_id(failed_run.get("html_url", ""))
        if not job_id:
            return ""
        log_r = await client.get(
            f"https://api.github.com/repos/{repo}/actions/jobs/{job_id}/logs",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
            follow_redirects=True,
        )
        if log_r.status_code != 200:
            return ""
        return log_r.text[-2000:]
    except Exception as e:
        print(f"[autotest] 실패 로그 조회 실패: {e}")
        return ""


async def process_task(r: aioredis.Redis, task: dict):
    project_id  = task.get("project_id", "")
    stage       = task.get("stage")
    context     = task.get("context", {})
    github_repo = task.get("github_repo", "")

    branch    = _find_upstream(context, "branch")
    head_sha  = _find_upstream(context, "head_sha")
    pr_number = _find_upstream(context, "pr_number")

    await emit(r, {
        "type": "message", "project_id": project_id, "agent": AGENT_NAME,
        "content": f"[AutoTest] '{stage}' 시작 — PR #{pr_number or '?'} CI 결과 대기 중...",
    })

    if not github_repo or not head_sha:
        await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                        "content": "⚠️ 대상 커밋이 없어 AutoTest를 건너뜁니다 (통과 처리)."})
        await emit(r, {"type": "stage_completed", "project_id": project_id, "agent": AGENT_NAME, "stage": stage,
                        "outputs": {"agent": AGENT_NAME, "passed": True, "summary": "대상 없음 — 스킵"}})
        return

    start = time.time()
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            state, summary = await get_check_status(client, github_repo, head_sha)
            if state != "pending":
                break
            if time.time() - start > MAX_WAIT_SEC:
                state, summary = "failure", "CI 대기 시간 초과 (20분)"
                break
            await emit(r, {"type": "progress", "project_id": project_id, "agent": AGENT_NAME,
                            "progress": min(90, int((time.time() - start) / MAX_WAIT_SEC * 100)),
                            "message": summary})
            await asyncio.sleep(POLL_INTERVAL_SEC)

    passed = state == "success"
    icon = "✅" if passed else "❌"

    # 예전엔 "실패한 체크: analyze-and-test"라는 요약만 갖고 파이프라인을
    # 멈췄다 — Implement 입장에선 뭘 고쳐야 하는지 전혀 알 수 없는 정보였다.
    # 실제 실패 로그를 받아와서 needs_rework로 넘기면, QA 실패와 동일한 재작업
    # 루프(_retry_implement_with_feedback, 횟수 제한 있음)를 그대로 타게 된다.
    feedback = None
    if not passed and state == "failure" and summary != "CI 대기 시간 초과 (20분)":
        async with httpx.AsyncClient(timeout=20) as client:
            detail = await fetch_failure_detail(client, github_repo, head_sha)
        if detail:
            feedback = f"GitHub Actions CI 실패 ({summary}). 실제 로그:\n{detail}"

    await emit(r, {"type": "message", "project_id": project_id, "agent": AGENT_NAME,
                    "content": f"{icon} AutoTest {'통과' if passed else '실패'} — {summary}"})
    await emit(r, {
        "type": "stage_completed", "project_id": project_id, "agent": AGENT_NAME, "stage": stage,
        "outputs": {
            "agent": AGENT_NAME,
            "passed": passed,
            **({"needs_rework": True, "feedback": feedback} if feedback else {}),
            "summary": summary,
            "branch": branch,
            "pr_number": pr_number,
            "head_sha": head_sha,
        },
    })


GROUP_NAME = "workers"


async def ensure_group(r: aioredis.Redis):
    """컨슈머 그룹 생성 (최초 1회) — 재시작마다 스트림 전체를 재생하는 걸
    막아서 이미 끝난 CI 폴링 작업이 반복되지 않게 한다."""
    try:
        await r.xgroup_create(name=STREAM_INBOX, groupname=GROUP_NAME, id="$", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            raise


async def main():
    if not GITHUB_TOKEN:
        print("[autotest] ERROR: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    print("[autotest] GitHub Actions CI 결과 기반 AutoTest 에이전트 시작")
    r = await aioredis.from_url(REDIS_URL, decode_responses=True)
    await ensure_group(r)

    while True:
        try:
            results = await r.xreadgroup(
                GROUP_NAME, "autotest", {STREAM_INBOX: ">"}, block=500, count=1
            )
            for _, messages in results:
                for msg_id, fields in messages:
                    await process_task(r, json.loads(fields["payload"]))
                    await r.xack(STREAM_INBOX, GROUP_NAME, msg_id)
        except Exception as e:
            print(f"[autotest] Error: {e}", file=sys.stderr)
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
