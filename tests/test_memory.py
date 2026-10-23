from qrobot.memory import (
    MemoryStore,
    contains_sensitive_data,
    extract_explicit_facts,
    parse_memory_command,
)


def test_memory_crud_and_scope_isolation(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    try:
        created, content = store.remember("group:1", "user:1", "我喜欢猫")
        assert created and content == "我喜欢猫"
        assert store.remember("group:1", "user:1", "我喜欢猫")[0] is False
        assert store.list("group:1", "user:2") == []
        recalled = store.recall("group:1", "user:1", "推荐一种我喜欢的宠物")
        assert [item.content for item in recalled] == ["我喜欢猫"]
        assert store.forget("group:1", "user:1", "喜欢猫") == 1
        assert store.list("group:1", "user:1") == []
    finally:
        store.close()


def test_sensitive_memory_is_rejected(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    try:
        created, detail = store.remember("group:1", "user:1", "我的密码是 abc123")
        assert created is False
        assert "敏感" in detail
    finally:
        store.close()
    assert contains_sensitive_data("token: sk_abcdefghijklmnopqrstuvwxyz")
    assert contains_sensitive_data("我的手机号是 13800138000")


def test_memory_commands_and_auto_capture():
    assert parse_memory_command("记住：我喜欢猫").action == "remember"
    assert parse_memory_command("忘记: 我喜欢猫").action == "forget"
    assert parse_memory_command("我的记忆").action == "list"
    assert parse_memory_command("清空我的记忆").action == "clear"
    assert parse_memory_command("你记住了吗？") is None
    assert "我喜欢打羽毛球" in extract_explicit_facts("顺便说一下，我喜欢打羽毛球。")


def test_memory_record_limit_and_file_permissions(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path, max_records_per_user=2)
    try:
        store.remember("group:1", "user:1", "我喜欢猫")
        store.remember("group:1", "user:1", "我喜欢狗")
        store.remember("group:1", "user:1", "我喜欢鸟")
        assert len(store.list("group:1", "user:1")) == 2
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        store.close()
