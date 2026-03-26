# -*- coding: utf-8 -*-
"""
微信读书 API Helper
用于查询用户最近阅读的书籍及阅读进度。
Cookie 失效时通过注入的 notify_fn 通知用户。
"""
import hashlib
import re
from http.cookies import SimpleCookie
from typing import Any, Callable, Dict, List, Optional

import requests
from requests.utils import cookiejar_from_dict

from app.log import logger


class WereadHelper:
    """微信读书 API 封装类。

    Args:
        cookie_string: 从浏览器复制的微信读书 Cookie 字符串，需包含 wr_skey
        notify_fn: Cookie 失效等异常时的通知回调，签名 ``(title: str, body: str) -> None``
    """

    # markedStatus → 中文标签
    _MARKED_STATUS = {
        0: "未读",
        1: "在读",
        2: "在读",
        4: "读完",
    }

    def __init__(
        self,
        cookie_string: Optional[str] = None,
        notify_fn: Optional[Callable[[str, str], None]] = None,
    ):
        self._notify = notify_fn or (lambda title, body: None)

        # 实例级 URL 常量（便于测试替换）
        self._base_url = "https://weread.qq.com"
        self._url_notebooks = f"{self._base_url}/api/user/notebook"
        self._url_read_info = f"{self._base_url}/web/book/readinfo"
        self._url_book_info = f"{self._base_url}/web/book/info"
        self._url_shelf_sync = f"{self._base_url}/web/shelf/sync"

        self.session = requests.Session()
        if cookie_string:
            self.session.cookies = self._parse_cookie_string(cookie_string)
        else:
            logger.warning("未提供微信读书 Cookie，个人数据接口将无法使用")

        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://weread.qq.com/",
            "Origin": "https://weread.qq.com",
        })

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_cookie_string(cookie_string: str) -> requests.cookies.RequestsCookieJar:
        """将浏览器复制的 Cookie 字符串转为 RequestsCookieJar"""
        cookie = SimpleCookie()
        cookie.load(cookie_string)
        cookies_dict = {k: m.value for k, m in cookie.items()}
        return cookiejar_from_dict(cookies_dict, cookiejar=None, overwrite=True)

    def _refresh_session(self) -> None:
        """访问首页以刷新 Session，防止 Cookie 过期"""
        try:
            self.session.get(self._base_url, timeout=10)
        except Exception:
            pass

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """GET 请求封装，自动检测 Cookie 失效并通知，失败返回 None"""
        try:
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, dict):
                errcode = data.get("errcode")
                if errcode and errcode != 0:
                    # -2012 / -2010 通常表示未登录或 Cookie 失效
                    if errcode in (-2012, -2010, -1012):
                        msg = "微信读书 Cookie 已失效，请重新从浏览器复制并更新配置（需包含 wr_skey）。"
                        logger.error(msg)
                        self._notify("微信读书 Cookie 已失效", msg)
                    else:
                        logger.warning(
                            "微信读书 API 返回错误: url=%s, errcode=%s, errmsg=%s",
                            url, errcode, data.get("errmsg", ""),
                        )
                    return None
            return data

        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                msg = "微信读书 Cookie 已失效（HTTP 401），请重新从浏览器复制并更新配置。"
                logger.error(msg)
                self._notify("微信读书 Cookie 已失效", msg)
            else:
                logger.error("微信读书 HTTP 错误: %s", e)
        except requests.RequestException as e:
            logger.error("微信读书请求异常: %s", e)
        except Exception as e:
            logger.error("微信读书处理异常: %s", e, exc_info=True)
        return None

    # ------------------------------------------------------------------
    # 书籍 ID 转换（与 weread2notion 实现一致）
    # ------------------------------------------------------------------

    @staticmethod
    def _transform_id(book_id: str):
        id_length = len(book_id)
        if re.match(r"^\d*$", book_id):
            ary = []
            for i in range(0, id_length, 9):
                ary.append(format(int(book_id[i: min(i + 9, id_length)]), "x"))
            return "3", ary
        result = ""
        for ch in book_id:
            result += format(ord(ch), "x")
        return "4", [result]

    @classmethod
    def calculate_book_str_id(cls, book_id: str) -> str:
        """将数字/字符串 bookId 转换为微信读书 Web 阅读器 URL 中的 strId"""
        md5 = hashlib.md5()
        md5.update(book_id.encode("utf-8"))
        digest = md5.hexdigest()
        result = digest[0:3]
        code, transformed_ids = cls._transform_id(book_id)
        result += code + "2" + digest[-2:]
        for i, tid in enumerate(transformed_ids):
            hex_len = format(len(tid), "x").zfill(2)
            result += hex_len + tid
            if i < len(transformed_ids) - 1:
                result += "g"
        if len(result) < 20:
            result += digest[0: 20 - len(result)]
        md5 = hashlib.md5()
        md5.update(result.encode("utf-8"))
        result += md5.hexdigest()[0:3]
        return result

    # ------------------------------------------------------------------
    # 核心 API
    # ------------------------------------------------------------------

    def get_notebook_list(self) -> List[Dict[str, Any]]:
        """获取书架上有划线/笔记的书籍列表（/api/user/notebook），按最近交互时间排序"""
        self._refresh_session()
        data = self._get(self._url_notebooks)
        if data:
            books = data.get("books", [])
            books.sort(key=lambda x: x.get("sort", 0), reverse=True)
            return books
        return []

    def get_shelf_books(self, sync_key: int = 0) -> List[Dict[str, Any]]:
        """获取书架书籍列表（/web/shelf/sync），含最近阅读时间和进度。

        Args:
            sync_key: 增量同步 key，传 0 获取全量数据

        Returns:
            按最近阅读时间倒序的书籍列表
        """
        self._refresh_session()
        data = self._get(self._url_shelf_sync, params={"synckey": sync_key})
        if not data:
            return []
        books = data.get("books", [])
        books.sort(key=lambda x: x.get("readUpdateTime", 0), reverse=True)
        return books

    def get_read_info(self, book_id: str) -> Optional[Dict[str, Any]]:
        """获取单本书的阅读详情（累计时长、进度百分比、状态、读完时间）"""
        return self._get(
            self._url_read_info,
            params={"bookId": book_id, "readingDetail": 1, "readingBookIndex": 1, "finishedDate": 1},
        )

    def get_book_info(self, book_id: str) -> Optional[Dict[str, Any]]:
        """获取书籍详细信息（封面、ISBN、评分等）"""
        return self._get(self._url_book_info, params={"bookId": book_id})

    # ------------------------------------------------------------------
    # 整合接口：最近阅读的书籍（含进度）
    # ------------------------------------------------------------------

    def get_recent_books(
        self,
        limit: int = 20,
        include_progress: bool = True,
    ) -> List[Dict[str, Any]]:
        """获取最近阅读的书籍列表，可选附带每本书的阅读进度。

        优先使用 /web/shelf/sync 接口（含 readUpdateTime/readingProgress），
        若该接口返回为空则回退到 /api/user/notebook。

        Args:
            limit: 最多返回几本（默认 20）
            include_progress: 是否补充调用 get_read_info 获取详细进度

        Returns:
            书籍列表，每项结构：
            {
                "book_id"         : str,
                "title"           : str,
                "author"          : str,
                "cover"           : str,
                "category"        : str,
                "read_update_time": int,
                "reading_time"    : int,
                "reading_progress": int,
                "status"          : str,       # "未读" / "在读" / "读完"
                "finished_date"   : str|None,  # "YYYY-MM-DD"
                "weread_url"      : str,
            }
        """
        shelf_books = self.get_shelf_books()
        result: List[Dict[str, Any]] = []

        if shelf_books:
            for item in shelf_books[:limit]:
                book = item.get("book") or item
                book_id = book.get("bookId") or item.get("bookId", "")
                result.append(self._build_entry_from_shelf(book_id, book, item))
        else:
            logger.info("shelf/sync 接口未返回数据，回退到 notebook 接口")
            for item in self.get_notebook_list()[:limit]:
                book = item.get("book", {})
                book_id = book.get("bookId", "")
                result.append(self._build_entry_from_notebook(book_id, book))

        if include_progress:
            for entry in result:
                book_id = entry.get("book_id", "")
                if not book_id:
                    continue
                try:
                    info = self.get_read_info(book_id)
                    if info:
                        self._enrich_with_read_info(entry, info)
                except Exception as e:
                    logger.debug("获取 %s 阅读详情失败: %s", entry.get("title"), e)

        return result

    # ------------------------------------------------------------------
    # 私有辅助方法
    # ------------------------------------------------------------------

    def _build_entry_from_shelf(
        self,
        book_id: str,
        book: Dict[str, Any],
        shelf_item: Dict[str, Any],
    ) -> Dict[str, Any]:
        """从 shelf/sync 条目构造统一数据结构"""
        return {
            "book_id": book_id,
            "title": book.get("title", ""),
            "author": book.get("author", ""),
            "cover": self._normalize_cover(book.get("cover", "")),
            "category": book.get("category", ""),
            "read_update_time": shelf_item.get("readUpdateTime", 0),
            "reading_time": 0,
            "reading_progress": shelf_item.get("readingProgress", 0),
            "status": self._MARKED_STATUS.get(shelf_item.get("markedStatus", 0), "在读"),
            "finished_date": None,
            "weread_url": self._build_weread_url(book_id),
        }

    def _build_entry_from_notebook(self, book_id: str, book: Dict[str, Any]) -> Dict[str, Any]:
        """从 notebook 条目构造统一数据结构"""
        return {
            "book_id": book_id,
            "title": book.get("title", ""),
            "author": book.get("author", ""),
            "cover": self._normalize_cover(book.get("cover", "")),
            "category": book.get("category", ""),
            "read_update_time": 0,
            "reading_time": 0,
            "reading_progress": 0,
            "status": "在读",
            "finished_date": None,
            "weread_url": self._build_weread_url(book_id),
        }

    def _enrich_with_read_info(self, entry: Dict[str, Any], info: Dict[str, Any]) -> None:
        """用 read_info 数据补充进度字段（原地修改 entry）"""
        entry["reading_time"] = info.get("readingTime", 0)
        entry["reading_progress"] = info.get("readingProgress", 0)
        marked_status = info.get("markedStatus", 0)
        entry["status"] = self._MARKED_STATUS.get(marked_status, "在读")
        if marked_status == 4 and info.get("finishedDate"):
            from datetime import datetime, timezone
            try:
                dt = datetime.fromtimestamp(info["finishedDate"], tz=timezone.utc)
                entry["finished_date"] = dt.strftime("%Y-%m-%d")
            except Exception:
                entry["finished_date"] = None

    def _build_weread_url(self, book_id: str) -> str:
        if not book_id:
            return ""
        return f"https://weread.qq.com/web/reader/{self.calculate_book_str_id(book_id)}"

    @staticmethod
    def _normalize_cover(cover: str) -> str:
        """将封面 URL 中的小尺寸标记替换为较大尺寸"""
        return cover.replace("/s_", "/t7_") if cover else ""

    @staticmethod
    def format_reading_time(seconds: int) -> str:
        """将阅读时长（秒）格式化为人类可读字符串"""
        if seconds <= 0:
            return "0 分钟"
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        parts = []
        if hours:
            parts.append(f"{hours} 小时")
        if minutes:
            parts.append(f"{minutes} 分钟")
        return " ".join(parts) if parts else "不足 1 分钟"
