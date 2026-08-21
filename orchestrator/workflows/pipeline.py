"""
파이프라인 워크플로우 정의.

각 스테이지는 이전 스테이지가 완료돼야 시작된다.
병렬 실행 가능한 스테이지는 동시에 시작된다.

PIPELINE:
  1. pm          (단독)
  2. designer + architect  (병렬)
  3. implement   (designer + architect 완료 후)
  4. qa          (implement 완료 후)
  5. autotest    (qa 완료 후)
  6. [APPROVAL]  ← 대표님 승인 대기
  7. release     (승인 후)
"""
from enum import Enum
from dataclasses import dataclass, field


class StageStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    WAITING   = "waiting_approval"


@dataclass
class Stage:
    name: str
    agents: list[str]          # 이 스테이지를 담당하는 에이전트 목록
    depends_on: list[str]      # 선행 스테이지 이름 목록
    requires_approval: bool = False
    status: StageStatus = StageStatus.PENDING
    outputs: dict = field(default_factory=dict)
    # requires_approval은 스테이지 "정의"에 영구히 붙어있는 값이라, approve()로
    # WAITING→PENDING 전환해도 advance_pipeline이 같은 스테이지를 다시 볼 때
    # "승인 필요하네"를 또 띄우는 무한 루프 버그가 있었다(실제로 발생 확인됨).
    # 한 번 승인됐는지 여부를 별도로 기억해서 재차 막지 않게 한다.
    approved: bool = False
    # 디자인 재작업을 "이 시나리오(Jira 이슈) 키 하나만"으로 좁히고 싶을 때 쓰는
    # 1회성 힌트. advance_pipeline이 이 스테이지를 다음번 실행할 때 한 번 읽어서
    # 시나리오 목록으로 쓰고 바로 None으로 비운다 — 영구 상태가 아니라 "다음
    # 실행 한 번"에만 적용되는 값이라 세션 스냅샷에 저장할 필요 없음.
    scenario_scope: list[dict] | None = None
    # design처럼 agents가 여럿(designer+architect)인 스테이지에서, 이번 실행
    # 라운드에 실제로 stage_completed를 보고한 에이전트 이름들. mark_completed가
    # agent_name과 함께 호출될 때만 쓰이고, stage.agents 전원이 여기 모여야
    # 스테이지가 진짜 COMPLETED로 전이한다(먼저 끝난 한 명만으로 착각해 다음
    # 게이트를 열어버리던 문제를 막기 위함). mark_running이 매 라운드 시작 시
    # 비운다 — 단, keep_agents_done이 True면 비우지 않는다(취소 시 이미 끝낸
    # 에이전트를 보존하는 용도. 아래 keep_agents_done 참고).
    agents_done: list[str] = field(default_factory=list)
    # "취소"(실행 중인 스테이지를 되돌림) 전용 1회성 힌트 — design처럼 에이전트가
    # 여럿(designer+architect)인 스테이지에서 한쪽만 아직 안 끝났는데 취소하면,
    # 이미 끝낸 쪽까지 통째로 재작업시키던 문제가 있었다(recoveryfit에서 실제
    # 발생: architect는 이미 끝났는데 취소→재승인하면 architect까지 처음부터 다시
    # 돎). True면 mark_running이 agents_done을 비우지 않고, advance_pipeline의
    # 태스크 발송 루프도 이미 agents_done에 있는 에이전트는 건너뛴다. scenario_scope와
    # 마찬가지로 다음 실행 한 번만 적용되고 advance_pipeline이 바로 False로 되돌린다.
    keep_agents_done: bool = False
    # agent_name → {"instruction", "dispatched_at", "manual"} — 이 스테이지의 각
    # 에이전트에게 실제로 마지막에 dispatch된 태스크 스냅샷. 플로우차트 탭에서
    # "지금/마지막으로 이 에이전트가 정확히 뭘 하고 있(었)는지"를 원시 로그가
    # 아니라 구조화된 형태로 보여주기 위함 — _send_task_or_manual이 dispatch
    # 시점마다 채운다. 완료돼도 지우지 않는다(끝난 뒤에도 "무슨 태스크였는지"
    # 확인할 수 있어야 하고, 다음 라운드 dispatch 때 자연히 덮어써진다).
    current_task: dict = field(default_factory=dict)


PIPELINE_DEFINITION: list[Stage] = [
    Stage("planning",      ["pm"],                      depends_on=[]),
    # PM 기획을 사람이 보고 승인해야 디자인이 시작되게 함 — 스프린트를 애자일하게
    # 통제하려면 매 전환마다(구현 시작 직전 제외하고 자동으로 흘려보내던) 사람이
    # 결과를 보고 넘어갈지 결정할 수 있어야 한다.
    Stage("design",        ["designer", "architect"],   depends_on=["planning"], requires_approval=True),
    # 디자인 목업을 사람이 직접 보고 승인해야 구현이 시작되게 함 — 승인 없이
    # 바로 구현 들어가면 방향이 틀렸을 때 코드까지 다 만들고 나서야 알게 됨.
    Stage("implement",     ["implement"],               depends_on=["design"], requires_approval=True),
    # implement→qa는 게이트 없이 자동 진행 — QA는 구현 산출물을 바로 검증하는
    # 단계라 사람이 매번 끼어들 필요가 적다는 판단.
    Stage("qa",            ["qa"],                      depends_on=["implement"]),
    # QA 결과를 사람이 보고 승인해야 오토테스트(CI)가 시작되게 함.
    Stage("autotest",      ["autotest"],                depends_on=["qa"], requires_approval=True),
    Stage("release",       ["release"],                 depends_on=["autotest"], requires_approval=True),
]


class Pipeline:
    def __init__(self, project_id: str, instruction: str, sprint: int = 1):
        self.project_id = project_id
        self.instruction = instruction
        # planning부터 전체 파이프라인을 되돌리는 재기획(_retry_planning_with_feedback)
        # 때마다 1씩 늘어나는 "몇 번째 재기획 라운드인지" 카운터. 그 시점에 새로 생성되는
        # 이슈/디자인 결과물에 태그로 붙여서, mtime만으로 짐작하던 "이번 라운드 산출물"을
        # 명시적으로 구분할 수 있게 한다.
        self.sprint = sprint
        self.stages: dict[str, Stage] = {s.name: s for s in [
            Stage(s.name, s.agents[:], s.depends_on[:], s.requires_approval)
            for s in PIPELINE_DEFINITION
        ]}

    def get_ready_stages(self) -> list[Stage]:
        """실행 가능한 스테이지 반환 (의존성 충족 + PENDING 상태)."""
        ready = []
        for stage in self.stages.values():
            if stage.status != StageStatus.PENDING:
                continue
            deps_done = all(
                self.stages[dep].status == StageStatus.COMPLETED
                for dep in stage.depends_on
            )
            if deps_done:
                ready.append(stage)
        return ready

    def mark_running(self, stage_name: str):
        stage = self.stages[stage_name]
        stage.status = StageStatus.RUNNING
        if stage.keep_agents_done:
            # "취소" 직후 재승인된 라운드 — 이미 끝낸 에이전트 기록을 보존한다.
            stage.keep_agents_done = False
        else:
            stage.agents_done = []  # 새 실행 라운드 — 지난 라운드에 보고한 에이전트 기록 초기화

    def mark_completed(self, stage_name: str, outputs: dict = {}, agent_name: str | None = None):
        """스테이지 완료 처리. design처럼 에이전트 2개(designer+architect)가 같은
        스테이지를 병렬로 끝내는 경우, 나중에 끝난 쪽이 먼저 끝난 쪽 outputs를
        통째로 덮어써서 날려버리던 문제가 있었다 — merge해서 둘 다 보존한다.

        agent_name을 주면(실제 파이프라인 이벤트 경로) 이 스테이지를 담당하는
        에이전트 전원(stage.agents)이 보고해야 실제로 COMPLETED로 전이한다 —
        안 그러면 designer/architect 중 먼저 끝난 쪽만으로 스테이지가 끝났다고
        착각해 다음 게이트(구현 시작 승인 등)가 나머지 에이전트를 기다리지 않고
        열려버렸다. agent_name을 안 주면(테스트에서 파이프라인을 특정 상태로
        미리 세팅할 때처럼) 예전처럼 즉시 COMPLETED로 전이한다."""
        stage = self.stages[stage_name]
        existing = stage.outputs or {}
        stage.outputs = {**existing, **outputs}
        if agent_name is None:
            stage.status = StageStatus.COMPLETED
            return
        if agent_name not in stage.agents_done:
            stage.agents_done.append(agent_name)
        stage.status = (
            StageStatus.COMPLETED if set(stage.agents_done) >= set(stage.agents) else StageStatus.RUNNING
        )

    def mark_failed(self, stage_name: str, outputs: dict = {}):
        """실패 시 COMPLETED로 전이시키지 않아 후속 스테이지가 절대 실행되지 않게 한다."""
        self.stages[stage_name].status = StageStatus.FAILED
        self.stages[stage_name].outputs = outputs

    def mark_waiting_approval(self, stage_name: str):
        self.stages[stage_name].status = StageStatus.WAITING

    def approve(self, stage_name: str):
        """승인자가 승인하면 PENDING으로 되돌려 실행 가능하게 하고, 이미
        승인됐다는 걸 기억해서 advance_pipeline이 다시 승인을 요구하지 않게 한다."""
        stage = self.stages[stage_name]
        if stage.status == StageStatus.WAITING:
            stage.status = StageStatus.PENDING
            stage.approved = True

    def is_done(self) -> bool:
        return all(s.status == StageStatus.COMPLETED for s in self.stages.values())

    def summary(self) -> dict:
        return {
            "project_id": self.project_id,
            "sprint": self.sprint,
            "stages": {
                name: {
                    "status": s.status, "agents": s.agents, "outputs": s.outputs,
                    "approved": s.approved, "agents_done": s.agents_done,
                    "current_task": s.current_task,
                }
                for name, s in self.stages.items()
            }
        }
