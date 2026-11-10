"""V2 正对照及 V3 冷缓存同步入口回归，不导入真实宿主。"""
import asyncio
import importlib.util
import threading
import types
from pathlib import Path

import pytest

from test_traktratingssync_v3_compatibility import _MediaSource, v3_helper


@pytest.mark.parametrize("payload", [None, {}])
def test_v3_explicit_no_match_is_not_transient(monkeypatch, v3_helper, payload):
    """空结果保持普通未命中语义，不转向 Trakt 标题兜底。"""
    module, helper, _saved, _updated = v3_helper

    async def convert(**_kwargs):
        """回放宿主未命中。"""
        return payload

    monkeypatch.setattr(module, "MediaChain", lambda: types.SimpleNamespace(async_convert_media_identity=convert))
    assert asyncio.run(helper._get_douban_info_by_tmdb(437068, "tt6878038", "Test")) == {}
    assert helper._match_transient is False


@pytest.mark.parametrize("outcome", [None, {}, {"id": "123", "title": "V2"}, TimeoutError("offline")])
def test_v2_existing_bridge_remains_authoritative(monkeypatch, v3_helper, outcome):
    """旧 API 存在时无论成功、空或异常均不尝试 V3，且不要求新枚举。"""
    import sys
    module, helper, _saved, _updated = v3_helper
    calls = []
    monkeypatch.delattr(sys.modules["app.schemas.types"], "MediaSource")

    async def legacy(tmdbid, mtype):
        """保留旧签名和空结果/异常区别。"""
        calls.append((tmdbid, mtype))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def convert(**_kwargs):
        """不可在 V2 后进行双重搜索。"""
        pytest.fail("不允许 V2 失败后尝试 V3")

    monkeypatch.setattr(module, "MediaChain", lambda: types.SimpleNamespace(
        async_get_doubaninfo_by_tmdbid=legacy, async_convert_media_identity=convert,
    ))
    result = asyncio.run(helper._get_douban_info_by_tmdb(437068, "tt6878038", "Test"))
    assert result == (outcome if isinstance(outcome, dict) else {})
    assert calls == [(437068, module.MediaType.MOVIE)]
    assert helper._match_transient is isinstance(outcome, Exception)


@pytest.mark.parametrize("source", [_MediaSource.Douban, "douban"])
@pytest.mark.parametrize("subject", [123, "123"])
def test_v3_valid_explicit_douban_details_keep_native_id(monkeypatch, v3_helper, source, subject):
    """确认豆瓣来源的原始数字/字符串 ID 不被重写。"""
    module, helper, _saved, _updated = v3_helper
    payload = {"id": subject, "source": source, "media_source": source, "title": "正确目标"}

    async def convert(**_kwargs):
        """回放合法原始详情。"""
        return payload

    monkeypatch.setattr(module, "MediaChain", lambda: types.SimpleNamespace(async_convert_media_identity=convert))
    assert asyncio.run(helper._get_douban_info_by_tmdb(437068, None, "Test")) == payload
    assert helper._match_transient is False


@pytest.mark.parametrize("title,year,accepted", [
    ("测试剧 第二季", "2026", True), ("测试剧", "2026", False),
    ("测试剧 第一季", "2026", False), ("测试剧 第二季", "2025", False),
])
def test_v3_season_keeps_season_year_checks_without_series_imdb(monkeypatch, v3_helper, title, year, accepted):
    """V3 仍显式按季匹配，禁止整剧转换或整剧 IMDb 优先直返。"""
    module, helper, _saved, _updated = v3_helper
    calls = []
    details = {"id": "456", "title": title, "year": year}

    class MediaChainV3:
        """参数签名来自官方 v3.2.5 ChainBase。"""

        async def async_tmdb_info(self, tmdbid, mtype, season=None):
            """返回已本地化名称。"""
            calls.append(("tmdb", tmdbid, mtype, season))
            return {"name": "测试剧"}

        async def async_match_doubaninfo(self, name, imdbid=None, mtype=None, year=None, season=None, raise_exception=False):
            """记录匹配参数并给出季度候选。"""
            calls.append(("match", name, imdbid, mtype, year, season, raise_exception))
            return details

        async def async_convert_media_identity(self, **_kwargs):
            """不核对季号的整剧转换不能用于季度。"""
            pytest.fail("季度不能使用整剧转换")

    monkeypatch.setattr(module, "MediaChain", MediaChainV3)
    result = asyncio.run(helper._get_douban_info_by_tmdb(
        2, "tt2", "Test", 2020, module.MediaType.TV, season=2, season_year="2026",
    ))
    assert result == (details if accepted else {})
    assert calls == [("tmdb", 2, module.MediaType.TV, None),
                     ("match", "测试剧", None, module.MediaType.TV, "2026", 2, False)]


@pytest.fixture
def movie_sync(monkeypatch, v3_helper):
    """使用真实包装、事件循环和匹配管理器，以内存替代持久化及豆瓣提交。"""
    module, helper, saved, updated = v3_helper
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(module.global_vars, "loop", loop)
    path = Path(module.__file__).with_name("matching_helper.py")
    spec = importlib.util.spec_from_file_location("v3_matching_test", path)
    matching = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(matching)
    helper._matching = matching.MatchingHelper(helper._save_data, helper._get_data)
    posts = []
    douban = types.SimpleNamespace(set_target_context=lambda *_args: None,
                                  set_watching_status=lambda **kwargs: posts.append(kwargs) or True)
    item = {"rating": 8, "movie": {"title": "Test", "year": 2017, "ids": {"trakt": 1, "tmdb": 437068, "imdb": "tt6878038"}}}
    try:
        yield module, helper, saved, updated, posts, douban, item
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()
        assert not thread.is_alive()


@pytest.mark.parametrize("mode", ["rating", "watched"])
@pytest.mark.parametrize("outcome", ["success", "wrong_source", "malformed", "identity", "missing_api", "exception", "no_match"])
def test_uncached_movie_sync_converts_or_defers_without_writing(monkeypatch, movie_sync, mode, outcome):
    """真实电影评分与看过入口均用冷缓存，错误不写入且保留正确匹配延期。"""
    module, helper, saved, _updated, posts, douban, item = movie_sync
    calls = []

    async def convert(target_source, media_source, media_id, mtype=None, season=None):
        """只回放宿主调用，不替换插件解析或同步逻辑。"""
        calls.append((target_source, media_source, media_id, mtype, season))
        if outcome == "exception":
            raise TimeoutError("offline")
        if outcome == "identity":
            return types.SimpleNamespace(media_source=_MediaSource.Douban, media_id="123")
        if outcome == "no_match":
            return {}
        if outcome == "malformed":
            return {"id": True}
        return {"id": "123", "title": "正确电影", "source": "tmdb" if outcome == "wrong_source" else "douban"}

    chain = types.SimpleNamespace()
    if outcome != "missing_api":
        chain.async_convert_media_identity = convert
    monkeypatch.setattr(module, "MediaChain", lambda: chain)
    finished, wait_retry = {}, {}
    before = int(module.time.time())
    if mode == "rating":
        result = helper.sync_one_rate(item, finished, wait_retry, module.MediaType.MOVIE, douban, False)
    else:
        result = helper.sync_one_watched_movie(item, finished, douban, False)
    after = int(module.time.time())
    assert result is (outcome == "success")
    if outcome == "success":
        assert posts == [{"subject_id": "123", "status": "collect", "private": False,
                          "rating": 4 if mode == "rating" else None}]
        assert next(iter(finished.values()))["douban_id"] == "123"
        assert not helper._matching.items
    else:
        assert not posts and not finished
        record = next(iter(saved["douban_matching"]["items"].values()))
        delay = 86400 if outcome == "no_match" else 300
        assert before + delay <= record["retry_after"] <= after + delay
        assert not helper._matching.should_retry(next(iter(helper._matching.items)))
    if outcome != "missing_api":
        assert calls == [(_MediaSource.Douban, _MediaSource.TMDB, "437068", module.MediaType.MOVIE, None)]
