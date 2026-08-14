// 회귀 테스트 — 프록시 에러 핸들러가 WebSocket 에러(res 자리에 raw socket이 옴,
// writeHead 없음)를 HTTP 에러처럼 다뤄서 "res.writeHead is not a function"
// uncaughtException을 반복 발생시키던 사고. 실제로 발생해서 서버가 불안정해졌고,
// 모바일에서 사이드바 버튼이 먹통이 되는 것처럼 보였다.
//
// 실행: cd web && node --test test_server.js
const { test } = require("node:test");
const assert = require("node:assert");
const { handleProxyError, isNextOwnedPath } = require("./server.js");

test("http error: writes 502 when headers not sent", () => {
  let status, body;
  const res = {
    headersSent: false,
    writeHead(code) { status = code; },
    end(text) { body = text; },
  };
  handleProxyError(new Error("ECONNREFUSED"), res);
  assert.strictEqual(status, 502);
  assert.ok(body.includes("unreachable"));
});

test("http error: does not double-write when headers already sent", () => {
  const res = {
    headersSent: true,
    writeHead() { throw new Error("should not be called"); },
    end() { throw new Error("should not be called"); },
  };
  assert.doesNotThrow(() => handleProxyError(new Error("boom"), res));
});

test("websocket error: destroys the raw socket instead of calling writeHead", () => {
  let destroyed = false;
  const socket = {
    // 실제 net.Socket처럼 writeHead가 아예 없다.
    destroy() { destroyed = true; },
  };
  assert.doesNotThrow(() => handleProxyError(new Error("ECONNREFUSED"), socket));
  assert.strictEqual(destroyed, true);
});

test("no res/socket at all: does not throw", () => {
  assert.doesNotThrow(() => handleProxyError(new Error("boom"), null));
  assert.doesNotThrow(() => handleProxyError(new Error("boom"), undefined));
});

test("isNextOwnedPath routes SPA/static paths to Next, everything else to proxy", () => {
  assert.strictEqual(isNextOwnedPath("/"), true);
  assert.strictEqual(isNextOwnedPath("/_next/static/chunk.js"), true);
  assert.strictEqual(isNextOwnedPath("/favicon.ico"), true);
  assert.strictEqual(isNextOwnedPath("/ws"), false);
  assert.strictEqual(isNextOwnedPath("/projects"), false);
  assert.strictEqual(isNextOwnedPath("/recordings/30dcf5ed"), false);
});
