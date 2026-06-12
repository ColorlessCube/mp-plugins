import importlib.util
import sys
import types
from enum import Enum
from pathlib import Path


class _MediaType(Enum):
    """测试用媒体类型。"""

    MOVIE = "电影"
    TV = "电视剧"


class _Logger:
    """测试用日志桩。"""

    def debug(self, *_args, **_kwargs):
        """记录 debug 日志。"""

    def info(self, *_args, **_kwargs):
        """记录 info 日志。"""

    def warn(self, *_args, **_kwargs):
        """记录 warn 日志。"""

    def warning(self, *_args, **_kwargs):
        """记录 warning 日志。"""

    def error(self, *_args, **_kwargs):
        """记录 error 日志。"""


class _Response:
    """测试用 HTTP 响应。"""

    def __init__(self, status_code=200, data=None, text="ok"):
        """初始化响应状态与 JSON 数据。"""
        self.status_code = status_code
        self._data = data or {"code": 0, "data": {"ok": True}}
        self.text = text
        self.cookies = {}

    def json(self):
        """返回 JSON 数据。"""
        return self._data


class _TTLCache:
    """测试用缓存桩。"""

    def __init__(self, *_args, **_kwargs):
        """初始化内存缓存。"""
        self._data = {}

    def get(self, key):
        """读取缓存。"""
        return self._data.get(key)

    def set(self, key, value):
        """写入缓存。"""
        self._data[key] = value

    def delete(self, key):
        """删除缓存。"""
        self._data.pop(key, None)

    def clear(self):
        """清空缓存。"""
        self._data.clear()


class _EventManager:
    """测试用事件管理器。"""

    @staticmethod
    def register(_event_type):
        """返回原函数的注册装饰器。"""

        def decorator(func):
            """保持被装饰函数不变。"""
            return func

        return decorator


def _load_quark_api_module(monkeypatch):
    """加载夸克 API 模块并替换 MoviePilot 边界依赖。"""
    calls = []

    class RequestUtilsStub:
        """记录 RequestUtils 调用的测试桩。"""

        def __init__(self, **kwargs):
            """记录初始化参数。"""
            calls.append(("init", kwargs))

        def request(self, **kwargs):
            """记录通用请求参数。"""
            calls.append(("request", kwargs))
            return _Response()

    monkeypatch.setitem(sys.modules, "schemas", types.SimpleNamespace(FileItem=object, StorageUsage=object))
    monkeypatch.setitem(sys.modules, "app.core.cache", types.SimpleNamespace(TTLCache=_TTLCache))
    monkeypatch.setitem(sys.modules, "app.core.config", types.SimpleNamespace(settings=types.SimpleNamespace(TEMP_PATH="/tmp")))
    monkeypatch.setitem(sys.modules, "app.log", types.SimpleNamespace(logger=_Logger()))
    monkeypatch.setitem(sys.modules, "app.utils.http", types.SimpleNamespace(RequestUtils=RequestUtilsStub))

    module_path = (
        Path(__file__).resolve().parents[1]
        / "plugins"
        / "codexquarkdisk"
        / "quark_api.py"
    )
    spec = importlib.util.spec_from_file_location("codexquarkdisk_quark_api_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, calls


def _load_scraper_module(monkeypatch):
    """加载媒体库刮削插件并替换 MoviePilot 边界依赖。"""
    settings = types.SimpleNamespace(
        TZ="Asia/Shanghai",
        RMT_MEDIAEXT=[".mkv", ".mp4"],
        TV_RENAME_FORMAT="{title}/Season {season}/{name}",
        MOVIE_RENAME_FORMAT="{title}/{name}",
        SCRAP_FOLLOW_TMDB=True,
    )
    monkeypatch.setitem(sys.modules, "app", types.SimpleNamespace(schemas=types.SimpleNamespace(FileItem=object)))
    monkeypatch.setitem(sys.modules, "app.chain.media", types.SimpleNamespace(MediaChain=object))
    monkeypatch.setitem(sys.modules, "app.chain.storage", types.SimpleNamespace(StorageChain=object))
    monkeypatch.setitem(sys.modules, "app.core.config", types.SimpleNamespace(settings=settings))
    monkeypatch.setitem(sys.modules, "app.core.metainfo", types.SimpleNamespace(MetaInfoPath=object))
    monkeypatch.setitem(sys.modules, "app.db.transferhistory_oper", types.SimpleNamespace(TransferHistoryOper=object))
    monkeypatch.setitem(sys.modules, "app.helper.nfo", types.SimpleNamespace(NfoReader=object))
    monkeypatch.setitem(sys.modules, "app.log", types.SimpleNamespace(logger=_Logger()))
    monkeypatch.setitem(sys.modules, "app.plugins", types.SimpleNamespace(_PluginBase=object))
    monkeypatch.setitem(sys.modules, "app.schemas", types.SimpleNamespace(MediaType=_MediaType, FileItem=object))
    monkeypatch.setitem(sys.modules, "app.utils.system", types.SimpleNamespace(SystemUtils=object))
    monkeypatch.setitem(
        sys.modules,
        "apscheduler.schedulers.background",
        types.SimpleNamespace(BackgroundScheduler=object),
    )
    monkeypatch.setitem(
        sys.modules,
        "apscheduler.triggers.cron",
        types.SimpleNamespace(CronTrigger=types.SimpleNamespace(from_crontab=lambda value: value)),
    )

    module_path = (
        Path(__file__).resolve().parents[1]
        / "plugins"
        / "codexlibraryscraper"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location("codexlibraryscraper_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_quark_api_request_uses_request_utils(monkeypatch):
    """夸克 API 请求应通过 RequestUtils 发出。"""
    module, calls = _load_quark_api_module(monkeypatch)
    api = module.QuarkApi.__new__(module.QuarkApi)
    api._headers = {"Cookie": "cookie", "User-Agent": "ua"}
    api._base_url = "https://pan.quark.cn/1/clouddrive"
    api._drive_url = "https://drive.quark.cn/1/clouddrive"
    api._drive_pc_url = "https://drive-pc.quark.cn/1/clouddrive"
    api._max_retries = 1
    api._timeout = 30
    api._cookie = "cookie"

    result = api._request(
        "/file/sort",
        params={"pdir_fid": "0"},
        use_drive=True,
    )

    assert result["data"]["ok"] is True
    assert calls[0] == ("init", {"headers": {"Cookie": "cookie", "User-Agent": "ua"}, "timeout": 30})
    assert calls[1][0] == "request"
    assert calls[1][1]["method"] == "get"
    assert calls[1][1]["url"] == "https://drive.quark.cn/1/clouddrive/file/sort"
    assert calls[1][1]["params"]["pdir_fid"] == "0"


def test_library_scraper_parses_storage_path_with_media_type(monkeypatch):
    """刮削路径应支持存储名路径和显式媒体类型。"""
    module = _load_scraper_module(monkeypatch)
    plugin = module.CodexLibraryScraper.__new__(module.CodexLibraryScraper)

    storage, path, media_type = plugin._CodexLibraryScraper__parse_scraper_path("夸克网盘:/电影/示例#电影")

    assert storage == "夸克网盘"
    assert path == Path("/电影/示例")
    assert media_type == module.MediaType.MOVIE
