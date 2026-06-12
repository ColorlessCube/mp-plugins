from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import List, Tuple, Dict, Any, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.core.config import settings
from app.core.metainfo import MetaInfoPath
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.nfo import NfoReader
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaType
from app.utils.system import SystemUtils


class CodexLibraryScraper(_PluginBase):
    """
    媒体库刮削插件，支持本地目录与 MoviePilot 存储目录刮削。
    """

    # 插件名称
    plugin_name = "媒体库刮削（本地版）"
    # 插件描述
    plugin_desc = "本地维护的媒体库刮削插件，支持本地目录和 MoviePilot 存储目录。"
    # 插件图标
    plugin_icon = "scraper.png"
    # 插件版本
    plugin_version = "2.2.2"
    # 插件作者
    plugin_author = "MoviePilot Local"
    # 作者主页
    author_url = ""
    # 插件配置项ID前缀
    plugin_config_prefix = "codex_libraryscraper_"
    # 加载顺序
    plugin_order = 7
    # 可使用的用户级别
    user_level = 1

    # 私有属性
    _scheduler = None
    _storagechain = None
    _scraper = None
    # 限速开关
    _enabled = False
    _onlyonce = False
    _cron = None
    _mode = ""
    _scraper_paths = ""
    _exclude_paths = ""
    # 退出事件
    _event = Event()

    def init_plugin(self, config: dict = None):
        """
        初始化插件配置并按需创建一次性刮削任务。

        :param config: 插件配置字典
        """

        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._mode = config.get("mode") or ""
            self._scraper_paths = config.get("scraper_paths") or ""
            self._exclude_paths = config.get("exclude_paths") or ""

        # 停止现有任务
        self.stop_service()

        # 启动定时任务 & 立即运行一次
        if self._enabled or self._onlyonce:

            if self._onlyonce:
                logger.info(f"媒体库刮削（本地版）服务，立即运行一次")
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(func=self.__libraryscraper, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="媒体库刮削（本地版）")
                # 关闭一次性开关
                self._onlyonce = False
                self.update_config({
                    "onlyonce": False,
                    "enabled": self._enabled,
                    "cron": self._cron,
                    "mode": self._mode,
                    "scraper_paths": self._scraper_paths,
                    "exclude_paths": self._exclude_paths
                })
                if self._scheduler.get_jobs():
                    # 启动服务
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def get_state(self) -> bool:
        """
        获取插件启用状态。
        """
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        获取插件命令列表。
        """
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件 API 路由。
        """
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            return [{
                "id": "CodexLibraryScraper",
                "name": "媒体库刮削（本地版）",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.__libraryscraper,
                "kwargs": {}
            }]
        elif self._enabled:
            return [{
                "id": "CodexLibraryScraper",
                "name": "媒体库刮削（本地版）",
                "trigger": CronTrigger.from_crontab("0 0 */7 * *"),
                "func": self.__libraryscraper,
                "kwargs": {}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        获取插件配置页面与默认配置。
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'mode',
                                            'label': '覆盖模式',
                                            'items': [
                                                {'title': '不覆盖已有元数据', 'value': ''},
                                                {'title': '覆盖所有元数据和图片', 'value': 'force_all'},
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'scraper_paths',
                                            'label': '刮削路径',
                                            'rows': 5,
                                            'placeholder': '每一行一个目录，支持 /media/movies 或 夸克网盘:/电影'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'exclude_paths',
                                            'label': '排除路径',
                                            'rows': 2,
                                            'placeholder': '每一行一个目录'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '刮削路径后拼接#电视剧/电影，强制指定该媒体路径媒体类型。'
                                                    '不加默认根据文件名自动识别媒体类型。'
                                                    '存储目录示例：夸克网盘:/电影#电影'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "cron": "0 0 */7 * *",
            "mode": "",
            "scraper_paths": "",
            "exclude_paths": ""
        }

    def get_page(self) -> List[dict]:
        """
        获取插件详情页面配置。
        """
        return []

    def __libraryscraper(self):
        """
        开始刮削媒体库
        """
        if not self._scraper_paths:
            return
        exclude_paths = self.__parse_exclude_paths()
        scraper_paths: List[Tuple[schemas.FileItem, MediaType]] = []
        scraper_seen = set()
        for raw_path in self._scraper_paths.split("\n"):
            raw_path = (raw_path or "").strip()
            if not raw_path:
                continue
            storage, path, mtype = self.__parse_scraper_path(raw_path)
            if storage == "local":
                self.__collect_local_dirs(
                    path=path,
                    mtype=mtype,
                    exclude_paths=exclude_paths,
                    scraper_paths=scraper_paths,
                    scraper_seen=scraper_seen,
                )
            else:
                self.__collect_storage_dirs(
                    storage=storage,
                    path=path,
                    mtype=mtype,
                    exclude_paths=exclude_paths,
                    scraper_paths=scraper_paths,
                    scraper_seen=scraper_seen,
                )
        # 开始刮削
        if scraper_paths:
            for fileitem, mtype in scraper_paths:
                logger.info(f"开始刮削目录：{fileitem.storage}:{fileitem.path} ...")
                self.__scrape_dir(fileitem=fileitem, mtype=mtype)
        else:
            logger.info(f"未发现需要刮削的目录")

    @property
    def storagechain(self) -> StorageChain:
        """
        获取存储处理链实例。
        """
        if not self._storagechain:
            self._storagechain = StorageChain()
        return self._storagechain

    @staticmethod
    def __parse_media_type(value: Optional[str]) -> Optional[MediaType]:
        value = (value or "").strip()
        if not value:
            return None
        return next(
            (
                media_type
                for media_type in MediaType.__members__.values()
                if media_type.value == value
            ),
            None,
        )

    def __parse_scraper_path(self, raw_path: str) -> Tuple[str, Path, Optional[MediaType]]:
        path = raw_path
        mtype = None
        if raw_path.count("#") == 1:
            path, mtype_text = raw_path.split("#", 1)
            mtype = self.__parse_media_type(mtype_text)
            if mtype_text and not mtype:
                logger.warning(f"媒体库刮削媒体类型无效：{mtype_text}")
        storage, path = self.__parse_storage_path(path)
        return storage, path, mtype

    @staticmethod
    def __parse_storage_path(path_text: str) -> Tuple[str, Path]:
        path_text = (path_text or "").strip()
        if ":" in path_text:
            storage, path = path_text.split(":", 1)
            if storage and path.startswith("/"):
                return storage.strip(), Path(path)
        return "local", Path(path_text)

    def __parse_exclude_paths(self) -> List[Tuple[str, Path]]:
        exclude_paths = []
        for raw_path in (self._exclude_paths or "").split("\n"):
            raw_path = raw_path.strip()
            if not raw_path:
                continue
            storage, path = self.__parse_storage_path(raw_path)
            exclude_paths.append((storage, path))
        return exclude_paths

    @staticmethod
    def __path_is_relative_to(path: Path, parent: Path) -> bool:
        try:
            return path == parent or path.is_relative_to(parent)
        except ValueError:
            return False

    def __is_excluded(self, storage: str, path: Path, exclude_paths: List[Tuple[str, Path]]) -> bool:
        for exclude_storage, exclude_path in exclude_paths:
            if storage != exclude_storage:
                continue
            if self.__path_is_relative_to(path, exclude_path):
                return True
        return False

    @staticmethod
    def __normalize_storage_path(path: Path) -> Path:
        path_text = path.as_posix()
        if not path_text.startswith("/"):
            path_text = f"/{path_text}"
        return Path(path_text)

    @staticmethod
    def __is_media_file(path: Path) -> bool:
        return path.suffix.lower() in settings.RMT_MEDIAEXT

    @staticmethod
    def __detect_media_type(path: Path) -> Optional[MediaType]:
        try:
            mtype = MetaInfoPath(path).type
        except Exception as err:
            logger.debug(f"识别媒体类型失败：{path} {err}")
            return None
        if mtype in (MediaType.MOVIE, MediaType.TV):
            return mtype
        return None

    @staticmethod
    def __get_media_dir_path(file_path: Path, mtype: MediaType) -> Optional[Path]:
        rename_format = settings.TV_RENAME_FORMAT \
            if mtype == MediaType.TV else settings.MOVIE_RENAME_FORMAT
        rename_format_level = len(rename_format.split("/")) - 1
        if rename_format_level < 1:
            return None
        try:
            return file_path.parents[rename_format_level - 1]
        except IndexError:
            logger.warning(f"无法根据重命名格式计算媒体目录：{file_path}")
            return None

    @staticmethod
    def __build_dir_fileitem(storage: str, path: Path, name: Optional[str] = None,
                             modify_time: Optional[float] = None) -> schemas.FileItem:
        path_text = path.as_posix()
        if path_text != "/" and not path_text.endswith("/"):
            path_text = f"{path_text}/"
        item_name = name or path.name or "/"
        return schemas.FileItem(
            storage=storage,
            type="dir",
            path=path_text,
            name=item_name,
            basename=Path(item_name).stem if item_name != "/" else "/",
            modify_time=modify_time,
        )

    def __append_scraper_dir(self, scraper_paths: List[Tuple[schemas.FileItem, MediaType]],
                             scraper_seen: set, fileitem: schemas.FileItem, mtype: MediaType):
        key = (fileitem.storage, Path(fileitem.path).as_posix().rstrip("/"), mtype.value)
        if key in scraper_seen:
            return
        scraper_seen.add(key)
        logger.info(f"发现目录：{fileitem.storage}:{fileitem.path} {mtype.value}")
        scraper_paths.append((fileitem, mtype))

    def __collect_local_dirs(self, path: Path, mtype: Optional[MediaType],
                             exclude_paths: List[Tuple[str, Path]],
                             scraper_paths: List[Tuple[schemas.FileItem, MediaType]],
                             scraper_seen: set):
        if not path.exists():
            logger.warning(f"媒体库刮削路径不存在：{path}")
            return
        logger.info(f"开始检索本地目录：{path} {mtype} ...")
        for file_path in SystemUtils.list_files(path, settings.RMT_MEDIAEXT):
            if self._event.is_set():
                logger.info(f"媒体库刮削服务停止")
                return
            if self.__is_excluded("local", file_path, exclude_paths):
                logger.debug(f"{file_path} 在排除目录中，跳过 ...")
                continue
            file_mtype = mtype or self.__detect_media_type(file_path)
            if not file_mtype:
                continue
            if mtype and file_path.parent == path:
                media_path = path
            else:
                media_path = self.__get_media_dir_path(file_path, file_mtype)
            if not media_path:
                continue
            dir_item = self.__build_dir_fileitem(
                storage="local",
                path=media_path,
                name=media_path.name,
                modify_time=media_path.stat().st_mtime if media_path.exists() else None,
            )
            self.__append_scraper_dir(scraper_paths, scraper_seen, dir_item, file_mtype)

    def __collect_storage_dirs(self, storage: str, path: Path, mtype: Optional[MediaType],
                               exclude_paths: List[Tuple[str, Path]],
                               scraper_paths: List[Tuple[schemas.FileItem, MediaType]],
                               scraper_seen: set):
        storage_path = self.__normalize_storage_path(path)
        root_item = self.storagechain.get_file_item(storage=storage, path=storage_path)
        if not root_item and storage_path.as_posix() == "/":
            root_item = self.__build_dir_fileitem(storage=storage, path=storage_path)
        if not root_item:
            logger.warning(f"媒体库刮削存储路径不存在：{storage}:{storage_path}")
            return
        if root_item.type != "dir":
            logger.warning(f"媒体库刮削存储路径不是目录：{storage}:{storage_path}")
            return

        logger.info(f"开始检索存储目录：{storage}:{storage_path} {mtype} ...")
        for fileitem in self.storagechain.list_files(fileitem=root_item, recursion=True) or []:
            if self._event.is_set():
                logger.info(f"媒体库刮削服务停止")
                return
            if fileitem.type != "file":
                continue
            file_path = Path(fileitem.path)
            if not self.__is_media_file(file_path):
                continue
            if self.__is_excluded(storage, file_path, exclude_paths):
                logger.debug(f"{storage}:{file_path} 在排除目录中，跳过 ...")
                continue
            file_mtype = mtype or self.__detect_media_type(file_path)
            if not file_mtype:
                continue
            if mtype and self.__normalize_storage_path(file_path.parent) == storage_path:
                media_path = storage_path
            else:
                media_path = self.__get_media_dir_path(file_path, file_mtype)
            if not media_path:
                continue
            media_item = self.storagechain.get_file_item(storage=storage, path=media_path)
            if not media_item:
                media_item = self.__build_dir_fileitem(storage=storage, path=media_path)
            self.__append_scraper_dir(scraper_paths, scraper_seen, media_item, file_mtype)

    def __scrape_dir(self, fileitem: schemas.FileItem, mtype: MediaType):
        """
        削刮一个目录，该目录必须是媒体文件目录
        """
        path = Path(fileitem.path)
        # 优先读取本地nfo文件
        tmdbid = self.__get_tmdbid_from_dir(fileitem=fileitem, mtype=mtype)
        meta = MetaInfoPath(path)
        meta.type = mtype
        if mtype == MediaType.MOVIE:
            meta.type = MediaType.MOVIE
        else:
            meta.type = MediaType.TV
        if tmdbid:
            # 按TMDBID识别
            logger.info(f"读取到本地nfo文件的tmdbid：{tmdbid}")
            mediainfo = self.chain.recognize_media(tmdbid=tmdbid, mtype=mtype)
        else:
            # 按名称识别
            mediainfo = self.chain.recognize_media(meta=meta)
        if not mediainfo:
            logger.warn(f"未识别到媒体信息：{fileitem.storage}:{path}")
            return

        # 如果未开启新增已入库媒体是否跟随TMDB信息变化则根据tmdbid查询之前的title
        if not settings.SCRAP_FOLLOW_TMDB:
            transfer_history = TransferHistoryOper().get_by_type_tmdbid(tmdbid=mediainfo.tmdb_id,
                                                                        mtype=mediainfo.type.value)
            if transfer_history:
                mediainfo.title = transfer_history.title
        # 获取图片
        self.chain.obtain_images(mediainfo)
        # 刮削
        MediaChain().scrape_metadata(
            fileitem=fileitem,
            meta=meta,
            mediainfo=mediainfo,
            overwrite=True if self._mode else False
        )
        logger.info(f"{fileitem.storage}:{path} 刮削完成")

    def __get_tmdbid_from_dir(self, fileitem: schemas.FileItem, mtype: MediaType) -> Optional[str]:
        path = Path(fileitem.path)
        if mtype == MediaType.MOVIE:
            nfo_paths = [path / "movie.nfo", path / f"{path.stem}.nfo"]
        else:
            nfo_paths = [path / "tvshow.nfo"]
        for nfo_path in nfo_paths:
            tmdbid = self.__get_tmdbid_from_fileitem_nfo(fileitem.storage, nfo_path)
            if tmdbid:
                return tmdbid
        return None

    def __get_tmdbid_from_fileitem_nfo(self, storage: str, nfo_path: Path) -> Optional[str]:
        if storage == "local":
            if nfo_path.exists():
                return self.__get_tmdbid_from_nfo(nfo_path)
            return None
        nfo_item = self.storagechain.get_file_item(storage=storage, path=nfo_path)
        if not nfo_item or nfo_item.type != "file":
            return None
        with TemporaryDirectory() as tmp_dir:
            local_path = Path(tmp_dir) / nfo_path.name
            downloaded = self.storagechain.download_file(fileitem=nfo_item, path=local_path)
            if downloaded and downloaded.exists():
                return self.__get_tmdbid_from_nfo(downloaded)
        return None

    @staticmethod
    def __get_tmdbid_from_nfo(file_path: Path):
        """
        从nfo文件中获取信息
        :param file_path:
        :return: tmdbid
        """
        if not file_path:
            return None
        xpaths = [
            "uniqueid[@type='Tmdb']",
            "uniqueid[@type='tmdb']",
            "uniqueid[@type='TMDB']",
            "tmdbid"
        ]
        try:
            reader = NfoReader(file_path)
            for xpath in xpaths:
                tmdbid = reader.get_element_value(xpath)
                if tmdbid:
                    return tmdbid
        except Exception as err:
            logger.warn(f"从nfo文件中获取tmdbid失败：{str(err)}")
        return None

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))
