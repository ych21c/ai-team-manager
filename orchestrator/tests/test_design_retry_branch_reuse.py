"""
회귀 테스트 — "디자인 다시 해줘"(내용 변경 없이 재실행 포함) 요청을 여러 번
보내면 매번 implement가 main에서 새 브랜치를 파서 새 PR을 열기만 하고, 아무
PR도 머지되지 않은 채 계속 쌓이던 사고.

실제로 있었던 사고: recoveryfit에서 "시작 페이지만 디자인 추가하라고" 요청을
같은 내용으로 반복 재실행했더니 PR #10 → #12 → #14가 전부 동일 내용으로
생성됐고 하나도 머지되지 않았다. 원인은 _retry_design_with_feedback이
implement/qa/autotest 스테이지를 리셋할 때 implement.outputs(branch/
pr_number)까지 무조건 비워버려서, QA 실패 재시도 경로
(_retry_implement_with_feedback)가 이미 갖고 있던 "실패한 브랜치를 이어서
고친다" 재사용 로직이 무력화됐기 때문이다(QA 재시도 메시지에 "이전 시도(브랜치:
알 수 없음)"라고 찍힐 정도로 branch 정보 자체가 날아갔었다).

수정: 이전 라운드가 아직 머지 전(autotest가 COMPLETED까지 못 감)이라면
implement.outputs를 보존하고, advance_pipeline이 implement를 다시 돌릴 때 그
branch를 retry_branch로 넘겨서 기존 PR을 이어서 고치게 한다. 이전 라운드가
이미 머지된 뒤(autotest COMPLETED)라면 그 브랜치는 죽은 브랜치이므로 여전히
새로 만든다(안전 유지).

실행: cd orchestrator && pytest tests/test_design_retry_branch_reuse.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from workflows.pipeline import Pipeline, StageStatus

pytestmark = pytest.mark.asyncio


async def _noop(*args, **kwargs):
    pass


def _pipeline_stuck_before_merge(pid: str, instruction: str) -> Pipeline:
    """QA가 계속 실패해서 autotest까지 못 간(=머지 전) 상태 — 이전 implement가
    이미 브랜치/PR을 만들어둔 채로 멈춰있다."""
    p = Pipeline(pid, instruction)
    p.mark_completed("planning", {})
    p.mark_completed("design", {"summary": "디자인 완료"})
    p.stages["design"].approved = True
    p.stages["implement"].approved = True
    p.mark_completed("implement", {"branch": "ai-implement/p1-aaaaaa", "pr_number": 10})
    p.stages["qa"].status = StageStatus.FAILED
    return p


def _pipeline_already_merged(pid: str, instruction: str) -> Pipeline:
    """이전 라운드가 autotest까지 끝나 이미 머지된 상태."""
    p = Pipeline(pid, instruction)
    p.mark_completed("planning", {})
    p.mark_completed("design", {"summary": "디자인 완료"})
    p.stages["design"].approved = True
    p.stages["implement"].approved = True
    p.mark_completed("implement", {"branch": "ai-implement/p1-aaaaaa", "pr_number": 10})
    p.mark_completed("qa", {"passed": True})
    p.mark_completed("autotest", {"passed": True})
    return p


@pytest.fixture(autouse=True)
def _stub_side_effects(monkeypatch):
    monkeypatch.setattr(main, "project_repos", {"p1": "org/repo"})
    monkeypatch.setattr(main, "add_jira_comment", _noop)
    monkeypatch.setattr(main, "broadcast", _noop)
    monkeypatch.setattr(main, "project_jira", {"p1": {"stories": [], "story_titles": {}}})
    monkeypatch.setattr(main.spawner, "spawn_team", lambda *a, **k: None)


async def test_retry_design_preserves_branch_when_prior_round_not_merged(monkeypatch):
    p = _pipeline_stuck_before_merge("p1", "시작 페이지만 디자인 추가하라고")
    monkeypatch.setattr(main.redis, "send_task", _noop)

    await main._retry_design_with_feedback(p, "(변경 없음 — 같은 내용으로 재실행)")

    assert p.stages["implement"].outputs.get("branch") == "ai-implement/p1-aaaaaa"
    assert p.stages["implement"].outputs.get("pr_number") == 10
    assert p.stages["implement"].approved is False


async def test_retry_design_clears_branch_when_prior_round_already_merged(monkeypatch):
    p = _pipeline_already_merged("p1", "시작 페이지만 디자인 추가하라고")
    monkeypatch.setattr(main.redis, "send_task", _noop)

    await main._retry_design_with_feedback(p, "새 요청")

    assert p.stages["implement"].outputs == {}


async def test_advance_pipeline_passes_retry_branch_to_implement_when_outputs_preserved(monkeypatch):
    """_retry_design_with_feedback가 보존해둔 branch를, advance_pipeline이
    design 완료 후 implement를 다시 돌릴 때 실제로 context["retry_branch"]로
    넘기는지 확인 — 이게 있어야 implement_openhands/run.py가 새 브랜치 대신
    기존 브랜치를 체크아웃한다."""
    p = _pipeline_stuck_before_merge("p1", "시작 페이지만 디자인 추가하라고")
    # design은 이미 COMPLETED, implement는 PENDING으로 되돌리되 outputs는
    # 보존된 상태를 직접 구성(=_retry_design_with_feedback 실행 직후 상태 재현).
    p.stages["implement"].status = StageStatus.PENDING

    sent_tasks = []

    async def _fake_send_task(agent_name, pid, task):
        sent_tasks.append((agent_name, task))

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    await main.advance_pipeline(p)

    implement_tasks = [t for name, t in sent_tasks if name == "implement"]
    assert len(implement_tasks) == 1
    assert implement_tasks[0]["context"]["retry_branch"] == "ai-implement/p1-aaaaaa"


async def test_advance_pipeline_does_not_set_retry_branch_for_fresh_implement(monkeypatch):
    """첫 구현(implement.outputs가 애초에 비어있는 정상 케이스)에는 retry_branch가
    안 붙어야 한다 — 항상 새 브랜치를 파는 기존 동작 유지."""
    p = Pipeline("p1", "PRD 원본")
    p.mark_completed("planning", {})
    p.mark_completed("design", {"summary": "디자인 완료"})
    p.stages["design"].approved = True
    p.stages["implement"].approved = True

    sent_tasks = []

    async def _fake_send_task(agent_name, pid, task):
        sent_tasks.append((agent_name, task))

    monkeypatch.setattr(main.redis, "send_task", _fake_send_task)

    await main.advance_pipeline(p)

    implement_tasks = [t for name, t in sent_tasks if name == "implement"]
    assert len(implement_tasks) == 1
    assert "retry_branch" not in implement_tasks[0]["context"]
