"""
AI Team Manager — 에이전트 베이스 (토큰 최적화)
- 역할별 모델 분리 (Sonnet/Haiku)
- 역할별 max_tokens 조정
- SOURCE_CONTEXT는 자가 개선 모드에서만 포함
- 컨텍스트 요약 전달 (전체 출력 대신 summary만)
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import httpx
import redis.asyncio as aioredis
import anthropic

REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379")
AGENT_NAME      = os.getenv("AGENT_NAME", "unknown")
AGENT_ROLE      = os.getenv("AGENT_ROLE", "Agent")
API_KEY         = os.getenv("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN    = os.getenv("GITHUB_TOKEN", "")
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://orchestrator:8000")
# TeamSpawner가 프로젝트마다 이 이미지로 격리된 컨테이너를 띄우며 PROJECT_ID를 넣어준다.
# 큐 이름에 project_id를 포함시켜야 여러 프로젝트의 동일 역할 컨테이너가 서로의
# 태스크를 중복으로 집어가지 않는다 (컨슈머 그룹 없는 단순 xread이기 때문).
PROJECT_ID = os.getenv("PROJECT_ID", "")

STREAM_INBOX  = f"agent:{AGENT_NAME}:{PROJECT_ID}:inbox" if PROJECT_ID else f"agent:{AGENT_NAME}:inbox"
STREAM_EVENTS = "orchestrator:events"


class _Tee:
    """stdout/stderr를 컨테이너 로그와 /workspace/logs 파일에 동시에 남긴다.
    컨테이너가 재시작/삭제돼도(TeamSpawner가 프로젝트별로 띄우는 컨테이너 특성상
    흔히 발생) shared-workspace 볼륨에 이력이 남아 나중에 분석할 수 있다."""
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
        # isatty()/fileno()/encoding 등 write/flush 말고 다른 파일류 속성을
        # 참조하는 라이브러리가 있어서(예: OpenHands가 isatty() 호출) 원본
        # 스트림으로 그대로 위임한다.
        return getattr(self._stream, name)


def _setup_file_log(name: str):
    os.makedirs("/workspace/logs", exist_ok=True)
    path = f"/workspace/logs/{name}.log"
    if os.path.exists(path) and os.path.getsize(path) > 5_000_000:
        os.replace(path, path + ".1")
    sys.stdout = _Tee(sys.stdout, path)
    sys.stderr = _Tee(sys.stderr, path)


_setup_file_log(f"{AGENT_NAME}-{PROJECT_ID}" if PROJECT_ID else AGENT_NAME)

# ── 역할별 모델 (비용 최적화) ────────────────────────────────────────
# Sonnet: 복잡한 기획·설계  /  Haiku: 단순 검증·배포
AGENT_MODELS = {
    "pm":        "claude-sonnet-4-6",
    "architect": "claude-sonnet-4-6",
    "designer":  "claude-sonnet-4-6",   # HTML 목업까지 만들어야 해서 Haiku→Sonnet
    "implement": "claude-sonnet-4-6",   # 코딩은 Sonnet
    "qa":        "claude-haiku-4-5-20251001",
    "autotest":  "claude-haiku-4-5-20251001",
    "release":   "claude-haiku-4-5-20251001",
}

# ── 역할별 max_tokens ────────────────────────────────────────────────
# 예전엔 역할별로 작은 값(400자/4096토큰 등)을 정해뒀다가 프로젝트가 커질 때마다
# (화면 수 많은 디자인, 긴 PRD 등) 중간에 잘리는 사고가 반복됐다 — 8192로 올려도
# recoveryFit(화면 12개)처럼 더 큰 프로젝트가 나오면 또 재현되는 식. 상한을
# "적당히 큰 값"이 아니라 각 모델이 스트리밍으로 낼 수 있는 절대 최대치로 맞춰서
# 이 클래스의 버그 자체를 없앤다. 이것도 넘기면(정말 그 모델의 절대 한도까지
# 다 쓴 경우) run() 안의 continuation 로직이 "이어서 작성해주세요" 후속 요청으로
# 이어붙인다 — 어떤 경우에도 조용히 잘리지 않게.
# claude-sonnet-4-6: 스트리밍 시 128K까지 베타 헤더 없이 지원.
# claude-haiku-4-5: 스트리밍 시 최대 64K.
_SONNET_MAX = 128000
_HAIKU_MAX  = 64000
AGENT_MAX_TOKENS = {
    "pm":        _SONNET_MAX,
    "architect": _SONNET_MAX,
    "designer":  _SONNET_MAX,
    "implement": _SONNET_MAX,
    "qa":        _HAIKU_MAX,
    "autotest":  _HAIKU_MAX,
    "release":   _HAIKU_MAX,
}
MAX_CONTINUATIONS = 5  # max_tokens에 걸려도 최대 이만큼 "이어서 작성" 후속 요청으로 이어붙인다

MODEL      = AGENT_MODELS.get(AGENT_NAME, os.getenv("LLM_MODEL", "claude-sonnet-4-6"))
MAX_TOKENS = AGENT_MAX_TOKENS.get(AGENT_NAME, _SONNET_MAX)

# ── 토큰 비용($) — 플로우차트 탭 게이트에 스테이지별 사용량을 보여주기 위함 ──
# USD / 1M 토큰. 모델이 바뀌면 이 표도 같이 갱신해야 함(자동 조회 API는 없음).
_TOKEN_PRICE_PER_MTOK = {
    "claude-sonnet-4-6":          (3.0, 15.0),
    "claude-haiku-4-5-20251001":  (1.0, 5.0),
}

def _cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = _TOKEN_PRICE_PER_MTOK.get(model)
    if not price:
        return None
    in_rate, out_rate = price
    return round(input_tokens * in_rate / 1_000_000 + output_tokens * out_rate / 1_000_000, 6)


def build_summary(full_response: str, agent_name: str) -> str:
    """예전엔 400자 → 6000자로 상한을 올렸다가도 다시 잘리는 사고가 반복됐다 —
    downstream(QA의 시나리오/디자인 검증, Jira 코멘트, 다음 스테이지 컨텍스트 등)이
    실제 산출물을 못 보는 문제라 상한 숫자를 올리는 식으로는 못 없앤다. 그래서
    아예 자르지 않는다 — summary는 항상 full_response 그대로."""
    return full_response

# ── 소스코드 구조 (자가 개선 모드에서만 사용) ────────────────────────
SOURCE_CONTEXT = """
## AI Team Manager 소스코드
/workspace/ai-team-manager/
├── orchestrator/main.py        # FastAPI 오케스트레이터
├── orchestrator/team_spawner.py
├── agents/base/agent.py        # 이 파일
├── web/app/page.tsx            # Web UI
└── docker-compose.yml

수정 후 재시작: POST http://orchestrator:8000/restart {"service": "web"}
"""

# ── 역할별 시스템 프롬프트 ────────────────────────────────────────────
ROLE_PROMPTS: dict[str, str] = {
    "pm": """당신은 AI Team Manager의 PM입니다.
역할: 요구사항 분석, PRD 작성, 마일스톤 수립.
산출물: /workspace/{project_id}/prd.md
출력: JSON { "summary": "...", "requirements": [...], "milestones": [...] }""",

    "designer": """당신은 AI Team Manager의 UX Designer입니다.
역할: UX 플로우, 화면 스펙, 컴포넌트 구조 정의 + 실제로 브라우저에서 눈으로 볼 수 있는 HTML 목업 제작.
산출물: /workspace/{project_id}/design_spec.md

반드시 아래를 모두 출력하세요:
1. JSON { "screens": [...], "components": [...], "design_tokens": {...} }
2. 프롬프트에 포함된 "시나리오 목록"의 시나리오마다 하나씩, 아래 형식으로 별도 HTML 목업을
   작성하세요 (시나리오가 여러 개면 블록도 여러 개):

   ## SCENARIO:{시나리오 key}
   ```html
   <!DOCTYPE html>...완전한 HTML...</html>
   ```

   각 목업은 인라인 CSS만 쓰고 외부 리소스 없이 그 파일 하나로 바로 열리게 만드세요.
   픽셀 퍼펙트일 필요 없고, 레이아웃/버튼 배치/색상 등을 눈으로 확인할 수 있는 와이어프레임
   수준이면 충분합니다. `## SCENARIO:{key}` 줄의 key는 반드시 프롬프트에 주어진 시나리오
   key를 정확히 그대로 쓰세요(제목을 새로 짓지 마세요).""",

    "architect": """당신은 AI Team Manager의 Software Architect입니다.
역할: 시스템 아키텍처, API 스펙, 데이터 모델 설계.
산출물: /workspace/{project_id}/architecture.md
출력: JSON { "tech_stack": {...}, "api_spec": [...], "data_models": [...] }""",

    "qa": """당신은 AI Team Manager의 QA Engineer입니다.
역할: 테스트 케이스 작성, 버그 리포트.
산출물: /workspace/{project_id}/qa_report.md
출력: JSON { "test_cases": [...], "bugs": [...] }""",

    "autotest": """당신은 AI Team Manager의 AutoTest Runner입니다.
역할: 테스트 실행, 결과 수집.
산출물: /workspace/{project_id}/test_results.md
출력: JSON { "passed": N, "failed": N, "results": [...] }""",

    "release": """당신은 AI Team Manager의 Release Manager입니다.
역할: 배포, 릴리즈 노트 작성.
자가 개선 시: POST http://orchestrator:8000/restart {"service": "web"}
산출물: /workspace/{project_id}/release_notes.md
출력: JSON { "version": "...", "release_notes": "..." }""",
}

# ── 채팅 트리아지 전용 프롬프트 ──────────────────────────────────────
# ROLE_PROMPTS["pm"]는 PRD 작성용이라 트리아지(이미 지나간 단계에 대한 후속
# 채팅을 어디로 돌려보낼지 판단)에는 안 맞는다 — 그래서 별도 프롬프트로 분리해서
# 실제 기획 태스크에는 절대 섞여 들어가지 않게 한다.
CHAT_TRIAGE_PROMPT = """당신은 AI Team Manager의 PM입니다.
지금 맡은 역할은 PRD 작성이 아니라, 이미 설계/구현이 진행된 프로젝트에 사용자가 채팅으로
보낸 후속 요청을 검토해서 어디로 돌려보낼지 결정하는 트리아지입니다.

아래 "현재 파이프라인 상태"를 참고해서 사용자의 새 메시지가 다음 중 무엇인지 판단하세요:
- "design": 화면/디자인/UX가 잘못됐거나 없어졌거나 다시 만들어야 함 → designer/architect 재작업
- "implement": 디자인은 그대로 두고 코드 동작만 고치면 됨 → 구현만 재작업
- "none": 단순 질문/잡담/애매해서 되물어야 하는 요청 → 아무 것도 재작업하지 않음

scope가 "design" 또는 "implement"면 target도 판단하세요 — "## 기존 이슈 목록"에 있는
화면/기능 중 하나를 고치는 요청이면 그 key(예: "ATM-5")를, 목록 어디에도 안 맞는
완전히 새로운 화면/기능 요청이면 "new"를, 판단이 안 서면 null을 쓰세요. target이
"new"면 new_story_title에 Jira에 새로 등록할 짧은 한 줄 제목도 채우세요 — 그 외에는
new_story_title을 빈 문자열로 두세요. target을 정하면 재작업이 그 화면/이슈 하나로
좁혀지고 나머지 화면은 안 건드리니, 확신 없으면 null로 두는 쪽이 안전합니다.

반드시 JSON 하나만 출력하세요 (다른 텍스트 없이):
{"scope": "design"|"implement"|"none",
 "target": "ATM-5"|"new"|null,
 "new_story_title": "target이 new일 때만 채우는 짧은 제목, 그 외엔 빈 문자열",
 "feedback": "design/implement로 보낼 때 담당 에이전트에게 그대로 전달할 구체적 지시문(2~5문장)",
 "reply": "사용자에게 보여줄 짧은 한국어 설명 — 모든 경우에 채우세요"}

판단이 애매하면 "none"을 고르고 reply에서 화면 문제인지 동작 문제인지 되물으세요."""


def get_system_prompt(project_id: str = "") -> str:
    base = ROLE_PROMPTS.get(AGENT_NAME, f"당신은 AI Team Manager의 {AGENT_ROLE}입니다.")
    # 자가 개선 모드에서만 소스코드 컨텍스트 추가
    if project_id == "self-improve":
        base += f"\n\n⚠️ 자가 개선 프로젝트 — 실제 소스코드를 수정하세요.{SOURCE_CONTEXT}"
    return base


def summarize_context(context: dict) -> str:
    """이전 스테이지 산출물의 summary를 그대로 전달한다. 예전엔 여기서 300자로
    또 잘랐는데, build_summary()가 이제 summary를 안 자르게 고쳐도 이 300자
    커트라인이 그대로면 QA/다음 스테이지가 결국 못 보는 건 똑같다 — 모델
    컨텍스트 윈도우가 충분히 크므로(1M 토큰) 여기서 굳이 다시 자르지 않는다."""
    if not context:
        return ""
    lines = ["\n## 이전 스테이지 요약"]
    for stage, outputs in context.items():
        if stage == "github_repo":
            lines.append(f"- GitHub: {outputs}")
            continue
        if isinstance(outputs, dict):
            summary = outputs.get("summary", outputs.get("agent", ""))
            if summary:
                lines.append(f"- {stage}: {summary}")
        elif isinstance(outputs, str):
            lines.append(f"- {stage}: {outputs}")
    return "\n".join(lines)


def parse_triage_decision(text: str) -> dict:
    """PM 트리아지 응답에서 JSON 결정을 뽑아낸다. 파싱 실패/scope 이상값이면
    scope=none으로 안전하게 폴백한다 — 이 스테이지는 무조건 stage_completed로
    끝나야 하므로(안 그러면 orchestrator가 영원히 응답을 못 받는다), 여기서
    예외를 던지는 대신 항상 dict를 반환한다.

    target/new_story_title은 여기서 실제 Jira 키인지 검증하지 않는다 — 이
    프로세스는 project_jira 상태를 모르기 때문에(그건 오케스트레이터만 앎),
    형식만 방어적으로 파싱해서 넘기고 실제 존재 여부 검증은 orchestrator의
    _handle_chat_triage_result가 한다."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {"scope": "none", "target": None, "new_story_title": "", "feedback": "", "reply": text.strip() or "요청을 이해하지 못했습니다."}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"scope": "none", "target": None, "new_story_title": "", "feedback": "", "reply": text.strip() or "요청을 이해하지 못했습니다."}
    scope = data.get("scope") if data.get("scope") in ("design", "implement", "none") else "none"
    target = data.get("target")
    target = str(target) if target else None
    return {
        "scope": scope,
        "target": target,
        "new_story_title": str(data.get("new_story_title", "")),
        "feedback": str(data.get("feedback", "")),
        "reply": str(data.get("reply", "")),
    }


async def emit(r: aioredis.Redis, event: dict):
    await r.xadd(STREAM_EVENTS, {"payload": json.dumps(event)})


async def process_chat_triage(r: aioredis.Redis, task: dict):
    """이미 지나간 단계에 대한 채팅 후속 요청을 검토해서 design/implement 중
    어디를 다시 돌릴지(또는 아무 것도 안 할지) 판단만 하는 가벼운 태스크.
    real PM 기획 태스크(process_task)와 달리 문서 파일 작성/git 커밋/목업
    파싱을 전혀 하지 않는다 — 이건 산출물이 아니라 라우팅 판단이기 때문."""
    project_id = task.get("project_id", "")
    user_message = task.get("instruction", "")
    context = dict(task.get("context", {}))
    stage_status = context.pop("stage_status", {})
    status_line = ", ".join(f"{k}={v}" for k, v in stage_status.items())
    existing_issues = context.pop("existing_issues", [])
    issues_line = "\n".join(f"- {i['key']}: {i['title']}" for i in existing_issues) or "(등록된 이슈 없음)"
    context_str = summarize_context(context)

    user_prompt = (
        f"사용자의 새 메시지: {user_message}\n\n"
        f"## 기존 이슈 목록\n{issues_line}\n\n"
        f"## 현재 파이프라인 상태\n{status_line}\n{context_str}"
    )

    full_response = ""
    try:
        client = anthropic.AsyncAnthropic(api_key=API_KEY)
        async with client.messages.stream(
            model=AGENT_MODELS.get("pm", MODEL),
            max_tokens=_SONNET_MAX,
            system=CHAT_TRIAGE_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        ) as stream:
            async for chunk in stream.text_stream:
                full_response += chunk
    except Exception as e:
        print(f"[{AGENT_NAME}] chat_triage 호출 실패: {e}", file=sys.stderr)

    decision = parse_triage_decision(full_response)
    await emit(r, {
        "type": "stage_completed",
        "project_id": project_id,
        "agent": AGENT_NAME,
        "stage": "chat_triage",
        "outputs": decision,
    })


def git_commit_and_push(workspace: str, github_repo: str, message: str) -> bool:
    """이 산출물을 스스로 커밋+푸시한다 (Designer 등이 자기 문서를 직접 올림 —
    implement가 나중에 add -A로 쓸어담을 때까지 기다리지 않아도 됨). main에
    직접 푸시하는 건 앱 코드가 아니라 문서/목업 파일이라 리뷰 없이 바로 반영해도
    괜찮다는 전제. 실패해도 파이프라인은 막지 않는다(비필수 부가 기능)."""
    if not GITHUB_TOKEN or not github_repo or not os.path.exists(f"{workspace}/.git"):
        return False
    try:
        def run(cmd):
            return subprocess.run(cmd, cwd=workspace, capture_output=True, text=True, timeout=30)
        run(["git", "config", "user.email", "ai-team-manager@bot"])
        run(["git", "config", "user.name", "AI Team Manager"])
        run(["git", "pull", "--rebase", "origin", "main"])
        run(["git", "add", "-A"])
        commit = run(["git", "commit", "-m", message])
        if commit.returncode != 0:
            return False  # 변경사항 없음 등 — 에러 아님
        push = run(["git", "push", "origin", "HEAD:main"])
        if push.returncode != 0:
            print(f"[{AGENT_NAME}] git push 실패: {push.stderr[:300]}")
            return False
        return True
    except Exception as e:
        print(f"[{AGENT_NAME}] git 작업 실패: {e}")
        return False


def parse_scenario_mockups(full_response: str) -> dict[str, str]:
    """'## SCENARIO:{key}' 마커 뒤에 오는 ```html``` 블록을 시나리오(Jira 스토리)별로
    추출한다. 마커가 하나도 없으면(레거시 단일 블록 응답 등) 첫 html 블록을 "main"
    시나리오로 취급해 예전과 동일하게 동작한다. 마커는 있었지만 key가 전부
    안전하지 않은 경우(예: 경로탈출) "main" 폴백으로 새지 않도록, 폴백은 마커
    자체가 하나도 없을 때만 탄다."""
    raw_matches = re.findall(r"##\s*SCENARIO:(\S+).*?```html\s*(.*?)```", full_response, re.DOTALL)
    if raw_matches:
        return {
            key: block.strip()
            for key, block in raw_matches
            if "/" not in key and ".." not in key
        }
    m = re.search(r"```html\s*(.*?)```", full_response, re.DOTALL)
    if m:
        return {"main": m.group(1).strip()}
    return {}


def git_new_branch_commit_push(workspace: str, github_repo: str, branch: str, message: str) -> bool:
    """design 목업을 main에 바로 올리지 않고 새 브랜치에 커밋+푸시한다 — 오케스트레이터가
    이 브랜치로 PR을 만들고 즉시 머지하므로, 머지 전/후 상태를 UI에서 분리해 보여줄 수 있다.
    origin/main 기준으로 새로 브랜치를 따서, 이 태스크가 만든 파일만 이 브랜치에 실린다."""
    if not GITHUB_TOKEN or not github_repo or not os.path.exists(f"{workspace}/.git"):
        return False
    try:
        def run(cmd):
            return subprocess.run(cmd, cwd=workspace, capture_output=True, text=True, timeout=30)
        run(["git", "config", "user.email", "ai-team-manager@bot"])
        run(["git", "config", "user.name", "AI Team Manager"])
        run(["git", "fetch", "origin", "main"])
        checkout = run(["git", "checkout", "-b", branch, "origin/main"])
        if checkout.returncode != 0:
            print(f"[{AGENT_NAME}] git checkout -b {branch} 실패: {checkout.stderr[:300]}")
            return False
        run(["git", "add", "-A"])
        commit = run(["git", "commit", "-m", message])
        if commit.returncode != 0:
            return False  # 변경사항 없음 등 — 에러 아님
        push = run(["git", "push", "origin", f"HEAD:{branch}"])
        if push.returncode != 0:
            print(f"[{AGENT_NAME}] git push({branch}) 실패: {push.stderr[:300]}")
            return False
        return True
    except Exception as e:
        print(f"[{AGENT_NAME}] git 브랜치 작업 실패: {e}")
        return False


async def process_task(r: aioredis.Redis, task: dict):
    if task.get("stage") == "chat_triage":
        await process_chat_triage(r, task)
        return

    project_id  = task.get("project_id", "")
    stage       = task.get("stage")
    instruction = task.get("instruction", "")
    context     = task.get("context", {})
    github_repo = task.get("github_repo", "")

    await emit(r, {
        "type": "message",
        "project_id": project_id,
        "agent": AGENT_NAME,
        "content": f"[{AGENT_ROLE}] '{stage}' 시작" + (" 🔧" if project_id == "self-improve" else ""),
    })

    # design 스테이지에서만 오는 시나리오(Jira 스토리) 목록 — summarize_context는
    # stage-output 형태(dict.summary / str)만 다루므로 별도로 꺼내서 프롬프트에 붙인다.
    scenarios = context.pop("scenarios", None) if AGENT_NAME == "designer" else None

    # 컨텍스트 요약 (전체 출력 대신 summary만 전달 → 토큰 절약)
    context_str = summarize_context(context)
    if github_repo:
        context_str += f"\n- GitHub Repo: {github_repo}"

    scenario_section = ""
    if scenarios:
        listed = "\n".join(f"- {s.get('key')}: {s.get('title', s.get('key'))}" for s in scenarios)
        scenario_section = f"\n\n## 시나리오 목록 (## SCENARIO:{{key}}에 이 key를 정확히 그대로 사용)\n{listed}"

    # 이 프로젝트에서 내가(이 역할이) 이전에 작성한 산출물이 있으면 이어서/수정해서
    # 작업하도록 포함시킨다 — 사람 팀원이 자기가 전에 쓴 문서를 기억하고 개정하듯,
    # 매번 백지 상태로 재작성하지 않게 하기 위함.
    workspace = f"/workspace/{project_id}"
    prior_output = ""
    prior_path = f"{workspace}/{AGENT_NAME}_output.md"
    if os.path.exists(prior_path):
        try:
            with open(prior_path, errors="replace") as f:
                prior_output = f.read()
        except OSError:
            pass
    prior_section = f"\n\n## 이 프로젝트에서 이전에 당신이 작성한 산출물 (참고해서 이어서/수정하세요)\n{prior_output}" if prior_output else ""

    user_prompt = f"""프로젝트: {project_id} | 스테이지: {stage}
지시사항: {instruction}
{context_str}
{scenario_section}
{prior_section}

{AGENT_ROLE}로서 산출물을 작성해주세요."""

    client = anthropic.AsyncAnthropic(api_key=API_KEY)
    full_response = ""
    conv_messages = [{"role": "user", "content": user_prompt}]
    # max_tokens continuation(재시도)이 여러 번 일어날 수 있어 이번 스테이지 실행
    # 전체의 토큰을 합산한다 — 마지막 시도분만 남기면 실제 쓴 비용보다 적게 보임.
    total_input_tokens = 0
    total_output_tokens = 0

    # MAX_TOKENS를 모델의 절대 상한까지 올려놔도, 이론적으로는 그 상한 자체에
    # 걸려서 잘릴 수 있다 — 그 경우 조용히 잘린 채로 끝내지 않고, 지금까지 쓴
    # 내용을 assistant 턴으로, "이어서 계속 작성해달라"를 새 user 턴으로 넣어
    # 후속 요청을 보낸다(trailing assistant prefill이 아니라 정상적인 멀티턴
    # 대화라 4.6 계열 모델에서도 문제없이 동작). MAX_CONTINUATIONS번까지 반복.
    for attempt in range(MAX_CONTINUATIONS + 1):
        async with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=get_system_prompt(project_id),
            messages=conv_messages,
        ) as stream:
            async for chunk in stream.text_stream:
                full_response += chunk
                if len(full_response) % 400 < 5:
                    await emit(r, {
                        "type": "progress",
                        "project_id": project_id,
                        "agent": AGENT_NAME,
                        "progress": min(90, len(full_response) // 40),
                        "message": full_response[-80:],
                    })
            final_message = await stream.get_final_message()

        total_input_tokens  += final_message.usage.input_tokens
        total_output_tokens += final_message.usage.output_tokens

        if final_message.stop_reason != "max_tokens":
            break
        if attempt == MAX_CONTINUATIONS:
            print(f"[{AGENT_NAME}] max_tokens continuation 한도({MAX_CONTINUATIONS}회) 도달 — 여기서 중단")
            break
        conv_messages = [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": full_response},
            {"role": "user", "content": (
                "방금 응답이 max_tokens 한도에 걸려 중간에 끊겼습니다. "
                "직전에 쓴 내용을 반복하지 말고, 끊긴 지점 바로 다음부터 자연스럽게 이어서 계속 작성해주세요."
            )},
        ]

    await emit(r, {
        "type": "message",
        "project_id": project_id,
        "agent": AGENT_NAME,
        "content": full_response,
    })

    os.makedirs(workspace, exist_ok=True)
    with open(f"{workspace}/{AGENT_NAME}_output.md", "w") as f:
        f.write(f"# {AGENT_ROLE}\nProject: {project_id} | Stage: {stage}\n\n{full_response}")
    # 최신 산출물과 별개로 이력을 계속 쌓아둔다 (사람 팀원의 작업 이력처럼) —
    # 위 파일은 "최신 버전"만 남기고 이건 전체 개정 이력.
    with open(f"{workspace}/{AGENT_NAME}_history.md", "a") as f:
        f.write(f"\n\n---\n## Stage: {stage}\n\n{full_response}")

    # 산출물(문서)을 스스로 커밋+푸시 — implement가 나중에 쓸어담을 때까지 기다리지
    # 않고, 사람 팀원이 자기 문서를 바로바로 올리듯 즉시 반영한다. design/pending 목업
    # 파일은 아직 안 만들었으므로(아래에서 생성) 여기서 add -A해도 목업까지 같이
    # main으로 새지 않는다 — 목업은 별도 브랜치+PR로만 반영돼야 하기 때문.
    git_commit_and_push(workspace, github_repo, f"docs: {AGENT_ROLE} 산출물 ({stage})")

    # Designer는 텍스트/JSON 말고 실제로 브라우저에서 볼 수 있는 HTML 목업도
    # 시나리오(Jira 스토리)별로 만든다. 파일 하나를 계속 덮어쓰던 예전과 달리
    # design/pending/{key}.html에 저장한 뒤 새 브랜치에 커밋해 PR을 올리고,
    # 오케스트레이터가 즉시 머지하게 한다(사람 승인 없이 디자이너가 직접 반영) —
    # 그래야 머지 전(적용 전)/후(적용됨) 버전을 UI에서 분리해서 계속 추적할 수 있다.
    design_preview = False
    if AGENT_NAME == "designer":
        mockups = parse_scenario_mockups(full_response)
        if mockups:
            pending_dir = f"{workspace}/design/pending"
            os.makedirs(pending_dir, exist_ok=True)
            for key, html_content in mockups.items():
                with open(f"{pending_dir}/{key}.html", "w") as f:
                    f.write(html_content)
            design_preview = True

            branch = f"design/{project_id}-{int(time.time())}"
            if git_new_branch_commit_push(workspace, github_repo, branch, f"design: 시나리오별 목업 ({stage})"):
                try:
                    async with httpx.AsyncClient(timeout=20) as client:
                        await client.post(
                            f"{ORCHESTRATOR_URL}/projects/{project_id}/design/publish",
                            json={"github_repo": github_repo, "branch": branch, "scenarios": list(mockups.keys())},
                        )
                except Exception as e:
                    print(f"[{AGENT_NAME}] design publish 호출 실패: {e}")

    summary = build_summary(full_response, AGENT_NAME)
    outputs = {
        "agent":        AGENT_NAME,
        "summary":      summary,
        "self_improve": project_id == "self-improve",
        "input_tokens":  total_input_tokens,
        "output_tokens": total_output_tokens,
    }
    cost = _cost_usd(MODEL, total_input_tokens, total_output_tokens)
    if cost is not None:
        outputs["cost_usd"] = cost
    # design 스테이지는 designer+architect 둘이 같은 stage_completed를 보내고
    # orchestrator가 outputs를 merge한다 — architect도 매번 "design_preview": False를
    # 보내면 나중에 끝나는 쪽이 designer가 만든 True를 덮어써버린다. designer가
    # 아닐 땐 이 키 자체를 안 보내서 merge 시 서로 안 건드리게 한다.
    if AGENT_NAME == "designer":
        outputs["design_preview"] = design_preview

    await emit(r, {
        "type": "stage_completed",
        "project_id": project_id,
        "agent": AGENT_NAME,
        "stage": stage,
        "outputs": outputs,
    })

    print(f"[{AGENT_NAME}/{MODEL}] stage='{stage}' tokens≈{len(full_response)//4}")


GROUP_NAME = "workers"


async def ensure_group(r: aioredis.Redis):
    """컨슈머 그룹 생성 (최초 1회). id='$'로 시작해 그룹 생성 시점 이후의
    메시지만 받는다 — 재시작할 때마다 스트림 전체 이력을 처음부터 재생하던
    버그(같은 implement 작업이 재시작마다 반복 실행돼 GitHub에 중복 PR이
    여러 번 쌓였던 문제)를 막기 위함. 그룹이 이미 있으면 그대로 둔다
    (재시작 시에는 그룹의 마지막 위치부터 이어서 받음 — 크래시로 인해
    ack 안 된 태스크만 재전달됨)."""
    try:
        await r.xgroup_create(name=STREAM_INBOX, groupname=GROUP_NAME, id="$", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            raise


async def main():
    if not API_KEY:
        print(f"[{AGENT_NAME}] ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    print(f"[{AGENT_NAME}] model={MODEL} max_tokens={MAX_TOKENS}")
    r = await aioredis.from_url(REDIS_URL, decode_responses=True)
    await ensure_group(r)

    while True:
        try:
            results = await r.xreadgroup(
                GROUP_NAME, AGENT_NAME, {STREAM_INBOX: ">"}, block=500, count=1
            )
            for _, messages in results:
                for msg_id, fields in messages:
                    await process_task(r, json.loads(fields["payload"]))
                    await r.xack(STREAM_INBOX, GROUP_NAME, msg_id)
        except Exception as e:
            print(f"[{AGENT_NAME}] Error: {e}", file=sys.stderr)
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
