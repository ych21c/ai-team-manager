// 회귀 테스트 — 채팅 메시지에 시각이 표시되지 않던 문제. 서버가 이미 주는
// ts(ms epoch)를 포맷하는 순수 함수만 검증한다.
//
// 실행: cd web && node --test test_format_time.js
const { test } = require("node:test");
const assert = require("node:assert");
const { formatMessageTime } = require("./app/formatTime.js");

test("formats a known timestamp as HH:MM in local time", () => {
  const d = new Date();
  d.setHours(9, 5, 0, 0);
  assert.strictEqual(formatMessageTime(d.getTime()), "09:05");
});

test("pads single-digit hours and minutes with zero", () => {
  const d = new Date();
  d.setHours(1, 2, 0, 0);
  assert.strictEqual(formatMessageTime(d.getTime()), "01:02");
});

test("returns empty string for missing timestamp", () => {
  assert.strictEqual(formatMessageTime(undefined), "");
  assert.strictEqual(formatMessageTime(null), "");
});

test("returns empty string for invalid timestamp instead of throwing", () => {
  assert.strictEqual(formatMessageTime(NaN), "");
  assert.doesNotThrow(() => formatMessageTime("not-a-number"));
});
