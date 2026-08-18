// 완료된 스테이지를 폐기(산출물을 버리고 이전 단계로 되돌리기)할 수 있는지
// 판단하는 순수 함수 — orchestrator/main.py의 _DISCARDABLE_STAGES와 반드시
// 같은 목록이어야 한다(다르면 프론트는 버튼을 보여주는데 서버는 400을
// 뱉거나, 반대로 서버는 되는데 프론트에 버튼이 없는 상황이 생김).
// planning은 되돌아갈 "이전 단계"가 없어서 제외.
const DISCARDABLE_STAGES = new Set(["design", "implement", "qa", "autotest", "release"]);

function canDiscardStage(stageName, status) {
  return DISCARDABLE_STAGES.has(stageName) && status === "completed";
}

module.exports = { DISCARDABLE_STAGES, canDiscardStage };
