"""逐季完成判断与同步回归测试，所有外部边界均离线替换。"""
import asyncio
import copy
import types

import pytest

from test_traktratingssync_netease_cookie_mode import _load_plugin_module
from test_traktratingssync_trakt_helper import _build_helper, _load_trakt_helper_module, _Response, block_network


def _season(number=1, total=3, watched=3, aired=3):
    """构造完整逐集记录，不通过观看次数推断完成。"""
    progress = {"number": number, "aired": aired, "completed": watched,
                "episodes": [{"number": index, "completed": index <= watched,
                              "last_watched_at": "2026-10-01T10:00:00Z"} for index in range(1, aired + 1)]}
    metadata = {"number": number, "episode_count": total, "aired_episodes": aired, "first_aired": "2026-09-01T10:00:00Z"}
    return progress, metadata


@pytest.mark.parametrize("total,watched,aired,finished", [(3, 3, 3, True), (8, 3, 3, False), (3, 2, 3, False), (0, 0, 0, False)])
def test_completion_requires_all_season_episodes_aired_and_watched(monkeypatch, total, watched, aired, finished):
    """完整看完才完成；追平、漏集和空季均不能完成。"""
    module = _load_trakt_helper_module(monkeypatch)
    progress, metadata = _season(total=total, watched=watched, aired=aired)
    result = module.TraktHelper.build_season_states({"seasons": [progress]}, [metadata])
    assert result[0]["completed"] is finished


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "future", "unknown_total", "special", "rewatch", "bad_reset"])
def test_completion_rejects_incomplete_or_ambiguous_evidence(monkeypatch, mutation):
    """不相信孤立的完成计数，也不把特别篇或旧重看记录算作完成。"""
    module = _load_trakt_helper_module(monkeypatch)
    progress, metadata = _season()
    payload = {"seasons": [progress]}
    if mutation == "missing":
        progress["episodes"].pop()
    elif mutation == "duplicate":
        progress["episodes"][2]["number"] = 2
    elif mutation == "future":
        metadata["aired_episodes"] = 2
    elif mutation == "unknown_total":
        metadata["episode_count"] = None
    elif mutation == "special":
        progress["number"] = metadata["number"] = 0
    else:
        payload["reset_at"] = "2026-10-02T00:00:00Z" if mutation == "rewatch" else "invalid"
    result = module.TraktHelper.build_season_states(payload, [metadata])
    assert not result or not result[0]["completed"]


def _state(completed=True, season=2):
    """构造不含任何评分的季度候选。"""
    return {"season": season, "completed": completed, "watched_episodes": 3 if completed else 1,
            "total_episodes": 3, "season_year": "2026",
            "show": {"title": "Test", "year": 2020, "ids": {"trakt": 1, "tmdb": 2}}}


def _douban():
    """记录提交与去重，隔离豆瓣网络和状态持久化。"""
    posts, skips = [], []
    return types.SimpleNamespace(requests_paused=False, set_target_context=lambda *_args: None,
                                 set_watching_status=lambda **kwargs: posts.append(kwargs) or True,
                                 record_unchanged=lambda: skips.append(True)), posts, skips


def test_finished_season_needs_no_rating_and_cannot_regress_to_watching(monkeypatch):
    """未打分仍提交看过，已看过不能被后续单集播放覆盖为在看。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, _saved, _updates = _build_helper(module)
    matches = []
    monkeypatch.setattr(helper, "_resolve_douban_info", lambda *_args, **kwargs: matches.append(kwargs) or {"id": "456", "title": "Test 第二季"})
    douban, posts, skips = _douban()
    watching = {}
    assert helper.sync_one_progress(_state(), "show", module.MediaType.TV, watching, douban, False)
    assert posts == [{"subject_id": "456", "status": "collect", "private": False, "rating": None}]
    assert matches == [{"season": 2, "season_year": "2026"}]
    assert helper.sync_one_progress(_state(False), "show", module.MediaType.TV, watching, douban, False)
    assert len(posts) == 1 and skips == [True]
    assert watching["电视剧_1_s2"]["status"] == "看完"


def test_completion_preserves_known_rating_and_updates_only_matching_season(monkeypatch):
    """季度目标分开缓存，状态变化不清空已有同步评分。"""
    module = _load_trakt_helper_module(monkeypatch)
    url = "https://movie.douban.com/j/subject/456/interest"
    helper, _saved, _updates = _build_helper(module, data={"douban_sync_state": {"synced": {url: {"interest": "do", "rating": "4"}}}},
                                            manual_mappings={"show:1:s2": "456", "show:1:s1": "123"})
    douban, posts, _skips = _douban()
    watching = {"电视剧_1_s1": {"douban_id": "123", "season": 1, "status": "在看", "show": _state()["show"]}}
    original = copy.deepcopy(watching["电视剧_1_s1"])
    assert helper.sync_one_progress(_state(), "show", module.MediaType.TV, watching, douban, False)
    assert posts[0]["rating"] == 4
    assert watching["电视剧_1_s1"] == original


def test_unmatched_or_conflicting_season_never_posts_or_marks_success(monkeypatch):
    """匹配失败或两季落到同一豆瓣条目时不写入。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, _saved, _updates = _build_helper(module)
    douban, posts, _skips = _douban()
    watching = {}
    monkeypatch.setattr(helper, "_resolve_douban_info", lambda *_args, **_kwargs: {})
    assert not helper.sync_one_progress(_state(), "show", module.MediaType.TV, watching, douban, False)
    watching["电视剧_1_s1"] = {"douban_id": "123", "season": 1, "show": _state()["show"]}
    monkeypatch.setattr(helper, "_resolve_douban_info", lambda *_args, **_kwargs: {"id": "123"})
    assert not helper.sync_one_progress(_state(), "show", module.MediaType.TV, watching, douban, False)
    assert not posts and "电视剧_1_s2" not in watching


def test_failed_post_keeps_existing_watching_cache(monkeypatch):
    """配额延期或提交失败不得提前把在看缓存改成看过。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, _saved, _updates = _build_helper(module)
    douban, _posts, _skips = _douban()
    douban.set_watching_status = lambda **_kwargs: False
    watching = {"电视剧_1_s2": {"douban_id": "456", "status": "在看", "private": False}}
    before = copy.deepcopy(watching)
    assert not helper.sync_one_progress(_state(), "show", module.MediaType.TV, watching, douban, False)
    assert watching == before


def test_generic_manual_mapping_is_not_reused_for_other_seasons(monkeypatch):
    """旧映射仅兼容第一季，第二季需要显式季度映射。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, _saved, _updates = _build_helper(module, manual_mappings={"show:1": "123", "tmdb:2:s2": "456"})
    show = _state()["show"]
    assert helper._lookup_manual_douban_id(show, module.MediaType.TV, "first", season=1) == "123"
    assert helper._lookup_manual_douban_id(show, module.MediaType.TV, "second", season=2) == "456"
    assert helper._lookup_manual_douban_id(show, module.MediaType.TV, "third", season=3) is None


def test_season_matching_passes_correct_season_and_year_to_core(monkeypatch):
    """绕过未透传季号的旧桥接，使用核心显式按季匹配接口。"""
    module = _load_trakt_helper_module(monkeypatch)
    calls = []
    async def metadata(**_kwargs):
        """返回本地化剧名。"""
        return {"name": "测试剧"}
    async def match(**kwargs):
        """记录匹配条件。"""
        calls.append(kwargs)
        return {"id": "456"}
    monkeypatch.setattr(module, "MediaChain", lambda: types.SimpleNamespace(async_tmdb_info=metadata, async_match_doubaninfo=match))
    helper, _saved, _updates = _build_helper(module)
    assert asyncio.run(helper._get_douban_info_by_tmdb(2, "tt2", "Test", 2020, module.MediaType.TV, season=2, season_year="2026"))["id"] == "456"
    assert calls[0]["season"] == 2 and calls[0]["year"] == "2026" and calls[0]["name"] == "测试剧"


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_completion_request_failure_is_unknown_not_empty(monkeypatch, status):
    """授权、限流或服务失败保留未知状态，并单独识别401。"""
    module = _load_trakt_helper_module(monkeypatch)
    calls = []
    monkeypatch.setattr(module, "RequestUtils", lambda **kwargs: types.SimpleNamespace(get_res=lambda **request: calls.append((kwargs, request)) or _Response(status)))
    helper, _saved, _updates = _build_helper(module)
    assert helper.fetch_show_progress("1", "token") is None
    assert helper.has_oauth_unauthorized() is (status == 401)
    assert calls[0][1]["params"] == {"hidden": "true", "specials": "false", "last_activity": "watched"}
    assert calls[0][0]["headers"]["Authorization"] == "Bearer token"


def test_tracked_season_is_checked_after_leaving_recent_history_window(monkeypatch):
    """最近来源为空时仍检查已跟踪在看季，没有评分也会提交完成候选。"""
    helper_module = _load_trakt_helper_module(monkeypatch)
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    helper, _saved, _updates = _build_helper(helper_module)
    progress, metadata = _season(number=2)
    calls = []
    helper.get_access_token = lambda **_kwargs: "token"
    helper.fetch_show_progress = lambda *_args: {"seasons": [progress]}
    helper.fetch_show_seasons = lambda *_args: [metadata]
    helper.sync_one_progress = lambda item, *_args: calls.append(item) or True
    plugin._trakt_helper = helper
    plugin._douban_helper = _douban()[0]
    plugin._data["watching"] = {"电视剧_1_s2": {"status": "在看", "season": 2, "show": _state()["show"]}}
    monkeypatch.setattr(plugin, "_fetch_trakt_progress_sources", lambda *_args: ([], []))
    plugin._sync_progress()
    assert len(calls) == 1 and calls[0]["completed"] is True
    assert "电视剧_1_s2" in plugin._data["watching"]


def test_legacy_watching_cache_is_enriched_and_kept_after_season_tracking(monkeypatch):
    """旧版缓存只含TraktID时补全媒体信息；迁移后保留历史并停止重复旧入口检查。"""
    helper_module = _load_trakt_helper_module(monkeypatch)
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    helper, _saved, _updates = _build_helper(helper_module)
    progress, metadata = _season()
    queries = []
    helper.get_access_token = lambda **_kwargs: "token"
    helper.fetch_show_details = lambda show_id: queries.append(show_id) or _state()["show"]
    helper.fetch_show_progress = lambda *_args: {"seasons": [progress]}
    helper.fetch_show_seasons = lambda *_args: [metadata]
    helper.sync_one_progress = lambda *_args: True
    plugin._trakt_helper = helper
    plugin._douban_helper = _douban()[0]
    plugin._data["watching"] = {"电视剧_1": {"status": "在看", "douban_id": "123"}}
    monkeypatch.setattr(plugin, "_fetch_trakt_progress_sources", lambda *_args: ([], []))
    plugin._sync_progress()
    assert queries == ["1"]
    assert plugin._data["watching"]["电视剧_1"]["season_tracking"] is True
    plugin._sync_progress()
    assert queries == ["1"]


def test_progress_failure_does_not_attempt_season_write(monkeypatch):
    """已取得最近观看记录但完整季度接口失败时不能据此改写豆瓣。"""
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    show = _state()["show"]
    plugin._trakt_helper = types.SimpleNamespace(get_access_token=lambda **_kwargs: "token", has_oauth_unauthorized=lambda: False,
                                               fetch_show_progress=lambda *_args: None, fetch_show_seasons=lambda *_args: [],
                                               sync_one_progress=lambda *_args: pytest.fail("未知完成状态不能写入"))
    plugin._douban_helper = _douban()[0]
    monkeypatch.setattr(plugin, "_fetch_trakt_progress_sources", lambda *_args: ([], [{"show": show}]))
    plugin._sync_progress()
    assert plugin._data["notification_issues"]["Trakt"]["active"]


def test_recent_season_does_not_backfill_all_previously_watched_seasons(monkeypatch):
    """单集来源定位第二季，不因完整进度接口含所有历史季而批量回填。"""
    helper_module = _load_trakt_helper_module(monkeypatch)
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    helper, _saved, _updates = _build_helper(helper_module)
    data = [_season(number=number) for number in range(1, 4)]
    calls = []
    helper.get_access_token = lambda **_kwargs: "token"
    helper.fetch_show_progress = lambda *_args: {"seasons": [row[0] for row in data]}
    helper.fetch_show_seasons = lambda *_args: [row[1] for row in data]
    helper.sync_one_progress = lambda item, *_args: calls.append(item) or True
    plugin._trakt_helper = helper
    plugin._douban_helper = _douban()[0]
    monkeypatch.setattr(plugin, "_fetch_trakt_progress_sources", lambda *_args: ([], [{"show": _state()["show"], "episode": {"season": 2}}]))
    plugin._sync_progress()
    assert [item["season"] for item in calls] == [2]


def test_legacy_show_metadata_is_cached_without_credentials(monkeypatch):
    """旧缓存补全查询成功后复用私有媒体信息，不重复请求或保存鉴权头。"""
    module = _load_trakt_helper_module(monkeypatch)
    calls = []
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(
        get_res=lambda **request: calls.append(request) or _Response(200, _state()["show"])))
    helper, saved, _updates = _build_helper(module)
    assert helper.fetch_show_details("1") == _state()["show"]
    assert helper.fetch_show_details("1") == _state()["show"]
    assert len(calls) == 1
    assert "token" not in str(saved) and "Authorization" not in str(saved)
