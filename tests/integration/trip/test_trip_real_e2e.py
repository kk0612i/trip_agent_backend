"""使用真实 DeepSeek、高德和 MySQL 跑通一次完整对话：收集 → 草稿 → 局部修改 → 撤销 → 问答 → 确认。

需显式设置 ``RUN_TRIP_INTEGRATION=1`` 且 .env 中已配置 DeepSeek 与高德密钥；结束后删除本测试写入的用户和行程数据
（景点属性缓存 poi_profiles 是全局缓存，不删除）。单次运行约 40 秒～1 分钟。
"""

import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

import main
from app.agent.trip.trip_graph import TripTurnRunner
from app.core.config import get_settings
from app.core.db import create_db_engine
from app.schemas.trip_plan_schema import MAX_ITEMS_PER_DAY

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_TRIP_INTEGRATION") != "1",
    reason="需显式开启真实环境端到端验证（调用 DeepSeek、高德并写入 MySQL）",
)


def item_ids(plan: dict) -> list[list[str]]:
    """按天列出草稿中的景点标识。"""
    return [[item["poi_id"] for item in day["items"]] for day in plan["days"]]


@pytest.mark.asyncio
async def test_real_trip_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实依赖下的多轮对话：每天不超过 5 个景点、景点带评分和图片、局部修改只动指定的天。"""
    settings = get_settings().model_copy(update={
        "app_env": "test", "log_format": "json", "log_enqueue": False, "log_file_enabled": False,
    })
    if not (settings.deepseek_api_key.get_secret_value() and settings.amap_api_key.get_secret_value()):
        pytest.skip("未配置 DeepSeek 或高德密钥")
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    cleanup_engine = create_db_engine(settings)
    email = f"trip-e2e-{uuid4().hex[:12]}@example.com"
    try:
        app = main.create_app()
        async with app.router.lifespan_context(app):
            assert isinstance(app.state.turn_runner, TripTurnRunner)
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://real-e2e",
                timeout=180,
            ) as client:
                credentials = {"email": email, "password": "integration-secret"}
                assert (await client.post("/api/auth/register", json=credentials)).status_code == 201
                token = (await client.post("/api/auth/login", json=credentials)).json()["access_token"]
                headers = {"Authorization": f"Bearer {token}"}
                trip_id = (await client.post("/api/trips/", headers=headers)).json()["trip_id"]
                counter = iter(range(1, 100))

                async def send(content: str) -> dict:
                    response = await client.post(
                        f"/api/trips/{trip_id}/messages", headers=headers,
                        json={"turn_id": f"e2e-{next(counter):04d}", "content": content},
                    )
                    assert response.status_code == 200, response.text
                    return response.json()

                # 1. 信息不全时追问
                asked = await send("我想去杭州玩")
                assert asked["status"] == "collecting" and asked["plan_changed"] is False

                # 2. 补全信息后生成第一版草稿
                first = await send("下周五出发玩2天，喜欢历史古迹和自然风光")
                assert first["status"] == "drafting" and first["plan_changed"] is True, first["reply"]
                plan = first["plan"]
                assert len(plan["days"]) == 2
                for day in plan["days"]:
                    assert 1 <= len(day["items"]) <= MAX_ITEMS_PER_DAY
                    for item in day["items"]:
                        assert item["rating"] is not None
                        assert all(photo["url"].startswith("https://") for photo in item["photos"])
                assert any(item["photos"] for day in plan["days"] for item in day["items"])
                v1 = item_ids(plan)

                # 3. 局部删除：第二天保持不变，第一天不再包含被删除的景点
                removed = v1[0][0]
                edited = await send("删掉第一天第一个景点")
                assert edited["plan_changed"] is True, edited["reply"]
                v2 = item_ids(edited["plan"])
                assert removed not in v2[0] and v2[1] == v1[1]

                # 4. 撤销回到第一版
                undone = await send("撤销")
                assert undone["plan_changed"] is True and item_ids(undone["plan"]) == v1

                # 5. 问答不改动草稿
                answered = await send("灵隐寺需要预约吗")
                assert answered["plan_changed"] is False and answered["reply"]
                assert answered["draft_version"] == undone["draft_version"]

                # 6. 确认后不能再修改
                confirmed = await client.post(
                    f"/api/trips/{trip_id}/confirm", headers=headers,
                    json={"draft_version": undone["draft_version"]},
                )
                assert confirmed.status_code == 200
                after = await send("改成三天")
                assert after["status"] == "confirmed" and after["plan_changed"] is False
                detail = (await client.get(f"/api/trips/{trip_id}", headers=headers)).json()
                assert item_ids(detail["plan"]) == v1
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
        await cleanup_engine.dispose()
        get_settings.cache_clear()
