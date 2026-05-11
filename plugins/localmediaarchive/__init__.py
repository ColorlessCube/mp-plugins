# -*- coding: utf-8 -*-
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.storage import StorageChain
from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase


class LocalMediaArchive(_PluginBase):
    plugin_name = "本地媒体归档"
    plugin_desc = "将长时间未更新的本地电影或剧集目录归档到指定存储目录，可按本地磁盘占用率触发。"
    plugin_icon = "folder.png"
    plugin_version = "1.0.0"
    plugin_author = "Codex"
    plugin_config_prefix = "local_media_archive_"
    plugin_order = 35
    auth_level = 1

    _enable: bool = False
    _notify: bool = False
    _dry_run: bool = True
    _cron: str = "0 3 * * *"
    _source_dirs: str = ""
    _target_storage: str = "夸克网盘"
    _target_path: str = "/"
    _stale_days: int = 30
    _min_usage_percent: float = 0.0
    _max_depth: int = 8
    _max_count: int = 3
    _overwrite: bool = False
    _include_hidden: bool = False

    _lock = threading.RLock()
    _last_result_key = "last_result"

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enable = bool(config.get("enable", False))
        self._notify = bool(config.get("notify", False))
        self._dry_run = bool(config.get("dry_run", True))
        self._cron = (config.get("cron") or "0 3 * * *").strip()
        self._source_dirs = (config.get("source_dirs") or "").strip()
        self._target_storage = (config.get("target_storage") or "夸克网盘").strip()
        self._target_path = (config.get("target_path") or "/").strip()
        self._stale_days = self._config_int(config.get("stale_days"), 30, minimum=1)
        self._min_usage_percent = self._config_float(
            config.get("min_usage_percent"), 0.0, minimum=0.0, maximum=100.0
        )
        self._max_depth = self._config_int(config.get("max_depth"), 8, minimum=1)
        self._max_count = self._config_int(config.get("max_count"), 3, minimum=1)
        self._overwrite = bool(config.get("overwrite", False))
        self._include_hidden = bool(config.get("include_hidden", False))

    @staticmethod
    def _config_int(value: Any, default: int, minimum: Optional[int] = None) -> int:
        try:
            result = int(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            result = default
        if minimum is not None:
            result = max(minimum, result)
        return result

    @staticmethod
    def _config_float(
        value: Any,
        default: float,
        minimum: Optional[float] = None,
        maximum: Optional[float] = None,
    ) -> float:
        try:
            result = float(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            result = default
        if minimum is not None:
            result = max(minimum, result)
        if maximum is not None:
            result = min(maximum, result)
        return result

    def get_state(self) -> bool:
        return self._enable

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/run",
                "endpoint": self.api_run,
                "methods": ["GET", "POST"],
                "summary": "立即执行本地媒体归档",
                "description": "按当前配置扫描并归档一次长时间未更新的本地媒体目录。",
            }
        ]

    def api_run(self) -> Dict[str, Any]:
        try:
            result = self.archive_once(manual=True)
            return {"success": True, "data": result, "message": result.get("message", "")}
        except Exception as err:
            logger.error(f"本地媒体归档手动执行失败：{err}", exc_info=True)
            return {"success": False, "message": str(err)}

    def get_page(self) -> Optional[List[dict]]:
        result = self.get_data(self._last_result_key) or {}
        rows = result.get("items") or []
        if not rows:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": result.get("message") or "暂无归档记录",
                    },
                }
            ]
        return [
            {
                "component": "VTable",
                "props": {"hover": True},
                "content": [
                    {
                        "component": "thead",
                        "content": [
                            {
                                "component": "tr",
                                "content": [
                                    {"component": "th", "text": "源目录"},
                                    {"component": "th", "text": "目标目录"},
                                    {"component": "th", "text": "状态"},
                                    {"component": "th", "text": "说明"},
                                ],
                            }
                        ],
                    },
                    {
                        "component": "tbody",
                        "content": [
                            {
                                "component": "tr",
                                "content": [
                                    {"component": "td", "text": item.get("source", "")},
                                    {"component": "td", "text": item.get("target", "")},
                                    {"component": "td", "text": item.get("status", "")},
                                    {"component": "td", "text": item.get("message", "")},
                                ],
                            }
                            for item in rows[-30:]
                        ],
                    },
                ],
            }
        ]

    def stop_service(self):
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enable:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron or "0 3 * * *")
        except Exception as err:
            logger.warn(f"本地媒体归档 cron 解析失败，使用默认 0 3 * * *：{err}")
            trigger = CronTrigger.from_crontab("0 3 * * *")
        return [
            {
                "id": "local_media_archive",
                "name": "本地媒体归档",
                "trigger": trigger,
                "func": self.archive_once,
                "kwargs": {
                    "coalesce": True,
                    "max_instances": 1,
                },
            }
        ]

    def archive_once(self, manual: bool = False) -> Dict[str, Any]:
        with type(self)._lock:
            if not self._enable and not manual:
                return {"message": "插件未启用，跳过"}

            source_dirs = self._parse_source_dirs()
            if not source_dirs:
                result = {"message": "未配置有效源目录", "items": []}
                self.save_data(self._last_result_key, result)
                return result

            target_root = Path(self._target_path)
            if not target_root.is_absolute():
                result = {"message": "目标目录必须是绝对路径", "items": []}
                self.save_data(self._last_result_key, result)
                return result

            logger.info(
                f"开始本地媒体归档：源目录 {len(source_dirs)} 个，目标 {self._target_storage}:{target_root}，"
                f"超过 {self._stale_days} 天未更新，单次最多 {self._max_count} 个"
            )
            moved = 0
            skipped = 0
            failed = 0
            items: List[Dict[str, str]] = []

            for source_dir in source_dirs:
                if moved >= self._max_count:
                    break
                if not self._should_process_source(source_dir):
                    skipped += 1
                    continue
                for media_dir in self._iter_media_dirs(source_dir):
                    if moved >= self._max_count:
                        break
                    candidate = self._inspect_candidate(media_dir)
                    if not candidate["eligible"]:
                        skipped += 1
                        continue
                    target_dir = target_root / media_dir.name
                    target = f"{self._target_storage}:{target_dir.as_posix()}"
                    if self._dry_run:
                        moved += 1
                        items.append(
                            {
                                "source": media_dir.as_posix(),
                                "target": target,
                                "status": "dry-run",
                                "message": candidate["reason"],
                            }
                        )
                        logger.info(f"本地媒体归档试运行：{media_dir} -> {target}")
                        continue
                    ok, message = self._archive_directory(media_dir, target_dir)
                    if ok:
                        moved += 1
                        items.append(
                            {
                                "source": media_dir.as_posix(),
                                "target": target,
                                "status": "success",
                                "message": message,
                            }
                        )
                    else:
                        failed += 1
                        items.append(
                            {
                                "source": media_dir.as_posix(),
                                "target": target,
                                "status": "failed",
                                "message": message,
                            }
                        )

            message = f"归档完成：处理 {moved} 个，跳过 {skipped} 个，失败 {failed} 个"
            result = {
                "message": message,
                "dry_run": self._dry_run,
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "items": items,
            }
            self.save_data(self._last_result_key, result)
            logger.info(f"本地媒体归档{message}")
            if self._notify and (moved or failed):
                self.systemmessage.put(
                    title="本地媒体归档完成",
                    message=message,
                    role="plugin",
                )
            return result

    def _parse_source_dirs(self) -> List[Path]:
        paths: List[Path] = []
        for raw_line in (self._source_dirs or "").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            path = Path(line).expanduser()
            if not path.is_absolute():
                logger.warn(f"本地媒体归档源目录不是绝对路径，跳过：{line}")
                continue
            if len(path.parts) <= 1:
                logger.warn(f"本地媒体归档源目录过于危险，跳过：{path}")
                continue
            if not path.exists() or not path.is_dir():
                logger.warn(f"本地媒体归档源目录不存在或不是目录，跳过：{path}")
                continue
            paths.append(path)
        return paths

    def _should_process_source(self, source_dir: Path) -> bool:
        if self._min_usage_percent <= 0:
            return True
        usage = shutil.disk_usage(source_dir)
        percent = (usage.used / usage.total * 100) if usage.total else 0
        if percent < self._min_usage_percent:
            logger.info(
                f"本地媒体归档跳过 {source_dir}：磁盘占用 {percent:.1f}% "
                f"低于阈值 {self._min_usage_percent:.1f}%"
            )
            return False
        return True

    def _iter_media_dirs(self, source_dir: Path):
        for item in sorted(source_dir.iterdir(), key=lambda p: p.stat().st_mtime):
            if not item.is_dir():
                continue
            if not self._include_hidden and item.name.startswith("."):
                continue
            yield item

    def _inspect_candidate(self, media_dir: Path) -> Dict[str, Any]:
        latest_mtime = 0.0
        media_count = 0
        tmp_count = 0
        cutoff = time.time() - self._stale_days * 86400

        for path in self._walk_limited(media_dir):
            try:
                stat = path.stat()
            except OSError:
                return {"eligible": False, "reason": f"无法读取文件状态：{path}"}
            latest_mtime = max(latest_mtime, stat.st_mtime)
            if path.is_file():
                suffix = path.suffix.lower()
                if suffix in settings.RMT_MEDIAEXT:
                    media_count += 1
                if suffix in settings.DOWNLOAD_TMPEXT:
                    tmp_count += 1

        if media_count <= 0:
            return {"eligible": False, "reason": "目录内没有媒体文件"}
        if tmp_count > 0:
            return {"eligible": False, "reason": "目录内存在下载临时文件"}
        if latest_mtime > cutoff:
            age_days = max(0, int((time.time() - latest_mtime) // 86400))
            return {"eligible": False, "reason": f"最近 {age_days} 天内仍有更新"}
        age_days = int((time.time() - latest_mtime) // 86400) if latest_mtime else self._stale_days
        return {"eligible": True, "reason": f"已 {age_days} 天未更新，媒体文件 {media_count} 个"}

    def _walk_limited(self, root: Path):
        root_depth = len(root.parts)
        for path in root.rglob("*"):
            if len(path.parts) - root_depth > self._max_depth:
                continue
            if not self._include_hidden and any(part.startswith(".") for part in path.parts[root_depth:]):
                continue
            yield path

    def _archive_directory(self, source_dir: Path, target_dir: Path) -> Tuple[bool, str]:
        try:
            if self._target_storage == "local":
                return self._archive_to_local(source_dir, target_dir)
            return self._archive_to_storage(source_dir, target_dir)
        except Exception as err:
            logger.error(f"本地媒体归档失败：{source_dir} -> {self._target_storage}:{target_dir}，{err}", exc_info=True)
            return False, str(err)

    def _archive_to_local(self, source_dir: Path, target_dir: Path) -> Tuple[bool, str]:
        if source_dir.resolve() == target_dir.resolve() or target_dir.resolve().is_relative_to(source_dir.resolve()):
            return False, "目标目录不能等于或位于源目录内部"
        if target_dir.exists():
            if not self._overwrite:
                return False, "目标目录已存在"
            shutil.rmtree(target_dir)
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, target_dir)
        shutil.rmtree(source_dir)
        logger.info(f"本地媒体归档成功：{source_dir} -> local:{target_dir}")
        return True, "已复制到目标目录并删除本地源目录"

    def _archive_to_storage(self, source_dir: Path, target_dir: Path) -> Tuple[bool, str]:
        storagechain = StorageChain()
        if not self._ensure_folder(storagechain, self._target_storage, target_dir):
            return False, f"无法创建目标目录：{self._target_storage}:{target_dir}"

        uploaded = 0
        for path in self._walk_limited(source_dir):
            if not path.is_file():
                continue
            relative = path.relative_to(source_dir)
            target_parent = target_dir / relative.parent
            parent_item = self._ensure_folder(storagechain, self._target_storage, target_parent)
            if not parent_item:
                return False, f"无法创建目标子目录：{self._target_storage}:{target_parent}"

            target_file = target_parent / path.name
            local_size = path.stat().st_size
            exists_item = storagechain.get_file_item(self._target_storage, target_file)
            if exists_item:
                if not self._overwrite and exists_item.size == local_size:
                    logger.info(f"目标文件已存在且大小一致，跳过上传：{self._target_storage}:{target_file}")
                    uploaded += 1
                    continue
                if not self._overwrite:
                    return False, f"目标文件已存在且大小不一致：{target_file}"
                if not storagechain.delete_file(exists_item):
                    return False, f"目标文件覆盖前删除失败：{target_file}"

            if not storagechain.upload_file(
                fileitem=parent_item,
                path=path,
                new_name=path.name,
            ):
                return False, f"上传失败：{path}"
            verified_item = storagechain.get_file_item(self._target_storage, target_file)
            if not verified_item:
                return False, f"上传后未能在目标存储回查到文件：{target_file}"
            if verified_item.size is not None and verified_item.size != local_size:
                return False, f"上传后目标文件大小不一致：{target_file}"
            uploaded += 1

        shutil.rmtree(source_dir)
        logger.info(f"本地媒体归档成功：{source_dir} -> {self._target_storage}:{target_dir}")
        return True, f"已上传 {uploaded} 个文件并删除本地源目录"

    def _ensure_folder(
        self,
        storagechain: StorageChain,
        storage: str,
        path: Path,
    ) -> Optional[schemas.FileItem]:
        """
        递归确保目标目录存在。
        """
        normalized = Path(path.as_posix())
        if normalized.as_posix() in {"", "."}:
            normalized = Path("/")
        exists = storagechain.get_file_item(storage, normalized)
        if exists:
            if exists.type == "dir":
                return exists
            logger.warn(f"本地媒体归档目标路径已存在但不是目录：{storage}:{normalized}")
            return None
        if normalized == normalized.parent:
            return schemas.FileItem(
                storage=storage,
                type="dir",
                path=normalized.as_posix(),
                name=normalized.name or normalized.as_posix(),
                basename=normalized.name,
            )
        parent = self._ensure_folder(storagechain, storage, normalized.parent)
        if not parent:
            return None
        return storagechain.create_folder(parent, normalized.name)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "扫描源目录下一级媒体文件夹，超过指定天数未更新且满足磁盘占用阈值时，归档到目标存储目录。建议先开启试运行确认结果。",
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(
                                3,
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "enable",
                                        "label": "启用插件",
                                    },
                                },
                            ),
                            self._col(
                                3,
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "dry_run",
                                        "label": "试运行",
                                        "hint": "只记录将要归档的目录，不执行上传和删除",
                                        "persistent-hint": True,
                                    },
                                },
                            ),
                            self._col(
                                3,
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "notify",
                                        "label": "发送通知",
                                    },
                                },
                            ),
                            self._col(
                                3,
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "overwrite",
                                        "label": "覆盖目标同名文件",
                                    },
                                },
                            ),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(
                                4,
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "cron",
                                        "label": "执行周期",
                                        "placeholder": "0 3 * * *",
                                        "hint": "Cron 表达式，默认每天 03:00",
                                        "persistent-hint": True,
                                    },
                                },
                            ),
                            self._col(
                                4,
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "stale_days",
                                        "label": "未更新天数",
                                        "type": "number",
                                        "suffix": "天",
                                    },
                                },
                            ),
                            self._col(
                                4,
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "min_usage_percent",
                                        "label": "本地磁盘占用触发阈值",
                                        "type": "number",
                                        "suffix": "%",
                                        "hint": "填 0 表示不检查磁盘占用",
                                        "persistent-hint": True,
                                    },
                                },
                            ),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(
                                4,
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "target_storage",
                                        "label": "目标存储",
                                        "placeholder": "夸克网盘 / alist / rclone / smb / local",
                                    },
                                },
                            ),
                            self._col(
                                8,
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "target_path",
                                        "label": "目标目录",
                                        "placeholder": "/archive/media",
                                        "hint": "归档时会在该目录下保留原媒体文件夹名称",
                                        "persistent-hint": True,
                                    },
                                },
                            ),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(
                                4,
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "max_count",
                                        "label": "单次最多归档目录数",
                                        "type": "number",
                                    },
                                },
                            ),
                            self._col(
                                4,
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "max_depth",
                                        "label": "扫描最大深度",
                                        "type": "number",
                                    },
                                },
                            ),
                            self._col(
                                4,
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "include_hidden",
                                        "label": "包含隐藏目录",
                                    },
                                },
                            ),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(
                                12,
                                {
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "source_dirs",
                                        "label": "源目录",
                                        "rows": 6,
                                        "placeholder": "/media/movies\n/media/tv",
                                        "hint": "每行一个本地父目录，插件只扫描其下一级子文件夹；支持用 # 写注释",
                                        "persistent-hint": True,
                                    },
                                },
                            )
                        ],
                    },
                ],
            }
        ], {
            "enable": False,
            "notify": False,
            "dry_run": True,
            "cron": "0 3 * * *",
            "source_dirs": "",
            "target_storage": "夸克网盘",
            "target_path": "/",
            "stale_days": 30,
            "min_usage_percent": 0,
            "max_depth": 8,
            "max_count": 3,
            "overwrite": False,
            "include_hidden": False,
        }

    @staticmethod
    def _col(md: int, component: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [component],
        }
