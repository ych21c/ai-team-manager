"""
회귀 테스트 — QA stage_completed 이벤트 유실 사고.

실제로 있었던 사고: counter-app 프로젝트의 QA 에이전트가 needs_rework 이벤트를
보냈지만, orchestrator의 event_loop가 그 이벤트를 처리하던 도중 죽었다 재연결
했다. 그 이벤트는 이미 consumer "orchestrator"에게 배달된(pending) 상태였는데,
poll_events가 "> "(새 메시지)만 읽어서 다시는 안 잡혔고, qa 스테이지가
"running"에 영원히 멈춰 자동 재작업 요청(_retry_implement_with_feedback)이
평생 발동되지 않았다.

실행 (Redis에 붙을 수 있는 곳에서, 예: docker compose exec orchestrator):
  pytest tests/test_redis_events.py -v
"""
import os
import sys
import uuid

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from redis_queue.redis_client import RedisQueue

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def isolated_stream():
    """production 스트림(orchestrator:events)을 안 건드리도록 테스트 전용
    스트림 이름을 매번 새로 발급하고, 끝나면 지운다."""
    stream = f"test:events:{uuid.uuid4().hex[:8]}"
    yield stream
    q = RedisQueue(REDIS_URL)
    r = await q.client()
    await r.delete(stream)
    await q.close()


async def test_new_event_is_delivered_normally(isolated_stream):
    q = RedisQueue(REDIS_URL)
    await q.ensure_events_group(stream=isolated_stream)
    await q.emit_event({"type": "stage_completed", "stage": "qa"}, stream=isolated_stream)

    events = await q.poll_events(stream=isolated_stream)
    assert len(events) == 1
    assert events[0]["stage"] == "qa"
    await q.close()


async def test_unacked_event_is_redelivered_after_reconnect(isolated_stream):
    """핵심 회귀 테스트: 이벤트를 읽었지만(claim) ack을 하지 않은 채(=처리 도중
    죽은 것을 흉내) 커넥션을 새로 맺어도, 다음 poll에서 그 이벤트가 다시
    나와야 한다. 고치기 전에는 여기서 events가 빈 리스트가 나와 실패했다."""
    q1 = RedisQueue(REDIS_URL)
    await q1.ensure_events_group(stream=isolated_stream)
    await q1.emit_event(
        {"type": "stage_completed", "project_id": "30dcf5ed", "agent": "qa",
         "stage": "qa", "outputs": {"passed": False, "needs_rework": True}},
        stream=isolated_stream,
    )

    # event_loop가 이 이벤트를 xreadgroup(">")으로 claim했지만 처리 중 예외로
    # ack_event를 못 부른 상황을 흉내낸다.
    first_read = await q1.poll_events(stream=isolated_stream)
    assert len(first_read) == 1
    # 일부러 ack 안 함 — 크래시 시뮬레이션.

    # event_loop의 except 블록: reset_connection() 후 재시도.
    await q1.reset_connection()

    q2 = RedisQueue(REDIS_URL)
    redelivered = await q2.poll_events(stream=isolated_stream)
    assert len(redelivered) == 1
    assert redelivered[0]["project_id"] == "30dcf5ed"
    assert redelivered[0]["outputs"]["needs_rework"] is True

    # 이제 정상적으로 ack하면 더 이상 재전달되지 않아야 한다.
    await q2.ack_event(redelivered[0]["_msg_id"], stream=isolated_stream)
    settled = await q2.poll_events(stream=isolated_stream)
    assert settled == []

    await q1.close()
    await q2.close()
