// 스프린트 플로우차트 각 스테이지 노드의 재실행/폐기 버튼 노출 규칙.
// 폐기(discard)는 orchestrator/main.py의 discard_stage가 실제로 허용하는
// 스테이지(_DISCARDABLE_STAGES)와 어긋나면 프론트는 버튼을 보여주는데
// 서버는 400을 뱉는(또는 그 반대) 상황이 생기므로, 둘을 맞춰두는 게 목적.
//
// 실행: cd web && node --test test_flowchart_rules.js
const { test } = require("node:test");
const assert = require("node:assert");
const { DISCARDABLE_STAGES, canDiscardStage, agentSubStatus } = require("./app/flowchartRules.js");

test("completed discardable stages can be discarded", () => {
  for (const name of ["design", "implement", "qa", "autotest", "release"]) {
    assert.strictEqual(canDiscardStage(name, "completed"), true, `${name} should be discardable once completed`);
  }
});

test("planning cannot be discarded even when completed — there's no earlier stage to return to", () => {
  assert.strictEqual(canDiscardStage("planning", "completed"), false);
});

test("a stage that hasn't produced output yet cannot be discarded", () => {
  assert.strictEqual(canDiscardStage("design", "pending"), false);
  assert.strictEqual(canDiscardStage("design", "running"), false);
  assert.strictEqual(canDiscardStage("design", "waiting_approval"), false);
});

test("DISCARDABLE_STAGES matches orchestrator's _DISCARDABLE_STAGES", () => {
  assert.deepStrictEqual([...DISCARDABLE_STAGES].sort(), ["autotest", "design", "implement", "qa", "release"]);
});

// ── agentSubStatus — design 노드 안 designer/architect 개별 상태 ─────────
// 배경: design 스테이지는 designer+architect 둘이 나눠 맡는데, 전체 stage
// status는 둘 다 끝나야 "completed"가 된다(orchestrator/workflows/pipeline.py
// mark_completed가 agents_done으로 전원 완료를 확인). 그래서 개별 에이전트가
// 먼저 끝났는지는 stage status만으로는 알 수 없고 agents_done을 따로 봐야 한다.

test("agent already in agents_done shows completed even while stage is still running", () => {
  assert.strictEqual(agentSubStatus("running", "architect", ["architect"]), "completed");
});

test("agent not yet in agents_done shows running while the stage is running", () => {
  assert.strictEqual(agentSubStatus("running", "designer", ["architect"]), "running");
});

test("agent not in agents_done shows pending when the stage hasn't started", () => {
  assert.strictEqual(agentSubStatus("pending", "designer", []), "pending");
});

test("missing agents_done (older snapshot before this field existed) is treated as nobody done yet", () => {
  assert.strictEqual(agentSubStatus("running", "designer", undefined), "running");
  assert.strictEqual(agentSubStatus("pending", "designer", undefined), "pending");
});

test("stage already completed reports completed per agent even if agents_done is stale/empty", () => {
  // completed는 정의상 stage.agents 전원이 끝나야만 도달하는 상태라(mark_completed가
  // 그렇게 게이트함), agents_done에 이름이 비어 있어도(예: 이 필드가 생기기 전에
  // 저장된 옛 스냅샷을 복원한 경우) "완료" 취급해야 한다 — 안 그러면 부모 노드는
  // "완료"인데 자식 행은 "대기"로 보이는 모순이 생긴다.
  assert.strictEqual(agentSubStatus("completed", "designer", []), "completed");
  assert.strictEqual(agentSubStatus("completed", "designer", undefined), "completed");
});
