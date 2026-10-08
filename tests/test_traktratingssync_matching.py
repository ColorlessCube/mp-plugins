"""验证未匹配处理、缓存延期与无需评分的电影历史，所有网络在边界隔离。"""
import json
import socket
import types
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from test_traktratingssync_netease_cookie_mode import _load_plugin_module
from test_traktratingssync_trakt_helper import _load_trakt_helper_module, _build_helper
from test_traktratingssync_douban_helper import _load_douban_helper_module, _build_helper as _douban


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """禁止任何真实 DNS 和 socket 请求。"""
    def deny(*_args, **_kwargs):
        """网络必须使用边界桩。"""
        raise AssertionError("禁止真实网络")
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)


def _plugin(monkeypatch):
    """构造使用内存存储和真实匹配管理器的独立插件。"""
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    plugin.init_plugin({})
    return plugin, module


def _unmatched(plugin, kind="weread_book", host="book.douban.com"):
    """准备明确未匹配的候选，不包含凭据。"""
    helper = plugin._get_matching_helper()
    key = helper.prepare(kind, "123", "微信读书", "测试书", host, {"book_id": "123", "title": "测试书", "status": "在读"})
    helper.fail(key, "尚未匹配")
    return key


def test_failed_match_delay_persists_without_being_extended(monkeypatch):
    """反复看到同一来源记录不会续期，延期到点与手动重试均可恢复搜索。"""
    plugin, module = _plugin(monkeypatch)
    key = _unmatched(plugin)
    first = plugin._get_matching_helper().items[key]["retry_after"]
    restored = module.MatchingHelper(plugin.save_data, plugin.get_data)
    restored.prepare("weread_book", "123", "微信读书", "测试书", "book.douban.com", {"book_id": "123"})
    assert restored.items[key]["retry_after"] == first
    assert not restored.should_retry(key)
    assert restored.retry(key) and restored.should_retry(key)
    assert restored.associate(key, "456")
    restored.matched(key)
    assert key not in restored.items and restored.get_manual(key) == "456"


def test_source_latest_candidate_wins_and_old_miss_can_leave_recent_window(monkeypatch):
    """恢复旧候选时以当前状态为准，避免在读覆盖新读完。"""
    plugin, _module = _plugin(monkeypatch)
    key = _unmatched(plugin)
    helper = plugin._get_matching_helper()
    helper.retry(key)
    assert helper.merge_candidates("weread_book", [], lambda item: item["book_id"])[0]["status"] == "在读"
    latest = {"book_id": "123", "status": "读完"}
    assert helper.merge_candidates("weread_book", [latest], lambda item: item["book_id"]) == [latest]


def test_old_candidates_are_replayed_in_small_batches(monkeypatch):
    """窗口外积压候选每种来源最多补入五条，避免请求突增。"""
    plugin, _module = _plugin(monkeypatch)
    helper = plugin._get_matching_helper()
    for identity in range(12):
        key = helper.prepare("weread_book", str(identity), "微信读书", str(identity), "book.douban.com", {"book_id": str(identity)})
        helper.fail(key, "未匹配")
        helper.retry(key)
    assert len(helper.merge_candidates("weread_book", [], lambda item: item["book_id"])) == 5


@pytest.mark.parametrize("url", ["https://evil.invalid/subject/123/", "http://book.douban.com/subject/123/", "https://movie.douban.com/subject/123/", "https://book.douban.com/podcast/123/", "https://book.douban.com/subject/123/?cookie=x", "0", "https://book.douban.com@evil.invalid/subject/123/"])
def test_manual_association_rejects_wrong_host_type_and_nonpositive_id(monkeypatch, url):
    """不允许跨类型、任意主机或恶意URL关联。"""
    plugin, module = _plugin(monkeypatch)
    key = _unmatched(plugin)
    assert not plugin._api_match_associate(module.DoubanMatchRequest(match_key=key, subject_url=url)).success
    assert not plugin._get_matching_helper().manual


@pytest.mark.parametrize("url", ["456", "https://book.douban.com/subject/456/"])
def test_admin_association_and_retry_only_save_state(monkeypatch, url):
    """保存关联与重试不会启动同步，并且不会变为配置项。"""
    plugin, module = _plugin(monkeypatch)
    key = _unmatched(plugin)
    monkeypatch.setattr(plugin, "run", lambda: pytest.fail("不能触发同步"))
    assert plugin._api_match_associate(module.DoubanMatchRequest(match_key=key, subject_url=url)).success
    assert plugin._get_matching_helper().get_manual(key) == "456"
    assert plugin._api_match_retry(key).success
    form, defaults = plugin.get_form()
    text = json.dumps(form, ensure_ascii=False)
    assert "处理未匹配记录" in text and "JSON.stringify" in text and "same-origin" in text
    assert defaults["_ui_match_key"] == key
    plugin.init_plugin({**plugin._config_updates[-1], **defaults})
    assert not any(name.startswith("_ui_") for name in plugin._config_updates[-1])


@pytest.mark.parametrize("path", ["/matches/associate", "/matches/{match_key}/retry"])
@pytest.mark.parametrize("cookie", [None, "test-member", "test-admin"])
def test_matching_routes_enforce_admin_cookie(monkeypatch, path, cookie):
    """成功路由同样经过真实 FastAPI 参数处理和管理员依赖。"""
    plugin, _module = _plugin(monkeypatch)
    key = _unmatched(plugin)
    api = next(item for item in plugin.get_api() if item["path"] == path)
    app = FastAPI()
    app.add_api_route(path, **{name: value for name, value in api.items() if name not in ("path", "allow_anonymous")})
    client = TestClient(app)
    if cookie:
        client.cookies.set("MoviePilot", cookie)
    response = client.post(path.replace("{match_key}", key), json={"match_key": key, "subject_url": "456"})
    assert response.status_code == (200 if cookie == "test-admin" else 403)
    if cookie == "test-admin":
        assert response.json()["success"]


def test_matching_actions_refuse_concurrent_sync(monkeypatch):
    """运行锁避免UI操作覆盖正在修改的候选列表。"""
    plugin, module = _plugin(monkeypatch)
    key = _unmatched(plugin)
    plugin._run_lock.acquire()
    try:
        assert not plugin._api_match_retry(key).success
        assert not plugin._api_match_associate(module.DoubanMatchRequest(match_key=key, subject_url="456")).success
    finally:
        plugin._run_lock.release()


def test_search_delay_manual_override_and_failed_search_short_cooldown(monkeypatch):
    """负缓存阻止重复请求，手动关联立即可用，暂时错误只延后五分钟。"""
    plugin, _module = _plugin(monkeypatch)
    plugin._douban_helper = types.SimpleNamespace(search_temporarily_failed=False)
    calls = []
    args = ("weread_book", "123", "微信读书", "测试书", "book.douban.com", {"book_id": "123"}, {})
    lookup = lambda: calls.append(True) or (None, None)
    assert plugin._find_douban_match(*args, lookup) == (None, None)
    key = next(iter(plugin._get_matching_helper().items))
    assert plugin._find_douban_match(*args, lookup) == (None, None) and len(calls) == 1
    plugin._get_matching_helper().associate(key, "456")
    assert plugin._find_douban_match(*args, lookup)[1] == "456" and len(calls) == 1
    def failing():
        """模拟网络请求超时。"""
        raise TimeoutError
    plugin._find_douban_match("weread_book", "other", *args[2:], failing)
    record = next(iter(plugin._get_matching_helper().items.values()))
    assert record["retry_after"] - record["last_seen_at"] == 300


def test_unmatched_is_visible_and_summary_cannot_claim_completion(monkeypatch):
    """未匹配与待写入分别呈现，成功通知列出剩余未匹配。"""
    plugin, module = _plugin(monkeypatch)
    _unmatched(plugin)
    helper = types.SimpleNamespace(requests_paused=False, is_authenticated=True, flush_pending=lambda: None,
        get_sync_summary=lambda: {"written": 1, "skipped": 0, "failed": 0, "pending": 0, "paused": False})
    monkeypatch.setattr(module, "DoubanHelper", lambda **_kwargs: helper)
    plugin._enable = True
    sent = []
    monkeypatch.setattr(plugin, "_send_notification", lambda *args: sent.append(args) or True)
    plugin.run()
    assert plugin.get_data("last_run")["status"] == "pending"
    assert plugin.get_data("last_run")["unmatched"] == 1
    assert "未匹配 1" in sent[-1][1]
    text = json.dumps(plugin.get_page(), ensure_ascii=False)
    assert "尚未匹配" in text and "下次重新匹配" in text and "待写入" in text


def _movie(identity=123, watched_at=None):
    """构造合法电影观看历史。"""
    return {"type": "movie", "watched_at": watched_at or datetime.now(timezone.utc).isoformat(),
            "movie": {"title": "测试电影", "year": 2025, "ids": {"trakt": identity, "tmdb": 456}}}


def test_movie_history_rejects_invalid_dates_and_types_and_deduplicates(monkeypatch):
    """愿望、正在播放、未来时间和无日期记录均不能证明电影看完。"""
    module = _load_trakt_helper_module(monkeypatch)
    now = datetime.now(timezone.utc)
    good = _movie()
    bad = [_movie(1, "bad"), _movie(2, (now + timedelta(days=1)).isoformat()),
           _movie(3, (now - timedelta(days=60)).isoformat()), _movie(4, now.replace(tzinfo=None).isoformat()),
           {"type": "movie", "movie": good["movie"]}, {**good, "type": "episode"}, {"progress": 99, "movie": good["movie"]}]
    assert module.TraktHelper.extract_watched_movies([good, good, *bad], 30) == [good]
    assert len(module.TraktHelper.extract_watched_movies([good, bad[2]], 0)) == 2


@pytest.mark.parametrize("submitted", [True, False])
def test_unrated_movie_preserves_rating_and_records_only_confirmed_write(monkeypatch, submitted):
    """电影没有评分也标记看过，写入延期不能提前保存成功记录。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, _saved, _updated = _build_helper(module)
    monkeypatch.setattr(helper, "_resolve_douban_info", lambda *_args: {"id": "789"})
    calls = []
    douban = types.SimpleNamespace(set_target_context=lambda *_args: None,
        record_unchanged=lambda: pytest.fail("首次不能跳过"), set_watching_status=lambda **kwargs: calls.append(kwargs) or submitted)
    finished = {}
    assert helper.sync_one_watched_movie(_movie(), finished, douban, True) == submitted
    assert calls[0] == {"subject_id": "789", "status": "collect", "private": True, "rating": None}
    assert bool(finished) == submitted


def test_rated_movie_history_reuses_finished_mapping_without_search_or_post(monkeypatch):
    """同一部电影已有成功评分同步时，历史同步不再额外请求。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, _saved, _updated = _build_helper(module)
    monkeypatch.setattr(helper, "_resolve_douban_info", lambda *_args: pytest.fail("无需重复匹配"))
    finished = {f"{module.MediaType.MOVIE}_123": {"douban_id": "789", "trakt_rating": 8}}
    skips = []
    douban = types.SimpleNamespace(record_unchanged=lambda: skips.append(True))
    assert helper.sync_one_watched_movie(_movie(), finished, douban, True)
    assert len(skips) == 1 and next(iter(finished.values()))["trakt_rating"] == 8


@pytest.mark.parametrize("failed,unauthorized", [(False, False), (True, False), (False, True)])
def test_movie_history_source_failure_preserves_snapshot_and_401_retries_once(monkeypatch, failed, unauthorized):
    """失败不能覆盖历史快照；只有401刷新授权并最多重试一次。"""
    plugin, plugin_module = _plugin(monkeypatch)
    module = _load_trakt_helper_module(monkeypatch)
    plugin_module.TraktHelper = module.TraktHelper
    plugin.save_data("trakt_movie_history", ["old"])
    replies = iter([None, [_movie()]] if unauthorized else [None if failed else [_movie()]])
    refreshed, posts = [], []
    helper = types.SimpleNamespace(get_access_token=lambda **kw: refreshed.append(kw) or "test-token",
        reset_oauth_unauthorized=lambda: None, has_oauth_unauthorized=lambda: unauthorized,
        fetch_history=lambda *_args: next(replies), extract_watched_movies=module.TraktHelper.extract_watched_movies,
        sync_one_watched_movie=lambda *args: posts.append(args) or True)
    plugin._trakt_helper = helper
    plugin._douban_helper = types.SimpleNamespace(requests_paused=False)
    errors = []
    monkeypatch.setattr(plugin, "_notify_trakt_read_issue", lambda title: errors.append(title))
    plugin._sync_movie_history()
    assert len(refreshed) == (2 if unauthorized else 1)
    if failed:
        assert plugin.get_data("trakt_movie_history") == ["old"] and errors and not posts
    else:
        assert len(posts) == 1 and plugin.get_data("trakt_movie_history")[0]["type"] == "movie"


@pytest.mark.parametrize("expired,changed", [(False, False), (True, False), (False, True)])
def test_season_cache_expires_and_invalidates_when_fresh_progress_changes(monkeypatch, expired, changed):
    """元数据缓存不能隐藏新播集数，过期读取失败也不能使用旧资料判定完成。"""
    module = _load_trakt_helper_module(monkeypatch)
    old = [{"number": 1, "aired_episodes": 2, "episode_count": 3}]
    helper, _saved, _updated = _build_helper(module, data={"trakt_seasons": {"123": {"seasons": old, "fetched_at": 1 if expired else module.time.time()}}})
    reads = []
    monkeypatch.setattr(helper, "_fetch_show_completion_data", lambda *_args: reads.append(True) or None)
    value = helper.fetch_show_seasons("123", {"seasons": [{"number": 1, "aired": 3 if changed else 2}]})
    assert len(reads) == (1 if expired or changed else 0)
    assert value == (None if expired or changed else old)


def test_book_search_no_longer_truncates_title_and_podcast_queries_are_bounded(monkeypatch):
    """长书名最多两次搜索，播客最多两个候选词，避免成串请求。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _douban(module, monkeypatch)
    queries = []
    monkeypatch.setattr(helper, "_search_subject", lambda *args: queries.append(args) or (None, None))
    assert helper.get_book_subject_id("一本很长但还没在豆瓣找到的书名", "作者") == (None, None)
    assert len(queries) == 2
    queries.clear()
    monkeypatch.setattr(helper, "_podcast_search_candidates", lambda _title: ["a", "b", "c", "d"])
    monkeypatch.setattr(helper, "_search_podcast_subject", lambda keyword: queries.append(keyword) or (None, None))
    assert helper.get_podcast_subject_id("播客") == (None, None)
    assert queries == ["a", "b"]


def test_unrated_update_cannot_remove_queued_explicit_rating(monkeypatch):
    """状态更新不能把前序尚未提交的明确评分从延期队列清掉。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _douban(module, monkeypatch)
    url = "https://movie.douban.com/j/subject/123/interest"
    helper._state["pending"][url] = {"data": {"interest": "collect", "rating": "4", "private": "1"}}
    monkeypatch.setattr(helper, "_submit_pending", lambda _url: False)
    helper._post_interest(url, "https://movie.douban.com/subject/123/", "movie.douban.com", {"interest": "collect", "private": "1"})
    assert helper._state["pending"][url]["data"]["rating"] == "4"


def test_retry_old_unmatched_season_rechecks_current_progress_not_saved_completion(monkeypatch):
    """离开历史窗口的未匹配季恢复时，使用最新观看证据而非失败快照。"""
    from test_traktratingssync_completion import _season, _state, _douban as season_douban
    plugin, _plugin_module = _plugin(monkeypatch)
    module = _load_trakt_helper_module(monkeypatch)
    helper, _saved, _updated = _build_helper(module)
    matching = plugin._get_matching_helper()
    state = _state(completed=True)
    key = matching.prepare("trakt_season", "1:s2", "Trakt", "测试剧 第2季", "movie.douban.com", state)
    matching.fail(key, "未匹配")
    matching.retry(key)
    progress, metadata = _season(number=2, watched=1)
    checks, candidates = [], []
    helper.get_access_token = lambda **_kwargs: "test-token"
    helper.fetch_show_progress = lambda *_args: checks.append(True) or {"seasons": [progress]}
    helper.fetch_show_seasons = lambda *_args: [metadata]
    helper.sync_one_progress = lambda item, *_args: candidates.append(item) or True
    plugin._trakt_helper = helper
    plugin._douban_helper = season_douban()[0]
    monkeypatch.setattr(plugin, "_fetch_trakt_progress_sources", lambda _token: ([], []))
    plugin._sync_progress()
    assert checks == [True] and len(candidates) == 1
    assert not candidates[0]["completed"] and candidates[0]["watched_episodes"] == 1


def test_same_run_rated_movie_is_not_requeued_without_rating(monkeypatch):
    """评分候选已处理或延期时，观看历史不能再次处理相同电影。"""
    plugin, plugin_module = _plugin(monkeypatch)
    module = _load_trakt_helper_module(monkeypatch)
    plugin_module.TraktHelper = module.TraktHelper
    plugin._movie_rating_candidates = {"123"}
    plugin._trakt_helper = types.SimpleNamespace(get_access_token=lambda **_kwargs: "test-token",
        reset_oauth_unauthorized=lambda: None, has_oauth_unauthorized=lambda: False,
        fetch_history=lambda *_args: [_movie()], extract_watched_movies=module.TraktHelper.extract_watched_movies,
        sync_one_watched_movie=lambda *_args: pytest.fail("评分候选不能重复进入历史同步"))
    plugin._douban_helper = types.SimpleNamespace(requests_paused=False)
    plugin._sync_movie_history()


def test_one_movie_exception_does_not_stop_remaining_candidates(monkeypatch):
    """单条写入异常不能中止其他电影的处理。"""
    plugin, plugin_module = _plugin(monkeypatch)
    module = _load_trakt_helper_module(monkeypatch)
    plugin_module.TraktHelper = module.TraktHelper
    calls = []
    def submit(item, *_args):
        """第一条报错，第二条成功。"""
        calls.append(item)
        if len(calls) == 1:
            raise TimeoutError
        return True
    plugin._trakt_helper = types.SimpleNamespace(get_access_token=lambda **_kwargs: "test-token",
        reset_oauth_unauthorized=lambda: None, has_oauth_unauthorized=lambda: False,
        fetch_history=lambda *_args: [_movie(), _movie(124)], extract_watched_movies=module.TraktHelper.extract_watched_movies,
        sync_one_watched_movie=submit)
    plugin._douban_helper = types.SimpleNamespace(requests_paused=False)
    monkeypatch.setattr(plugin, "_notify_issue", lambda *_args: None)
    plugin._sync_movie_history()
    assert len(calls) == 2


@pytest.mark.parametrize("ratings", [None, [], [{"movie": {"title": "测试电影", "ids": {"trakt": 123}}, "rating": 6}]])
def test_rating_retry_uses_fresh_source_and_does_not_replay_removed_rating(monkeypatch, ratings):
    """来源失败保留候选，明确撤销评分后移除候选，仍有评分时采用最新值。"""
    plugin, plugin_module = _plugin(monkeypatch)
    module = _load_trakt_helper_module(monkeypatch)
    plugin_module.TraktHelper = module.TraktHelper
    plugin._sync_type = "movies"
    matching = plugin._get_matching_helper()
    key = matching.prepare("trakt_rating", "123", "Trakt", "测试电影", "movie.douban.com", {"movie": {"ids": {"trakt": 123}}, "rating": 10})
    matching.fail(key, "未匹配")
    matching.retry(key)
    posts = []
    plugin._trakt_helper = types.SimpleNamespace(fetch_ratings=lambda _kind: ratings,
        sync_one_rate=lambda item, *_args: posts.append(item["rating"]) or True)
    plugin._douban_helper = types.SimpleNamespace(requests_paused=False)
    monkeypatch.setattr(plugin, "_notify_trakt_read_issue", lambda _title: None)
    plugin._sync_ratings()
    assert posts == ([6] if ratings else [])
    assert (key in matching.items) is (ratings != [])
