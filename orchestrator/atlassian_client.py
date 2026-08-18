"""
Atlassian 연동 — Jira 이슈 생성 + Confluence 페이지 작성
PM Agent 산출물을 자동으로 Jira/Confluence에 등록
"""
import os
import json
import base64
import httpx

EMAIL   = os.getenv("ATLASSIAN_EMAIL", "")
TOKEN   = os.getenv("ATLASSIAN_API_TOKEN", "")
DOMAIN  = os.getenv("ATLASSIAN_DOMAIN", "")
JIRA_PK = os.getenv("JIRA_PROJECT_KEY", "ATM")
CONF_SK = os.getenv("CONFLUENCE_SPACE_KEY", "ATM")

def _auth_header() -> dict:
    creds = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    return {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


_issue_types_cache: tuple[str, str, str] | None = None


def _pick_issue_types(types: list[dict]) -> tuple[str, str, str]:
    """createmeta가 돌려준 이슈 타입 목록에서 (에픽용, 스토리용, 서브태스크용)
    이름을 골라낸다. hierarchyLevel(1=에픽, 0=스토리)과 subtask 플래그로
    구분한다 — 이름을 하드코딩하면 팀 관리형 프로젝트나 로케일이 다른
    인스턴스에서 계속 400으로 실패한다. 에픽 레벨(1) 타입이 아직 없는
    프로젝트(과거 "작업"만 있던 2단 구성)에서는 스토리용 타입을 에픽 자리에도
    그대로 써서 예전 2단 동작으로 자연히 폴백한다. HTTP 호출과 분리해서 순수
    함수로 둔 이유는 이 선택 로직만 목킹 없이 단위 테스트하기 위함."""
    story = next((t["name"] for t in types if not t.get("subtask") and t.get("hierarchyLevel", 0) <= 0), "Task")
    epic  = next((t["name"] for t in types if not t.get("subtask") and t.get("hierarchyLevel", 0) > 0), story)
    child = next((t["name"] for t in types if t.get("subtask")), "Subtask")
    return epic, story, child


async def _get_issue_types() -> tuple[str, str, str]:
    """이 Jira 프로젝트에서 실제로 쓸 수 있는 이슈 타입을 조회해서
    (에픽용, 스토리용, 서브태스크용) 3단으로 반환한다. 이름을 하드코딩하면
    팀 관리형 프로젝트나 로케일이 다른 인스턴스에서 이슈 타입이 없어 400으로
    계속 실패하므로, hierarchyLevel(1=에픽, 0=스토리)과 subtask 플래그로
    동적으로 골라 쓴다. 에픽 레벨(1) 타입이 이 프로젝트에 아직 없으면(과거
    "작업"만 있던 2단 구성) 스토리용 타입을 에픽 자리에도 그대로 써서 예전
    2단 동작으로 자연히 폴백한다."""
    global _issue_types_cache
    if _issue_types_cache is not None:
        return _issue_types_cache
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"https://{DOMAIN}/rest/api/3/issue/createmeta",
            headers=_auth_header(),
            params={"projectKeys": JIRA_PK, "expand": "projects.issuetypes"},
        )
        if r.status_code != 200:
            print(f"[jira] 이슈 타입 조회 실패 ({r.status_code}): {r.text[:200]}")
            _issue_types_cache = ("Task", "Task", "Subtask")
            return _issue_types_cache
        projects = r.json().get("projects", [])
        types = projects[0].get("issuetypes", []) if projects else []
        epic, story, child = _pick_issue_types(types)
        _issue_types_cache = (epic, story, child)
        print(f"[jira] 이슈 타입 확인됨: 에픽='{epic}', 스토리='{story}', 서브태스크='{child}'")
        return _issue_types_cache


async def ensure_jira_project() -> bool:
    """Jira 프로젝트가 없으면 생성."""
    if not all([EMAIL, TOKEN, DOMAIN]):
        return False
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"https://{DOMAIN}/rest/api/3/project/{JIRA_PK}",
            headers=_auth_header(),
        )
        if r.status_code == 200:
            return True
        # accountId 조회 (프로젝트 리더 필수)
        me = await client.get(f"https://{DOMAIN}/rest/api/3/myself", headers=_auth_header())
        account_id = me.json().get("accountId", "") if me.status_code == 200 else ""
        # 없으면 생성
        r2 = await client.post(
            f"https://{DOMAIN}/rest/api/3/project",
            headers=_auth_header(),
            json={
                "key": JIRA_PK,
                "name": "AI Team Manager",
                "projectTypeKey": "software",
                "projectTemplateKey": "com.pyxis.greenhopper.jira:gh-scrum-template",
                "description": "AI Team Manager 프로젝트 관리",
                "leadAccountId": account_id,
            },
        )
        return r2.status_code in (200, 201)


async def create_jira_epic(project_name: str, summary: str, description: str) -> str | None:
    """Epic(대용 상위 이슈) 생성 후 key 반환 (예: ATM-1)."""
    if not all([EMAIL, TOKEN, DOMAIN]):
        return None
    epic_type, _, _ = await _get_issue_types()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://{DOMAIN}/rest/api/3/issue",
            headers=_auth_header(),
            json={
                "fields": {
                    "project": {"key": JIRA_PK},
                    "summary": f"[{project_name}] {summary}",
                    "description": {
                        "type": "doc", "version": 1,
                        "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}]
                    },
                    "issuetype": {"name": epic_type},
                }
            },
        )
        if r.status_code == 201:
            return r.json().get("key")
        print(f"[jira] Epic 생성 실패 ({r.status_code}): {r.text[:300]}")
    return None


STAGE_SUBTASK_LABELS = {"design": "디자인", "implement": "구현", "qa": "QA"}


async def create_stage_subtasks(story_key: str, story_title: str) -> dict[str, str]:
    """Story(요구사항) 밑에 design/implement/qa 하위 작업(Subtask) 3개를 만든다.
    각 단계가 자기 하위 작업만 업데이트하게 해서, 코멘트만 계속 쌓이던 스토리
    하나에 세 단계 상태가 뒤섞여 Jira만 보고는 "디자인은 끝났는데 구현은
    아직인지" 구분이 안 되던 문제를 해결한다.
    반환: {"design": "ATM-3", "implement": "ATM-4", "qa": "ATM-5"} — 생성 실패한
    단계는 키가 아예 빠진다(호출부가 .get(stage, story_key)로 story 자체에
    폴백하도록)."""
    if not all([EMAIL, TOKEN, DOMAIN]):
        return {}
    _, _, subtask_type = await _get_issue_types()
    result: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=15) as client:
        for stage, label in STAGE_SUBTASK_LABELS.items():
            r = await client.post(
                f"https://{DOMAIN}/rest/api/3/issue",
                headers=_auth_header(),
                json={
                    "fields": {
                        "project": {"key": JIRA_PK},
                        "summary": f"[{label}] {story_title}"[:200],
                        "issuetype": {"name": subtask_type},
                        "parent": {"key": story_key},
                    }
                },
            )
            if r.status_code == 201:
                result[stage] = r.json().get("key")
            else:
                print(f"[jira] {label} 하위 작업 생성 실패 ({r.status_code}): {r.text[:300]}")
    return result


async def create_jira_stories(epic_key: str, project_name: str, requirements: list) -> list[dict]:
    """요구사항 목록 → Jira Story 생성 + 그 밑에 design/implement/qa 하위 작업
    3개씩 생성.
    requirements 항목은 문자열(초기 기획의 불릿 목록)이거나 {"id", "title", ...} 형태의
    dict(재작업 시 PM이 REQ-00-A처럼 구조화해 내놓는 개별 요구사항)일 수 있다 —
    dict면 title(없으면 id)을 스토리 제목으로 쓴다. 안 그러면 dict 전체가 str()로
    찍혀 스토리 제목이 알아볼 수 없게 된다.
    반환: [{"key": "ATM-2", "title": "...", "subtasks": {"design": "ATM-3", ...}}, ...]
    — title은 design 스테이지가 시나리오별 목업 파일을 만들 때 Jira 스토리 제목으로
    라벨을 붙이는 데 쓴다."""
    if not all([EMAIL, TOKEN, DOMAIN]):
        return []
    _, story_type, _ = await _get_issue_types()
    stories = []
    async with httpx.AsyncClient(timeout=15) as client:
        for req in requirements[:10]:  # 최대 10개
            if isinstance(req, dict):
                title = str(req.get("title") or req.get("id") or req)[:200]
            else:
                title = str(req)[:200]
            r = await client.post(
                f"https://{DOMAIN}/rest/api/3/issue",
                headers=_auth_header(),
                json={
                    "fields": {
                        "project": {"key": JIRA_PK},
                        "summary": title,
                        "issuetype": {"name": story_type},
                        "parent": {"key": epic_key},
                    }
                },
            )
            if r.status_code == 201:
                key = r.json().get("key")
                subtasks = await create_stage_subtasks(key, title)
                stories.append({"key": key, "title": title, "subtasks": subtasks})
            else:
                print(f"[jira] Story 생성 실패 ({r.status_code}): {r.text[:300]}")
    return stories


async def create_confluence_page(project_name: str, body_html: str) -> dict | None:
    """프로젝트 전용 Confluence 페이지를 새로 만든다 (프로젝트당 1개).
    반환: {"id": "...", "url": "..."} — id는 이후 update_confluence_page에 필요."""
    if not all([EMAIL, TOKEN, DOMAIN]):
        return None
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://{DOMAIN}/wiki/rest/api/content",
            headers=_auth_header(),
            json={
                "type": "page",
                "title": f"{project_name} — 프로젝트 문서",
                "space": {"key": CONF_SK},
                "body": {"storage": {"value": body_html, "representation": "storage"}},
            },
        )
        if r.status_code == 200:
            data = r.json()
            page_id = data.get("id")
            return {"id": page_id, "url": f"https://{DOMAIN}/wiki/spaces/{CONF_SK}/pages/{page_id}"}
        print(f"[jira] Confluence 페이지 생성 실패 ({r.status_code}): {r.text[:300]}")
    return None


async def update_confluence_page(page_id: str, project_name: str, body_html: str) -> bool:
    """이미 만들어둔 프로젝트 페이지를 최신 내용으로 갱신한다 (사람 팀원이 위키를
    계속 고쳐쓰듯 — 개요/아키텍처/리소스/히스토리를 매 마일스톤마다 다시 씀).
    Confluence는 낙관적 동시성 제어라 현재 버전 번호를 먼저 조회해야 한다."""
    if not all([EMAIL, TOKEN, DOMAIN]):
        return False
    async with httpx.AsyncClient(timeout=15) as client:
        cur = await client.get(f"https://{DOMAIN}/wiki/rest/api/content/{page_id}", headers=_auth_header())
        if cur.status_code != 200:
            print(f"[jira] Confluence 페이지 조회 실패 ({cur.status_code}): {cur.text[:300]}")
            return False
        version = cur.json().get("version", {}).get("number", 1)

        r = await client.put(
            f"https://{DOMAIN}/wiki/rest/api/content/{page_id}",
            headers=_auth_header(),
            json={
                "type": "page",
                "title": f"{project_name} — 프로젝트 문서",
                "version": {"number": version + 1},
                "body": {"storage": {"value": body_html, "representation": "storage"}},
            },
        )
        if r.status_code == 200:
            return True
        print(f"[jira] Confluence 페이지 갱신 실패 ({r.status_code}): {r.text[:300]}")
    return False


def parse_pm_requirements(pm_output: str, project_name: str) -> tuple[str, list]:
    """PM 산출물 텍스트에서 (summary, requirements)를 뽑아낸다. JSON 파싱을
    시도하고(requirements 항목은 문자열 또는 {"id","title",...} dict), 실패하면
    텍스트 줄 단위 불릿으로 폴백한다. sync_pm_output(최초 기획)과
    _sync_new_requirements_to_epic(main.py, 재작업 시 기존 Epic 아래 신규 이슈
    추가)이 같은 파싱 로직을 공유하기 위해 분리했다."""
    try:
        data = json.loads(pm_output[pm_output.find("{"):pm_output.rfind("}")+1])
        requirements = data.get("requirements", [])
        summary = data.get("summary", project_name)
    except Exception:
        summary = project_name
        requirements = [line.strip("- •") for line in pm_output.split("\n") if line.strip().startswith(("-", "•", "*"))][:10]
    return summary, requirements


async def sync_pm_output(project_name: str, pm_output: str) -> dict:
    """
    PM 산출물을 파싱해서 Jira Epic/Story를 등록한다 (Confluence는 여기서 안 만든다 —
    프로젝트 전체 문서는 main.py가 여러 스테이지의 산출물을 모아 하나의 살아있는
    페이지로 별도 관리한다).
    반환: { "epic": "ATM-1", "stories": ["ATM-2", ...],
            "story_subtasks": {"ATM-2": {"design": "ATM-3", ...}}, "jira_url": "...", "summary": "..." }
    """
    if not all([EMAIL, TOKEN, DOMAIN]):
        return {}

    await ensure_jira_project()

    summary, requirements = parse_pm_requirements(pm_output, project_name)

    # Jira Epic 생성
    epic_key = await create_jira_epic(project_name, summary, pm_output[:500])

    # Story 생성 (+ design/implement/qa 하위 작업)
    story_records = []
    if epic_key and requirements:
        story_records = await create_jira_stories(epic_key, project_name, requirements)
    stories = [s["key"] for s in story_records]
    story_titles = {s["key"]: s["title"] for s in story_records}
    story_subtasks = {s["key"]: s["subtasks"] for s in story_records if s.get("subtasks")}

    return {
        "epic":           epic_key,
        "stories":        stories,
        "story_titles":   story_titles,
        "story_subtasks": story_subtasks,
        "jira_url":       f"https://{DOMAIN}/browse/{epic_key}" if epic_key else None,
        "summary":        summary,
    }


async def get_transition_id(issue_key: str, target_status: str) -> str | None:
    """이슈의 전환 가능한 transition ID 조회."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"https://{DOMAIN}/rest/api/3/issue/{issue_key}/transitions",
            headers=_auth_header(),
        )
        if r.status_code == 200:
            for t in r.json().get("transitions", []):
                if target_status.lower() in t["to"]["name"].lower():
                    return t["id"]
    return None


async def update_jira_status(issue_key: str, status: str) -> bool:
    """Jira 이슈 상태 변경. status: 'In Progress' | 'Done' | 'To Do'"""
    if not all([EMAIL, TOKEN, DOMAIN]):
        return False
    tid = await get_transition_id(issue_key, status)
    if not tid:
        return False
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"https://{DOMAIN}/rest/api/3/issue/{issue_key}/transitions",
            headers=_auth_header(),
            json={"transition": {"id": tid}},
        )
        return r.status_code == 204


async def add_jira_comment(issue_key: str, comment: str) -> bool:
    """Jira 이슈에 코멘트 추가."""
    if not all([EMAIL, TOKEN, DOMAIN]):
        return False
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"https://{DOMAIN}/rest/api/3/issue/{issue_key}/comment",
            headers=_auth_header(),
            json={
                "body": {
                    "type": "doc", "version": 1,
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": comment}]}]
                }
            },
        )
        return r.status_code == 201


async def link_pr_to_jira(issue_key: str, pr_url: str, repo: str) -> bool:
    """PR URL을 Jira 이슈에 원격 링크로 등록."""
    if not all([EMAIL, TOKEN, DOMAIN]):
        return False
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"https://{DOMAIN}/rest/api/3/issue/{issue_key}/remotelink",
            headers=_auth_header(),
            json={
                "object": {
                    "url":   pr_url,
                    "title": f"PR: {repo}",
                    "icon":  {"url16x16": "https://github.com/favicon.ico", "title": "GitHub"},
                }
            },
        )
        return r.status_code == 201


async def add_jira_attachment(issue_key: str, file_path: str, filename: str | None = None) -> bool:
    """Jira 이슈에 파일을 첨부한다. 링크와 달리, 나중에 로컬 파일이 다음 라운드
    산출물로 덮어써지거나 지워져도(QA 녹화 영상 qa_recording.mp4처럼 매 라운드
    같은 경로에 덮어쓰는 산출물이 실제로 있음) Jira 쪽엔 첨부 시점 버전이 그대로
    영구 보존된다 — "이전 산출물" 이력을 지키는 유일한 방법이라 링크 대신 이걸
    쓴다. 첨부 엔드포인트는 XSRF 우회용 X-Atlassian-Token 헤더가 필요하고
    Content-Type을 절대 수동 지정하면 안 된다(httpx가 멀티파트 경계를 직접
    잡아야 함) — 그래서 _auth_header()를 그대로 재사용하지 않고 필요한 헤더만
    새로 구성한다."""
    if not all([EMAIL, TOKEN, DOMAIN]) or not os.path.exists(file_path):
        return False
    creds = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    headers = {
        "Authorization": f"Basic {creds}",
        "X-Atlassian-Token": "no-check",
        "Accept": "application/json",
    }
    name = filename or os.path.basename(file_path)
    async with httpx.AsyncClient(timeout=60) as client:
        with open(file_path, "rb") as f:
            r = await client.post(
                f"https://{DOMAIN}/rest/api/3/issue/{issue_key}/attachments",
                headers=headers,
                files={"file": (name, f)},
            )
        return r.status_code == 200
