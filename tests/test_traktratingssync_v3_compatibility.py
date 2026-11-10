"""MoviePilot v3.2.5 媒体链契约回归；仅宿主边界使用离线桩。"""
import asyncio
import sys
from enum import Enum

import pytest

from test_traktratingssync_trakt_helper import _build_helper, _load_trakt_helper_module


class _MediaSource(str, Enum):
    """保留官方 v3.2.5 的来源名称和值。"""

    TMDB = "themoviedb"
    Douban = "douban"


@pytest.fixture
def v3_helper(monkeypatch):
    """仅安装宿主边界，直接加载真实 TraktHelper，初始缓存为空。"""
    module = _load_trakt_helper_module(monkeypatch)
    monkeypatch.setattr(sys.modules["app.schemas.types"], "MediaSource", _MediaSource, raising=False)
    helper, saved, updated = _build_helper(module)
    return module, helper, saved, updated


def test_v3_uncached_movie_converts_tmdb_to_raw_douban_details(monkeypatch, v3_helper):
    """无旧 API、无缓存时按官方签名转换，并保留返回的原始豆瓣详情。"""
    module, helper, saved, updated = v3_helper
    calls = []
    details = {"id": "27099082", "title": "出租车司机", "alt_title": "出租车司机", "year": "2017"}

    class MediaChainV3:
        """签名及原始 dict 返回契约来自官方 tag v3.2.5 projection.py:556。"""

        async def async_convert_media_identity(self, target_source, media_source, media_id, mtype=None, season=None):
            """记录完整转换参数，拒绝错误关键字。"""
            calls.append((target_source, media_source, media_id, mtype, season))
            return details

        async def async_match_doubaninfo(self, **_kwargs):
            """TMDB 转换失败也不能另用 Trakt 标题兜底。"""
            pytest.fail("有 TMDB ID 时禁止独立标题匹配")

    monkeypatch.setattr(module, "MediaChain", MediaChainV3)
    result = asyncio.run(helper._get_douban_info_by_tmdb(
        437068, "tt6878038", "A Taxi Driver", 2017, module.MediaType.MOVIE,
    ))

    assert result == details
    assert calls == [(_MediaSource.Douban, _MediaSource.TMDB, "437068", module.MediaType.MOVIE, None)]
    assert helper._match_transient is False
    assert saved == updated == {}


@pytest.mark.parametrize("source_key", ["media_source", "source"])
@pytest.mark.parametrize("source", ["themoviedb", "imdb", "doubanmusic", "", None])
def test_v3_explicit_non_douban_source_is_rejected(monkeypatch, v3_helper, source_key, source):
    """转换结果显式声明非豆瓣来源或空来源时不能把原生 ID 当成豆瓣 ID。"""
    module, helper, _saved, _updated = v3_helper

    async def convert(target_source, media_source, media_id, mtype=None, season=None):
        """返回带数字 ID 的错误来源，避免仅检测 id 就误写。"""
        return {"id": "437068", source_key: source, "title": "错误来源"}

    monkeypatch.setattr(module, "MediaChain", lambda: type("Chain", (), {
        "async_convert_media_identity": staticmethod(convert),
    })())
    result = asyncio.run(helper._get_douban_info_by_tmdb(437068, "tt6878038", "A Taxi Driver"))

    assert result == {}
    assert helper._match_transient is True


@pytest.mark.parametrize("payload", [
    {"id": True}, {"id": 0}, {"id": -1}, {"id": 1.5}, {"id": "0"}, {"id": "000"},
    {"id": "-1"}, {"id": " 123"}, {"id": "123 "}, {"id": "１２３"}, {"id": "12/34"},
    {"id": []}, {"id": None}, {"title": "没有原生 ID"},
    {"media_source": "douban", "media_id": "123"},
    [{"id": "123"}], False, "", 0,
])
def test_v3_malformed_conversion_is_transient_not_a_subject(monkeypatch, v3_helper, payload):
    """仅官方原始详情字典中的有效数字 id 可作为目标，结构异常保守延期。"""
    module, helper, _saved, _updated = v3_helper

    async def convert(**_kwargs):
        """隔离转换输出边界。"""
        return payload

    monkeypatch.setattr(module, "MediaChain", lambda: type("Chain", (), {
        "async_convert_media_identity": staticmethod(convert),
    })())
    assert asyncio.run(helper._get_douban_info_by_tmdb(437068, None, "Test")) == {}
    assert helper._match_transient is True


def test_missing_v3_source_enum_defers_before_conversion(monkeypatch, v3_helper):
    """宿主无来源枚举时不能猜枚举值或静默继续转换。"""
    module, helper, _saved, _updated = v3_helper
    monkeypatch.delattr(sys.modules["app.schemas.types"], "MediaSource")

    async def convert(**_kwargs):
        """缺少必要契约时不允许调用。"""
        pytest.fail("没有 MediaSource 不能转换")

    monkeypatch.setattr(module, "MediaChain", lambda: type("Chain", (), {
        "async_convert_media_identity": staticmethod(convert),
    })())
    assert asyncio.run(helper._get_douban_info_by_tmdb(437068, None, "Test")) == {}
    assert helper._match_transient is True
