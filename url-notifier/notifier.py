"""
URL Notifier — cloudflared REST API에서 터널 URL 수집
cloudflared는 --metrics 포트에 /quicktunnel 엔드포인트를 제공함
"""
import os
import time
import httpx

SLACK    = os.getenv("SLACK_WEBHOOK_URL", "")
MAX_WAIT = 90


def fetch_url(host: str, timeout: int = MAX_WAIT) -> str:
    """cloudflared quicktunnel API에서 URL 가져오기"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = httpx.get(f"http://{host}:20241/quicktunnel", timeout=3)
            if r.status_code == 200:
                data = r.json()
                url = data.get("url", "")
                if url:
                    return f"https://{url}" if not url.startswith("http") else url
        except Exception:
            pass
        time.sleep(4)
    return ""


def send_slack(msg: str):
    if not SLACK:
        return
    try:
        httpx.post(SLACK, json={"text": msg}, timeout=10)
    except Exception as e:
        print(f"[notifier] Slack 실패: {e}")


def main():
    print("[notifier] 터널 시작 대기 중... (최대 90초)")
    time.sleep(10)  # cloudflared 초기 연결 대기

    web_url = fetch_url("tunnel-web")
    api_url = fetch_url("tunnel-api")

    banner = f"""
╔══════════════════════════════════════════════════════════════╗
║            AI Dev Team — 외부 접속 URL                      ║
╠══════════════════════════════════════════════════════════════╣
║  Web UI  : {(web_url or '준비 중...'):<48} ║
║  API     : {(api_url or '준비 중...'):<48} ║
║  API 문서: {((api_url+'/docs') if api_url else '준비 중...'):<48} ║
╚══════════════════════════════════════════════════════════════╝
"""
    print(banner)

    if SLACK and web_url:
        send_slack(f"*AI Dev Team 시작* 🚀\n• Web: {web_url}\n• API: {api_url}/docs")


if __name__ == "__main__":
    main()
