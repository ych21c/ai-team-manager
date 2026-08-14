"""
AI Team Manager — 통합 테스트
API 크레딧 없이 테스트 가능한 모든 연동을 검증합니다.
"""
import asyncio
import os
import base64
import httpx
from dotenv import load_dotenv

load_dotenv()

# ── 설정 ─────────────────────────────────────────────────────────
ATLASSIAN_EMAIL  = os.getenv("ATLASSIAN_EMAIL", "")
ATLASSIAN_TOKEN  = os.getenv("ATLASSIAN_API_TOKEN", "")
ATLASSIAN_DOMAIN = os.getenv("ATLASSIAN_DOMAIN", "")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "ATM")
CONF_SPACE_KEY   = os.getenv("CONFLUENCE_SPACE_KEY", "ATM")
GITHUB_TOKEN     = os.getenv("GITHUB_TOKEN", "")
GITHUB_OWNER     = os.getenv("GITHUB_REPO", "")
ORCHESTRATOR_URL = "http://localhost:8000"

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
NC     = "\033[0m"

def ok(msg):   print(f"  {GREEN}✅ {msg}{NC}")
def fail(msg): print(f"  {RED}❌ {msg}{NC}")
def info(msg): print(f"  {YELLOW}ℹ  {msg}{NC}")

def auth_header():
    creds = base64.b64encode(f"{ATLASSIAN_EMAIL}:{ATLASSIAN_TOKEN}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Content-Type": "application/json", "Accept": "application/json"}


# ── 1. Orchestrator 헬스체크 ──────────────────────────────────────
async def test_orchestrator():
    print(f"\n{CYAN}[1] Orchestrator 상태{NC}")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{ORCHESTRATOR_URL}/health")
            if r.status_code == 200:
                data = r.json()
                ok(f"Orchestrator 실행 중 — 프로젝트 {data.get('projects', 0)}개")
                return True
            fail(f"응답 코드: {r.status_code}")
    except Exception as e:
        fail(f"연결 실패: {e}")
        info("docker compose up 으로 먼저 실행해주세요")
    return False


# ── 2. Jira 연결 테스트 ───────────────────────────────────────────
async def test_jira_connection():
    print(f"\n{CYAN}[2] Jira 연결{NC}")
    if not all([ATLASSIAN_EMAIL, ATLASSIAN_TOKEN, ATLASSIAN_DOMAIN]):
        fail(".env에 ATLASSIAN_* 설정 없음")
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://{ATLASSIAN_DOMAIN}/rest/api/3/myself",
                headers=auth_header()
            )
            if r.status_code == 200:
                user = r.json()
                ok(f"Jira 연결됨 — {user.get('displayName', '')} ({user.get('emailAddress', '')})")
                return True
            fail(f"인증 실패 ({r.status_code}): {r.text[:100]}")
    except Exception as e:
        fail(f"연결 실패: {e}")
    return False


# ── 3. Jira 프로젝트 확인/생성 ────────────────────────────────────
async def test_jira_project():
    print(f"\n{CYAN}[3] Jira 프로젝트 ({JIRA_PROJECT_KEY}){NC}")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://{ATLASSIAN_DOMAIN}/rest/api/3/project/{JIRA_PROJECT_KEY}",
                headers=auth_header()
            )
            if r.status_code == 200:
                proj = r.json()
                ok(f"프로젝트 존재: {proj.get('name')} ({JIRA_PROJECT_KEY})")
                return True
            info(f"프로젝트 없음 — 생성 시도 중...")
            # accountId 조회
            me = await client.get(f"https://{ATLASSIAN_DOMAIN}/rest/api/3/myself", headers=auth_header())
            account_id = me.json().get("accountId", "") if me.status_code == 200 else ""
            r2 = await client.post(
                f"https://{ATLASSIAN_DOMAIN}/rest/api/3/project",
                headers=auth_header(),
                json={
                    "key": JIRA_PROJECT_KEY,
                    "name": "AI Team Manager",
                    "projectTypeKey": "software",
                    "description": "AI Team Manager 프로젝트 관리",
                    "leadAccountId": account_id,
                }
            )
            if r2.status_code in (200, 201):
                ok(f"프로젝트 생성 완료: {JIRA_PROJECT_KEY}")
                return True
            fail(f"프로젝트 생성 실패: {r2.text[:200]}")
    except Exception as e:
        import traceback
        fail(f"오류: {e}")
        traceback.print_exc()
    return False


# ── 4. Jira 이슈 생성 테스트 ─────────────────────────────────────
async def test_jira_create_issue():
    print(f"\n{CYAN}[4] Jira 이슈 생성{NC}")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"https://{ATLASSIAN_DOMAIN}/rest/api/3/issue",
                headers=auth_header(),
                json={
                    "fields": {
                        "project": {"key": JIRA_PROJECT_KEY},
                        "summary": "[테스트] AI Team Manager 연동 확인",
                        "description": {
                            "type": "doc", "version": 1,
                            "content": [{"type": "paragraph", "content": [
                                {"type": "text", "text": "AI Team Manager 자동 생성 테스트 이슈입니다."}
                            ]}]
                        },
                        "issuetype": {"name": "Task"},
                    }
                }
            )
            if r.status_code == 201:
                issue = r.json()
                key = issue.get("key")
                url = f"https://{ATLASSIAN_DOMAIN}/browse/{key}"
                ok(f"이슈 생성: {key}")
                info(f"확인: {url}")
                return key
            fail(f"생성 실패 ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        fail(f"오류: {e}")
    return None


# ── 5. Confluence 연결 테스트 ─────────────────────────────────────
async def test_confluence():
    print(f"\n{CYAN}[5] Confluence 연결{NC}")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # 전체 스페이스 목록으로 확인
            r = await client.get(
                f"https://{ATLASSIAN_DOMAIN}/wiki/rest/api/space?limit=10",
                headers=auth_header()
            )
            if r.status_code == 200:
                spaces = r.json().get("results", [])
                if spaces:
                    names = ", ".join(f"{s['name']}({s['key']})" for s in spaces)
                    ok(f"Confluence 스페이스 {len(spaces)}개: {names}")
                    return True
                info("스페이스 없음 — Confluence에서 스페이스를 먼저 만들어주세요")
            else:
                fail(f"연결 실패 ({r.status_code})")
    except Exception as e:
        fail(f"오류: {e}")
    return False


# ── 6. Confluence 페이지 생성 테스트 ─────────────────────────────
async def test_confluence_page():
    import time
    print(f"\n{CYAN}[6] Confluence 페이지 생성{NC}")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # 사용 가능한 스페이스 조회
            r0 = await client.get(
                f"https://{ATLASSIAN_DOMAIN}/wiki/rest/api/space?limit=1",
                headers=auth_header()
            )
            spaces = r0.json().get("results", []) if r0.status_code == 200 else []
            if not spaces:
                fail("사용 가능한 Confluence 스페이스 없음")
                return False
            space_key = spaces[0]["key"]
            # 제목에 타임스탬프 추가 → 중복 방지
            title = f"[테스트] AI Team Manager PRD {int(time.time())}"
            r = await client.post(
                f"https://{ATLASSIAN_DOMAIN}/wiki/rest/api/content",
                headers=auth_header(),
                json={
                    "type": "page",
                    "title": title,
                    "space": {"key": space_key},
                    "body": {
                        "storage": {
                            "value": "<h1>AI Team Manager 테스트 페이지</h1><p>자동 생성된 테스트 페이지입니다.</p>",
                            "representation": "storage"
                        }
                    }
                }
            )
            if r.status_code == 200:
                page = r.json()
                page_id = page.get("id")
                url = f"https://{ATLASSIAN_DOMAIN}/wiki/spaces/{space_key}/pages/{page_id}"
                ok(f"페이지 생성 완료: {title}")
                info(f"확인: {url}")
                return True
            fail(f"생성 실패 ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        fail(f"오류: {e}")
    return False


# ── 7. GitHub 연결 테스트 ─────────────────────────────────────────
async def test_github():
    print(f"\n{CYAN}[7] GitHub 연결{NC}")
    if not GITHUB_TOKEN:
        fail(".env에 GITHUB_TOKEN 없음")
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
            )
            if r.status_code == 200:
                user = r.json()
                ok(f"GitHub 연결됨 — {user.get('login')} ({user.get('name', '')})")
                return True
            fail(f"인증 실패 ({r.status_code})")
    except Exception as e:
        fail(f"오류: {e}")
    return False


# ── 8. GitHub 레포 생성 테스트 ────────────────────────────────────
async def test_github_create_repo():
    print(f"\n{CYAN}[8] GitHub 레포 생성{NC}")
    repo_name = "atm-test-repo"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.github.com/user/repos",
                headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
                json={
                    "name": repo_name,
                    "description": "AI Team Manager 연동 테스트",
                    "private": False,
                    "auto_init": True,
                }
            )
            if r.status_code == 201:
                repo = r.json()
                ok(f"레포 생성: {repo.get('full_name')}")
                info(f"확인: {repo.get('html_url')}")
                return True
            elif r.status_code == 422:
                ok(f"레포 이미 존재: {GITHUB_OWNER}/{repo_name}")
                return True
            fail(f"생성 실패 ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        fail(f"오류: {e}")
    return False


# ── 9. Orchestrator 프로젝트 생성 테스트 ─────────────────────────
async def test_create_project():
    print(f"\n{CYAN}[9] 프로젝트 생성 API{NC}")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{ORCHESTRATOR_URL}/projects",
                json={"name": "테스트 프로젝트", "instruction": "테스트용 프로젝트입니다."}
            )
            if r.status_code == 200:
                data = r.json()
                ok(f"프로젝트 생성: {data.get('name')} (ID: {data.get('project_id')})")
                if data.get("github_url"):
                    ok(f"GitHub 레포: {data.get('github_url')}")
                return True
            fail(f"실패 ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        fail(f"오류: {e}")
    return False


# ── 메인 ─────────────────────────────────────────────────────────
async def main():
    print(f"\n{'='*50}")
    print(f"  AI Team Manager — 통합 테스트")
    print(f"{'='*50}")

    results = {}
    results["orchestrator"]      = await test_orchestrator()
    results["jira_connection"]   = await test_jira_connection()
    results["jira_project"]      = await test_jira_project() if results["jira_connection"] else False
    results["jira_issue"]        = await test_jira_create_issue() if results["jira_project"] else False
    results["confluence"]        = await test_confluence()
    results["confluence_page"]   = await test_confluence_page()
    results["github"]            = await test_github()
    results["github_repo"]       = await test_github_create_repo() if results["github"] else False
    results["project_api"]       = await test_create_project() if results["orchestrator"] else False

    # 결과 요약
    print(f"\n{'='*50}")
    print(f"  테스트 결과 요약")
    print(f"{'='*50}")
    passed = sum(1 for v in results.values() if v)
    total  = len(results)
    for name, result in results.items():
        status = f"{GREEN}✅{NC}" if result else f"{RED}❌{NC}"
        print(f"  {status} {name}")
    print(f"\n  {passed}/{total} 통과")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    asyncio.run(main())
