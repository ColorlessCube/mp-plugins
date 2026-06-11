"""豆瓣书影音同步插件的网易云开放平台 Helper 测试。"""
import importlib.util
from pathlib import Path


def _load_helper_module():
    """从插件仓库源码直接加载网易云开放平台 Helper。"""
    repo_root = Path(__file__).resolve().parents[3]
    import sys

    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    helper_path = (
        Path(__file__).resolve().parents[1]
        / "plugins"
        / "traktratingssync"
        / "netease_openapi_helper.py"
    )
    spec = importlib.util.spec_from_file_location("netease_openapi_helper", helper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_normalize_device_id_generates_stable_alnum_value():
    """未配置设备 ID 时，应生成稳定且符合网易云开放平台约束的值。"""
    helper_module = _load_helper_module()
    first = helper_module.NeteaseOpenApiHelper.normalize_device_id("", "app-id")
    second = helper_module.NeteaseOpenApiHelper.normalize_device_id("", "app-id")

    assert first == second
    assert first.isalnum()
    assert len(first) <= 64


def test_format_album_records_keeps_latest_play_order():
    """最近播放专辑记录应转换为插件统一结构并按播放时间倒序。"""
    helper_module = _load_helper_module()
    records = [
        {
            "record": {
                "id": "a1",
                "name": "Album A",
                "artists": [{"name": "Artist A"}],
                "coverImgUrl": "https://example.com/a.jpg",
            },
            "playTime": 100,
        },
        {
            "record": {
                "id": "b1",
                "name": "Album B",
                "artists": [{"name": "Artist B"}],
            },
            "playTime": 200,
        },
        {
            "record": {
                "id": "a1",
                "name": "Album A",
                "artists": [{"name": "Artist A"}],
            },
            "playTime": 300,
        },
    ]

    albums = helper_module.NeteaseOpenApiHelper._format_album_records(records=records, limit=10)

    assert [album["album"] for album in albums] == ["Album A", "Album B"]
    assert albums[0]["artist"] == "Artist A"
    assert albums[0]["total_play_count"] == 2
    assert albums[0]["play_time"] == 300
