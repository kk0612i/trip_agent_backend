"""在真实 MySQL 上验证行程会话完整流程，结束后删除本测试写入的全部数据。"""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

import main
from app.core.config import get_settings
from app.core.db import create_db_engine
from app.repository.poi_profile_repository import PoiProfileRepository
from app.repository.trip_turn_repository import TripTurnRepository
from app.schemas.trip_plan_schema import PoiProfile
from app.schemas.trip_schema import TripContext, TurnOutcome
from tests.trip.trip_session_fakes import FakeTurnRunner, make_requirements

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_TRIP_INTEGRATION") != "1",
    reason="需显式开启 MySQL 行程集成验证（会写入并清理真实表数据）",
)


@pytest.mark.asyncio
async def test_trip_flow_with_mysql(monkeypatch: pytest.MonkeyPatch) -> None:
    """创建 → 生成草稿 → 修改 → 确认 → 确认后再发消息，并验证幂等与唯一键冲突识别。"""
    settings = get_settings().model_copy(update={
        "app_env": "test", "log_format": "json", "log_enqueue": False, "log_file_enabled": False,
    })
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    cleanup_engine = create_db_engine(settings)
    email = f"trip-it-{uuid4().hex[:12]}@example.com"
    poi_id = f"TEST-{uuid4().hex[:16]}"
    runner = FakeTurnRunner()
    try:
        app = main.create_app()
        async with app.router.lifespan_context(app):
            app.state.turn_runner = runner
            session_factory = app.state.session_factory
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://mysql-integration",
            ) as client:
                credentials = {"email": email, "password": "integration-secret"}
                assert (await client.post("/api/auth/register", json=credentials)).status_code == 201
                token = (await client.post("/api/auth/login", json=credentials)).json()["access_token"]
                headers = {"Authorization": f"Bearer {token}"}

                created = await client.post("/api/trips/", headers=headers)
                assert created.status_code == 201
                trip_id = created.json()["trip_id"]
                assert created.json()["status"] == "collecting"
                assert datetime.fromisoformat(created.json()["created_at"]).utcoffset() == UTC.utcoffset(None)

                async def send(turn_id: str, content: str):
                    return await client.post(
                        f"/api/trips/{trip_id}/messages", headers=headers,
                        json={"turn_id": turn_id, "content": content},
                    )

                first = await send("it-turn-0001", "规划杭州两天")
                assert first.status_code == 200
                assert first.json()["status"] == "drafting" and first.json()["draft_version"] == 1
                replay = await send("it-turn-0001", "规划杭州两天")
                assert replay.json()["reply"] == first.json()["reply"] and len(runner.calls) == 1

                edited = await send("it-turn-0002", "修改：轻松一点")
                assert edited.json()["draft_version"] == 2 and edited.json()["plan"]["warnings"] == ["v2"]
                assert runner.calls[-1][0].draft.warnings == ["v1"]

                stale = await client.post(
                    f"/api/trips/{trip_id}/confirm", headers=headers, json={"draft_version": 1},
                )
                assert stale.status_code == 409 and stale.json()["code"] == "DRAFT_VERSION_CONFLICT"
                confirmed = await client.post(
                    f"/api/trips/{trip_id}/confirm", headers=headers, json={"draft_version": 2},
                )
                assert confirmed.status_code == 200 and confirmed.json()["confirmed_version"] == 2
                again = await client.post(
                    f"/api/trips/{trip_id}/confirm", headers=headers, json={"draft_version": 2},
                )
                assert again.json() == confirmed.json()

                async def change_city(context: TripContext, content: str) -> TurnOutcome:
                    return TurnOutcome(
                        route="readonly", reply="行程已确认，不能再修改",
                        requirements=make_requirements(days=5),
                    )

                runner.handler = change_city
                after = await send("it-turn-0003", "改成五天")
                assert after.status_code == 200
                assert after.json()["status"] == "confirmed" and after.json()["plan_changed"] is False

                # 同一轮次在执行期间已被另一请求保存：真实 MySQL 1062 错误按幂等重放处理。
                async def save_first(context: TripContext, content: str) -> TurnOutcome:
                    async with session_factory() as session, session.begin():
                        repo = TripTurnRepository(session)
                        now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
                        await repo.add(trip_id, "it-turn-0004", "user", content, None, now)
                        await repo.add(trip_id, "it-turn-0004", "assistant", "先保存的回复", None, now)
                    return TurnOutcome(route="chat", reply="后到的回复", requirements=context.requirements)

                runner.handler = save_first
                assert (await send("it-turn-0004", "你好")).json()["reply"] == "先保存的回复"

                detail = (await client.get(f"/api/trips/{trip_id}", headers=headers)).json()
                assert detail["status"] == "confirmed" and detail["city_name"] == "杭州"
                assert detail["requirements"]["days"] == 2 and detail["plan"]["warnings"] == ["v2"]
                turns = (await client.get(f"/api/trips/{trip_id}/turns", headers=headers)).json()
                assert [item["turn_id"] for item in turns] == [
                    "it-turn-0001", "it-turn-0001", "it-turn-0002", "it-turn-0002",
                    "it-turn-0003", "it-turn-0003", "it-turn-0004", "it-turn-0004",
                ]
                listed = (await client.get("/api/trips/", headers=headers)).json()
                assert [item["trip_id"] for item in listed] == [trip_id]

                async with session_factory() as session:
                    row = (await session.execute(text(
                        "SELECT lock_token, pending_question IS NULL FROM trips WHERE id = :id",
                    ), {"id": trip_id})).one()
                assert row == (None, 1)

                # MySQL 的 INSERT IGNORE 路径：重复写入保留先写入的记录。
                profile = PoiProfile(
                    poi_id=poi_id, subtype="museum", indoor="indoor", duration_minutes=120, source="llm",
                )
                async with session_factory() as session, session.begin():
                    repo = PoiProfileRepository(session)
                    await repo.insert_ignore([(profile, "测试景点")])
                    await repo.insert_ignore([(profile.model_copy(update={"duration_minutes": 30}), "测试景点")])
                async with session_factory() as session, session.begin():
                    assert await PoiProfileRepository(session).get_many([poi_id]) == {poi_id: profile}
    finally:
        async with cleanup_engine.begin() as connection:
            user_id = await connection.scalar(text("SELECT id FROM users WHERE email = :email"), {"email": email})
            if user_id is not None:
                trip_filter = "trip_id IN (SELECT id FROM trips WHERE user_id = :user_id)"
                await connection.execute(text(f"DELETE FROM trip_turns WHERE {trip_filter}"), {"user_id": user_id})
                await connection.execute(
                    text(f"DELETE FROM trip_plan_versions WHERE {trip_filter}"), {"user_id": user_id},
                )
                await connection.execute(text("DELETE FROM trips WHERE user_id = :user_id"), {"user_id": user_id})
                await connection.execute(text("DELETE FROM users WHERE id = :user_id"), {"user_id": user_id})
            await connection.execute(text("DELETE FROM poi_profiles WHERE poi_id = :poi_id"), {"poi_id": poi_id})
        await cleanup_engine.dispose()
        get_settings.cache_clear()
