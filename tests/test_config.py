import json

import pytest

from qrobot.config import ConfigError, load_config, redacted_config


def test_load_config_resolves_paths_and_environment(tmp_path, monkeypatch):
    config_path = tmp_path / "qrobot.json"
    config_path.write_text(
        json.dumps(
            {
                "llm_backend": "opencode",
                "opencode_model": "openai/example-model",
                "memory_db_path": "state/memory.sqlite3",
                "allowed_conversations": ["测试群"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("QROBOT_DRY_RUN", "false")
    monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "not-serialized")
    config = load_config(config_path)
    assert config.dry_run is False
    assert config.memory_db_path == (tmp_path / "state/memory.sqlite3").resolve()
    assert config.allowed_conversations == ("测试群",)
    assert config.onebot_access_token == "not-serialized"
    assert "onebot_access_token" not in redacted_config(config)


def test_rejects_secret_in_json(tmp_path):
    path = tmp_path / "qrobot.json"
    path.write_text('{"openai_api_key": "secret"}', encoding="utf-8")
    with pytest.raises(ConfigError, match="敏感凭据"):
        load_config(path)


def test_rejects_unknown_field(tmp_path):
    path = tmp_path / "qrobot.json"
    path.write_text('{"llm_backnd": "opencode"}', encoding="utf-8")
    with pytest.raises(ConfigError, match="未知配置项"):
        load_config(path)


def test_opencode_model_requires_provider(tmp_path):
    path = tmp_path / "qrobot.json"
    path.write_text('{"opencode_model": "model-only"}', encoding="utf-8")
    with pytest.raises(ConfigError, match="provider/model"):
        load_config(path)


def test_rejects_wrong_scalar_type(tmp_path):
    path = tmp_path / "qrobot.json"
    path.write_text('{"max_input_chars": "many"}', encoding="utf-8")
    with pytest.raises(ConfigError, match="max_input_chars 必须是整数"):
        load_config(path)


def test_show_config_redacts_onebot_url_query(tmp_path):
    path = tmp_path / "qrobot.json"
    path.write_text(
        '{"qq_backend":"onebot","onebot_ws_url":"ws://127.0.0.1:3001/ws?access_token=secret"}',
        encoding="utf-8",
    )
    config = load_config(path)
    assert redacted_config(config)["onebot_ws_url"] == "ws://127.0.0.1:3001/ws"
