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
  POST /run  { project_id, workspace, environment, platforms, app_version,
               repo_url, callback_url, progress_url }
  → 202 즉시 응답, 백그라운드 스레드에서 실제 빌드+업로드 수행 후
    callback_url로 최종 결과 POST(progress_url로는 단계별 진행 상황을 그때그때
    POST — 둘 다 실패해도 배포 자체는 계속 진행, 통보만 못 갈 뿐).

workspace가 아직 clone된 적 없는 빈 디렉토리(또는 존재하지 않음)라면 repo_url로
clone부터 한다(orchestrator가 이미 갖고 있는 GITHUB_TOKEN을 넣어 만든 URL —
child-care-medication류 프로젝트 생성 때와 동일한 방식). workspace가 git
저장소가 아닌데 비어있지도 않으면(사람이 다른 걸 넣어둔 경우) 안전하게
중단한다 — 함부로 지우고 clone하지 않는다.

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
import os
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

# 프론트엔드(web/app/page.tsx DeployPanel)가 이 순서대로 스텝칩을 그린다 —
# 순서를 바꾸면 그쪽 DEPLOY_PHASE_ORDER도 같이 맞춰야 한다.
PHASE_LABELS = {
    "workspace":   "워크스페이스 준비",
    "version":     "버전 확인",
    "build":       "flutter build",
    "fastlane":    "fastlane 업로드",
    "commit_push": "버전 커밋 · 푸시",
}


def _report_phase(progress_url: str | None, project_id: str, phase: str, status: str, detail: str = ""):
    if not progress_url:
        return
    body = {
        "project_id": project_id, "phase": phase, "status": status,
        "label": PHASE_LABELS.get(phase, phase), "detail": detail,
    }
    try:
        req = urllib.request.Request(
            progress_url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[deploy_runner] progress 전송 실패 (phase={phase}): {e}")


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


def _clone(workspace: str, repo_url: str, log: list[str]) -> bool:
    parent = os.path.dirname(workspace.rstrip("/")) or "."
    os.makedirs(parent, exist_ok=True)
    return _run(["git", "clone", repo_url, workspace], parent, log)


def _ensure_workspace(workspace: str, repo_url: str | None, log: list[str]):
    """workspace가 이미 clone된 git 저장소면 아무것도 안 한다. 존재하지 않거나
    빈 디렉토리인데 repo_url이 있으면 그 자리에 clone한다. git 저장소도 아니고
    비어있지도 않으면(사람이 다른 걸 넣어둔 경우) 절대 건드리지 않고 중단한다."""
    if os.path.isdir(os.path.join(workspace, ".git")):
        return
    if os.path.isdir(workspace) and os.listdir(workspace):
        raise RuntimeError(
            f"{workspace}는 git 저장소가 아니고 비어있지도 않습니다 — 잘못 지정된 "
            "경로일 수 있어 안전을 위해 자동 clone하지 않습니다. 직접 확인해주세요."
        )
    if not repo_url:
        raise RuntimeError(
            f"{workspace}에 clone된 저장소가 없습니다 — repo_url이 없어 자동 clone할 "
            "수 없습니다(프로젝트에 연결된 GitHub 레포가 없거나 GITHUB_TOKEN 미설정)."
        )
    log.append(f"[INFO] {workspace}에 clone된 저장소가 없어 clone합니다: {repo_url}")
    if not _clone(workspace, repo_url, log):
        raise RuntimeError("git clone 실패 — 로그 참고")


def _deploy(job: dict):
    project_id   = job["project_id"]
    workspace    = job["workspace"]
    environment  = job.get("environment") or "prod"
    platforms    = job.get("platforms") or ["ios", "android"]
    app_version  = job.get("app_version")
    repo_url     = job.get("repo_url")
    callback_url = job["callback_url"]
    progress_url = job.get("progress_url")

    log: list[str] = []
    result = {"project_id": project_id, "success": False, "log_tail": "", "error": None,
              "app_version": app_version, "build_number": None}

    phase = "workspace"
    try:
        _report_phase(progress_url, project_id, "workspace", "start")
        _ensure_workspace(workspace, repo_url, log)

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
        _report_phase(progress_url, project_id, "workspace", "success")

        phase = "version"
        _report_phase(progress_url, project_id, "version", "start")
        version = _read_version(workspace)
        if not version:
            raise RuntimeError("pubspec.yaml에서 버전을 읽지 못했습니다")
        build_name, build_number = version
        if app_version and app_version != build_name:
            _write_app_version(workspace, app_version, build_number, log)
        else:
            app_version = build_name
        _report_phase(progress_url, project_id, "version", "success", f"{app_version}+{build_number}")

        phase = "build"
        _report_phase(progress_url, project_id, "build", "start")
        if not _run(["scripts/build_release.sh", environment], workspace, log):
            raise RuntimeError("build_release.sh 실패 — 로그 참고")
        _report_phase(progress_url, project_id, "build", "success")

        phase = "fastlane"
        _report_phase(progress_url, project_id, "fastlane", "start")
        lane = LANE_BY_PLATFORMS.get(frozenset(platforms))
        if not lane:
            raise RuntimeError(f"지원하지 않는 platforms 조합: {platforms}")
        if not _run(["bundle", "exec", "fastlane", lane, f"env:{environment}"], workspace, log):
            raise RuntimeError(f"fastlane {lane} 실패 — 로그 참고")
        _report_phase(progress_url, project_id, "fastlane", "success")

        final_version = _read_version(workspace)
        build_number = final_version[1] if final_version else build_number
        result["app_version"] = app_version
        result["build_number"] = build_number

        phase = "commit_push"
        _report_phase(progress_url, project_id, "commit_push", "start")
        commit_msg = f"chore: release {app_version}+{build_number}"
        push_warn = ""
        if _run(["git", "-C", workspace, "commit", "-am", commit_msg], workspace, log):
            if not _run(["git", "-C", workspace, "push"], workspace, log):
                push_warn = ("push 실패 — 스토어 업로드는 이미 끝났으니 배포는 성공으로 처리하고, "
                              f"버전 커밋({commit_msg})은 수동으로 push 해주세요.")
                log.append(f"[WARN] {push_warn}")
        _report_phase(progress_url, project_id, "commit_push", "success", push_warn)

        result["success"] = True

    except Exception as e:
        result["error"] = str(e)
        log.append(f"[ERROR] {e}")
        _report_phase(progress_url, project_id, phase, "fail", str(e))

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
