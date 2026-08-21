"""
Pipeline/Stage 상태 머신 전체 시나리오 테스트.

오늘 실제로 발생했던 두 개의 회귀 버그를 포함해서, 파이프라인이 가질 수 있는
모든 전이 경로를 커버한다:
  1. 정상 진행 경로 (planning → design → implement → qa → autotest → release)
  2. 승인 게이트 무한 루프 버그 (approve()해도 계속 승인 요구하던 것)
  3. 병렬 스테이지(design: designer+architect) outputs 덮어쓰기 버그
  4. 실패 시 이후 스테이지가 절대 진행되지 않는지
  5. 의존성 미충족 시 준비 안 됨
  6. is_done() 최종 상태

실행: cd orchestrator && pytest tests/ -v
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workflows.pipeline import Pipeline, StageStatus


def new_pipeline(pid="test-project") -> Pipeline:
    return Pipeline(pid, instruction="테스트 지시사항")


# ── 1. 초기 상태 ─────────────────────────────────────────────────────

def test_fresh_pipeline_all_stages_pending():
    p = new_pipeline()
    assert all(s.status == StageStatus.PENDING for s in p.stages.values())
    assert not p.is_done()


def test_fresh_pipeline_only_planning_ready():
    """의존성이 없는 planning만 처음부터 준비돼야 하고, 나머지는 아니어야 한다."""
    p = new_pipeline()
    ready = {s.name for s in p.get_ready_stages()}
    assert ready == {"planning"}


def test_stage_not_ready_until_dependency_completed():
    p = new_pipeline()
    # design은 planning 의존 — planning이 running이든 뭐든 COMPLETED가 아니면 준비 안 됨
    p.mark_running("planning")
    assert "design" not in {s.name for s in p.get_ready_stages()}


# ── 2. 정상 진행 경로 (전체 시나리오) ────────────────────────────────

def test_full_happy_path_progresses_through_all_stages():
    p = new_pipeline()

    p.mark_running("planning")
    assert p.stages["planning"].status == StageStatus.RUNNING
    p.mark_completed("planning", {"summary": "PRD 완료"})
    assert {s.name for s in p.get_ready_stages()} == {"design"}

    # design은 designer+architect 병렬 — 이 시점에 스테이지 자체는 하나
    p.mark_running("design")
    p.mark_completed("design", {"agent": "designer", "summary": "화면 스펙"})
    assert p.stages["design"].status == StageStatus.COMPLETED
    # design 완료 후에는 implement가 dependency 조건은 만족 (승인 게이트는 별개 관심사)
    assert {s.name for s in p.get_ready_stages()} == {"implement"}

    p.mark_running("implement")
    p.mark_completed("implement", {"pr_url": "https://github.com/x/y/pull/1", "branch": "ai-implement/x"})
    assert {s.name for s in p.get_ready_stages()} == {"qa"}

    p.mark_running("qa")
    p.mark_completed("qa", {"passed": True})
    assert {s.name for s in p.get_ready_stages()} == {"autotest"}

    p.mark_running("autotest")
    p.mark_completed("autotest", {"passed": True})
    assert {s.name for s in p.get_ready_stages()} == {"release"}

    p.mark_running("release")
    p.mark_completed("release", {"summary": "배포 완료"})

    assert p.is_done()


# ── 3. 승인 게이트 — 오늘 발견된 무한 루프 버그의 회귀 테스트 ────────
#
# 버그였던 동작: requires_approval은 Stage에 영구히 붙는 정적 필드라서,
# approve()로 WAITING→PENDING 전환해도 "이 스테이지는 승인이 필요한가?"를
# 다시 체크하면 여전히 True가 나와 무한히 승인을 요구했다. 실제 main.py의
# advance_pipeline은 `stage.requires_approval and not stage.approved`로
# 체크하므로, 여기서도 동일한 조건식으로 검증한다.

def needs_approval(stage) -> bool:
    """main.py의 advance_pipeline이 쓰는 것과 동일한 조건 — 여기서 갈라지면
    실제 승인 루프 버그가 재발한다는 뜻이다."""
    return stage.requires_approval and not stage.approved


def test_approval_required_stage_starts_unapproved():
    p = new_pipeline()
    stage = p.stages["implement"]
    assert stage.requires_approval is True
    assert stage.approved is False
    assert needs_approval(stage) is True


def test_approve_noop_if_not_waiting():
    """대기 중이 아닌 스테이지에 approve()를 불러도 아무 일도 안 일어나야 한다
    (예: 아직 도달하지도 않은 스테이지를 잘못된 순서로 승인 누르는 경우)."""
    p = new_pipeline()
    p.approve("implement")  # 아직 PENDING이지 WAITING이 아님
    assert p.stages["implement"].status == StageStatus.PENDING
    assert p.stages["implement"].approved is False


def test_approve_flips_waiting_to_pending_and_marks_approved():
    p = new_pipeline()
    p.mark_waiting_approval("implement")
    assert p.stages["implement"].status == StageStatus.WAITING

    p.approve("implement")
    assert p.stages["implement"].status == StageStatus.PENDING
    assert p.stages["implement"].approved is True


def test_regression_approved_stage_never_asks_again():
    """핵심 회귀 테스트: 한 번 승인된 스테이지는, 몇 번을 다시 '준비됐는지'
    체크해도 다시는 승인을 요구하면 안 된다. 이게 실제로 깨졌던 버그다."""
    p = new_pipeline()
    p.mark_completed("planning", {})
    p.mark_completed("design", {})

    stage = p.stages["implement"]
    assert needs_approval(stage) is True  # 최초 진입 시엔 승인 필요

    p.mark_waiting_approval("implement")
    p.approve("implement")

    # advance_pipeline이 몇 번을 다시 호출되더라도(재연결, 재시도 등) 이제는
    # 절대 다시 막히면 안 된다.
    for _ in range(5):
        assert needs_approval(p.stages["implement"]) is False


def test_release_stage_also_has_independent_approval_state():
    """release도 requires_approval이지만, implement 승인 여부와는 독립적이어야
    한다 (플래그가 스테이지별로 따로 관리되는지 확인)."""
    p = new_pipeline()
    p.mark_waiting_approval("implement")
    p.approve("implement")

    assert p.stages["implement"].approved is True
    assert p.stages["release"].approved is False
    assert needs_approval(p.stages["release"]) is True


def test_qa_retry_bypasses_approval_gate_by_design():
    """QA 재작업 재시도(_retry_implement_with_feedback)는 advance_pipeline의
    get_ready_stages 루프를 안 거치고 mark_running을 직접 불러서 재시도한다
    — 즉 이미 승인된 적 없어도(approved=False) 자동 재시도 자체는 막히지 않아야
    한다. 이 테스트는 그 전제가 되는 mark_running이 승인 상태와 무관하게
    항상 동작함을 보증한다."""
    p = new_pipeline()
    stage = p.stages["implement"]
    assert stage.approved is False
    p.mark_running("implement")  # _retry_implement_with_feedback와 동일한 호출
    assert stage.status == StageStatus.RUNNING


# ── 4. 병렬 스테이지 outputs 병합 — 오늘 발견된 덮어쓰기 버그 ────────
#
# 버그였던 동작: design은 designer+architect 둘이 각자 stage_completed를
# 보내는데, mark_completed가 outputs를 통째로 교체해서 나중에 끝난 쪽이
# 먼저 끝난 쪽 결과(예: design_preview 플래그)를 지워버렸다.

def test_mark_completed_merges_outputs_from_parallel_agents():
    p = new_pipeline()
    p.mark_completed("design", {"agent": "designer", "design_preview": True, "summary": "designer summary"})
    p.mark_completed("design", {"agent": "architect", "summary": "architect summary"})

    outputs = p.stages["design"].outputs
    # architect가 나중에 끝났어도 자신이 안 건드린 키(design_preview)는 살아있어야 함
    assert outputs["design_preview"] is True
    # 공통 키(agent/summary)는 마지막에 쓴 쪽 값으로 남는 것이 현재 의도된 동작
    assert outputs["agent"] == "architect"
    assert outputs["summary"] == "architect summary"


def test_mark_completed_first_call_alone_sets_all_its_keys():
    p = new_pipeline()
    p.mark_completed("planning", {"summary": "PRD", "requirements": ["a", "b"]})
    assert p.stages["planning"].outputs == {"summary": "PRD", "requirements": ["a", "b"]}


# ── 4b. agent_name과 함께 호출된 mark_completed — 전원 완료를 기다림 ──
#
# 오늘 발견된 레이스: design은 designer+architect 둘이 각자 stage_completed를
# 보내는데, 먼저 끝난 쪽만으로 스테이지를 COMPLETED로 바꿔버려서 다음 게이트
# (구현 시작 승인)가 나머지 에이전트를 기다리지 않고 열릴 수 있었다.
# agent_name을 주면(main.py handle_agent_event의 실제 경로) stage.agents
# 전원이 보고해야만 COMPLETED로 전이해야 한다.

def test_partial_completion_with_agent_name_stays_running():
    p = new_pipeline()
    p.mark_running("design")
    p.mark_completed("design", {"agent": "architect"}, agent_name="architect")
    assert p.stages["design"].status == StageStatus.RUNNING
    assert p.stages["design"].agents_done == ["architect"]


def test_full_completion_with_agent_name_from_both_agents():
    p = new_pipeline()
    p.mark_running("design")
    p.mark_completed("design", {"agent": "architect"}, agent_name="architect")
    p.mark_completed("design", {"agent": "designer"}, agent_name="designer")
    assert p.stages["design"].status == StageStatus.COMPLETED
    assert set(p.stages["design"].agents_done) == {"architect", "designer"}


def test_same_agent_reporting_twice_does_not_fake_full_completion():
    """같은 에이전트가 실수로 두 번 보고해도(재전달 등) 나머지 에이전트가
    아직이면 여전히 COMPLETED가 되면 안 된다."""
    p = new_pipeline()
    p.mark_running("design")
    p.mark_completed("design", {}, agent_name="architect")
    p.mark_completed("design", {}, agent_name="architect")
    assert p.stages["design"].status == StageStatus.RUNNING
    assert p.stages["design"].agents_done == ["architect"]


def test_single_agent_stage_completes_immediately_even_with_agent_name():
    """agents가 1개뿐인 스테이지(pm/implement/qa/...)는 agent_name을 줘도
    그 하나로 바로 전원 완료 조건을 만족해야 한다 — 이 스테이지들의 동작은
    바뀌면 안 된다."""
    p = new_pipeline()
    p.mark_running("planning")
    p.mark_completed("planning", {"summary": "PRD"}, agent_name="pm")
    assert p.stages["planning"].status == StageStatus.COMPLETED


def test_mark_completed_without_agent_name_still_completes_immediately():
    """agent_name을 안 주면(테스트 세팅 등 기존 호출 관례) 예전처럼 즉시
    COMPLETED로 전이해야 한다 — 이 저장소의 다른 테스트 다수가 이 관례에
    의존하므로 하위 호환이 깨지면 안 된다."""
    p = new_pipeline()
    p.mark_completed("design", {"agent": "architect"})
    assert p.stages["design"].status == StageStatus.COMPLETED


def test_mark_running_resets_agents_done_for_a_new_round():
    """재작업으로 design이 다시 도는데 이전 라운드에 이미 완료 보고했던
    에이전트 이름이 남아있으면, 이번 라운드엔 그 에이전트가 실제로 다시
    끝나지 않았는데도 "이미 끝남" 취급돼 스테이지가 조기 완료될 수 있다."""
    p = new_pipeline()
    p.mark_running("design")
    p.mark_completed("design", {}, agent_name="architect")
    assert p.stages["design"].agents_done == ["architect"]

    p.mark_running("design")  # 재작업으로 다시 시작
    assert p.stages["design"].agents_done == []
    p.mark_completed("design", {}, agent_name="designer")
    assert p.stages["design"].status == StageStatus.RUNNING  # architect가 아직 이번 라운드엔 안 끝남


def test_mark_running_preserves_agents_done_when_keep_flag_set():
    """"취소" 시나리오 — design의 architect는 이미 끝났고 designer만 아직 안
    끝난 채로 취소했을 때, keep_agents_done을 세워두면 재승인해서 다시 도는
    라운드에서도 architect의 완료 기록이 지워지면 안 된다(지워지면
    advance_pipeline이 architect한테도 새 태스크를 다시 보내버림 —
    recoveryfit에서 실제 재현된 문제)."""
    p = new_pipeline()
    p.mark_running("design")
    p.mark_completed("design", {"architecture_summary": "구조"}, agent_name="architect")
    assert p.stages["design"].agents_done == ["architect"]

    p.stages["design"].keep_agents_done = True
    p.mark_running("design")  # 취소 후 재승인
    assert p.stages["design"].agents_done == ["architect"]
    assert p.stages["design"].outputs == {"architecture_summary": "구조"}  # outputs도 그대로


def test_keep_agents_done_flag_is_one_shot():
    """scenario_scope와 동일한 패턴 — 다음 mark_running 한 번에만 적용되고
    그 다음부터는 다시 정상적으로(매 라운드) agents_done을 비워야 한다."""
    p = new_pipeline()
    p.mark_running("design")
    p.mark_completed("design", {}, agent_name="architect")
    p.stages["design"].keep_agents_done = True
    p.mark_running("design")
    assert p.stages["design"].keep_agents_done is False

    p.mark_completed("design", {}, agent_name="designer")  # 이번 라운드 마저 완료
    p.mark_running("design")  # 다음(세 번째) 라운드 — 이번엔 정상적으로 비워져야 함
    assert p.stages["design"].agents_done == []


def test_summary_includes_agents_done_for_persistence():
    p = new_pipeline()
    p.mark_running("design")
    p.mark_completed("design", {}, agent_name="architect")
    data = p.summary()
    assert data["stages"]["design"]["agents_done"] == ["architect"]


# ── 5. 실패 처리 — 이후 스테이지가 절대 진행되지 않아야 함 ──────────

def test_mark_failed_blocks_downstream_forever():
    p = new_pipeline()
    p.mark_completed("planning", {})
    p.mark_completed("design", {})
    p.mark_completed("implement", {})
    p.mark_running("qa")
    p.mark_completed("qa", {"passed": True})
    p.mark_running("autotest")

    p.mark_failed("autotest", {"passed": False, "summary": "CI 실패"})

    assert p.stages["autotest"].status == StageStatus.FAILED
    # release는 autotest 의존 — FAILED는 COMPLETED가 아니므로 절대 준비되면 안 됨
    assert "release" not in {s.name for s in p.get_ready_stages()}
    assert not p.is_done()


def test_failed_stage_stays_failed_not_completed():
    p = new_pipeline()
    p.mark_failed("planning", {"error": "뭔가 잘못됨"})
    assert p.stages["planning"].status == StageStatus.FAILED
    assert p.stages["planning"].status != StageStatus.COMPLETED


# ── 6. 여러 스테이지 동시 준비 (병렬 실행) ───────────────────────────

def test_design_is_single_stage_with_two_agents_not_two_stages():
    p = new_pipeline()
    p.mark_completed("planning", {})
    ready = p.get_ready_stages()
    assert [s.name for s in ready] == ["design"]
    assert set(p.stages["design"].agents) == {"designer", "architect"}


# ── 7. 완료 판정 ──────────────────────────────────────────────────────

def test_is_done_false_until_every_stage_completed():
    p = new_pipeline()
    names = ["planning", "design", "implement", "qa", "autotest"]
    for n in names:
        assert not p.is_done()
        p.mark_completed(n, {})
    assert not p.is_done()  # release 남음
    p.mark_completed("release", {})
    assert p.is_done()


# ── 8. summary() 직렬화 — 웹 UI/영속화가 참조하는 필드 존재 확인 ─────

def test_summary_includes_approved_flag_for_persistence():
    """approved가 summary에 없으면 orchestrator 재시작 시 승인 상태가
    복원이 안 돼서 똑같은 버그가 재발할 수 있다 — 필드 존재를 못박아둔다."""
    p = new_pipeline()
    p.mark_waiting_approval("implement")
    p.approve("implement")
    data = p.summary()
    assert data["stages"]["implement"]["approved"] is True
    assert data["stages"]["planning"]["approved"] is False


def test_summary_includes_current_task_field():
    """플로우차트 탭이 "지금 이 에이전트가 정확히 뭘 지시받았는지" 구조화된
    형태로 보여주는 기반 — summary에 current_task가 빠지면 프론트가 못 받는다."""
    p = new_pipeline()
    assert p.summary()["stages"]["planning"]["current_task"] == {}

    p.stages["planning"].current_task["pm"] = {"instruction": "로그인 화면 추가", "dispatched_at": 123.0, "manual": False}
    assert p.summary()["stages"]["planning"]["current_task"] == {
        "pm": {"instruction": "로그인 화면 추가", "dispatched_at": 123.0, "manual": False},
    }
