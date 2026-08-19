// 완료된 스테이지를 폐기(산출물을 버리고 이전 단계로 되돌리기)할 수 있는지
// 판단하는 순수 함수 — orchestrator/main.py의 _DISCARDABLE_STAGES와 반드시
// 같은 목록이어야 한다(다르면 프론트는 버튼을 보여주는데 서버는 400을
// 뱉거나, 반대로 서버는 되는데 프론트에 버튼이 없는 상황이 생김).
// planning은 되돌아갈 "이전 단계"가 없어서 제외.
const DISCARDABLE_STAGES = new Set(["design", "implement", "qa", "autotest", "release"]);

// "failed"도 폐기 가능 대상에 포함한다 — orchestrator/main.py의 stage_failed
// 이벤트 처리가 생기면서(에이전트가 예외로 죽으면 명시적으로 실패 보고) 이제
// design/pm/architect/release 같은 스테이지도 실제로 "failed" 상태에 도달할 수
// 있는데, 여기서 빠지면 실패한 스테이지에 재실행/폐기 버튼이 하나도 안 뜨는
// 막다른 골목이 된다.
function canDiscardStage(stageName, status) {
  return DISCARDABLE_STAGES.has(stageName) && (status === "completed" || status === "failed");
}

// "취소" — 아직 안 끝난(running) 스테이지를 강제로 이전 단계로 되돌릴 수 있는지.
// 백엔드 discard_stage(orchestrator/main.py)는 스테이지 status를 검사하지 않고
// _DISCARDABLE_STAGES에만 있으면 그대로 실행하므로(토큰 소진 등으로 에이전트가
// 죽어서 running에 영영 멈춰있는 경우 대비), 프론트도 같은 스테이지 목록에 대해
// running일 때만 취소 버튼을 노출한다. planning은 discard 자체가 안 되는 첫
// 스테이지라 여기서도 제외 — 멈추면 채팅으로 재요청해서 재기획(retry-planning)
// 경로를 써야 한다.
//
// 주의: 취소해도 에이전트 컨테이너 자체를 죽이지는 않는다(CLAUDE.md — running
// 중 컨테이너 재생성은 진행 중이던 redis 태스크를 xack 전에 유실시킨다). 만약
// 원래 에이전트가 살아있어서 나중에 완료 이벤트를 보내면, 그 사이 같은 스테이지가
// 다시 실행돼 있을 경우 stale한 결과로 덮어써질 수 있다 — 그 정도 레이스는 감수하고
// "죽은 것 같은 작업을 손으로 풀어주는" 용도로만 쓴다.
function canCancelStage(stageName, status) {
  return DISCARDABLE_STAGES.has(stageName) && status === "running";
}

// design처럼 에이전트 여럿(designer+architect)이 한 스테이지를 나눠 맡을 때,
// 그중 한 에이전트만의 상태를 순수하게 계산한다 — stage 전체 status는 전원이
// 끝나야 "completed"가 되므로(orchestrator/workflows/pipeline.py의
// mark_completed), 개별 에이전트가 먼저 끝났는지는 agentsDone(서버가 보내는
// stageInfo.agents_done)으로 따로 봐야 한다.
function agentSubStatus(stageStatus, agentName, agentsDone) {
  // stage 전체가 completed라는 건 정의상 agents 전원이 끝났다는 뜻이다(전원이
  // 끝나야만 completed로 전이하므로) — agents_done이 없는 옛 스냅샷(이 필드가
  // 생기기 전에 저장된 상태)을 복원했을 때도 개별 에이전트가 "pending"으로
  // 잘못 보이지 않게 먼저 확인한다.
  if (stageStatus === "completed") return "completed";
  if ((agentsDone ?? []).includes(agentName)) return "completed";
  return stageStatus === "running" ? "running" : "pending";
}

module.exports = { DISCARDABLE_STAGES, canDiscardStage, canCancelStage, agentSubStatus };
