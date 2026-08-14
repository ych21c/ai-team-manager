// 회귀 테스트 — 디자인이 승인 대기(waiting_approval)로 넘어갔는데 승인
// 버튼이 화면에 안 뜨던 사고. approval_required는 단발성 WS 이벤트라, 그
// 순간 브라우저가 연결돼 있지 않았으면(새로고침, WS 재연결 타이밍) 놓치고,
// approvals 상태는 그 이벤트로만 채워지므로 서버는 승인 대기인데 화면엔
// 버튼이 영영 안 뜨는 상태가 됐다. init/project_updated로 받는 서버 상태
// (stages[].status)를 기준으로 매번 다시 맞추게 고쳤다.
//
// 실행: cd web && node --test test_reconcile_approvals.js
const { test } = require("node:test");
const assert = require("node:assert");
const { reconcileApprovals, approvalMessageFor } = require("./app/reconcileApprovals.js");

test("restores a missed approval card from server state (the actual bug)", () => {
  const projects = [
    { id: "30dcf5ed", stages: { implement: { status: "waiting_approval" } } },
  ];
  const result = reconcileApprovals([], projects);
  assert.strictEqual(result.length, 1);
  assert.strictEqual(result[0].project_id, "30dcf5ed");
  assert.strictEqual(result[0].stage, "implement");
  assert.ok(result[0].message.includes("승인"));
});

test("does not duplicate a card that is already present", () => {
  const projects = [
    { id: "p1", stages: { implement: { status: "waiting_approval" } } },
  ];
  const existing = [{ project_id: "p1", stage: "implement", message: "이미 떠 있던 카드" }];
  const result = reconcileApprovals(existing, projects);
  assert.strictEqual(result.length, 1);
  assert.strictEqual(result[0].message, "이미 떠 있던 카드"); // 기존 카드를 덮어쓰지 않음
});

test("drops a stale card once the stage is no longer waiting_approval", () => {
  const projects = [
    { id: "p1", stages: { implement: { status: "running" } } },
  ];
  const existing = [{ project_id: "p1", stage: "implement", message: "승인 대기였음" }];
  const result = reconcileApprovals(existing, projects);
  assert.deepStrictEqual(result, []);
});

test("drops a stale card when its project no longer exists (e.g. deleted)", () => {
  const existing = [{ project_id: "gone", stage: "implement", message: "x" }];
  const result = reconcileApprovals(existing, []);
  assert.deepStrictEqual(result, []);
});

test("only restores cards for stages actually waiting_approval, across multiple projects", () => {
  const projects = [
    { id: "p1", stages: { design: { status: "completed" }, implement: { status: "waiting_approval" } } },
    { id: "p2", stages: { release: { status: "waiting_approval" }, autotest: { status: "completed" } } },
  ];
  const result = reconcileApprovals([], projects);
  const keys = result.map(a => `${a.project_id}:${a.stage}`).sort();
  assert.deepStrictEqual(keys, ["p1:implement", "p2:release"]);
});

test("implement gets the design-approval-specific message, other stages get the generic one", () => {
  assert.match(approvalMessageFor("implement"), /디자인 목업/);
  assert.strictEqual(approvalMessageFor("release"), "'release' 스테이지를 시작하려면 승인이 필요합니다.");
});

test("a project with no stages at all does not throw", () => {
  const projects = [{ id: "p1" }];
  assert.doesNotThrow(() => reconcileApprovals([], projects));
  assert.deepStrictEqual(reconcileApprovals([], projects), []);
});
