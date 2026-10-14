"""通过 HTTP 接口验证行程会话的创建、对话、版本与确认行为。"""

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.trip import Trip
from app.models.trip_plan_version import TripPlanVersion
from app.schemas.trip_schema import TripContext, TurnOutcome
from tests.trip.trip_session_fakes import (
    SUZHOU, BlockingHandler, FakeTurnRunner, make_plan, make_requirements,
)


async def create_trip(client: AsyncClient, headers: dict[str, str]) -> int:
    """创建行程并返回标识。"""
    response = await client.post("/api/trips/", headers=headers)
    assert response.status_code == 201
    return response.json()["trip_id"]


async def send(
    client: AsyncClient, headers: dict[str, str], trip_id: int, turn_id: str, content: str,
):
    """发送一条消息并返回原始响应。"""
    return await client.post(
        f"/api/trips/{trip_id}/messages", headers=headers, json={"turn_id": turn_id, "content": content},
    )


async def get_trip_row(session_factory: async_sessionmaker[AsyncSession], trip_id: int) -> Trip:
    """直接读取数据库中的行程行。"""
    async with session_factory() as session:
        return await session.get(Trip, trip_id)


@pytest.mark.asyncio
async def test_create_list_and_get_trip(client: AsyncClient, headers: dict[str, str]) -> None:
    """新行程处于收集状态，列表按标识倒序，详情包含空需求。"""
    first = await client.post("/api/trips/", headers=headers)
    second_id = await create_trip(client, headers)

    assert first.status_code == 201
    detail = first.json()
    assert detail["status"] == "collecting"
    assert detail["plan"] is None and detail["draft_version"] is None
    assert detail["requirements"]["city"] is None
    assert detail["created_at"].endswith("Z")
    listed = await client.get("/api/trips/", headers=headers)
    assert [item["trip_id"] for item in listed.json()] == [second_id, detail["trip_id"]]
    fetched = await client.get(f"/api/trips/{second_id}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["trip_id"] == second_id
    turns = await client.get(f"/api/trips/{second_id}/turns", headers=headers)
    assert turns.json() == []


@pytest.mark.asyncio
async def test_trip_requires_login(client: AsyncClient) -> None:
    """未登录访问行程接口返回 401。"""
    response = await client.get("/api/trips/")
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_TOKEN"


@pytest.mark.asyncio
async def test_other_users_trip_is_not_found(
    client: AsyncClient, headers: dict[str, str],
    login: Callable[[str], Awaitable[dict[str, str]]], fake_runner: FakeTurnRunner,
) -> None:
    """其他用户的行程与不存在的行程返回相同的 404，且不会执行图。"""
    trip_id = await create_trip(client, headers)
    other = await login("other@example.com")

    for response in (
        await client.get(f"/api/trips/{trip_id}", headers=other),
        await client.get(f"/api/trips/{trip_id}/turns", headers=other),
        await send(client, other, trip_id, "turn-other-1", "规划"),
        await client.post(f"/api/trips/{trip_id}/confirm", headers=other, json={"draft_version": 1}),
        await client.get("/api/trips/999999", headers=headers),
    ):
        assert response.status_code == 404
        assert response.json() == {"code": "TRIP_NOT_FOUND", "message": "行程不存在", "errors": []}
    assert (await client.get("/api/trips/", headers=other)).json() == []
    assert fake_runner.calls == []


@pytest.mark.asyncio
async def test_first_draft_locks_city_and_edits_append_versions(
    client: AsyncClient, headers: dict[str, str], fake_runner: FakeTurnRunner,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """首次草稿使行程进入 drafting 并记录城市，修改追加新版本并提供上一版本用于撤销。"""
    trip_id = await create_trip(client, headers)

    first = await send(client, headers, trip_id, "turn-0001", "规划杭州两天")
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "drafting" and body["draft_version"] == 1 and body["plan_changed"] is True
    assert body["plan"]["warnings"] == ["v1"]
    detail = (await client.get(f"/api/trips/{trip_id}", headers=headers)).json()
    assert detail["city_name"] == "杭州" and detail["draft_version"] == 1
    assert detail["plan"]["city"]["adcode"] == "330100"

    second = await send(client, headers, trip_id, "turn-0002", "修改：轻松一点")
    assert second.json()["draft_version"] == 2
    context: TripContext = fake_runner.calls[-1][0]
    assert context.status == "drafting" and context.draft_version == 1
    assert context.draft.warnings == ["v1"] and context.previous_draft is None
    assert [turn.role for turn in context.recent_turns] == ["user", "assistant"]
    assert context.last_change_summary == "首次生成"

    await send(client, headers, trip_id, "turn-0003", "修改：再轻松一点")
    context = fake_runner.calls[-1][0]
    assert context.draft_version == 2 and context.previous_draft.warnings == ["v1"]

    row = await get_trip_row(session_factory, trip_id)
    assert row.current_draft_version == 3 and row.requirements["pace"] == "relaxed"
    assert row.lock_token is None and row.lock_expires_at is None
    async with session_factory() as session:
        versions = list(await session.scalars(
            select(TripPlanVersion).where(TripPlanVersion.trip_id == trip_id).order_by(TripPlanVersion.version),
        ))
    assert [(item.version, item.source_turn_id) for item in versions] == [
        (1, "turn-0001"), (2, "turn-0002"), (3, "turn-0003"),
    ]
    turns = (await client.get(f"/api/trips/{trip_id}/turns", headers=headers)).json()
    assert [(item["role"], item["draft_version"]) for item in turns] == [
        ("user", None), ("assistant", 1), ("user", None), ("assistant", 2), ("user", None), ("assistant", 3),
    ]
    assert turns[0]["content"] == "规划杭州两天"


@pytest.mark.asyncio
async def test_collecting_question_updates_requirements_and_pending(
    client: AsyncClient, headers: dict[str, str], session_factory: async_sessionmaker[AsyncSession],
    fake_runner: FakeTurnRunner,
) -> None:
    """收集阶段的追问保存需求、城市和待回答问题，下一轮回答后清除问题。"""
    trip_id = await create_trip(client, headers)

    asked = (await send(client, headers, trip_id, "turn-ask-1", "提问")).json()
    assert asked["status"] == "collecting" and asked["plan_changed"] is False
    assert asked["pending_question"] == {"kind": "missing_info", "text": "想去几天？", "options": ["两天", "三天"]}
    detail = (await client.get(f"/api/trips/{trip_id}", headers=headers)).json()
    assert detail["city_name"] == "杭州" and detail["pending_question"]["options"] == ["两天", "三天"]

    answered = (await send(client, headers, trip_id, "turn-ask-2", "两天吧")).json()
    assert fake_runner.calls[-1][0].pending.text == "想去几天？"
    assert answered["pending_question"] is None
    row = await get_trip_row(session_factory, trip_id)
    assert row.pending_question is None and row.requirements["pace"] == "packed"


@pytest.mark.asyncio
async def test_same_turn_id_is_replayed_without_running_again(
    client: AsyncClient, headers: dict[str, str], fake_runner: FakeTurnRunner,
) -> None:
    """同一 turn_id 重试返回已保存的回复，不重复执行图，也不重复写入消息。"""
    trip_id = await create_trip(client, headers)
    first = await send(client, headers, trip_id, "turn-same-1", "规划")
    await send(client, headers, trip_id, "turn-same-2", "修改")

    replay = await send(client, headers, trip_id, "turn-same-1", "规划")

    assert replay.status_code == 200
    assert len(fake_runner.calls) == 2
    assert replay.json()["reply"] == first.json()["reply"]
    assert replay.json()["plan_changed"] is True
    # 返回当前行程状态，而不是当时的状态。
    assert replay.json()["draft_version"] == 2
    turns = (await client.get(f"/api/trips/{trip_id}/turns", headers=headers)).json()
    assert len(turns) == 4


@pytest.mark.asyncio
async def test_concurrent_message_is_rejected_while_running(
    client: AsyncClient, headers: dict[str, str], fake_runner: FakeTurnRunner,
) -> None:
    """图执行期间同一行程的第二条消息返回 409，第一条完成后可继续对话。"""
    trip_id = await create_trip(client, headers)
    blocking = BlockingHandler()
    fake_runner.handler = blocking
    first = asyncio.create_task(send(client, headers, trip_id, "turn-busy-1", "规划"))
    await asyncio.wait_for(blocking.entered.wait(), 5)

    second = await send(client, headers, trip_id, "turn-busy-2", "修改")

    assert second.status_code == 409
    assert second.json()["code"] == "TRIP_BUSY"
    assert second.json()["message"] == "上一条消息仍在处理，请稍后再试"
    blocking.release.set()
    assert (await first).status_code == 200
    assert (await send(client, headers, trip_id, "turn-busy-2", "修改")).json()["draft_version"] == 2


@pytest.mark.asyncio
async def test_runner_failure_returns_503_and_releases_lock(
    client: AsyncClient, headers: dict[str, str], fake_runner: FakeTurnRunner,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """图执行异常返回 503，不写入对话记录，并释放租约。"""
    trip_id = await create_trip(client, headers)

    async def explode(context: TripContext, message: str) -> TurnOutcome:
        raise RuntimeError("private-model-detail")

    fake_runner.handler = explode
    response = await send(client, headers, trip_id, "turn-fail-1", "规划")

    assert response.status_code == 503
    assert response.json() == {
        "code": "TRIP_TURN_FAILED", "message": "行程规划服务暂时不可用，请稍后重试", "errors": [],
    }
    assert "private-model-detail" not in response.text
    row = await get_trip_row(session_factory, trip_id)
    assert row.lock_token is None
    assert (await client.get(f"/api/trips/{trip_id}/turns", headers=headers)).json() == []


@pytest.mark.asyncio
async def test_confirm_flow(client: AsyncClient, headers: dict[str, str]) -> None:
    """未生成草稿不可确认；版本不一致冲突；确认成功后重复确认幂等，其他版本冲突。"""
    trip_id = await create_trip(client, headers)
    url = f"/api/trips/{trip_id}/confirm"

    not_ready = await client.post(url, headers=headers, json={"draft_version": 1})
    assert not_ready.status_code == 409 and not_ready.json()["code"] == "TRIP_NOT_CONFIRMABLE"

    await send(client, headers, trip_id, "turn-confirm-1", "规划")
    await send(client, headers, trip_id, "turn-confirm-2", "修改")
    stale = await client.post(url, headers=headers, json={"draft_version": 1})
    assert stale.status_code == 409 and stale.json()["code"] == "DRAFT_VERSION_CONFLICT"

    confirmed = await client.post(url, headers=headers, json={"draft_version": 2})
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed" and confirmed.json()["confirmed_version"] == 2
    again = await client.post(url, headers=headers, json={"draft_version": 2})
    assert again.status_code == 200 and again.json() == confirmed.json()
    other = await client.post(url, headers=headers, json={"draft_version": 1})
    assert other.status_code == 409 and other.json()["code"] == "DRAFT_VERSION_CONFLICT"
    detail = (await client.get(f"/api/trips/{trip_id}", headers=headers)).json()
    assert detail["status"] == "confirmed" and detail["confirmed_version"] == 2
    assert detail["plan"]["warnings"] == ["v2"]

    invalid = await client.post(url, headers=headers, json={"draft_version": 0})
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_confirm_during_turn_discards_new_draft(
    client: AsyncClient, headers: dict[str, str], fake_runner: FakeTurnRunner,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """图执行期间行程被确认，新草稿被丢弃、需求不变，回复被替换。"""
    trip_id = await create_trip(client, headers)
    await send(client, headers, trip_id, "turn-race-1", "规划")
    blocking = BlockingHandler()
    fake_runner.handler = blocking
    pending = asyncio.create_task(send(client, headers, trip_id, "turn-race-2", "修改"))
    await asyncio.wait_for(blocking.entered.wait(), 5)

    confirmed = await client.post(f"/api/trips/{trip_id}/confirm", headers=headers, json={"draft_version": 1})
    assert confirmed.status_code == 200
    blocking.release.set()
    response = await pending

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "行程在处理期间已被确认或更新，本次修改未生效，请查看最新行程。"
    assert body["status"] == "confirmed" and body["draft_version"] == 1 and body["plan_changed"] is False
    row = await get_trip_row(session_factory, trip_id)
    assert row.current_draft_version == 1 and row.requirements["pace"] == "normal"
    assert row.lock_token is None
    turns = (await client.get(f"/api/trips/{trip_id}/turns", headers=headers)).json()
    assert turns[-1]["content"] == body["reply"] and turns[-1]["draft_version"] is None


@pytest.mark.asyncio
async def test_confirmed_trip_requirements_are_frozen(
    client: AsyncClient, headers: dict[str, str], fake_runner: FakeTurnRunner,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """确认后的普通对话不修改需求和城市，图收到 confirmed 状态。"""
    trip_id = await create_trip(client, headers)
    await send(client, headers, trip_id, "turn-frozen-1", "规划")
    await client.post(f"/api/trips/{trip_id}/confirm", headers=headers, json={"draft_version": 1})

    async def change_city(context: TripContext, message: str) -> TurnOutcome:
        return TurnOutcome(route="readonly", reply="行程已确认", requirements=make_requirements(city=SUZHOU))

    fake_runner.handler = change_city
    response = await send(client, headers, trip_id, "turn-frozen-2", "换成苏州")

    assert response.status_code == 200
    assert response.json()["status"] == "confirmed" and response.json()["plan_changed"] is False
    assert fake_runner.calls[-1][0].status == "confirmed"
    row = await get_trip_row(session_factory, trip_id)
    assert row.requirements["city"]["name"] == "杭州" and row.city_name == "杭州"
    assert row.status == "confirmed" and row.confirmed_version == 1


@pytest.mark.asyncio
async def test_new_plan_after_confirm_is_discarded(
    client: AsyncClient, headers: dict[str, str], fake_runner: FakeTurnRunner,
) -> None:
    """已确认的行程即使图返回新草稿也不会写入新版本。"""
    trip_id = await create_trip(client, headers)
    await send(client, headers, trip_id, "turn-after-1", "规划")
    await client.post(f"/api/trips/{trip_id}/confirm", headers=headers, json={"draft_version": 1})

    async def new_plan(context: TripContext, message: str) -> TurnOutcome:
        return TurnOutcome(
            route="edit", reply="已修改", requirements=context.requirements, new_plan=make_plan(warning="x"),
        )

    fake_runner.handler = new_plan
    body = (await send(client, headers, trip_id, "turn-after-2", "修改")).json()
    assert body["plan_changed"] is False and body["draft_version"] == 1
    assert body["plan"]["warnings"] == ["v1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"turn_id": "short", "content": "你好"},
    {"turn_id": "turn-valid-1", "content": ""},
    {"turn_id": "turn valid 1", "content": "你好"},
    {"turn_id": "turn-valid-1", "content": "你好", "user_id": 2},
])
async def test_invalid_message_request(
    client: AsyncClient, headers: dict[str, str], payload: dict, fake_runner: FakeTurnRunner,
) -> None:
    """消息请求格式不合法时返回 422，不执行图。"""
    trip_id = await create_trip(client, headers)
    response = await client.post(f"/api/trips/{trip_id}/messages", headers=headers, json=payload)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert fake_runner.calls == []
