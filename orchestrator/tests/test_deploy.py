"""
회귀 테스트 — 스프린트 최하단 배포 카드가 쓰는 엔드포인트들.

  1. update_deploy_config — 부분 업데이트가 기존 값을 덮어쓰지 않고 merge되는지,
     environment/platforms 검증이 잘못된 값을 거부하는지.
  2. trigger_deploy — release 미완료/중복 실행/host_workspace_path 미설정을
     각각 거부하는지, 정상 트리거 시 deploy_status가 running으로 바뀌고
     deploy_runner에 POST가 나가는지, deploy_runner에 연결 실패하면 502로
     실패 처리하는지.
  3. deploy_callback — 성공/실패 결과가 deploy_status에 반영되는지.
  4. _make_project_info — deploy_config/deploy_status가 항상 실리는지(기본값 포함).

실행: cd orchestrator && pytest tests/test_deploy.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from workflows.pipeline import Pipeline, StageStatus


async def _noop(*args, **kwargs):
    pass


def _project_with_release(status: StageStatus, pid: str = "p1") -> Pipeline:
    p = Pipeline(pid, instruction="테스트")
    p.mark_completed("planning", {})
    p.stages["design"].approved = True
    p.mark_completed("design", {})
    p.stages["implement"].approved = True
    p.mark_completed("implement", {})
    p.mark_completed("qa", {})
    p.stages["autotest"].approved = True
    p.mark_completed("autotest", {})
    p.stages["release"].approved = True
    if status == StageStatus.COMPLETED:
        p.mark_completed("release", {})
    else:
        p.stages["release"].status = status
    return p


@pytest.fixture(autouse=True)
def _stub_side_effects(monkeypatch):
    monkeypatch.setattr(main, "broadcast", _noop)
    monkeypatch.setattr(main, "_save_project", lambda pid: None)
    monkeypatch.setattr(main, "_add_history", _noop)
    monkeypatch.setattr(main, "project_deploy_config", {})
    monkeypatch.setattr(main, "project_deploy_status", {})


# ── update_deploy_config ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_deploy_config_partial_merge(monkeypatch):
    monkeypatch.setattr(main, "projects", {"p1": Pipeline("p1", "x")})
    monkeypatch.setattr(main, "project_deploy_config", {"p1": {"app_name": "GoodEnough", "app_version": "1.0.0"}})

    result = await main.update_deploy_config("p1", main.DeployConfigUpdate(app_version="1.0.1"))

    assert result["deploy_config"] == {"app_name": "GoodEnough", "app_version": "1.0.1"}


@pytest.mark.asyncio
async def test_update_deploy_config_rejects_bad_environment(monkeypatch):
    monkeypatch.setattr(main, "projects", {"p1": Pipeline("p1", "x")})

    with pytest.raises(main.HTTPException) as exc_info:
        await main.update_deploy_config("p1", main.DeployConfigUpdate(environment="staging"))

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_update_deploy_config_rejects_bad_platforms(monkeypatch):
    monkeypatch.setattr(main, "projects", {"p1": Pipeline("p1", "x")})

    with pytest.raises(main.HTTPException) as exc_info:
        await main.update_deploy_config("p1", main.DeployConfigUpdate(platforms=["windows"]))

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_update_deploy_config_rejects_empty_platforms(monkeypatch):
    monkeypatch.setattr(main, "projects", {"p1": Pipeline("p1", "x")})

    with pytest.raises(main.HTTPException) as exc_info:
        await main.update_deploy_config("p1", main.DeployConfigUpdate(platforms=[]))

    assert exc_info.value.status_code == 400


# ── trigger_deploy ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trigger_deploy_requires_release_completed(monkeypatch):
    p = _project_with_release(StageStatus.PENDING)
    monkeypatch.setattr(main, "projects", {"p1": p})
    monkeypatch.setattr(main, "project_deploy_config", {"p1": {"host_workspace_path": "/tmp/app"}})

    with pytest.raises(main.HTTPException) as exc_info:
        await main.trigger_deploy("p1")

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_trigger_deploy_rejects_duplicate_run(monkeypatch):
    p = _project_with_release(StageStatus.COMPLETED)
    monkeypatch.setattr(main, "projects", {"p1": p})
    monkeypatch.setattr(main, "project_deploy_config", {"p1": {"host_workspace_path": "/tmp/app"}})
    monkeypatch.setattr(main, "project_deploy_status", {"p1": {"status": "running"}})

    with pytest.raises(main.HTTPException) as exc_info:
        await main.trigger_deploy("p1")

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_trigger_deploy_requires_workspace_path(monkeypatch):
    p = _project_with_release(StageStatus.COMPLETED)
    monkeypatch.setattr(main, "projects", {"p1": p})
    monkeypatch.setattr(main, "project_deploy_config", {"p1": {}})

    with pytest.raises(main.HTTPException) as exc_info:
        await main.trigger_deploy("p1")

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_trigger_deploy_success_posts_to_runner_and_marks_running(monkeypatch):
    p = _project_with_release(StageStatus.COMPLETED)
    monkeypatch.setattr(main, "projects", {"p1": p})
    monkeypatch.setattr(main, "project_deploy_config", {
        "p1": {"host_workspace_path": "/tmp/app", "environment": "test", "platforms": ["ios"], "app_version": "1.0.1"},
    })

    posted = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            posted["url"] = url
            posted["json"] = json
            return _FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: _FakeClient())

    result = await main.trigger_deploy("p1")

    assert result == {"ok": True}
    assert main.project_deploy_status["p1"]["status"] == "running"
    assert posted["url"] == f"{main.DEPLOY_RUNNER_URL}/run"
    assert posted["json"]["workspace"] == "/tmp/app"
    assert posted["json"]["environment"] == "test"
    assert posted["json"]["platforms"] == ["ios"]
    assert posted["json"]["app_version"] == "1.0.1"


@pytest.mark.asyncio
async def test_trigger_deploy_runner_unreachable_marks_failed(monkeypatch):
    p = _project_with_release(StageStatus.COMPLETED)
    monkeypatch.setattr(main, "projects", {"p1": p})
    monkeypatch.setattr(main, "project_deploy_config", {"p1": {"host_workspace_path": "/tmp/app"}})

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            raise ConnectionError("connection refused")

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: _FakeClient())

    with pytest.raises(main.HTTPException) as exc_info:
        await main.trigger_deploy("p1")

    assert exc_info.value.status_code == 502
    assert main.project_deploy_status["p1"]["status"] == "failed"


# ── deploy_callback ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deploy_callback_unknown_project_404(monkeypatch):
    monkeypatch.setattr(main, "projects", {})

    with pytest.raises(main.HTTPException) as exc_info:
        await main.deploy_callback(main.DeployCallback(project_id="ghost", success=True))

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_deploy_callback_success_updates_status(monkeypatch):
    monkeypatch.setattr(main, "projects", {"p1": Pipeline("p1", "x")})
    monkeypatch.setattr(main, "project_deploy_status", {"p1": {"status": "running", "started_at": 123}})

    await main.deploy_callback(main.DeployCallback(
        project_id="p1", success=True, app_version="1.0.1", build_number="22", log_tail="ok",
    ))

    st = main.project_deploy_status["p1"]
    assert st["status"] == "success"
    assert st["started_at"] == 123
    assert st["app_version"] == "1.0.1"
    assert st["build_number"] == "22"


@pytest.mark.asyncio
async def test_deploy_callback_failure_updates_status(monkeypatch):
    monkeypatch.setattr(main, "projects", {"p1": Pipeline("p1", "x")})
    monkeypatch.setattr(main, "project_deploy_status", {"p1": {"status": "running", "started_at": 123}})

    await main.deploy_callback(main.DeployCallback(
        project_id="p1", success=False, error="build_release.sh failed", log_tail="...",
    ))

    st = main.project_deploy_status["p1"]
    assert st["status"] == "failed"
    assert st["error"] == "build_release.sh failed"


# ── _make_project_info ──────────────────────────────────────────────

def test_make_project_info_includes_deploy_defaults(monkeypatch):
    monkeypatch.setattr(main, "projects", {"p1": Pipeline("p1", "x")})
    monkeypatch.setattr(main, "project_deploy_config", {})
    monkeypatch.setattr(main, "project_deploy_status", {})

    info = main._make_project_info("p1")

    assert info["deploy_config"] == {}
    assert info["deploy_status"] == {"status": "idle"}


def test_make_project_info_includes_stored_deploy_state(monkeypatch):
    monkeypatch.setattr(main, "projects", {"p1": Pipeline("p1", "x")})
    monkeypatch.setattr(main, "project_deploy_config", {"p1": {"app_name": "GoodEnough"}})
    monkeypatch.setattr(main, "project_deploy_status", {"p1": {"status": "success", "build_number": "22"}})

    info = main._make_project_info("p1")

    assert info["deploy_config"] == {"app_name": "GoodEnough"}
    assert info["deploy_status"] == {"status": "success", "build_number": "22"}
