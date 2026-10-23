from qrobot.notifications import DBusNotificationParser


def test_parse_qq_notification():
    parser = DBusNotificationParser(("QQ",))
    lines = [
        "method call time=1 sender=:1.2 -> destination=:1.3 serial=1 path=/org/freedesktop/Notifications; interface=org.freedesktop.Notifications; member=Notify",
        '   string "QQ"',
        "   uint32 0",
        '   string "qq"',
        '   string "测试群"',
        '   string "小明：/ai 你好"',
        "   array [",
        "   ]",
        "   array [",
        "   ]",
        "   int32 -1",
    ]
    result = None
    for line in lines:
        result = parser.feed_line(line) or result
    assert result is not None
    assert result.conversation == "测试群"
    assert result.sender == "小明"
    assert result.text == "/ai 你好"


def test_ignore_other_app():
    parser = DBusNotificationParser(("QQ",))
    lines = [
        "method call sender=:1; interface=x; member=Notify",
        '   string "Mail"',
        "   uint32 0",
        '   string "mail"',
        '   string "Inbox"',
        '   string "message"',
        "   int32 -1",
    ]
    assert all(parser.feed_line(line) is None for line in lines)
