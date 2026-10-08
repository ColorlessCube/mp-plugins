"""验证精简配置、管理员操作、实际同步状态和通知降噪。"""
import json
import socket
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from test_traktratingssync_netease_cookie_mode import _load_plugin_module
from test_traktratingssync_douban_helper import _Response, _build_helper, _load_douban_helper_module


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """所有客户端均由边界桩代替，禁止真实 DNS 和 socket。"""
    def deny(*_args, **_kwargs):
        """拒绝真实网络连接。"""
        raise AssertionError("测试禁止真实网络")
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)


def _plugin(monkeypatch, **config):
    """构造已加载配置的独立插件实例。"""
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    plugin.init_plugin(config)
    return plugin, module


def test_form_save_migrates_tokens_and_preserves_settings_without_hidden_fields(monkeypatch):
    """移除旧字段后再次保存表单，授权和其它平台数据仍完整。"""
    plugin, _module = _plugin(monkeypatch, trakt_access_token="private-access", trakt_client_id="client",
                              trakt_client_secret="obsolete", trakt_auth_mode="device", netease_app_id="obsolete",
                              weread_api_key="private-key", douban_cookie="private-cookie", cron="0 10 * * *")
    plugin.save_data("finished", {"movie": {"title": "history"}})
    plugin.save_data("trakt_token", {"client_id": "client", "access_token": "private-access", "refresh_token": "private-refresh", "expires_at": 9999999999})
    form, defaults = plugin.get_form()
    snapshot = dict(plugin._config_updates[-1])
    assert set(snapshot) == {key for key in defaults if not key.startswith("_ui_")}
    assert "trakt_access_token" not in snapshot
    assert "private-access" not in json.dumps(form)
    assert not {"trakt_client_secret", "trakt_auth_mode", "trakt_authorize", "trakt_authorization_response", "douban_resume", "netease_app_id"} & set(snapshot)
    plugin.init_plugin({**snapshot, **{key: value for key, value in defaults.items() if key.startswith("_ui_")}})
    assert plugin._trakt_access_token == "private-access"
    assert plugin._data["trakt_token"]["refresh_token"] == "private-refresh"
    assert plugin._data["finished"]["movie"]["title"] == "history"
    assert plugin._config_updates[-1]["douban_cookie"] == "private-cookie"
    assert plugin._config_updates[-1]["cron"] == "0 10 * * *"
    assert not any(key.startswith("_ui_") for key in plugin._config_updates[-1])


def test_unlimited_history_days_remains_zero_after_config_save(monkeypatch):
    """界面承诺的0表示不限天数，保存和重载不能变回30天。"""
    plugin, _module = _plugin(monkeypatch, trakt_history_days=0)
    assert plugin._trakt_history_days == 0
    assert plugin._config_updates[-1]["trakt_history_days"] == 0
    plugin.init_plugin(plugin._config_updates[-1])
    assert plugin._trakt_history_days == 0


@pytest.mark.parametrize("path", ["/oauth/start", "/douban/resume"])
@pytest.mark.parametrize("cookie", [None, "test-member", "invalid-cookie"])
def test_configuration_actions_reject_unauthenticated_or_non_admin_requests(monkeypatch, path, cookie):
    """新的按钮接口必须先通过管理员身份校验，不能匿名执行。"""
    plugin, _module = _plugin(monkeypatch)
    api = next(item for item in plugin.get_api() if item["path"] == path)
    app = FastAPI()
    options = {key: value for key, value in api.items() if key not in ("path", "allow_anonymous")}
    app.add_api_route(path, **options)
    client = TestClient(app)
    if cookie:
        client.cookies.set("MoviePilot", cookie)
    assert client.post(path).status_code == 403
    assert not plugin._data.get("trakt_pkce_pending")


def test_resume_action_preserves_queue_and_does_not_start_sync(monkeypatch):
    """管理员恢复只解除人工暂停，不触发写入或删除待同步内容。"""
    plugin, _module = _plugin(monkeypatch)
    state = {"requires_verification": True, "reason": "验证", "pending": {"target": {"data": {"interest": "do"}}}}
    plugin.save_data("douban_sync_state", state)
    monkeypatch.setattr(plugin, "run", lambda: pytest.fail("恢复按钮不能触发同步"))
    assert plugin._api_douban_resume().success
    assert plugin._data["douban_sync_state"]["pending"] == state["pending"]
    assert "requires_verification" not in plugin._data["douban_sync_state"]


def test_pending_queue_completion_is_visible_even_if_source_cache_not_updated(monkeypatch):
    """延期队列独立写入成功后，页面根据真实提交结果而非来源列表显示。"""
    plugin, _module = _plugin(monkeypatch)
    url = "https://book.douban.com/j/subject/123/interest"
    plugin.save_data("weread_books", [{"book_id": "book", "title": "已读完但未写入", "status": "读完"}])
    plugin.save_data("weread_book_id_map", {"book": {"subject_id": "123"}})
    plugin.save_data("douban_sync_state", {"pending": {url: {"data": {"interest": "collect"}, "host": "book.douban.com"}}, "synced": {}})
    text = json.dumps(plugin.get_page(), ensure_ascii=False)
    assert "来源阅读状态" in text
    assert "待处理" in text
    assert "已同步" not in text
    plugin.save_data("douban_sync_state", {"pending": {}, "synced": {url: {"interest": "collect"}}})
    assert "已同步" in json.dumps(plugin.get_page(), ensure_ascii=False)
    assert plugin.get_data("weread_synced") is None


def test_podcast_page_groups_episodes_and_distinguishes_source_from_douban(monkeypatch):
    """同播客多集只显示一行，来源听完不能冒充豆瓣写入成功。"""
    plugin, _module = _plugin(monkeypatch)
    plugin.save_data("xiaoyuzhou_episodes", [
        {"podcast_id": "one", "podcast_name": "播客A", "is_finished": True},
        {"podcast_id": "one", "podcast_name": "播客A", "listen_pct": .5},
    ])
    plugin.save_data("xiaoyuzhou_podcast_map", {"播客A": {"subject_id": "123"}})
    text = json.dumps(plugin.get_page(), ensure_ascii=False)
    assert "1 个播客（来源 2 条单集）" in text
    assert "尚未同步" in text
    assert "已同步" not in text


def test_notification_failure_retries_before_success_cooldown_then_deduplicates(monkeypatch):
    """发送失败不进入六小时冷却，成功发送后才抑制重复异常。"""
    plugin, module = _plugin(monkeypatch, netease_cookie="credential", notification_channel="bark")
    clock = [100000.0]
    calls = []
    responses = iter([False, True])
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    monkeypatch.setattr(plugin, "_send_notification", lambda title, body: calls.append((title, body)) or next(responses))
    assert not plugin._notify_issue("网易云音乐", "Cookie失效", "private-diagnostics")
    assert plugin._data["notification_issues"]["网易云音乐"]["last_success_at"] == 0
    assert not plugin._notify_issue("网易云音乐", "Cookie失效", "private-diagnostics")
    assert len(calls) == 1
    clock[0] += 301
    assert plugin._notify_issue("网易云音乐", "Cookie失效", "private-diagnostics")
    clock[0] += 301
    assert not plugin._notify_issue("网易云音乐", "Cookie失效", "private-diagnostics")
    assert len(calls) == 2
    assert "private-diagnostics" not in json.dumps(calls)
    assert "credential" not in json.dumps(plugin._data["notification_issues"])


def test_recovery_notification_failure_does_not_keep_platform_in_error(monkeypatch):
    """恢复提醒投递失败不能让页面继续声称平台故障，提醒可稍后重试。"""
    plugin, module = _plugin(monkeypatch)
    clock = [100000.0]
    calls = []
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    monkeypatch.setattr(plugin, "_send_notification", lambda *args: calls.append(args) or False)
    plugin.save_data("notification_issues", {"Trakt": {"active": True, "last_success_at": 90000}})
    plugin._resolve_issue("Trakt")
    assert not plugin._data["notification_issues"]["Trakt"]["active"]
    assert plugin._data["notification_issues"]["Trakt"]["recovery_pending"]
    plugin._resolve_issue("Trakt")
    assert len(calls) == 1
    clock[0] += 301
    monkeypatch.setattr(plugin, "_send_notification", lambda *args: calls.append(args) or True)
    plugin._resolve_issue("Trakt")
    plugin._resolve_issue("Trakt")
    assert len(calls) == 2
    assert not plugin._data["notification_issues"]["Trakt"]["recovery_pending"]


@pytest.mark.parametrize("mode,written,expected", [("changes", 0, 0), ("changes", 2, 1), ("errors", 2, 0), ("off", 2, 0)])
def test_sync_summary_is_one_message_only_for_changes(monkeypatch, mode, written, expected):
    """有新增时只发送数量摘要，无新增或仅异常模式不发送常规结果。"""
    plugin, module = _plugin(monkeypatch, enable=True, notification_mode=mode)
    helper = types.SimpleNamespace(requests_paused=False, is_authenticated=True, flush_pending=lambda: None,
                                   get_sync_summary=lambda: {"written": written, "skipped": 49, "failed": 0, "pending": 2, "paused": False})
    monkeypatch.setattr(module, "DoubanHelper", lambda **_kwargs: helper)
    calls = []
    monkeypatch.setattr(plugin, "_send_notification", lambda *args: calls.append(args) or True)
    plugin.run()
    assert len(calls) == expected
    assert plugin._data["last_run"]["written"] == written
    assert plugin._data["last_run"]["pending"] == 2
    assert plugin._data["last_run"]["status"] == "completed"
    assert "本轮写入" in json.dumps(plugin.get_page(), ensure_ascii=False)


def test_failed_queue_item_keeps_name_and_reason_without_cookie(monkeypatch):
    """异常队列展示条目名称和原因，不能存入Cookie、ck或响应正文。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    helper.set_target_context("123", "book.douban.com", "书籍A", "微信读书")
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(post_res=lambda **_request: _Response(404, {"private": "response"})))
    url = "https://book.douban.com/j/subject/123/interest"
    assert not helper._post_interest(url, "https://book.douban.com/subject/123/", "book.douban.com", {"interest": "collect", "ck": "private-cookie"})
    assert helper._state["targets"][url]["title"] == "书籍A"
    assert "404" in helper._state["pending"][url]["last_error"]
    assert "private-cookie" not in json.dumps(helper._state)
    assert "response" not in json.dumps(helper._state)
