// approval_required WS 이벤트는 단발성이다 — 그 순간 브라우저가 연결돼 있지
// 않았거나(새로고침, WS 재연결 타이밍) 이벤트를 놓치면 서버는 승인 대기
// 상태(stages[stage].status === "waiting_approval")인데 화면엔 승인 버튼이
// 영영 안 뜨는 상태가 된다(counter-app에서 실제로 재현됨: 디자인이 승인
// 대기로 넘어갔는데 승인 버튼이 안 보였음).
//
// init/project_added/project_updated로 받는 프로젝트 상태를 진실의 원천으로
// 삼아 매번 승인 목록을 다시 맞춘다 — 놓친 이벤트가 있어도 새로고침이나 다음
// 상태 업데이트에서 저절로 복구되고, 반대로 이미 승인 대기가 아닌 스테이지의
// 카드는 자동으로 사라진다.

const APPROVAL_MESSAGES = {
  implement: "디자인 목업을 확인하고 승인해주세요 (헤더의 🎨 디자인 버튼). 승인해야 구현이 시작됩니다.",
};

function approvalMessageFor(stageName) {
  return APPROVAL_MESSAGES[stageName] ?? `'${stageName}' 스테이지를 시작하려면 승인이 필요합니다.`;
}

function reconcileApprovals(prevApprovals, projects) {
  const stillPending = prevApprovals.filter((a) => {
    const p = projects.find((pr) => pr.id === a.project_id);
    return p?.stages?.[a.stage]?.status === "waiting_approval";
  });
  const known = new Set(stillPending.map((a) => `${a.project_id}:${a.stage}`));
  const restored = [];
  projects.forEach((p) => {
    Object.entries(p.stages ?? {}).forEach(([stageName, info]) => {
      if (info?.status === "waiting_approval" && !known.has(`${p.id}:${stageName}`)) {
        restored.push({ project_id: p.id, stage: stageName, message: approvalMessageFor(stageName) });
      }
    });
  });
  return [...stillPending, ...restored];
}

module.exports = { reconcileApprovals, approvalMessageFor };
