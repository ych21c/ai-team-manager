// 스프린트 플로우차트 각 스테이지 노드의 재실행/폐기 버튼 노출 규칙.
// 폐기(discard)는 orchestrator/main.py의 discard_stage가 실제로 허용하는
// 스테이지(_DISCARDABLE_STAGES)와 어긋나면 프론트는 버튼을 보여주는데
// 서버는 400을 뱉는(또는 그 반대) 상황이 생기므로, 둘을 맞춰두는 게 목적.
//
// 실행: cd web && node --test test_flowchart_rules.js
const { test } = require("node:test");
const assert = require("node:assert");
const { DISCARDABLE_STAGES, canDiscardStage } = require("./app/flowchartRules.js");

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
