// 커스텀 Next.js 서버 — 리버스 프록시.
//
// 외부에서는 ngrok 터널이 web:3000(이 서버)만 뚫고 있고 orchestrator:8000은
// 노출되지 않는다. 프론트엔드가 API_URL/WS_URL을 window.location 기준
// same-origin으로 잡기 때문에(app/page.tsx), "/"(SPA)와 "/_next/*" 정적 자원을
// 뺀 나머지 모든 경로(/projects, /github/repos, /ws, /recordings/*, /docs 등)를
// 여기서 orchestrator로 그대로 전달해야 어느 기기에서 접속해도 동작한다.
const { createServer } = require("http");
const { parse } = require("url");
const next = require("next");
const httpProxy = require("http-proxy");

const dev = process.env.NODE_ENV !== "production";
const app = next({ dev });
const handle = app.getRequestHandler();

const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL || "http://orchestrator:8000";
const proxy = httpProxy.createProxyServer({ target: ORCHESTRATOR_URL, changeOrigin: true });

// http-proxy는 일반 HTTP 프록시 에러든 WebSocket 업그레이드 프록시 에러든 같은
// "error" 이벤트로 보내는데, 세 번째 인자의 실제 타입이 다르다 — HTTP는 진짜
// http.ServerResponse(res.writeHead 있음)지만 WS는 raw net.Socket(writeHead
// 없음)이다. 구분 없이 항상 res.writeHead()를 부르면 orchestrator가 잠깐이라도
// 재시작되는 동안 WS 재연결 시도마다 "res.writeHead is not a function"
// uncaughtException이 반복 발생해서 서버가 불안정해졌다(실제로 재현/확인됨 —
// 모바일에서 사이드바 버튼이 먹통이 되는 것처럼 보인 원인). 소켓인지 확인해서
// 각각 맞는 방식으로 정리한다. server.js에는 별도 테스트 러너가 없어서
// 순수 함수로 분리해 test_server.js에서 node:test로 검증한다.
function handleProxyError(err, res) {
  console.error("[proxy] error:", err.message);
  if (!res) return;
  if (typeof res.writeHead === "function") {
    if (!res.headersSent) {
      res.writeHead(502, { "Content-Type": "text/plain" });
      res.end("Bad gateway (orchestrator unreachable)");
    }
  } else if (typeof res.destroy === "function") {
    res.destroy();
  }
}

proxy.on("error", (err, _req, res) => handleProxyError(err, res));

function isNextOwnedPath(pathname) {
  return pathname === "/" || pathname.startsWith("/_next") || pathname === "/favicon.ico";
}

module.exports = { handleProxyError, isNextOwnedPath };

// `node server.js`로 직접 실행될 때만 실제 서버를 띄운다 — require()로
// 테스트에서 불러올 때 Next.js 서버가 같이 뜨는 걸 막기 위함.
if (require.main === module) {
app.prepare().then(() => {
  const server = createServer((req, res) => {
    const parsedUrl = parse(req.url, true);
    if (isNextOwnedPath(parsedUrl.pathname)) {
      handle(req, res, parsedUrl);
    } else {
      proxy.web(req, res);
    }
  });

  // WebSocket(/ws)도 orchestrator로 그대로 업그레이드 프록시
  server.on("upgrade", (req, socket, head) => {
    proxy.ws(req, socket, head);
  });

  server.listen(3000, "0.0.0.0", () => {
    console.log(`> Ready on http://0.0.0.0:3000 (proxying non-SPA paths to ${ORCHESTRATOR_URL})`);
  });
});
}
