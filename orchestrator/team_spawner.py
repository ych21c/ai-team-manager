"""
TeamSpawner — 프로젝트 생성 시 Docker API로 에이전트 컨테이너 세트를 동적 생성.

각 프로젝트마다 격리된 팀:
  agent-pm-{project_id}
  agent-designer-{project_id}
  agent-architect-{project_id}
  agent-qa-{project_id}
  agent-autotest-{project_id}
  agent-release-{project_id}

Implement(OpenDevin)은 무거우므로 공유 인스턴스 사용.
"""
import os
import docker
from docker.errors import DockerException, NotFound

REDIS_URL      = os.getenv("REDIS_URL", "redis://redis:6379")
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")   # pm/designer/architect/release가 자기 산출물을 직접 커밋/푸시할 때 씀
AGENT_IMAGE    = "ai-dev-team-agent-pm"   # 공통 이미지 (모든 에이전트 동일)
NETWORK_NAME   = "ai-dev-team_dev-team-net"
WORKSPACE_VOL  = "ai-dev-team_shared-workspace"

AGENT_ROLES = {
    "pm":        "Product Manager",
    "designer":  "UX Designer",
    "architect": "Software Architect",
    "release":   "Release Manager",
    # implement(OpenHands)/autotest(GitHub CI 폴러)/qa(Firebase Test Lab, Flutter+gcloud)는
    # 여기서 스폰하지 않는다. docker-compose의 정적 agent-* 싱글턴 서비스가 모든 프로젝트를 처리한다.
}


class TeamSpawner:
    def __init__(self):
        try:
            self.client = docker.from_env()
        except DockerException:
            self.client = None
            print("[spawner] Docker 소켓 연결 실패 — 팀 스폰 비활성화")

    def _container_name(self, agent: str, project_id: str) -> str:
        # 컨테이너명에 허용되는 문자만 사용
        safe_id = project_id.replace(" ", "-").lower()[:20]
        return f"agent-{agent}-{safe_id}"

    def spawn_team(self, project_id: str, anthropic_api_key: str) -> dict[str, str]:
        """프로젝트용 에이전트 팀 생성. 이미 존재하면 재사용."""
        if not self.client:
            return {}

        spawned = {}
        for agent_name, agent_role in AGENT_ROLES.items():
            container_name = self._container_name(agent_name, project_id)

            # 이미 실행 중이면 재사용
            try:
                c = self.client.containers.get(container_name)
                if c.status != "running":
                    c.start()
                spawned[agent_name] = container_name
                print(f"[spawner] 재사용: {container_name}")
                continue
            except NotFound:
                pass

            # 새 컨테이너 생성
            try:
                c = self.client.containers.run(
                    image=AGENT_IMAGE,
                    name=container_name,
                    detach=True,
                    restart_policy={"Name": "unless-stopped"},
                    environment={
                        "AGENT_NAME":        agent_name,
                        "AGENT_ROLE":        agent_role,
                        "REDIS_URL":         REDIS_URL,
                        "ANTHROPIC_API_KEY": anthropic_api_key,
                        "GITHUB_TOKEN":      GITHUB_TOKEN,
                        "PROJECT_ID":        project_id,    # 이 팀은 이 프로젝트 전용
                        "PYTHONUNBUFFERED":  "1",
                    },
                    network=NETWORK_NAME,
                    volumes={
                        WORKSPACE_VOL: {"bind": "/workspace", "mode": "rw"},
                    },
                )
                spawned[agent_name] = container_name
                print(f"[spawner] 생성: {container_name} ({c.short_id})")
            except Exception as e:
                print(f"[spawner] 에러 {container_name}: {e}")

        return spawned

    def stop_team(self, project_id: str):
        """프로젝트 팀 컨테이너 중지 및 삭제."""
        if not self.client:
            return
        for agent_name in AGENT_ROLES:
            container_name = self._container_name(agent_name, project_id)
            try:
                c = self.client.containers.get(container_name)
                c.stop(timeout=5)
                c.remove()
                print(f"[spawner] 삭제: {container_name}")
            except NotFound:
                pass
            except Exception as e:
                print(f"[spawner] 삭제 에러 {container_name}: {e}")

    def pause_team(self, project_id: str):
        """프로젝트 팀 컨테이너를 삭제하지 않고 정지만 한다 ('인액티브' 버튼용).
        산출물/큐는 그대로 남아있어 나중에 재개하면 spawn_team의 재사용 로직이
        같은 컨테이너를 다시 시작시켜준다."""
        if not self.client:
            return
        for agent_name in AGENT_ROLES:
            container_name = self._container_name(agent_name, project_id)
            try:
                c = self.client.containers.get(container_name)
                if c.status == "running":
                    c.stop(timeout=5)
                    print(f"[spawner] 일시정지: {container_name}")
            except NotFound:
                pass
            except Exception as e:
                print(f"[spawner] 일시정지 에러 {container_name}: {e}")

    def list_team(self, project_id: str) -> dict[str, str]:
        """프로젝트 팀 컨테이너 상태 조회."""
        if not self.client:
            return {}
        result = {}
        for agent_name in AGENT_ROLES:
            container_name = self._container_name(agent_name, project_id)
            try:
                c = self.client.containers.get(container_name)
                result[agent_name] = c.status
            except NotFound:
                result[agent_name] = "not_found"
        return result
