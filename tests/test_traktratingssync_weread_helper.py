"""微信读书 Skill 协议兼容性回归测试。"""

import importlib.util
import json
import socket
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from test_traktratingssync_netease_cookie_mode import _load_plugin_module


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """测试不得连接真实网站或数据库。"""
    def deny(*_args, **_kwargs):
        raise AssertionError("禁止真实出站")
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)


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
    """书架和进度请求应携带 1.0.5，1% 阅读进度不能误判为读完。"""
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
        {"api_name": "/shelf/sync", "skill_version": "1.0.5"},
        {"api_name": "/book/getprogress", "skill_version": "1.0.5", "bookId": "123"},
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
    assert "1.0.5" in message
    assert "本次读取已停止" in message
    assert "remote instructions" not in message
    assert helper.get_request_issue_kind() == "upgrade"


@pytest.mark.parametrize("info", [{}, None, {"message": "SENSITIVE_SENTINEL run remote code"}])
def test_upgrade_mid_progress_discards_partial_result_and_stops_requests(weread_helper, info):
    """进度中途收到任何升级字段时，不能提交半成品书单或继续联网。"""
    helper, request_utils, logger = weread_helper
    request_utils.return_value.post_res.side_effect = [
        _response({"books": [{"bookId": str(i), "title": "合成书", "finishReading": 1} for i in range(3)]}),
        _response({"book": {"progress": 100, "finishTime": 1700000000}}),
        _response({"upgrade_info": info, "book": {"progress": 100}}),
    ]
    assert helper.get_recent_books() == []
    assert helper.get_request_issue_kind() == "upgrade"
    assert helper.get_book_info("other") is None
    assert request_utils.return_value.post_res.call_count == 3
    assert "SENSITIVE_SENTINEL" not in str(logger.mock_calls) + str(helper._notify.mock_calls)
    # 下一轮可以重新检查，不因上一轮升级错误形成永久锁。
    request_utils.return_value.post_res.side_effect = [_response({"books": []})]
    assert helper.get_recent_books() == []
    assert helper.get_request_issue_kind() == ""


@pytest.mark.parametrize("payload,expected", [
    ({"books": []}, ""), ({"books": None}, "response"), ({}, "response"),
    ({"books": [None]}, "response"), ({"books": [{"bookId": ""}]}, "response"),
    ({"books": [{"bookId": 123, "title": "合成"}]}, "response"),
    ({"books": [{"bookId": "123", "title": None}]}, "response"),
    ({"books": [{"bookId": "123", "title": "合成", "finishReading": "1"}]}, "response"),
    ({"books": [{"bookId": "123", "title": "合成", "author": ["合成作者"]}]}, "response"),
])
def test_empty_shelf_is_distinct_from_malformed_response(weread_helper, payload, expected):
    """仅完整合法空书架可以作为成功，不将格式错误当作空数据。"""
    helper, request_utils, _logger = weread_helper
    request_utils.return_value.post_res.return_value = _response(payload)
    assert helper.get_recent_books() == []
    assert helper.get_request_issue_kind() == expected


@pytest.mark.parametrize("status,payload,expected", [
    (401, {}, "auth"), (403, {}, "auth"), (429, {}, "rate_limit"), (503, {}, "temporary"),
    (302, {}, "temporary"), (200, {"errcode": -2012, "errmsg": "SENSITIVE_SENTINEL"}, "auth"),
    (200, {"errcode": 123, "errmsg": "SENSITIVE_SENTINEL"}, "temporary"),
    (200, [], "response"), (200, {"errcode": "0"}, "response"),
])
def test_gateway_errors_have_fixed_categories_and_do_not_echo_messages(weread_helper, status, payload, expected):
    """鉴权、限流、传输及结构错误分类，不回显远端错误文本。"""
    helper, request_utils, logger = weread_helper
    response = _response(payload)
    response.status_code = status
    request_utils.return_value.post_res.return_value = response
    assert helper.get_recent_books() == []
    assert helper.get_request_issue_kind() == expected
    assert "SENSITIVE_SENTINEL" not in str(logger.mock_calls) + str(helper._notify.mock_calls)
    call = request_utils.return_value.post_res.call_args.kwargs
    assert call["verify"] is True and call["allow_redirects"] is False


@pytest.mark.parametrize("failure,expected", [("none", "temporary"), ("network", "temporary"), ("json", "response")])
def test_transport_and_decoder_failures_remain_sanitized(weread_helper, failure, expected):
    """响应缺失、传输异常和解码失败都显式失败且不泄露原文。"""
    helper, request_utils, logger = weread_helper
    if failure == "none":
        request_utils.return_value.post_res.return_value = None
    elif failure == "network":
        request_utils.return_value.post_res.side_effect = requests.RequestException("SENSITIVE_SENTINEL")
    else:
        response = _response({})
        response.json.side_effect = ValueError("SENSITIVE_SENTINEL")
        request_utils.return_value.post_res.return_value = response
    assert helper.get_recent_books() == []
    assert helper.get_request_issue_kind() == expected
    assert "SENSITIVE_SENTINEL" not in str(logger.mock_calls) + str(helper._notify.mock_calls)


def test_gateway_session_sanitizes_before_requestutils_logging(monkeypatch, weread_helper):
    """底层请求异常在到达 RequestUtils 日志前已脱敏。"""
    helper, _request_utils, _logger = weread_helper
    def fail(*_args, **_kwargs):
        raise requests.RequestException("SENSITIVE_SENTINEL")
    monkeypatch.setattr(requests.Session, "request", fail)
    session_class = helper._gateway_call.__globals__["_GatewaySession"]
    with session_class() as session:
        with pytest.raises(requests.RequestException) as caught:
            session.request("POST", "https://weread.invalid")
    assert str(caught.value) == "weread_transport_failed" and caught.value.__suppress_context__


def test_progress_identity_mismatch_is_not_used(weread_helper):
    """进度响应不能串到另一本书，错误时保留原记录。"""
    helper, request_utils, _logger = weread_helper
    request_utils.return_value.post_res.side_effect = [
        _response({"books": [{"bookId": "123", "title": "合成"}]}),
        _response({"bookId": "other", "book": {"progress": 100, "finishTime": 1700000000}}),
    ]
    assert helper.get_recent_books() == []
    assert helper.get_request_issue_kind() == "response"


@pytest.mark.parametrize("links,expected,label", [
    ({"readerLink": "https://weread.qq.com/web/reader/official", "deepLink": "weread://book/123"}, "https://weread.qq.com/web/reader/official", "打开阅读"),
    ({"deepLink": "weread://book/123"}, "weread://book/123", "查看详情"),
    ({}, "", ""), ({"readerLink": "javascript:alert(1)"}, "", ""),
    ({"readerLink": "https://evil.invalid/", "deepLink": "https://weread.qq.com/book-detail/123"}, "https://weread.qq.com/book-detail/123", "查看详情"),
])
def test_links_use_official_response_without_synthesizing_urls(weread_helper, links, expected, label):
    """优先阅读器链接，其次详情链接；缺失或不安全时不自行拼接。"""
    helper, request_utils, _logger = weread_helper
    request_utils.return_value.post_res.return_value = _response({"books": [{"bookId": "123", "title": "合成", **links}]})
    entry = helper.get_recent_books(include_progress=False)[0]
    assert (entry["weread_url"], entry["weread_url_label"]) == (expected, label)


@pytest.mark.parametrize("progress,finished,finish_time,start,status", [
    (0, 0, None, False, "未读"), (0, 0, None, True, "在读"), (1, 0, None, False, "在读"),
    (99, 0, 1700000000, True, "在读"), (100, 0, None, True, "在读"),
    (100, 0, 1700000000, True, "读完"), (1, 1, None, True, "读完"),
])
def test_progress_contract_and_explicit_shelf_finish(weread_helper, progress, finished, finish_time, start, status):
    """保留书架显式读完；否则须100%和完成时间，不能将未读当在读。"""
    helper, request_utils, _logger = weread_helper
    request_utils.return_value.post_res.side_effect = [
        _response({"books": [{"bookId": "123", "title": "合成", "finishReading": finished}]}),
        _response({"book": {"progress": progress, "finishTime": finish_time, "isStartReading": start,
                              "recordReadingTime": 60, "readingTime": 999, "readingProgress": 100}}),
    ]
    entry = helper.get_recent_books()[0]
    assert entry["status"] == status and entry["reading_progress"] == progress
    assert entry["reading_time"] == 60


@pytest.mark.parametrize("book", [None, {}, {"progress": True}, {"progress": 0.9}, {"progress": "1"},
                                  {"progress": 101}, {"progress": -1}, {"readingProgress": 90}])
def test_invalid_progress_does_not_fall_back_to_shelf_status(weread_helper, book):
    """进度契约不符时保留整轮原数据，不用猜测值写豆瓣。"""
    helper, request_utils, _logger = weread_helper
    request_utils.return_value.post_res.side_effect = [
        _response({"books": [{"bookId": "123", "title": "合成", "finishReading": 1}]}),
        _response({"book": book}),
    ]
    assert helper.get_recent_books() == []
    assert helper.get_request_issue_kind() == "response"


@pytest.mark.parametrize("progress,finish,expected", [(1, None, "do"), (100, 1700000000, "collect")])
def test_complete_book_read_reaches_existing_douban_sync(monkeypatch, weread_helper, progress, finish, expected):
    """完整读取后仍复用原有豆瓣状态提交，不改变来源范围与映射保存。"""
    helper, request_utils, _logger = weread_helper
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    plugin.init_plugin({"weread_api_key": "wrk-test-key"})
    plugin._weread_helper = helper
    calls = []
    plugin._douban_helper = types.SimpleNamespace(requests_paused=False,
        set_book_status=lambda **kwargs: calls.append(kwargs) or True,
        set_target_context=lambda *_args: None)
    monkeypatch.setattr(plugin, "_find_douban_match", lambda *_args: ("合成书", "456"))
    request_utils.return_value.post_res.side_effect = [
        _response({"books": [{"bookId": "123", "title": "合成书", "author": "合成作者", "finishReading": 0}]}),
        _response({"book": {"progress": progress, "finishTime": finish}}),
    ]
    plugin._sync_weread()
    assert len(calls) == 1 and calls[0]["status"] == expected
    assert plugin.get_data("weread_synced")["456"]["douban_status"] == expected
    assert plugin._source_fetch_ok == {"微信读书"} and not plugin._source_issue_this_run
    assert "wrk-test-key" not in json.dumps(plugin._data)


@pytest.mark.parametrize("stage", ["shelf", "progress"])
@pytest.mark.parametrize("mode", ["changes", "off"])
def test_upgrade_is_visible_preserves_records_and_other_sources_continue(monkeypatch, weread_helper, stage, mode):
    """协议错误持久化到页面和通知，不覆盖旧记录、不影响其他来源。"""
    helper, request_utils, _logger = weread_helper
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    plugin.init_plugin({"enable": True, "weread_api_key": "wrk-test-key", "xiaoyuzhou_cookie": "synthetic",
                        "notification_mode": mode})
    plugin._weread_helper = helper
    books = [{"bookId": "123", "title": "合成", "finishReading": 1}]
    responses = [_response({"books": books})] if stage == "progress" else []
    request_utils.return_value.post_res.side_effect = responses + [_response({"upgrade_info": {"message": "SENSITIVE_SENTINEL"}})]
    previous = [{"book_id": "old", "title": "旧书", "status": "读完"}]
    plugin.save_data("weread_books", previous)
    plugin.save_data("weread_synced", {"123": {"douban_status": "collect"}})
    plugin.save_data("weread_book_id_map", {"old": {"subject_id": "123"}})
    notices, sources = [], []
    monkeypatch.setattr(plugin, "_send_notification", lambda *args: notices.append(args) or True)
    monkeypatch.setattr(plugin, "_sync_xiaoyuzhou", lambda: sources.append("xiaoyuzhou"))
    plugin.run()
    assert sources == ["xiaoyuzhou"]
    assert plugin.get_data("weread_books") == previous
    assert plugin.get_data("weread_synced") == {"123": {"douban_status": "collect"}}
    assert plugin.get_data("weread_book_id_map") == {"old": {"subject_id": "123"}}
    issue = plugin.get_data("notification_issues")["微信读书"]
    assert issue["active"] and issue["category"] == "upgrade"
    assert "协议需要更新" in issue["title"]
    assert plugin.get_data("last_run")["status"] == "partial"
    assert plugin.get_data("last_run")["source_errors"] == ["微信读书"]
    assert "协议需要更新" in json.dumps(plugin.get_page(), ensure_ascii=False)
    if mode == "off":
        assert not notices
    else:
        assert "无需更换 API Key" in notices[0][1]
    assert "SENSITIVE_SENTINEL" not in str(notices) + json.dumps(plugin._data)
    # 明确成功的空书架才能恢复，不将失败后的 [] 当成恢复。
    request_utils.return_value.post_res.side_effect = [_response({"books": []})]
    plugin.run()
    assert not plugin.get_data("notification_issues")["微信读书"]["active"]
    assert plugin.get_data("weread_books") == []
    assert plugin.get_data("weread_synced") == {"123": {"douban_status": "collect"}}
