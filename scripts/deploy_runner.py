#!/usr/bin/env python3
"""
호스트 네이티브 배포 러너 — orchestrator(Docker Linux 컨테이너) 대신 실제
`flutter build`/`fastlane`을 실행한다. Xcode는 이 Mac 호스트에만 있고 Docker
컨테이너 안에서는 접근 불가능하므로, 이 스크립트는 Docker가 아니라 호스트
python3로 직접 띄운다:

    python3 scripts/deploy_runner.py

표준 라이브러리만 사용한다 — 이 호스트의 python3(3.9)는 pip/site-packages가
망가져 있어(memory: project_ai_dev_team_dev_workflow) 서드파티 의존성을 깔 수
없다.

프로토콜(orchestrator/main.py의 POST /projects/{id}/deploy가 호출):
  POST /run  { project_id, workspace, environment, platforms, app_version, callback_url }
  → 202 즉시 응답, 백그라운드 스레드에서 실제 빌드+업로드 수행 후
    callback_url로 결과 POST.

컨벤션(모든 프로젝트가 이 구조를 가져야 함, child-care-medication 기준):
  {workspace}/pubspec.yaml           — `version: X.Y.Z+N` 줄
  {workspace}/scripts/build_release.sh <test|dev|prod>
  {workspace}/fastlane/Fastfile      — upload_ios / upload_android / upload_all 레인
  {workspace}/Gemfile                — `bundle exec fastlane ...`로 실행
"""
# 이 호스트의 python3는 3.9라 `X | None` 타입 힌트(PEP 604, 3.10+)를 그대로
# 쓰면 함수 정의 시점에 TypeError가 난다 — annotations를 지연 평가로 만들어
# 3.9에서도 그대로 돌아가게 한다.
from __future__ import annotations

import json
import re
import subprocess
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"   # 외부 노출 방지 — 이 호스트 안에서만 접근 가능
PORT = 8765
LOG_TAIL_CHARS = 4000

VERSION_RE = re.compile(r"^version:\s*(\S+)\+(\d+)\s*$", re.MULTILINE)

LANE_BY_PLATFORMS = {
    frozenset({"ios"}): "upload_ios",
    frozenset({"android"}): "upload_android",
    frozenset({"ios", "android"}): "upload_all",
}


def _run(cmd: list[str], cwd: str, log: list[str]) -> bool:
    log.append(f"$ {' '.join(cmd)}  (cwd={cwd})")
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=60 * 60,
        )
    except subprocess.TimeoutExpired as e:
        log.append(f"[TIMEOUT] {e}")
        return False
    if result.stdout:
        log.append(result.stdout)
    if result.stderr:
        log.append(result.stderr)
    if result.returncode != 0:
        log.append(f"[FAILED] exit={result.returncode}")
        return False
    return True


def _read_version(workspace: str) -> tuple[str, str] | None:
    try:
        with open(f"{workspace}/pubspec.yaml") as f:
            content = f.read()
    except OSError:
        return None
    m = VERSION_RE.search(content)
    if not m:
        return None
    return m.group(1), m.group(2)


def _write_app_version(workspace: str, app_version: str, build_number: str, log: list[str]):
    path = f"{workspace}/pubspec.yaml"
    with open(path) as f:
        content = f.read()
    new_content, n = VERSION_RE.subn(f"version: {app_version}+{build_number}", content, count=1)
    if n != 1:
        raise RuntimeError("pubspec.yaml에서 version 줄을 찾지 못했습니다")
    with open(path, "w") as f:
        f.write(new_content)
    log.append(f"[INFO] pubspec.yaml version → {app_version}+{build_number}")


def _deploy(job: dict):
    project_id   = job["project_id"]
    workspace    = job["workspace"]
    environment  = job.get("environment") or "prod"
    platforms    = job.get("platforms") or ["ios", "android"]
    app_version  = job.get("app_version")
    callback_url = job["callback_url"]

    log: list[str] = []
    result = {"project_id": project_id, "success": False, "log_tail": "", "error": None,
              "app_version": app_version, "build_number": None}

    try:
        import os
        if not os.path.isdir(workspace):
            raise RuntimeError(f"workspace 디렉토리가 없습니다: {workspace}")

        # 사람이 그 워크스페이스에서 손대던 작업을 덮어쓰지 않기 위한 안전장치 —
        # 추적 중인 파일에 커밋 안 된 변경이 있으면 중단한다(untracked 파일은
        # 허용 — .claude/ 같은 도구 설정 디렉토리가 흔히 있음).
        status = subprocess.run(
            ["git", "-C", workspace, "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True,
        ).stdout.strip()
        if status:
            raise RuntimeError(f"워크스페이스에 커밋되지 않은 변경사항이 있어 배포를 중단합니다:\n{status}")

        if not _run(["git", "-C", workspace, "checkout", "main"], workspace, log):
            raise RuntimeError("git checkout main 실패")
        if not _run(["git", "-C", workspace, "pull", "--ff-only"], workspace, log):
            raise RuntimeError("git pull 실패")

        version = _read_version(workspace)
        if not version:
            raise RuntimeError("pubspec.yaml에서 버전을 읽지 못했습니다")
        build_name, build_number = version
        if app_version and app_version != build_name:
            _write_app_version(workspace, app_version, build_number, log)
        else:
            app_version = build_name

        if not _run(["scripts/build_release.sh", environment], workspace, log):
            raise RuntimeError("build_release.sh 실패 — 로그 참고")

        lane = LANE_BY_PLATFORMS.get(frozenset(platforms))
        if not lane:
            raise RuntimeError(f"지원하지 않는 platforms 조합: {platforms}")
        if not _run(["bundle", "exec", "fastlane", lane, f"env:{environment}"], workspace, log):
            raise RuntimeError(f"fastlane {lane} 실패 — 로그 참고")

        final_version = _read_version(workspace)
        build_number = final_version[1] if final_version else build_number
        result["app_version"] = app_version
        result["build_number"] = build_number

        commit_msg = f"chore: release {app_version}+{build_number}"
        if _run(["git", "-C", workspace, "commit", "-am", commit_msg], workspace, log):
            if not _run(["git", "-C", workspace, "push"], workspace, log):
                log.append("[WARN] push 실패 — 스토어 업로드는 이미 끝났으니 배포는 성공으로 처리하고, "
                            f"버전 커밋({commit_msg})은 수동으로 push 해주세요.")

        result["success"] = True

    except Exception as e:
        result["error"] = str(e)
        log.append(f"[ERROR] {e}")

    tail = "\n".join(log)
    result["log_tail"] = tail[-LOG_TAIL_CHARS:]

    try:
        req = urllib.request.Request(
            callback_url, data=json.dumps(result).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"[deploy_runner] callback 전송 실패 ({callback_url}): {e}")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/run":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            job = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        threading.Thread(target=_deploy, args=(job,), daemon=True).start()

        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"accepted": True}).encode())

    def log_message(self, fmt, *args):
        print(f"[deploy_runner] {fmt % args}")


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[deploy_runner] listening on http://{HOST}:{PORT}")
    server.serve_forever()
