"""验证评分不代表看完、实时手动内容保护及准确异常汇总，禁止真实网络。"""
import json
import types

import pytest

from test_traktratingssync_douban_helper import _Response, _build_helper, _load_douban_helper_module, block_network
from test_traktratingssync_trakt_helper import _build_helper as _build_trakt, _load_trakt_helper_module
from test_traktratingssync_presentation import _plugin
from test_traktratingssync_completion import _state, _douban


def _interest_form(subject="123", rating="4"):
    """提供含用户短评及标签的匿名表单样本。"""
    return {"html": f'''<form action="https://movie.douban.com/j/subject/{subject}/interest" method="POST">
        <input name="rating" value="{rating}"><input name="tags" value="收藏 原著">
        <textarea name="comment">我的短评 &amp; 备注</textarea><input name="foldcollect" value="U">
        </form>'''}


def _live_reader(helper, module, monkeypatch):
    """恢复被旧测试夹具隔离的真实读取逻辑，只替换 HTTP 边界。"""
    monkeypatch.setattr(helper, "_read_interest_fields", types.MethodType(module.DoubanHelper._read_interest_fields, helper))


@pytest.mark.parametrize("source_rating,expected", [(None, "4"), (5, "5")])
def test_changed_status_preserves_live_manual_comment_tags_and_unspecified_rating(monkeypatch, source_rating, expected):
    """状态变化保留实时手动内容，只有明确来源评分才覆盖星级。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    _live_reader(helper, module, monkeypatch)
    calls = []
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(
        get_res=lambda **_request: _Response(200, _interest_form()),
        post_res=lambda **request: calls.append(request) or _Response(200, {"r": 0})))
    assert helper.set_watching_status("123", "collect", False, source_rating)
    data = calls[0]["data"]
    assert data["rating"] == expected
    assert data["comment"] == "我的短评 & 备注" and data["tags"] == "收藏 原著"
    assert "我的短评" not in json.dumps(helper._state, ensure_ascii=False)
    assert "收藏 原著" not in json.dumps(helper._state, ensure_ascii=False)
    assert "ck" not in helper._state["synced"][calls[0]["url"]]


@pytest.mark.parametrize("response", [None, _Response(503), _Response(200, {}),
    _Response(200, _interest_form("456")), _Response(200, _interest_form(rating="invalid")),
    _Response(200, {"html": '<form action="https://movie.douban.com/j/subject/123/interest"><input name="rating"></form>'})])
def test_unreadable_existing_content_is_queued_without_post_or_success(monkeypatch, response):
    """网络、格式或条目核对失败时，不写入、不清空、也不记录成功。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    _live_reader(helper, module, monkeypatch)
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(
        get_res=lambda **_request: response,
        post_res=lambda **_request: pytest.fail("未确认已有内容不能写入")))
    assert not helper.set_watching_status("123", "collect", False)
    helper.flush_pending()
    assert not helper._state["synced"] and len(helper._state["pending"]) == 1
    assert "保护手动内容" in next(iter(helper._state["pending"].values()))["last_error"]


def test_verification_during_preservation_read_stops_writes(monkeypatch):
    """读取用户内容时遇到验证码，同样暂停全部后续请求。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    _live_reader(helper, module, monkeypatch)
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(
        get_res=lambda **_request: _Response(200, text="TCaptcha.js"),
        post_res=lambda **_request: pytest.fail("验证码后不能提交")))
    assert not helper.set_watching_status("123", "collect", False)
    assert helper.requests_paused


def test_podcast_json_edit_contract_preserves_content(monkeypatch):
    """播客编辑接口返回 JSON 字段而非 HTML，也必须保留手动内容。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    _live_reader(helper, module, monkeypatch)
    posts = []
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(
        get_res=lambda **_request: _Response(200, {"r": 0, "id": "123", "rating": 3,
            "comment": "播客笔记", "selected_tags": "访谈"}),
        post_res=lambda **request: posts.append(request) or _Response(200, {"r": 0})))
    assert helper.set_podcast_status("123", "collect", False)
    assert posts[0]["data"]["rating"] == "3"
    assert posts[0]["data"]["comment"] == "播客笔记"
    assert posts[0]["data"]["tags"] == "访谈"


def test_unchanged_target_does_not_read_or_overwrite_manual_rating(monkeypatch):
    """来源状态未变时，实时内容保护不增加请求，也不覆盖手动调整。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    helper._state["synced"]["https://movie.douban.com/j/subject/123/interest"] = {"interest": "collect", "rating": "4"}
    monkeypatch.setattr(helper, "_read_interest_fields", lambda *_args: pytest.fail("未变化不能请求"))
    assert helper.set_watching_status("123", "collect", False)
    assert helper.get_sync_summary()["skipped"] == 1


def test_whole_show_rating_never_posts_or_creates_finished_record(monkeypatch):
    """整剧评分只缓存来源星级，不匹配第一季或标记看过。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, _updates = _build_trakt(module)
    monkeypatch.setattr(helper, "_resolve_douban_info", lambda *_args: pytest.fail("整剧评分不得直接匹配豆瓣"))
    finished = {}
    assert helper.sync_one_rate({"rating": 8, "show": _state()["show"]}, finished, {}, module.MediaType.TV, object(), False)
    assert not finished
    assert saved["trakt_show_ratings"]["1"]["douban_rating"] == 4


def test_removed_whole_show_rating_is_not_reused_for_future_season(monkeypatch):
    """成功读取空评分列表会撤销旧来源评分，避免后续覆盖豆瓣手动星级。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, _updates = _build_trakt(module, data={"trakt_show_ratings": {"1": {"douban_rating": 4}}})
    helper.cache_show_ratings([])
    assert saved["trakt_show_ratings"] == {}


def test_privacy_change_is_not_ignored_when_rating_is_unspecified(monkeypatch):
    """保留星级的去重比较仍须识别用户更改的隐私设置。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    helper._state["synced"]["https://movie.douban.com/j/subject/123/interest"] = {"interest": "collect", "rating": "4", "private": "on"}
    posts = []
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(
        post_res=lambda **request: posts.append(request) or _Response(200, {"r": 0})))
    assert helper.set_watching_status("123", "collect", False)
    assert posts[0]["data"]["private"] == ""


def test_new_show_rating_updates_verified_incomplete_season_without_completing_it(monkeypatch):
    """在看季的新星级应更新，但不能被整剧评分强制改成看过。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, _saved, _updates = _build_trakt(module, data={"trakt_show_ratings": {"1": {"douban_rating": 4}}})
    monkeypatch.setattr(helper, "_resolve_douban_info", lambda *_args, **_kwargs: {"id": "456"})
    douban, posts, _skips = _douban()
    watching = {"电视剧_1_s2": {"douban_id": "456", "status": "在看", "private": False, "match_verified": True, "douban_rating": 3}}
    assert helper.sync_one_progress(_state(False), "show", module.MediaType.TV, watching, douban, False)
    assert posts[0]["status"] == "do" and posts[0]["rating"] == 4


@pytest.mark.parametrize("response,category", [(None, "temporary"), (_Response(503), "temporary"),
    (_Response(401), "auth"), (_Response(403), "application"),
    (_Response(403, text="<html>blocked</html>"), "access"), (_Response(429), "rate_limit")])
def test_trakt_request_classification_and_action_match_failure(monkeypatch, response, category):
    """按真实失败类型提示，网络、限流与403不能被当成令牌过期。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, _saved, _updates = _build_trakt(module)
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(get_res=lambda **_request: response))
    assert helper.fetch_show_progress("1", "token") is None
    assert helper.get_request_issue_kind() == category
    plugin, _plugin_module = _plugin(monkeypatch)
    plugin._trakt_helper = helper
    sent = []
    monkeypatch.setattr(plugin, "_send_notification", lambda *args: sent.append(args) or True)
    plugin._notify_trakt_read_issue("Trakt季度进度读取未完成")
    assert plugin.get_data("notification_issues")["Trakt"]["category"] == category
    if category in ("temporary", "rate_limit"):
        assert "无需重新授权" in sent[0][1] and "Client ID" not in sent[0][1]
    if category == "application":
        assert "403" in sent[0][1] and "不等同于令牌过期" in sent[0][1]


def test_different_temporary_trakt_failures_share_notification_cooldown(monkeypatch):
    """同轮多个剧集或接口临时失败，不重复推送相同操作提示。"""
    plugin, _module = _plugin(monkeypatch)
    sent = []
    monkeypatch.setattr(plugin, "_send_notification", lambda *args: sent.append(args) or True)
    plugin._notify_issue("Trakt", "评分读取失败", "", category="temporary")
    plugin._notify_issue("Trakt", "季度读取失败", "", category="temporary")
    assert len(sent) == 1


def test_partial_summary_names_affected_sources_instead_of_claiming_completion(monkeypatch):
    """即使已有成功写入，来源不完整时也必须显示部分完成。"""
    plugin, module = _plugin(monkeypatch, enable=True, trakt_client_id="client")
    helper = types.SimpleNamespace(requests_paused=False, is_authenticated=True, flush_pending=lambda: None,
        get_sync_summary=lambda: {"written": 2, "skipped": 3, "failed": 0, "pending": 0, "paused": False})
    monkeypatch.setattr(module, "DoubanHelper", lambda **_kwargs: helper)
    sent = []
    monkeypatch.setattr(plugin, "_send_notification", lambda *args: sent.append(args) or True)
    monkeypatch.setattr(plugin, "_sync_trakt", lambda: plugin._source_issue_this_run.add("Trakt"))
    plugin.run()
    assert plugin.get_data("last_run")["status"] == "partial"
    assert sent[-1][0] == "豆瓣同步部分完成" and "未完整读取：Trakt" in sent[-1][1]
