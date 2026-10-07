"""应用启动和退出日志验证。"""

import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from main import create_app


def test_application_lifecycle(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """验证生命周期加载配置、正常启动和关闭，无外部服务依赖。"""
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_ENQUEUE", "false")
    app = create_app()
    with TestClient(app) as client:
        assert isinstance(app.state.settings, Settings)
        assert client.get("/openapi.json").status_code == 200
    payloads = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    lifecycle = [item for item in payloads if item["event"].startswith("application_")]
    assert [item["event"] for item in lifecycle] == ["application_started", "application_stopped"]
    assert lifecycle[0]["run_id"] == lifecycle[1]["run_id"]
    assert lifecycle[1]["duration_ms"] >= 0
    assert lifecycle[0]["entry_point"] == "api"
