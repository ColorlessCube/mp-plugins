"""
QuarkDisk V3 兼容适配；原作者 xijin285，派生分支由 ColorlessCube 维护。
基于 18dbf2d752de2f7729d06a532125c6a2d9d24a5b；2026-11-09 修改。
保留 GPLv3，来源和修改见本目录 UPSTREAM.md；非上游官方发布。
"""

import base64
import hashlib
import json
import math
import mimetypes
import os
import time
import uuid
from urllib.parse import urlsplit
from wsgiref.handlers import format_date_time
from threading import Lock
from pathlib import Path
from typing import Optional, List, Dict, Any
from xml.sax.saxutils import escape

import requests

from app import schemas
from app.core.cache import TTLCache
from app.core.config import settings
from app.log import logger
from app.schemas.exception import StorageQueryError
from app.utils.http import RequestUtils
from app.runtime.progress import ProgressHelper
from app.runtime.stop import runtime_stop_state


class RateLimiter:
    """
    简单速率限制器
    """

    def __init__(self, max_calls: int = 2, time_window: float = 1.0, name: str = ""):
        self.max_calls = max_calls
        self.time_window = time_window
        self.name = name
        self._lock = Lock()
        self._call_times: List[float] = []

    def acquire(self):
        """等待直至本地速率窗口允许本次请求。"""
        now = time.monotonic()
        with self._lock:
            self._call_times = [
                call_at for call_at in self._call_times if now - call_at < self.time_window
            ]
            if len(self._call_times) >= self.max_calls:
                oldest_call = min(self._call_times)
                wait_time = self.time_window - (now - oldest_call)
                if wait_time > 0:
                    time.sleep(wait_time)
                    now = time.monotonic()
                    self._call_times = [
                        call_at
                        for call_at in self._call_times
                        if now - call_at < self.time_window
                    ]
            self._call_times.append(now)


class IdPathCache:
    """
    路径与 ID 双向缓存
    """

    def __init__(self, region_prefix: str = "quarkdisk", maxsize: int = 512):
        self.id_to_dir = TTLCache(
            region=f"{region_prefix}_id_to_dir",
            maxsize=maxsize,
            ttl=60 * 30,
        )
        self.dir_to_id = TTLCache(
            region=f"{region_prefix}_dir_to_id",
            maxsize=maxsize,
            ttl=60 * 30,
        )

    def add_cache(self, file_id: str, directory: str):
        """同步更新文件 ID 与路径的双向缓存。"""
        old_directory = self.id_to_dir.get(str(file_id))
        if old_directory and old_directory != directory:
            self.dir_to_id.delete(key=old_directory)

        old_id = self.dir_to_id.get(directory)
        if old_id and old_id != str(file_id):
            self.id_to_dir.delete(key=old_id)

        self.id_to_dir.set(key=str(file_id), value=directory)
        self.dir_to_id.set(key=directory, value=str(file_id))

    def get_id_by_dir(self, directory: str) -> Optional[str]:
        """从缓存查询路径对应的文件 ID。"""
        return self.dir_to_id.get(directory)

    def remove(self, file_id: Optional[str] = None, directory: Optional[str] = None):
        """按文件 ID 或目录路径移除双向缓存。"""
        if file_id is not None:
            old_directory = self.id_to_dir.get(str(file_id))
            if old_directory:
                self.id_to_dir.delete(key=str(file_id))
                self.dir_to_id.delete(key=old_directory)
        elif directory is not None:
            old_id = self.dir_to_id.get(directory)
            if old_id:
                self.dir_to_id.delete(key=directory)
                self.id_to_dir.delete(key=old_id)
        else:
            raise ValueError("file_id 和 directory 至少提供一个")

    def clear(self):
        """清空本客户端的双向路径缓存。"""
        self.id_to_dir.clear()
        self.dir_to_id.clear()


class QuarkApi:
    """
    夸克网盘基础操作
    """

    # 请求重试次数
    _max_retries = 3

    # 请求超时时间(秒)
    _timeout = 30

    # 默认分片大小
    _default_part_size = 10 * 1024 * 1024

    transtype = {
        "move": "移动",
    }

    def __init__(self, cookie: str):
        try:
            self._cookie = cookie.strip()
            self._disk_name = "夸克网盘"
            self._base_url = "https://pan.quark.cn/1/clouddrive"
            self._drive_url = "https://drive.quark.cn/1/clouddrive"
            self._drive_pc_url = "https://drive-pc.quark.cn/1/clouddrive"
            self._id_cache = IdPathCache(region_prefix="quarkdisk_path")
            self._list_rate_limiter = RateLimiter(max_calls=1, time_window=1.5, name="list")
            self._path_rate_limiter = RateLimiter(max_calls=2, time_window=1.0, name="path_to_id")
            self._get_item_fail_records: Dict[str, int] = {}
            logger.info(f"【夸克】初始化API客户端, Cookie长度: {len(cookie)}")

            # 生成设备ID
            device_id = str(int(time.time() * 1000))

            def filter_cookies(cookie_str, exclude_keys):
                """从调试展示副本中去掉指定 Cookie 字段。"""
                filtered = []
                for item in cookie_str.split(";"):
                    if '=' in item:
                        key, value = item.strip().split('=', 1)
                        if key not in exclude_keys:
                            filtered.append(f"{key}={value}")
                return '; '.join(filtered)

            self._headers = {
                "Cookie": self._cookie,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Content-Type": "application/json;charset=UTF-8",
                "Referer": "https://pan.quark.cn",
                "Origin": "https://pan.quark.cn",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "x-requested-with": "XMLHttpRequest",
                "x-device-id": device_id,
                "x-client-version": "3.1.0",
                "x-platform-version": "web",
                "x-platform-type": "web",
                "x-sdk-version": "3.1.0",
                "x-web-timestamp": str(int(time.time() * 1000)),
                "x-device-model": "web",
                "x-device-name": "Chrome",
                "x-device-platform": "web",
                "x-app-id": "30",
                "x-app-version": "3.1.0",
                "x-app-package": "com.quark.pan"
            }
            # 日志输出前过滤Cookie字段
            filtered_headers = self._headers.copy()
            filtered_headers["Cookie"] = filter_cookies(self._headers["Cookie"], ["_UP_A4A_11_", "_UP_D_", "_qk_bx_ck_v1", "tfstk"])
            # logger.info(f"【夸克】请求头: {filtered_headers}")

            # 解析Cookie
            try:
                cookie_dict = {}
                for item in cookie.split(";"):
                    if "=" in item:
                        key, value = item.strip().split("=", 1)
                        cookie_dict[key] = value
                # logger.info(f"【夸克】Cookie解析结果: {cookie_dict.keys()}")

                # 验证cookie是否有效
                try:
                    logger.info("【夸克】开始验证Cookie（通过list接口）")
                    root_item = schemas.FileItem(
                        storage=self._disk_name,
                        fileid="0",
                        parent_fileid=None,
                        name="/",
                        basename="/",
                        extension=None,
                        type="dir",
                        path="/",
                        size=None,
                        modify_time=None,
                        pickcode=None
                    )
                    items = self.list(root_item)
                    if items is None:
                        raise Exception("Cookie验证失败: 无法获取根目录文件列表")
                    logger.info(f"【夸克】Cookie验证成功，根目录文件数: {len(items)}")
                except Exception as e:
                    logger.error(f"【夸克】Cookie验证失败: {str(e)}")
                    raise

            except Exception as e:
                logger.error(f"【夸克】解析Cookie失败: {str(e)}")
                raise

        except Exception as e:
            logger.error(f"【夸克】初始化API客户端失败: {str(e)}")
            raise

    def _normalize_path(self, path: str, is_dir: bool = False) -> str:
        path_str = str(path or "/").replace("\\", "/")
        if not path_str.startswith("/"):
            path_str = "/" + path_str
        path_str = path_str.replace("//", "/")
        if path_str != "/" and path_str.endswith("/") and not is_dir:
            path_str = path_str[:-1]
        return path_str

    def _join_path(self, parent_path: str, name: str, is_dir: bool = False) -> str:
        parent = self._normalize_path(parent_path, is_dir=True)
        if parent != "/" and not parent.endswith("/"):
            parent = f"{parent}/"
        full_path = f"{parent}{name}"
        if is_dir and not full_path.endswith("/"):
            full_path += "/"
        return self._normalize_path(full_path, is_dir=is_dir)

    def _is_directory(self, item: Dict[str, Any]) -> bool:
        """
        判断夸克返回项是否为目录。
        根据 pan.quark.cn 前端逻辑，优先使用 file_type：file_type == 0 表示目录。
        """
        file_type = item.get("file_type")
        if file_type is not None:
            try:
                return int(file_type) == 0
            except (TypeError, ValueError):
                return str(file_type).lower() in {"0", "folder", "dir"}

        if "file" in item:
            return item.get("file") is False

        if item.get("dir") is True:
            return True

        obj_category = item.get("obj_category")
        if obj_category:
            return False

        format_type = item.get("format_type")
        if format_type:
            return False

        return False

    def _update_cookie_from_response(self, response: requests.Response):
        if not response or not response.cookies:
            return
        cookie_dict = {}
        for item in self._cookie.split(";"):
            if "=" in item:
                key, value = item.strip().split("=", 1)
                cookie_dict[key] = value
        updated = False
        for key in ["__puus", "__pus"]:
            if key in response.cookies:
                cookie_dict[key] = response.cookies.get(key)
                updated = True
        if updated:
            self._cookie = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
            self._headers["Cookie"] = self._cookie

    def _request(
        self,
        path: str,
        method: str = "GET",
        *,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        use_drive: bool = False,
        use_drive_pc: bool = False,
        use_pan: bool = False,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        if use_drive_pc:
            base_url = self._drive_pc_url
        elif use_drive:
            base_url = self._drive_url
        elif use_pan:
            base_url = self._base_url
        else:
            base_url = self._base_url
        url = f"{base_url}{path}"
        request_headers = dict(self._headers)
        request_params = {"pr": "ucpro", "fr": "pc"}
        if params:
            request_params.update(params)
        if headers:
            request_headers.update(headers)

        last_error = None
        # 无幂等凭据的写请求回执未知时不可重放；只对只读方法做有界重试。
        attempts = self._max_retries if method.upper() in {"GET", "HEAD", "OPTIONS"} else 1
        client = RequestUtils(headers=request_headers, timeout=timeout or self._timeout, verify=True)
        try:
            for retry in range(attempts):
                resp = None
                try:
                    resp = client.request(
                        method=method, url=url, params=request_params, json=json_data,
                        raise_exception=True, allow_redirects=False,
                    )
                    if resp is None:
                        raise StorageQueryError("夸克请求无响应")
                    self._update_cookie_from_response(resp)
                    if resp.status_code != 200:
                        last_error = StorageQueryError(f"夸克请求失败：HTTP {resp.status_code}")
                    else:
                        data = resp.json()
                        status, code = data.get("status", 200), data.get("code", 0)
                        if type(status) is not int or type(code) is not int:
                            raise StorageQueryError("夸克请求响应状态格式无效")
                        if status >= 400 or code != 0:
                            last_error = StorageQueryError(f"夸克请求失败：status={status}，code={code}")
                        else:
                            return data
                except StorageQueryError as error:
                    last_error = error
                except Exception as error:
                    last_error = StorageQueryError(f"夸克请求失败：{type(error).__name__}")
                finally:
                    if resp is not None:
                        try:
                            resp.close()
                        except Exception as error:
                            logger.warning(f"【夸克】请求连接释放失败：{type(error).__name__}")
                if retry < attempts - 1:
                    time.sleep(1)
        finally:
            try:
                client.close()
            except Exception as error:
                logger.warning(f"【夸克】请求客户端释放失败：{type(error).__name__}")
        raise last_error or StorageQueryError("夸克请求失败")

    def _file_to_item(self, parent_path: str, item: Dict[str, Any]) -> schemas.FileItem:
        is_dir = self._is_directory(item)
        file_path = self._join_path(parent_path, item["file_name"], is_dir=is_dir)
        cache_key = file_path[:-1] if is_dir and file_path != "/" else file_path
        self._id_cache.add_cache(str(item["fid"]), cache_key)
        return schemas.FileItem(
            storage=self._disk_name,
            fileid=str(item["fid"]),
            parent_fileid=str(item.get("pdir_fid") or item.get("parent_id") or "0"),
            name=item["file_name"],
            basename=Path(item["file_name"]).stem,
            extension=Path(item["file_name"]).suffix[1:] if not is_dir and Path(item["file_name"]).suffix else None,
            type="dir" if is_dir else "file",
            path=file_path,
            size=None if is_dir else item.get("size"),
            modify_time=int(item.get("updated_at") or item.get("modified_time") or 0),
            pickcode=str(item),
        )

    def _path_to_id(self, path: str):
        """
        通过路径获取ID
        """
        try:
            path = self._normalize_path(path)
            logger.info(f"【夸克】开始获取路径 {path} 的ID")

            # 根目录
            if path == "/" or path == "":
                logger.info("【夸克】根目录ID为0")
                return "0"

            # 检查缓存
            cache_id = self._id_cache.get_id_by_dir(path)
            if cache_id:
                logger.info(f"【夸克】从缓存获取到ID: {cache_id}")
                return cache_id

            logger.info(f"【夸克】开始逐级查找路径: {path}")

            parts = [p for p in Path(path).parts if p != "/"]
            if not parts:
                return "0"

            current_id = "0"
            current_path = ""
            for part in parts:
                try:
                    self._path_rate_limiter.acquire()
                    logger.info(f"【夸克】查找目录 {part} (当前ID: {current_id})")
                    resp_json = self._request(
                        "/file/sort",
                        method="GET",
                        params={
                            "pr": "ucpro",
                            "fr": "pc",
                            "uc_param_str": "",
                            "pdir_fid": current_id,
                            "_page": 1,
                            "_size": 100,
                            "_fetch_total": 1,
                            "_fetch_sub_dirs": 1,
                            "_sort": "file_type:asc,file_name:asc",
                        },
                        use_drive=True,
                    )

                    found = False
                    for item in resp_json.get("data", {}).get("list", []):
                        if item["file_name"] == part:
                            current_id = str(item["fid"])
                            current_path = self._join_path(current_path or "/", part)
                            self._id_cache.add_cache(current_id, current_path)
                            found = True
                            logger.info(f"【夸克】找到目录 {part} 的ID: {current_id}")
                            break

                    if not found:
                        logger.error(f"【夸克】未找到目录: {part}")
                        return None

                except Exception as e:
                    logger.error(f"【夸克】查找目录ID失败: {str(e)}")
                    return None

            # 缓存结果
            self._id_cache.add_cache(current_id, path)
            logger.info(f"【夸克】路径 {path} 的最终ID为: {current_id}")
            return current_id

        except Exception as e:
            logger.error(f"【夸克】获取路径ID失败: {str(e)}")
            return None

    def list(self, fileitem: schemas.FileItem) -> List[schemas.FileItem]:
        """
        获取文件列表（新版，完全模拟网页端接口，GET方式）
        """
        try:
            if fileitem.type == "file":
                detail_item = self.detail(fileitem)
                if detail_item:
                    return [detail_item]
                return [fileitem]

            items = []
            page = 1
            size = 100
            logger.info(f"【夸克】开始获取目录 {fileitem.path} 的文件列表（新版/sort接口）")
            parent_id = self._path_to_id(fileitem.path)
            if not parent_id:
                logger.error(f"【夸克】获取文件列表失败: 无法获取目录ID {fileitem.path}")
                return []
            logger.info(f"【夸克】目录 {fileitem.path} 的ID为 {parent_id}")
            while True:
                try:
                    self._list_rate_limiter.acquire()
                    logger.info(f"【夸克】请求第 {page} 页文件列表")
                    resp_json = self._request(
                        "/file/sort",
                        method="GET",
                        params={
                            "pr": "ucpro",
                            "fr": "pc",
                            "uc_param_str": "",
                            "pdir_fid": parent_id,
                            "_page": page,
                            "_size": size,
                            "_fetch_total": 1,
                            "_fetch_sub_dirs": 0,
                            "_sort": "file_type:asc,updated_at:desc",
                        },
                        use_drive=True,
                    )
                    data = resp_json.get("data", {})
                    item_list = data.get("list", [])
                    if not item_list:
                        logger.info("【夸克】没有更多文件")
                        break
                    for item in item_list:
                        try:
                            items.append(self._file_to_item(fileitem.path, item))
                        except Exception as e:
                            logger.error(f"【夸克】处理文件项失败: {str(e)}")
                            continue
                    if len(item_list) < size:
                        break
                    page += 1
                except Exception as e:
                    logger.error(f"【夸克】获取文件列表失败: {str(e)}")
                    return []
            logger.info(f"【夸克】共获取到 {len(items)} 个文件")
            return items
        except Exception as e:
            logger.error(f"【夸克】获取文件列表失败: {str(e)}")
            return []

    def list_strict(self, fileitem: schemas.FileItem) -> List[schemas.FileItem]:
        """
        严格列举目录的全部分页；缺失总数、重复项或查询故障均不可视为空目录。

        :param fileitem: 带已确认目录 ID 的文件项
        :return: 完整直接子项列表
        :raises StorageQueryError: 无法证明列表完整时抛出，不包含上游凭据或响应正文
        """
        page, size = 1, 100
        result, seen_ids, seen_names = [], set(), set()
        expected_total = None
        try:
            if fileitem.type != "dir" or not fileitem.fileid:
                raise ValueError("目录信息不完整")
            while True:
                self._list_rate_limiter.acquire()
                response = self._request(
                    "/file/sort", method="GET", use_drive=True,
                    params={"pdir_fid": fileitem.fileid, "_page": page, "_size": size,
                            "_fetch_total": 1, "_fetch_sub_dirs": 0,
                            "_sort": "file_type:asc,file_name:asc"},
                )
                if (not isinstance(response, dict) or type(response.get("status")) is not int
                        or response["status"] != 200 or type(response.get("code")) is not int
                        or response["code"] != 0):
                    raise ValueError("查询状态无效")
                data, metadata = response.get("data"), response.get("metadata")
                if not isinstance(data, dict) or not isinstance(metadata, dict):
                    raise ValueError("查询结构无效")
                items, total = data.get("list"), metadata.get("_total")
                if (not isinstance(items, list) or len(items) > size
                        or type(total) is not int or total < 0):
                    raise ValueError("分页元数据无效")
                if expected_total is None:
                    expected_total = total
                if total != expected_total or len(result) + len(items) > total:
                    raise ValueError("分页总数不一致")
                for item in items:
                    if not isinstance(item, dict):
                        raise ValueError("文件项无效")
                    name, fid = item.get("file_name"), item.get("fid")
                    if (not isinstance(name, str) or not name or name in {".", ".."}
                            or any(char in name for char in ("/", "\\", "\x00"))
                            or not isinstance(fid, (str, int)) or isinstance(fid, bool)
                            or not str(fid) or str(fid) == "0"
                            or str(fid) in seen_ids or name in seen_names
                            or type(item.get("file_type")) is not int or item["file_type"] < 0):
                        raise ValueError("文件项不完整或重复")
                    seen_ids.add(str(fid))
                    seen_names.add(name)
                    if item["file_type"] != 0 and (type(item.get("size")) is not int or item["size"] < 0):
                        raise ValueError("文件大小无效")
                    timestamp = item.get("updated_at")
                    if timestamp is None:
                        timestamp = item.get("modified_time")
                    if timestamp is not None and (type(timestamp) not in (int, float)
                                                  or not math.isfinite(timestamp) or timestamp < 0):
                        raise ValueError("文件修改时间无效")
                    converted = self._file_to_item(fileitem.path, item)
                    converted.modify_time = (timestamp / 1000 if timestamp >= 100000000000 else timestamp) if timestamp is not None else None
                    result.append(converted)
                if len(result) == total:
                    return result
                if not items or page >= 10000:
                    raise ValueError("分页未完整结束")
                page += 1
        except StorageQueryError:
            raise
        except Exception as error:
            raise StorageQueryError(f"夸克严格目录查询失败：page={page}，原因={type(error).__name__}") from None

    def get_item_strict(self, path: Path) -> Optional[schemas.FileItem]:
        """
        逐级严格查询路径，不使用可能过期的 ID 缓存或吞异常的旧查询。

        :param path: 云盘内路径
        :return: 确认存在的条目；仅完整父目录未命中时返回 None
        :raises StorageQueryError: 无法确认目标状态时抛出
        """
        path_str = self._normalize_path(str(path))
        current = schemas.FileItem(storage=self._disk_name, fileid="0", type="dir", path="/", name="/")
        parts = Path(path_str).parts[1:]
        if not parts:
            self.list_strict(current)
            return current
        if any(part in {".", ".."} for part in parts):
            raise StorageQueryError("夸克严格查询路径含无效分量")
        for part in parts:
            if current.type != "dir":
                return None
            children = self.list_strict(current)
            current = next((item for item in children if item.name == part), None)
            if current is None:
                return None
        return current

    def get_item(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取文件信息
        """
        try:
            path_str = self._normalize_path(str(path))
            now = time.time()

            item = None
            file_id = self._id_cache.get_id_by_dir(path_str)
            if file_id:
                last_cache_error = None
                for _ in range(3):
                    try:
                        resp = self._request(
                            "/file/info",
                            method="GET",
                            params={
                                "fid": file_id,
                                "_fetch_full_path": 0,
                                "need_profile_tags": 1,
                            },
                            use_drive=True,
                        )
                        item = resp.get("data")
                        if item:
                            break
                    except Exception as e:
                        last_cache_error = e
                    time.sleep(0.3)
                if not item and last_cache_error:
                    logger.warning(f"【夸克】通过缓存ID获取文件信息失败，尝试父目录遍历: {str(last_cache_error)}")

            if not item:
                parent_path = self._normalize_path(str(Path(path_str).parent), is_dir=True)
                if parent_path == ".":
                    parent_path = "/"
                parent_id = self._path_to_id(parent_path)
                if parent_id is None:
                    return None

                target_name = Path(path_str).name
                page = 1
                size = 100
                while True:
                    resp = self._request(
                        "/file/sort",
                        method="GET",
                        params={
                            "pr": "ucpro",
                            "fr": "pc",
                            "uc_param_str": "",
                            "pdir_fid": parent_id,
                            "_page": page,
                            "_size": size,
                            "_fetch_total": 1,
                            "_fetch_sub_dirs": 1,
                            "_sort": "file_type:asc,file_name:asc",
                        },
                        use_drive=True,
                    )
                    item_list = resp.get("data", {}).get("list", [])
                    item = next(
                        (sub_item for sub_item in item_list if sub_item.get("file_name") == target_name),
                        None,
                    )
                    if item or len(item_list) < size:
                        break
                    page += 1
            if not item:
                self._get_item_fail_records[path_str] = self._get_item_fail_records.get(path_str, 0) + 1
                return None

            self._get_item_fail_records.pop(path_str, None)
            is_dir = self._is_directory(item)
            normalized_path = self._normalize_path(path_str, is_dir=is_dir)
            cache_key = normalized_path[:-1] if is_dir and normalized_path != "/" else normalized_path
            self._id_cache.add_cache(str(item["fid"]), cache_key)
            return schemas.FileItem(
                storage=self._disk_name,
                fileid=str(item["fid"]),
                parent_fileid=str(item.get("parent_id") or item.get("pdir_fid") or "0"),
                name=item["file_name"],
                basename=Path(item["file_name"]).stem,
                extension=Path(item["file_name"]).suffix[1:] if not is_dir and Path(item["file_name"]).suffix else None,
                type="dir" if is_dir else "file",
                path=normalized_path,
                size=item.get("size") if not is_dir else None,
                modify_time=int(item.get("modified_time") or item.get("updated_at") or 0),
                pickcode=str(item),
            )
        except Exception as e:
            logger.error(f"【夸克】获取文件信息失败: {str(e)}")
            self._get_item_fail_records[path_str] = self._get_item_fail_records.get(path_str, 0) + 1
            return None

    def detail(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件详情
        """
        return self.get_item(Path(fileitem.path))

    def get_parent(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取父目录
        """
        try:
            parent_path = self._normalize_path(str(Path(fileitem.path).parent))
            if parent_path == ".":
                parent_path = "/"
            return self.get_item(Path(parent_path))
        except Exception as e:
            logger.error(f"【夸克】获取父目录失败: {str(e)}")
            return None

    def create_folder(self, fileitem: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        """
        创建文件夹
        """
        try:
            target_path = self._join_path(fileitem.path, name, is_dir=True)
            parent_id = self._path_to_id(fileitem.path)
            if not parent_id:
                return None
            resp = self._request(
                "/file",
                method="POST",
                json_data={
                    "dir_init_lock": False,
                    "dir_path": "",
                    "file_name": name,
                    "pdir_fid": parent_id,
                },
            )
            item = resp.get("data")
            if item:
                try:
                    created_item = self._file_to_item(fileitem.path, item)
                    if created_item:
                        return created_item
                except Exception as e:
                    logger.warning(f"【夸克】解析创建目录返回数据失败，尝试回查目录: {str(e)}")

            time.sleep(0.5)
            return self.get_item(Path(target_path))
        except Exception as e:
            logger.error(f"【夸克】创建文件夹失败: {str(e)}")
            return None

    def get_folder(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取目录，如目录不存在则创建
        """

        def __find_dir(parent_item: schemas.FileItem, dir_name: str) -> Optional[schemas.FileItem]:
            for sub_item in self.list(parent_item):
                if sub_item.type == "dir" and sub_item.name == dir_name:
                    return sub_item
            return None

        try:
            normalized_path = self._normalize_path(str(path), is_dir=True)
            folder = self.get_item(Path(normalized_path))
            if folder and folder.type == "dir":
                return folder

            current_item = schemas.FileItem(
                storage=self._disk_name,
                fileid="0",
                parent_fileid=None,
                name="/",
                basename="/",
                extension=None,
                type="dir",
                path="/",
                size=None,
                modify_time=None,
                pickcode=None,
            )

            for part in Path(normalized_path).parts[1:]:
                dir_item = __find_dir(current_item, part)
                if not dir_item:
                    dir_item = self.create_folder(current_item, part)
                    if not dir_item:
                        dir_item = self.get_item(Path(self._join_path(current_item.path, part, is_dir=True)))
                if not dir_item:
                    logger.error(f"【夸克】获取目录失败，无法创建目录: {normalized_path}")
                    return None
                current_item = dir_item

            return current_item
        except Exception as e:
            logger.error(f"【夸克】获取目录失败: {str(e)}")
            return None

    def upload(self, fileitem: schemas.FileItem, path: Path, new_name: Optional[str] = None) -> Optional[schemas.FileItem]:
        """
        上传文件
        """
        try:
            if not path.exists() or not path.is_file():
                logger.error(f"【夸克】上传文件不存在: {path}")
                return None

            parent_id = self._path_to_id(fileitem.path)
            if not parent_id:
                return None

            target_name = new_name or path.name
            file_size = path.stat().st_size
            now_ms = int(time.time() * 1000)
            md5_hex, sha1_hex = self._calculate_hashes(path)
            format_type = mimetypes.guess_type(target_name)[0] or ""
            if not format_type and Path(target_name).suffix.lower() == ".nfo":
                format_type = "text/xml"
            mime_type = format_type or "application/octet-stream"
            target_path = self._join_path(fileitem.path, target_name)

            def _build_uploaded_item(fid: Optional[str] = None) -> schemas.FileItem:
                return schemas.FileItem(
                    storage=self._disk_name,
                    fileid=str(fid or ""),
                    parent_fileid=str(parent_id),
                    name=target_name,
                    basename=Path(target_name).stem,
                    extension=Path(target_name).suffix[1:] if Path(target_name).suffix else None,
                    type="file",
                    path=target_path,
                    size=file_size,
                    modify_time=int(time.time()),
                    pickcode="",
                )

            pre_resp = self._request(
                "/file/upload/pre",
                method="POST",
                json_data={
                    "ccp_hash_update": True,
                    "dir_name": "",
                    "file_name": target_name,
                    "format_type": format_type,
                    "l_created_at": now_ms,
                    "l_updated_at": now_ms,
                    "pdir_fid": parent_id,
                    "size": file_size,
                },
            )

            pre_data = pre_resp.get("data")
            metadata = pre_resp.get("metadata") or {}
            if not pre_data:
                logger.error(f"【夸克】上传预检未返回 data: name={target_name}, size={file_size}, format_type={format_type!r}")
                return None

            if pre_data.get("finish"):
                uploaded_item = self.get_item(Path(target_path))
                if uploaded_item:
                    return uploaded_item
                fid = pre_data.get("fid")
                if fid:
                    self._id_cache.add_cache(str(fid), target_path)
                return _build_uploaded_item(fid=fid)

            hash_resp = self._request(
                "/file/update/hash",
                method="POST",
                json_data={
                    "md5": md5_hex,
                    "sha1": sha1_hex,
                    "task_id": pre_data.get("task_id"),
                },
            )

            hash_data = hash_resp.get("data") or {}
            if hash_data.get("finish"):
                uploaded_item = self.get_item(Path(target_path))
                if uploaded_item:
                    return uploaded_item
                fid = hash_data.get("fid") or pre_data.get("fid")
                if fid:
                    self._id_cache.add_cache(str(fid), target_path)
                return _build_uploaded_item(fid=fid)

            part_size = int(metadata.get("part_size") or self._default_part_size)
            if part_size <= 0:
                part_size = self._default_part_size
            etags = self._upload_parts(
                local_path=path,
                pre_data=pre_data,
                part_size=part_size,
                mime_type=mime_type,
            )
            if not etags:
                logger.error(f"【夸克】上传分片结果为空: name={target_name}")
                return None

            if not self._commit_upload(pre_data=pre_data, etags=etags):
                logger.error(f"【夸克】上传提交失败: name={target_name}")
                return None

            self._request(
                "/file/upload/finish",
                method="POST",
                json_data={
                    "obj_key": pre_data.get("obj_key"),
                    "task_id": pre_data.get("task_id"),
                },
            )

            time.sleep(1)
            uploaded_item = self.get_item(Path(target_path))
            if uploaded_item:
                return uploaded_item

            fid = pre_data.get("fid") or hash_data.get("fid")
            if fid:
                self._id_cache.add_cache(str(fid), target_path)
            return _build_uploaded_item(fid=fid)
        except Exception as e:
            logger.error(f"【夸克】上传文件失败: {str(e)}")
            return None

    def _calculate_hashes(self, path: Path) -> tuple[str, str]:
        md5_hasher = hashlib.md5()
        sha1_hasher = hashlib.sha1()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                if chunk:
                    md5_hasher.update(chunk)
                    sha1_hasher.update(chunk)
        return md5_hasher.hexdigest(), sha1_hasher.hexdigest()

    def _build_oss_url(self, pre_data: Dict[str, Any]) -> str:
        bucket = pre_data.get("bucket")
        upload_url = pre_data.get("upload_url") or ""
        obj_key = pre_data.get("obj_key")
        if not all([bucket, upload_url, obj_key]):
            raise Exception("OSS 上传地址参数不完整")
        if upload_url.startswith("http://"):
            suffix = upload_url[7:]
        elif upload_url.startswith("https://"):
            suffix = upload_url[8:]
        else:
            suffix = upload_url
        return f"https://{bucket}.{suffix}/{obj_key}"

    def _build_auth_meta(
        self,
        method: str,
        content_md5: str,
        content_type: str,
        date_str: str,
        extra_headers: Dict[str, str],
        bucket: str,
        obj_key: str,
        query_string: str,
    ) -> str:
        canonical_lines = [method, content_md5, content_type, date_str]
        for key in sorted(extra_headers.keys()):
            canonical_lines.append(f"{key}:{extra_headers[key]}")
        canonical_lines.append(f"/{bucket}/{obj_key}?{query_string}")
        return "\n".join(canonical_lines)

    @staticmethod
    def _http_gmt_date() -> str:
        """
        生成标准 HTTP-date / GMT 时间，格式示例：Sat, 18 Apr 2026 08:20:48 GMT
        """
        return format_date_time(time.time())

    def _request_upload_auth(self, pre_data: Dict[str, Any], auth_meta: str) -> str:
        resp = self._request(
            "/file/upload/auth",
            method="POST",
            json_data={
                "auth_info": pre_data.get("auth_info"),
                "auth_meta": auth_meta,
                "task_id": pre_data.get("task_id"),
            },
        )
        auth_key = (resp.get("data") or {}).get("auth_key")
        if not auth_key:
            raise Exception("获取上传鉴权失败")
        return auth_key

    def _request_upload_data(self, method: str, url: str, **kwargs: Any) -> Dict[str, str]:
        """通过宿主发送既有 OSS 数据请求；保持单次发送并关闭响应。"""
        client = RequestUtils(timeout=max(self._timeout, 120), verify=True)
        response = None
        try:
            response = client.request(method=method, url=url, raise_exception=True,
                                      allow_redirects=False, **kwargs)
            if response is None or response.status_code != 200:
                status = response.status_code if response is not None else "无响应"
                raise OSError(f"夸克上传数据请求失败：{status}")
            return dict(response.headers)
        except Exception as error:
            raise OSError(f"夸克上传数据请求未完成：{type(error).__name__}") from None
        finally:
            for resource in (response, client):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception as error:
                        logger.warning(f"【夸克】上传连接释放失败：{type(error).__name__}")

    def _upload_parts(
        self,
        local_path: Path,
        pre_data: Dict[str, Any],
        part_size: int,
        mime_type: str,
    ) -> List[str]:
        etags: List[str] = []
        bucket = pre_data.get("bucket")
        obj_key = pre_data.get("obj_key")
        upload_url = pre_data.get("upload_url")
        upload_id = pre_data.get("upload_id")
        if not all([bucket, obj_key, upload_url, upload_id]):
            raise Exception("上传预检返回信息不完整")

        target_url = self._build_oss_url(pre_data)

        with open(local_path, "rb") as handle:
            part_number = 1
            while True:
                chunk = handle.read(part_size)
                if not chunk:
                    break

                time_str = self._http_gmt_date()
                signed_headers = {
                    "x-oss-date": time_str,
                    "x-oss-user-agent": "aliyun-sdk-js/1.0.0 Microsoft Edge 142.0.0.0 on Windows 10 64-bit",
                }
                auth_meta = self._build_auth_meta(
                    method="PUT",
                    content_md5="",
                    content_type=mime_type,
                    date_str=time_str,
                    extra_headers=signed_headers,
                    bucket=bucket,
                    obj_key=obj_key,
                    query_string=f"partNumber={part_number}&uploadId={upload_id}",
                )
                auth_key = self._request_upload_auth(pre_data=pre_data, auth_meta=auth_meta)

                response_headers = self._request_upload_data(
                    "PUT", target_url,
                    params={"partNumber": part_number, "uploadId": upload_id},
                    headers={
                        "Authorization": auth_key,
                        "Content-Type": mime_type,
                        "Referer": "https://pan.quark.cn/",
                        "x-oss-date": time_str,
                        "x-oss-user-agent": signed_headers["x-oss-user-agent"],
                    },
                    data=chunk,
                )
                etag = response_headers.get("Etag") or response_headers.get("ETag")
                if not etag:
                    raise Exception(f"分片上传缺少 ETag: part={part_number}")
                etags.append(etag)
                part_number += 1

        return etags

    def _commit_upload(self, pre_data: Dict[str, Any], etags: List[str]) -> bool:
        callback_info = pre_data.get("callback") or {}
        bucket = pre_data.get("bucket")
        obj_key = pre_data.get("obj_key")
        upload_url = pre_data.get("upload_url")
        upload_id = pre_data.get("upload_id")
        if not all([bucket, obj_key, upload_url, upload_id]):
            raise Exception("上传提交参数不完整")

        xml_body = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>", "<CompleteMultipartUpload>"]
        for index, etag in enumerate(etags, start=1):
            xml_body.extend(
                [
                    "<Part>",
                    f"<PartNumber>{index}</PartNumber>",
                    f"<ETag>{escape(etag)}</ETag>",
                    "</Part>",
                ]
            )
        xml_body.append("</CompleteMultipartUpload>")
        body = "\n".join(xml_body)
        content_md5 = base64.b64encode(hashlib.md5(body.encode("utf-8")).digest()).decode("utf-8")
        callback_base64 = base64.b64encode(json.dumps(callback_info, ensure_ascii=False).encode("utf-8")).decode("utf-8")

        time_str = self._http_gmt_date()
        signed_headers = {
            "x-oss-callback": callback_base64,
            "x-oss-date": time_str,
            "x-oss-user-agent": "aliyun-sdk-js/1.0.0 Microsoft Edge 142.0.0.0 on Windows 10 64-bit",
        }
        auth_meta = self._build_auth_meta(
            method="POST",
            content_md5=content_md5,
            content_type="application/xml",
            date_str=time_str,
            extra_headers=signed_headers,
            bucket=bucket,
            obj_key=obj_key,
            query_string=f"uploadId={upload_id}",
        )
        auth_key = self._request_upload_auth(pre_data=pre_data, auth_meta=auth_meta)

        target_url = self._build_oss_url(pre_data)
        self._request_upload_data(
            "POST", target_url,
            params={"uploadId": upload_id},
            headers={
                "Authorization": auth_key,
                "Content-MD5": content_md5,
                "Content-Type": "application/xml",
                "Referer": "https://pan.quark.cn/",
                "x-oss-callback": callback_base64,
                "x-oss-date": time_str,
                "x-oss-user-agent": signed_headers["x-oss-user-agent"],
            },
            data=body.encode("utf-8"),
        )
        return True

    @staticmethod
    def _open_download_directory(directory: Path, *, create: bool = True) -> int:
        """逐级以目录描述符打开目标父目录，拒绝符号链接和路径回退。"""
        directory = directory.absolute()
        if ".." in directory.parts:
            raise ValueError("下载目录包含路径回退")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        current_fd = os.open(directory.anchor, flags)
        try:
            for part in directory.parts[1:]:
                try:
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except Exception:
            os.close(current_fd)
            raise

    def download(self, fileitem: schemas.FileItem, path: Optional[Path] = None) -> Optional[Path]:
        """
        安全下载到 V3 临时目录或指定的新文件，不覆盖现有目标。

        :param fileitem: 待下载的云端文件
        :param path: 已存在目录、完整新文件路径；为空时使用宿主临时目录
        :return: 完整本地文件路径；取消、查询或下载故障返回 None
        """
        directory_fd = None
        temporary_identity = None
        temporary_fd = None
        response = client = progress = None
        succeeded = False
        try:
            if runtime_stop_state.consume_transfer_stop(fileitem.path):
                return None
            current_item = self.get_item_strict(Path(fileitem.path))
            if current_item is None or current_item.type != "file" or not current_item.fileid:
                return None
            fileitem = current_item
            name = fileitem.name
            if (not isinstance(name, str) or not name or name in {".", ".."}
                    or any(char in name for char in ("/", "\\", "\x00"))):
                raise ValueError("下载文件名不安全")
            if path is None:
                # 宿主 download/image 消费后不会删除文件；每次请求独占目录，
                # 保留 basename/类型且不复用旧结果。由宿主 TEMP 清理策略回收，
                # 不在返回 Path 后提前清理，也不按名字回滚删除任何目录/文件。
                target = Path(settings.TEMP_PATH)
                parent_fd = self._open_download_directory(target)
                try:
                    request_directory = f".quark-download-{uuid.uuid4().hex}"
                    os.mkdir(request_directory, mode=0o700, dir_fd=parent_fd)
                finally:
                    os.close(parent_fd)
                local_path = target / request_directory / name
            else:
                target = Path(path)
                local_path = target / name if target.is_dir() else target
            if local_path.name in {"", ".", ".."}:
                raise ValueError("下载目标文件名无效")
            directory_fd = self._open_download_directory(local_path.parent)
            try:
                os.stat(local_path.name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError("下载目标已存在")

            reply = self._request("/file/download", method="POST",
                                  json_data={"fids": [str(fileitem.fileid)]}, use_drive=True)
            download_data = reply.get("data")
            if isinstance(download_data, dict):
                download_data = [download_data]
            if not isinstance(download_data, list) or not download_data or not isinstance(download_data[0], dict):
                raise ValueError("下载接口未返回文件")
            download_url = (download_data[0].get("download_url") or download_data[0].get("url")
                            or download_data[0].get("DownloadUrl"))
            address = urlsplit(download_url or "")
            if address.scheme != "https" or not address.hostname or address.username or address.password:
                raise ValueError("下载地址无效")
            headers = {"User-Agent": self._headers.get("User-Agent"),
                       "Referer": "https://pan.quark.cn/", "Accept": "*/*", "Accept-Encoding": "identity"}
            # 非夸克签名下载地址不应收到账号 Cookie；重定向也不得转发认证。
            if address.hostname == "quark.cn" or address.hostname.endswith(".quark.cn"):
                headers["Cookie"] = self._cookie
            client = RequestUtils(headers=headers, timeout=max(self._timeout, 120), verify=True)
            response = client.get_res(download_url, stream=True, allow_redirects=False,
                                      raise_exception=True)
            if response is None or response.status_code != 200:
                raise OSError("下载响应不是 HTTP 200")
            content_length = response.headers.get("Content-Length")
            expected_length = None
            if content_length is not None:
                if not isinstance(content_length, str) or not content_length.isascii() or not content_length.isdigit():
                    raise ValueError("下载响应长度无效")
                expected_length = int(content_length)
            if response.headers.get("Content-Encoding", "identity").lower() not in {"identity", ""}:
                raise ValueError("下载响应使用了意外的传输压缩")
            progress = ProgressHelper(hashlib.md5(fileitem.path.encode("utf-8")).hexdigest())
            progress.start()
            # 匿名 inode 没有可抢占的暂存名字；失败仅 close，不执行检查后 unlink。
            # 不支持 O_TMPFILE 的平台/文件系统安全失败，不回退到有竞态的命名暂存。
            anonymous_flags = getattr(os, "O_TMPFILE", None)
            if anonymous_flags is None:
                raise OSError("下载需要 Linux O_TMPFILE 支持")
            temporary_fd = os.open(".", os.O_RDWR | anonymous_flags, 0o600, dir_fd=directory_fd)
            temporary_identity = os.fstat(temporary_fd)
            written = 0
            with os.fdopen(temporary_fd, "wb", closefd=False) as output:
                for chunk in response.iter_content(chunk_size=8192):
                    if runtime_stop_state.consume_transfer_stop(fileitem.path):
                        raise InterruptedError("下载已取消")
                    if not chunk:
                        continue
                    output.write(chunk)
                    written += len(chunk)
                    if fileitem.size is not None and written > fileitem.size:
                        raise OSError("下载大小超出文件元数据")
                    if fileitem.size:
                        progress.update(value=min(written * 100 / fileitem.size, 99))
            if runtime_stop_state.consume_transfer_stop(fileitem.path):
                raise InterruptedError("下载已取消")
            if fileitem.size is not None and written != fileitem.size:
                raise OSError("下载内容不完整")
            if expected_length is not None and written != expected_length:
                raise OSError("下载内容与响应长度不一致")
            current_directory = os.stat(local_path.parent, follow_symlinks=False)
            opened_directory = os.fstat(directory_fd)
            if (current_directory.st_dev, current_directory.st_ino) != (opened_directory.st_dev, opened_directory.st_ino):
                raise OSError("下载目录已被替换")
            # Linux procfs 魔术链接绑定已打开 inode，而不是可被替换的暂存名字。
            # 不使用 AT_EMPTY_PATH（它可能要求额外 capability），不覆盖已有目标。
            os.link(f"/proc/self/fd/{temporary_fd}", local_path.name, src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd, follow_symlinks=True)
            # V3 只能返回 Path，不能承诺其在返回后不被同 UID 改名。
            # 重新无符号链接遍历并核对发布结果；观察到替换则失败，绝不按名字回滚删除。
            verified_fd = self._open_download_directory(local_path.parent, create=False)
            try:
                verified_directory = os.fstat(verified_fd)
                published = os.stat(local_path.name, dir_fd=verified_fd, follow_symlinks=False)
                if ((verified_directory.st_dev, verified_directory.st_ino)
                        != (opened_directory.st_dev, opened_directory.st_ino)
                        or (published.st_dev, published.st_ino)
                        != (temporary_identity.st_dev, temporary_identity.st_ino)):
                    raise OSError("下载发布路径已被替换")
            finally:
                os.close(verified_fd)
            succeeded = True
            return local_path
        except Exception as error:
            logger.error(f"【夸克】下载未完成：{type(error).__name__}")
            return None
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
            if temporary_fd is not None:
                os.close(temporary_fd)
            for resource in (response, client):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception as error:
                        logger.warning(f"【夸克】下载连接释放失败：{type(error).__name__}")
            if progress is not None:
                try:
                    progress.end(text="下载完成" if succeeded else "下载未完成", value=100 if succeeded else None)
                except Exception as error:
                    logger.warning(f"【夸克】下载进度结束失败：{type(error).__name__}")

    def delete(self, fileitem: schemas.FileItem) -> Optional[bool]:
        """
        删除文件
        """
        try:
            file_id = self._path_to_id(fileitem.path)
            if not file_id:
                return None
            self._request(
                "/file/delete",
                method="POST",
                json_data={
                    "action_type": 1,
                    "exclude_fids": [],
                    "filelist": [file_id],
                },
            )
            self._id_cache.remove(file_id=file_id)
            return True
        except Exception as e:
            logger.error(f"【夸克】删除文件失败: {str(e)}")
            return None

    def rename(self, fileitem: schemas.FileItem, name: str) -> Optional[bool]:
        """
        重命名文件
        """
        try:
            file_id = fileitem.fileid or self._path_to_id(fileitem.path)
            if not file_id:
                return None
            self._request(
                "/file/rename",
                method="POST",
                json_data={
                    "fid": file_id,
                    "file_name": name,
                },
                use_drive=True,
            )
            self._id_cache.remove(file_id=file_id)
            new_path = self._join_path(str(Path(fileitem.path).parent), name, is_dir=fileitem.type == "dir")
            self._id_cache.add_cache(file_id, new_path[:-1] if fileitem.type == "dir" and new_path != "/" else new_path)
            return True
        except Exception as e:
            logger.error(f"【夸克】重命名文件失败: {str(e)}")
            return None

    def copy(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        复制文件或目录
        """
        logger.warning(
            "【夸克】当前未确认支持复制接口，跳过 copy 操作: %s -> %s/%s",
            fileitem.path,
            path,
            new_name,
        )
        return False

    def move(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        移动文件或目录
        """
        try:
            target_parent_id = self._path_to_id(str(path))
            if not target_parent_id:
                return False
            source_id = fileitem.fileid or self._path_to_id(fileitem.path)
            if not source_id:
                return False
            self._request(
                "/file/move",
                method="POST",
                json_data={
                    "action_type": 1,
                    "exclude_fids": [],
                    "filelist": [source_id],
                    "to_pdir_fid": target_parent_id,
                },
            )
            self._id_cache.remove(file_id=source_id)

            moved_path = self._join_path(str(path), fileitem.name, is_dir=fileitem.type == "dir")
            self._id_cache.add_cache(
                str(source_id),
                moved_path[:-1] if fileitem.type == "dir" and moved_path != "/" else moved_path,
            )

            if new_name and new_name != fileitem.name:
                moved_item = schemas.FileItem(
                    storage=self._disk_name,
                    fileid=str(source_id),
                    parent_fileid=str(target_parent_id),
                    name=fileitem.name,
                    basename=Path(fileitem.name).stem,
                    extension=fileitem.extension,
                    type=fileitem.type,
                    path=moved_path,
                    size=fileitem.size,
                    modify_time=fileitem.modify_time,
                    pickcode=fileitem.pickcode,
                )
                return bool(self.rename(moved_item, new_name))

            return True
        except Exception as e:
            logger.error(f"【夸克】移动文件失败: {str(e)}")
            return False

    def usage(self) -> Optional[schemas.StorageUsage]:
        """
        获取存储空间使用情况
        """
        try:
            resp = self._request(
                "/member",
                method="GET",
                params={
                    "uc_param_str": "",
                    "fetch_subscribe": "true",
                    "_ch": "home",
                    "fetch_identity": "true",
                },
                use_drive_pc=True,
            )
            data = resp.get("data")
            if not data:
                return None
            total_capacity = int(data.get("total_capacity") or data.get("secret_total_capacity") or 0)
            used_capacity = int(data.get("use_capacity") or data.get("secret_use_capacity") or 0)
            if total_capacity <= 0:
                return None
            available_capacity = max(total_capacity - used_capacity, 0)
            return schemas.StorageUsage(
                total=total_capacity,
                available=available_capacity,
            )
        except Exception as e:
            logger.error(f"【夸克】获取存储空间使用情况失败：{type(e).__name__}")
            return None

    def support_transtype(self) -> dict:
        """
        支持的整理方式
        """
        return self.transtype

    def is_support_transtype(self, transtype: str) -> bool:
        """
        是否支持整理方式
        """
        return transtype in self.transtype

    def clear_cache(self):
        """
        清理缓存
        """
        self._id_cache.clear()
        self._get_item_fail_records.clear()
