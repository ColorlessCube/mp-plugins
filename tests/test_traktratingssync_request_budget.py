"""验证豆瓣写入去重、共享预算、持久化暂停和延期队列。"""
import json
import socket
import types

import pytest

from test_traktratingssync_douban_helper import _Response, _build_helper, _load_douban_helper_module
from test_traktratingssync_netease_cookie_mode import _load_plugin_module
from test_traktratingssync_trakt_helper import _build_helper as _build_trakt, _load_trakt_helper_module


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """所有外部请求都必须被测试桩拦截。"""
    def deny(*_args, **_kwargs):
        """禁止真实 DNS 或 socket 请求。"""
        raise AssertionError("测试禁止真实网络访问")
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)


def _submit(helper, subject="123", host="movie.douban.com", status="do"):
    """构造豆瓣状态提交。"""
    return helper._post_interest(f"https://{host}/j/subject/{subject}/interest",
                                 f"https://{host}/subject/{subject}/", host,
                                 {"interest": status, "ck": helper.ck})


def _requests(monkeypatch, module, response):
    """记录请求并提供固定响应。"""
    calls = []
    def post(**kwargs):
        """记录写入请求。"""
        calls.append(kwargs)
        return response
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(post_res=post))
    return calls


def test_shared_budget_defers_and_restores_queue_without_ck(monkeypatch):
    """多个平台共用额度，队列跨重载保留且不保存 ck。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    helper._write_limit = 1
    calls = _requests(monkeypatch, module, _Response(200, {"r": 0}))
    assert _submit(helper)
    assert not _submit(helper, "456", "book.douban.com")
    helper.flush_pending()
    assert len(calls) == 1
    assert "mIHe" not in json.dumps(helper._state)
    restored = _build_helper(module, monkeypatch)
    restored._state = json.loads(json.dumps(helper._state))
    restored.flush_pending()
    assert len(calls) == 2
    assert calls[-1]["url"].startswith("https://book.douban.com/")
    assert not restored._state["pending"]


def test_unchanged_and_reverted_states_do_not_write_or_leave_stale_queue(monkeypatch):
    """已成功状态重复提交或回退到成功状态时，不再写入且撤销过期待处理目标。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    calls = _requests(monkeypatch, module, _Response(200, {"r": 0}))
    assert _submit(helper)
    assert _submit(helper)
    assert not _submit(helper, status="collect")
    assert _submit(helper, status="do")
    assert len(calls) == 1
    assert not helper._state["pending"]


@pytest.mark.parametrize("response", [
    _Response(200, text='<title>禁止访问</title><script src="TCaptcha.js"></script>'),
    _Response(404, {"r": 1, "error": "redirect to https://sec.douban.com/check"}),
    _Response(403),
])
def test_verification_pauses_all_hosts_and_survives_reload(monkeypatch, response):
    """验证响应后不再请求其他域名，重载后也不会自动再试。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    calls = _requests(monkeypatch, module, response)
    assert not _submit(helper)
    assert not _submit(helper, "456", "music.douban.com")
    helper.flush_pending()
    assert len(calls) == 1
    assert helper.requests_paused
    saved = json.loads(json.dumps(helper._state))
    # 构造函数在未更换 Cookie 时不能进行首页刷新。
    saved["cookie_fingerprint"] = module.hashlib.sha256(b"dbcl2=test").hexdigest()
    restored = module.DoubanHelper(user_cookie="dbcl2=test", get_data_fn=lambda _key: saved)
    assert restored.requests_paused
    restored.flush_pending()
    assert len(calls) == 1


def test_changed_cookie_resumes_verification_but_keeps_pending(monkeypatch):
    """更新 Cookie 允许重新检查登录，不删除待同步记录。"""
    module = _load_douban_helper_module(monkeypatch)
    state = {"requires_verification": True, "cookie_fingerprint": "previous", "pending": {}}
    refreshes = []
    def refresh(helper):
        """模拟通过新 Cookie 刷新登录。"""
        refreshes.append(True)
        helper.cookies["ck"] = "fresh"
    monkeypatch.setattr(module.DoubanHelper, "_refresh_ck", refresh)
    helper = module.DoubanHelper(user_cookie="dbcl2=new", get_data_fn=lambda _key: state)
    assert not helper.requests_paused
    assert helper.is_authenticated
    assert refreshes == [True]


def test_ordinary_404_does_not_pause_and_timeout_is_not_retried(monkeypatch):
    """普通条目 404 单独延期，超时不立即重试或记录成功。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    calls = _requests(monkeypatch, module, _Response(404, {"r": 1, "code": 404}))
    assert not _submit(helper)
    assert not helper.requests_paused
    helper.flush_pending()
    assert len(calls) == 1
    assert not helper._state["synced"]
    helper = _build_helper(module, monkeypatch)
    calls = _requests(monkeypatch, module, None)
    assert not _submit(helper)
    helper.flush_pending()
    assert len(calls) == 1


def test_rate_limit_uses_retry_after_and_blocks_search(monkeypatch):
    """429 冷却遵守服务端时间，期间搜索也不请求。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: 100)
    notices = []
    helper._notify = lambda _title, body: notices.append(body)
    calls = _requests(monkeypatch, module, _Response(429, headers={"Retry-After": "7200"}))
    assert not _submit(helper)
    assert helper._state["blocked_until"] == 7300
    assert helper._search_subject("test", "1001") == (None, None)
    assert len(calls) == 1
    assert "冷却结束" in notices[0]
    assert "在浏览器完成验证" not in notices[0]


@pytest.mark.parametrize("requires_verification", [True, False])
def test_pause_page_distinguishes_verification_from_automatic_cooldown(monkeypatch, requires_verification):
    """人工验证和自动冷却显示不同的恢复提示。"""
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    plugin.save_data("douban_sync_state", {"requires_verification": requires_verification,
        "blocked_until": 9999999999, "reason": "test", "pending": {}})
    text = json.dumps(plugin.get_page(), ensure_ascii=False)
    assert ("请先完成浏览器验证" in text) is requires_verification
    assert ("冷却结束后按定时任务继续处理" in text) is not requires_verification


@pytest.mark.parametrize("payload", [{"r": False}, {"r": 1}, {}, {"r": "0"}])
def test_http_200_error_json_is_not_recorded_as_success(monkeypatch, payload):
    """HTTP 成功但业务未确认写入时，不能丢弃待同步记录。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    calls = _requests(monkeypatch, module, _Response(200, payload))
    assert not _submit(helper)
    assert len(calls) == 1
    assert not helper._state["synced"]
    assert len(helper._state["pending"]) == 1


def test_three_network_failures_cool_down_but_preserve_all_targets(monkeypatch):
    """连续网络异常终止后续尝试，待处理目标仍保留。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    calls = _requests(monkeypatch, module, None)
    for subject in ("123", "456", "789", "999"):
        assert not _submit(helper, subject)
    helper.flush_pending()
    assert len(calls) == 3
    assert helper.requests_paused
    assert len(helper._state["pending"]) == 4


def test_explicit_resume_consumes_switch_and_preserves_server_cooldown(monkeypatch):
    """人工恢复不删除队列，也不绕过服务端指定冷却。"""
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    plugin.save_data("douban_sync_state", {"requires_verification": True, "blocked_until": 9999999999,
        "pending": {"target": {"data": {"interest": "do"}}}})
    plugin.init_plugin({"douban_resume": True, "douban_write_limit": 3, "douban_write_interval": 20})
    state = plugin.get_data("douban_sync_state")
    assert not state.get("requires_verification")
    assert state["blocked_until"] == 9999999999
    assert "target" in state["pending"]
    assert plugin._config_updates[-1]["douban_resume"] is False
    assert plugin._config_updates[-1]["douban_write_limit"] == 3


def test_write_interval_is_shared_across_platforms(monkeypatch):
    """跨平台写入也必须等待统一最小间隔。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    clock = [100.0]
    sleeps = []
    def sleep(seconds):
        """推进虚拟时钟并记录等待。"""
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", sleep)
    monkeypatch.setattr(module.random, "uniform", lambda low, _high: low)
    _requests(monkeypatch, module, _Response(200, {"r": 0}))
    assert _submit(helper)
    assert _submit(helper, "456", "book.douban.com")
    assert _submit(helper, "789", "music.douban.com")
    assert sleeps == [10, 10]


def test_progress_deduplicates_playback_and_history(monkeypatch):
    """同剧多集和观看历史合并后只提交一次，保留最高播放进度。"""
    plugin_module = _load_plugin_module(monkeypatch)
    plugin = plugin_module.TraktRatingsSync()
    show = {"title": "Test", "ids": {"trakt": 1, "tmdb": 2}}
    calls = []
    plugin._trakt_helper = types.SimpleNamespace(get_access_token=lambda: "token", has_oauth_unauthorized=lambda: False,
        sync_one_progress=lambda item, *_args: calls.append(item) or True)
    plugin._douban_helper = types.SimpleNamespace(requests_paused=False)
    monkeypatch.setattr(plugin, "_fetch_trakt_progress_sources", lambda _token: (
        [{"show": show, "progress": 20}, {"show": show, "progress": 60}], [{"show": show}]))
    plugin._sync_progress()
    assert len(calls) == 1
    assert calls[0]["progress"] == 60


def test_watching_state_skips_mapping_and_post_but_updates_local_progress(monkeypatch):
    """已在看的剧集仅更新本地进度；隐私设置变化才再次提交。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, _saved, _updated = _build_trakt(module)
    item = {"show": {"title": "Test", "ids": {"trakt": 1, "tmdb": 2}}, "progress": 70}
    watching = {"电视剧_1": {"status": "在看", "douban_id": "123", "private": False, "progress": 20}}
    monkeypatch.setattr(helper, "_resolve_douban_info", lambda *_args: pytest.fail("状态未变不应重新匹配"))
    douban = types.SimpleNamespace(record_unchanged=lambda: None,
                                  set_watching_status=lambda **_kwargs: pytest.fail("状态未变不应写入"))
    assert helper.sync_one_progress(item, "show", module.MediaType.TV, watching, douban, False)
    assert watching["电视剧_1"]["progress"] == 70


def test_run_stops_other_platforms_after_verification_and_unlocks(monkeypatch):
    """出现验证后不进入其他平台，执行异常后也释放运行锁。"""
    module = _load_plugin_module(monkeypatch)
    helper = types.SimpleNamespace(requests_paused=False, flush_pending=lambda: None,
        get_sync_summary=lambda: {"written": 0, "skipped": 0, "failed": 1, "pending": 1, "paused": True})
    monkeypatch.setattr(module, "DoubanHelper", lambda **_kwargs: helper)
    plugin = module.TraktRatingsSync()
    plugin.init_plugin({"enable": True, "trakt_client_id": "id", "weread_api_key": "key"})
    monkeypatch.setattr(plugin, "_sync_trakt", lambda: setattr(helper, "requests_paused", True))
    monkeypatch.setattr(plugin, "_sync_weread", lambda: pytest.fail("验证后不得继续其他平台"))
    plugin.run()
    assert plugin._run_lock.acquire(blocking=False)
    plugin._run_lock.release()


def test_run_does_not_overlap_manual_and_scheduled_invocations(monkeypatch):
    """已有运行时手动重复触发不产生任何同步请求。"""
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    monkeypatch.setattr(plugin, "_run_sync", lambda: pytest.fail("不应并发执行"))
    plugin._run_lock.acquire()
    try:
        plugin.run()
    finally:
        plugin._run_lock.release()


def test_run_releases_lock_after_exception(monkeypatch):
    """同步异常不应永久阻止下次定时运行。"""
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    def fail():
        """模拟同步过程异常。"""
        raise RuntimeError("test")
    monkeypatch.setattr(plugin, "_run_sync", fail)
    with pytest.raises(RuntimeError):
        plugin.run()
    assert plugin._run_lock.acquire(blocking=False)
    plugin._run_lock.release()
