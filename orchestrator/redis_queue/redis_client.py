"""
Redis Streams 기반 메시지 큐.
에이전트 → 오케스트레이터 / 오케스트레이터 → 에이전트 통신.
"""
import json
import asyncio
import redis.asyncio as aioredis
from typing import AsyncIterator

STREAM_AGENT_INBOX        = "agent:{name}:inbox"              # 전역 공유 에이전트 (implement/autotest)
STREAM_AGENT_INBOX_SCOPED = "agent:{name}:{project_id}:inbox"  # 프로젝트별 격리 에이전트 (pm/designer/architect/qa/release)
STREAM_EVENTS      = "orchestrator:events"  # 에이전트 → 오케스트레이터 (모든 이벤트)
CONSUMER_GROUP     = "orchestrator-group"


class RedisQueue:
    def __init__(self, url: str):
        self.url = url
        self._client: aioredis.Redis | None = None

    async def reset_connection(self):
        """커넥션 에러 이후 재사용을 위해 캐시된 클라이언트를 버린다 — client()가
        끊어진 커넥션을 계속 재사용하지 않고 다음 호출에서 새로 맺게 한다."""
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass
        self._client = None

    async def client(self) -> aioredis.Redis:
        if self._client is None:
            self._client = await aioredis.from_url(self.url, decode_responses=True)
        return self._client

    # ── 오케스트레이터 → 에이전트 ───────────────────────────────────
    async def send_task(self, agent_name: str, project_id: str | None, task: dict) -> str:
        """project_id가 None이면 전역 공유 큐(implement/autotest)로, 아니면
        프로젝트별로 격리된 큐(TeamSpawner가 spawn한 pm/designer/architect/qa/release)로 보낸다."""
        r = await self.client()
        stream = (
            STREAM_AGENT_INBOX.format(name=agent_name)
            if project_id is None
            else STREAM_AGENT_INBOX_SCOPED.format(name=agent_name, project_id=project_id)
        )
        msg_id = await r.xadd(stream, {"payload": json.dumps(task)})
        return msg_id

    # ── 에이전트 → 오케스트레이터 ───────────────────────────────────
    async def emit_event(self, event: dict, stream: str = STREAM_EVENTS) -> str:
        r = await self.client()
        msg_id = await r.xadd(stream, {"payload": json.dumps(event)})
        return msg_id

    # ── 오케스트레이터: 에이전트 이벤트 수신 (컨슈머 그룹) ───────────
    # 예전엔 last_id="$"로 매번 새로 읽어서, orchestrator가 재시작되는 그
    # 짧은 순간에 에이전트가 emit한 이벤트(예: stage_completed)를 영영
    # 놓치는 문제가 있었다 — 실제로 PM이 응답을 다 만들어놓고도 orchestrator가
    # 그 순간 재시작 중이라 파이프라인이 영원히 "running"에 멈춰있던 사고가
    # 있었음. 컨슈머 그룹 + ack으로 바꿔서 재시작해도 안 놓치게 한다.
    async def ensure_events_group(self, stream: str = STREAM_EVENTS):
        r = await self.client()
        try:
            await r.xgroup_create(name=stream, groupname=CONSUMER_GROUP, id="$", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def poll_events(self, consumer: str = "orchestrator", stream: str = STREAM_EVENTS) -> list[dict]:
        """새 이벤트("> ")뿐 아니라 이 consumer에게 이미 배달됐지만 ack이 안 된
        pending 이벤트("0")도 같이 읽는다. handle_agent_event가 처리 도중
        예외를 던지면(예: Jira API 에러) event_loop의 바깥 try/except가 잡아서
        재연결만 하고 넘어가는데, 그 이벤트는 ack이 안 된 채 이 consumer 이름으로
        영원히 pending 상태에 남아 "> " 읽기로는 다시는 안 잡힌다 — 실제로
        QA stage_completed 이벤트가 이렇게 유실돼 파이프라인이 "running"에
        영원히 멈춘 사고가 있었다. 매 poll마다 pending도 같이 읽어서 재시도되게 한다."""
        r = await self.client()
        events = []
        for start_id in ("0", ">"):
            results = await r.xreadgroup(
                CONSUMER_GROUP, consumer, {stream: start_id},
                block=None if start_id == "0" else 100, count=50,
            )
            for _, messages in results:
                for msg_id, fields in messages:
                    data = json.loads(fields["payload"])
                    data["_msg_id"] = msg_id
                    events.append(data)
        return events

    async def ack_event(self, msg_id: str, stream: str = STREAM_EVENTS):
        r = await self.client()
        await r.xack(stream, CONSUMER_GROUP, msg_id)

    # ── 에이전트: 자신의 inbox에서 task 수신 ────────────────────────
    async def receive_task(self, agent_name: str, block_ms: int = 500) -> dict | None:
        r = await self.client()
        stream = STREAM_AGENT_INBOX.format(name=agent_name)
        # 그룹 없이 단순 xread (에이전트당 1개 인스턴스 가정)
        results = await r.xread({stream: ">"}, block=block_ms, count=1)
        if not results:
            return None
        _, messages = results[0]
        msg_id, fields = messages[0]
        data = json.loads(fields["payload"])
        data["_msg_id"] = msg_id
        return data

    async def close(self):
        if self._client:
            await self._client.aclose()
