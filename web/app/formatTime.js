// 채팅 메시지 옆에 보여줄 시각 포맷. 서버는 이미 메시지마다 ts(ms epoch)를
// 내려주고 있었는데(orchestrator/main.py _store_message) 화면에는 표시하고
// 있지 않았다 — 순수 함수로 분리해서 테스트 가능하게 한다.
function formatMessageTime(ts) {
  if (ts === null || ts === undefined || Number.isNaN(ts)) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "";
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

module.exports = { formatMessageTime };
