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


PIPELINE_DEFINITION: list[Stage] = [
    Stage("planning",      ["pm"],                      depends_on=[]),
    Stage("design",        ["designer", "architect"],   depends_on=["planning"]),
    # 디자인 목업을 사람이 직접 보고 승인해야 구현이 시작되게 함 — 승인 없이
    # 바로 구현 들어가면 방향이 틀렸을 때 코드까지 다 만들고 나서야 알게 됨.
    Stage("implement",     ["implement"],               depends_on=["design"], requires_approval=True),
    Stage("qa",            ["qa"],                      depends_on=["implement"]),
    Stage("autotest",      ["autotest"],                depends_on=["qa"]),
    Stage("release",       ["release"],                 depends_on=["autotest"], requires_approval=True),
]


class Pipeline:
    def __init__(self, project_id: str, instruction: str):
        self.project_id = project_id
        self.instruction = instruction
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
        self.stages[stage_name].status = StageStatus.RUNNING

    def mark_completed(self, stage_name: str, outputs: dict = {}):
        self.stages[stage_name].status = StageStatus.COMPLETED
        # design처럼 에이전트 2개(designer+architect)가 같은 스테이지를 병렬로
        # 끝내는 경우, 나중에 끝난 쪽이 먼저 끝난 쪽 outputs를 통째로 덮어써서
        # 날려버리던 문제가 있었다 — merge해서 둘 다 보존한다.
        existing = self.stages[stage_name].outputs or {}
        self.stages[stage_name].outputs = {**existing, **outputs}

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
            "stages": {
                name: {"status": s.status, "agents": s.agents, "outputs": s.outputs, "approved": s.approved}
                for name, s in self.stages.items()
            }
        }
