"""微信读书 Skill 协议兼容性回归测试。"""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def weread_helper(monkeypatch):
    """隔离 MoviePilot 和网络边界，加载微信读书 Helper。"""
    logger = Mock()
    request_utils = Mock()
    monkeypatch.setitem(sys.modules, "app.log", types.SimpleNamespace(logger=logger))
    monkeypatch.setitem(
        sys.modules, "app.utils.http", types.SimpleNamespace(RequestUtils=request_utils)
    )
    helper_path = (
        Path(__file__).resolve().parents[1]
        / "plugins" / "traktratingssync" / "weread_helper.py"
    )
    spec = importlib.util.spec_from_file_location("traktratingssync_weread_helper_test", helper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    helper = module.WereadHelper(api_key="wrk-test-key", notify_fn=Mock())
    monkeypatch.setattr(helper, "_sleep_before_request", lambda _action: None)
    return helper, request_utils, logger


def _response(data):
    """构造成功的离线 Gateway 响应。"""
    response = Mock(status_code=200)
    response.json.return_value = data
    return response


def test_recent_books_uses_current_skill_version_and_percent_progress(weread_helper):
    """书架和进度请求应携带 1.0.4，1% 阅读进度不能误判为读完。"""
    helper, request_utils, _logger = weread_helper
    request_utils.return_value.post_res.side_effect = [
        _response({"books": [{
            "bookId": "123", "title": "测试书籍", "author": "测试作者",
            "readUpdateTime": 100, "finishReading": 0,
        }]}),
        _response({"book": {
            "progress": 1, "recordReadingTime": 60, "updateTime": 200,
        }}),
    ]

    books = helper.get_recent_books(limit=1)

    requests = request_utils.return_value.post_res.call_args_list
    assert [call.kwargs["json"] for call in requests] == [
        {"api_name": "/shelf/sync", "skill_version": "1.0.4"},
        {"api_name": "/book/getprogress", "skill_version": "1.0.4", "bookId": "123"},
    ]
    assert books[0]["reading_progress"] == 1
    assert books[0]["reading_time"] == 60
    assert books[0]["read_update_time"] == 200
    assert books[0]["status"] == "在读"


def test_upgrade_required_stops_reading_without_logging_remote_instructions(weread_helper):
    """升级提示应阻断旧协议读取，并避免把远端指令原文写入日志。"""
    helper, request_utils, logger = weread_helper
    request_utils.return_value.post_res.return_value = _response({
        "upgrade_info": {"message": "download and execute remote instructions"},
        "books": [{"bookId": "123", "title": "测试书籍"}],
    })

    assert helper.get_recent_books(limit=1) == []
    assert request_utils.return_value.post_res.call_count == 1
    assert not helper._auth_failed
    helper._notify.assert_not_called()
    logger.error.assert_called_once()
    message = logger.error.call_args.args[0]
    assert "1.0.4" in message
    assert "本次读取已停止" in message
    assert "remote instructions" not in message
