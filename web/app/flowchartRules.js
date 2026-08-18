// 완료된 스테이지를 폐기(산출물을 버리고 이전 단계로 되돌리기)할 수 있는지
// 판단하는 순수 함수 — orchestrator/main.py의 _DISCARDABLE_STAGES와 반드시
// 같은 목록이어야 한다(다르면 프론트는 버튼을 보여주는데 서버는 400을
// 뱉거나, 반대로 서버는 되는데 프론트에 버튼이 없는 상황이 생김).
// planning은 되돌아갈 "이전 단계"가 없어서 제외.
const DISCARDABLE_STAGES = new Set(["design", "implement", "qa", "autotest", "release"]);

function canDiscardStage(stageName, status) {
  return DISCARDABLE_STAGES.has(stageName) && status === "completed";
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

module.exports = { DISCARDABLE_STAGES, canDiscardStage, agentSubStatus };
