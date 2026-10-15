# -*- coding: utf-8 -*-
"""
微信读书 API Helper
用于查询用户最近阅读的书籍及阅读进度。
通过微信读书 Skill API Key 调用 Agent API Gateway，避免浏览器 Cookie
快速过期导致单本阅读进度不可用。

直接运行本文件可快速测试：
    python weread_helper.py
"""
import hashlib
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import requests

from app.log import logger
from app.utils.http import RequestUtils


class _GatewaySession(requests.Session):
    """脱敏传输异常，避免 RequestUtils 记录携带 API Key 的底层请求对象。"""

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        """发出请求；失败只传播固定异常，不暴露认证信息。"""
        try:
            return super().request(*args, **kwargs)
        except requests.RequestException:
            raise requests.RequestException("weread_transport_failed") from None


class WereadHelper:
    """微信读书 API 封装类。

    Args:
        api_key: 微信读书 Skill API Key，格式为 ``wrk-...``
        notify_fn: 登录态失效等异常时的通知回调，签名 ``(title: str, body: str) -> None``
    """

    # markedStatus → 中文标签
    _MARKED_STATUS = {
        0: "未读",
        1: "在读",
        2: "在读",
        4: "读完",
    }
    _REQUEST_JITTER_RANGE = (0.8, 2.0)
    _SKILL_VERSION = "1.0.5"

    def __init__(
        self,
        api_key: Optional[str] = None,
        notify_fn: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        """绑定 Skill API Key；请求错误只保留分类，凭据不持久化。"""
        self._request_issue_kind = ""
        self._notify = notify_fn or (lambda title, body: None)
        self._auth_failed = False
        self._api_key = (api_key or "").strip()

        # 实例级 URL 常量（便于测试替换）
        self._url_gateway = "https://i.weread.qq.com/api/agent/gateway"

        if self._api_key:
            logger.debug("微信读书使用 Skill API Key 模式认证，key_length=%d", len(self._api_key))
        else:
            logger.warning("未提供微信读书 Skill API Key，个人数据接口将无法使用")

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _sleep_before_request(self, action: str) -> None:
        """请求前随机等待，降低批量同步的固定节奏。"""
        delay = random.uniform(*self._REQUEST_JITTER_RANGE)
        logger.debug("微信读书%s前随机等待 %.2f 秒", action, delay)
        time.sleep(delay)

    def get_request_issue_kind(self) -> str:
        """返回本次书架/进度操作的错误分类；空字符串表示尚无读取错误。"""
        return self._request_issue_kind

    def _set_request_issue(self, kind: str) -> None:
        """保留本次操作的首个错误，不记录远端升级指令、正文或异常文本。"""
        if self._request_issue_kind:
            return
        self._request_issue_kind = kind
        hints = {
            "upgrade": f"微信读书要求升级 Skill 协议，当前版本 {self._SKILL_VERSION}；本次读取已停止，请更新插件的微信读书接口适配",
            "auth": "微信读书 API Key 已失效或无权限，请检查授权",
            "rate_limit": "微信读书请求受到限流，本次读取已停止，等待下次定时重试",
            "temporary": "微信读书请求暂未完成，本次读取已停止，等待下次定时重试",
            "response": "微信读书响应格式不符合已核对的 Skill 协议，本次读取已停止",
        }
        logger.error(hints[kind])
        if kind == "auth":
            self._auth_failed = True
            self._notify("微信读书 API Key 已失效", hints[kind])

    def _gateway_call(
        self,
        api_name: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """调用固定网关；错误终止整次操作，不忽略升级字段或回显服务端指令。"""
        if self._request_issue_kind:
            return None
        if not self._api_key:
            self._set_request_issue("auth")
            return None
        body = {**(payload or {}), "api_name": api_name, "skill_version": self._SKILL_VERSION}
        resp = None
        try:
            self._sleep_before_request("Skill API")
            with _GatewaySession() as session:
                session.trust_env = False
                resp = RequestUtils(session=session, headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json", "Accept": "application/json",
                }, timeout=20).post_res(self._url_gateway, json=body, verify=True, allow_redirects=False)
                if resp is None:
                    self._set_request_issue("temporary")
                    return None
                if resp.status_code != 200:
                    self._set_request_issue("auth" if resp.status_code in (401, 403) else
                                            "rate_limit" if resp.status_code == 429 else "temporary")
                    return None
                data = resp.json()
        except requests.RequestException:
            self._set_request_issue("temporary")
            return None
        except Exception:
            self._set_request_issue("response")
            return None
        finally:
            if resp is not None:
                resp.close()

        if not isinstance(data, dict):
            self._set_request_issue("response")
            return None
        if "upgrade_info" in data:
            self._set_request_issue("upgrade")
            return None
        errcode = data.get("errcode", data.get("errCode", 0))
        if type(errcode) is not int:
            self._set_request_issue("response")
            return None
        if errcode:
            self._set_request_issue("auth" if errcode in (-2012, -2010, -1012, 401, 403) else
                                    "rate_limit" if errcode == 429 else "temporary")
            return None
        if "data" in data:
            if not isinstance(data["data"], dict):
                self._set_request_issue("response")
                return None
            return data["data"]
        return data

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
        """获取微信读书书架列表，并按最近阅读时间排序。"""
        data = self._gateway_call("/shelf/sync")
        if data is None:
            return []
        books = data.get("books")
        if (not isinstance(books, list) or any(
                not isinstance(book, dict) or not isinstance(book.get("bookId"), str)
                or not book["bookId"].strip() or not isinstance(book.get("title"), str)
                or not book["title"].strip()
                or any(book.get(field) is not None and not isinstance(book[field], str)
                       for field in ("author", "cover", "category"))
                or ("finishReading" in book and (type(book["finishReading"]) is not int
                                                 or book["finishReading"] not in (0, 1))) for book in books)):
            self._set_request_issue("response")
            return []
        books = sorted(books, key=lambda book: self._safe_int(book.get("readUpdateTime") or book.get("updateTime")), reverse=True)
        return [{"book": book} for book in books]

    def get_book_progress(self, book_id: str) -> Optional[Dict[str, Any]]:
        """获取单本书当前阅读进度（百分比、阅读时长、最后阅读位置）。"""
        data = self._gateway_call("/book/getprogress", {"bookId": book_id})
        if data is not None and (not isinstance(data.get("book"), dict)
                                 or self._extract_progress(data["book"]) is None
                                 or ("bookId" in data and data["bookId"] != book_id)):
            self._set_request_issue("response")
            return None
        return data

    def get_book_info(self, book_id: str) -> Optional[Dict[str, Any]]:
        """获取书籍详细信息（封面、ISBN、评分等）"""
        return self._gateway_call("/book/info", {"bookId": book_id})

    # ------------------------------------------------------------------
    # 整合接口：最近阅读的书籍（含进度）
    # ------------------------------------------------------------------

    def get_recent_books(
        self,
        limit: int = 20,
        include_progress: bool = True,
    ) -> List[Dict[str, Any]]:
        """获取最近阅读的书籍列表，可选附带每本书的阅读进度。

        使用 ``/shelf/sync`` 获取书架并按最近阅读时间排序，再逐本调用
        ``/book/getprogress`` 获取进度。

        Args:
            limit: 最多返回几本（默认 20）
            include_progress: 是否补充调用 get_book_progress 获取详细进度

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
        self._request_issue_kind = ""
        self._auth_failed = False
        result: List[Dict[str, Any]] = []

        for item in self.get_notebook_list()[:limit]:
            book = item.get("book", {})
            book_id = book.get("bookId", "")
            result.append(self._build_entry_from_notebook(book_id, book))

        if include_progress:
            for entry in result:
                progress_info = self.get_book_progress(entry["book_id"])
                if self._request_issue_kind:
                    return []
                try:
                    self._enrich_with_book_progress(entry, progress_info)
                except Exception:
                    self._set_request_issue("response")
                    return []

        return result

    # ------------------------------------------------------------------
    # 私有辅助方法
    # ------------------------------------------------------------------

    def _build_entry_from_notebook(self, book_id: str, book: Dict[str, Any]) -> Dict[str, Any]:
        """从 notebook 条目构造统一数据结构"""
        read_update_time = self._safe_int(book.get("readUpdateTime") or book.get("updateTime"))
        finished = type(book.get("finishReading")) is int and book["finishReading"] == 1
        url, label = self._book_link(book)
        return {
            "book_id": book_id,
            "title": book.get("title", ""),
            "author": book.get("author") or "",
            "cover": self._normalize_cover(book.get("cover") or ""),
            "category": book.get("category") or "",
            "read_update_time": read_update_time,
            "reading_time": 0,
            "reading_progress": 0,
            "status": "读完" if finished else "在读",
            "finished_date": None,
            "weread_url": url,
            "weread_url_label": label,
        }

    def _enrich_with_book_progress(self, entry: Dict[str, Any], payload: Dict[str, Any]) -> None:
        """用 getProgress 数据补充进度与阅读时长。"""
        book = payload.get("book") if isinstance(payload.get("book"), dict) else payload

        reading_time = self._extract_reading_time(book)
        if reading_time is not None:
            entry["reading_time"] = reading_time

        reading_progress = self._extract_progress(book)
        if reading_progress is not None:
            entry["reading_progress"] = reading_progress
            finish_time = self._extract_finish_time(book)
            if reading_progress == 100 and finish_time > 0:
                entry["status"] = "读完"
                entry["finished_date"] = time.strftime("%Y-%m-%d", time.localtime(finish_time))
            elif entry.get("status") != "读完":
                entry["status"] = "在读" if reading_progress > 0 or book.get("isStartReading") in (True, 1) else "未读"

        update_time = self._extract_read_update_time(book)
        if update_time > 0:
            entry["read_update_time"] = update_time

    @classmethod
    def _extract_progress(cls, payload: Dict[str, Any]) -> Optional[int]:
        """只使用 getprogress 文档定义的整数百分比，不猜测其他接口字段。"""
        return cls._normalize_progress_value(payload.get("progress"))

    @staticmethod
    def _extract_reading_time(payload: Dict[str, Any]) -> Optional[int]:
        for value in (
            payload.get("recordReadingTime"),
            payload.get("readingTime"),
            (payload.get("readingDetail") or {}).get("readingTime") if isinstance(payload.get("readingDetail"), dict) else None,
        ):
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError, OverflowError):
                continue
        return None

    @staticmethod
    def _extract_finish_time(payload: Dict[str, Any]) -> int:
        """提取读完时间戳。"""
        return WereadHelper._safe_int(payload.get("finishTime"))

    @staticmethod
    def _extract_read_update_time(payload: Dict[str, Any]) -> int:
        candidates = [
            payload.get("readUpdateTime"),
            payload.get("updateTime"),
        ]
        book = payload.get("book")
        if isinstance(book, dict):
            candidates.extend([book.get("readUpdateTime"), book.get("updateTime")])
        for value in candidates:
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return 0

    @staticmethod
    def _safe_int(value: Any) -> int:
        """将输入转换为整数，失败时返回 0。"""
        if value is None or value == "":
            return 0
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _normalize_progress_value(value: Any) -> Optional[int]:
        return value if type(value) is int and 0 <= value <= 100 else None

    @staticmethod
    def _book_link(book: Dict[str, Any]) -> Tuple[str, str]:
        """只采用官方回包中的安全链接，保留阅读器和详情的不同语义。"""
        for field, label in (("readerLink", "打开阅读"), ("deepLink", "查看详情")):
            value = book.get(field)
            if not isinstance(value, str) or len(value) > 2048 or any(ord(c) <= 32 for c in value):
                continue
            try:
                url = urlsplit(value)
                if (not url.username and not url.password and
                        ((url.scheme == "https" and url.hostname == "weread.qq.com" and url.port in (None, 443))
                         or (url.scheme == "weread" and url.netloc))):
                    return value, label
            except ValueError:
                continue
        return "", ""

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


# ---------------------------------------------------------------------------
# 本地测试入口（直接 python weread_helper.py 运行）
# ---------------------------------------------------------------------------

def _sep(title: str) -> None:
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


def main() -> None:
    """交互式测试 WereadHelper Skill API Key 模式。"""
    import os
    import sys
    import logging

    # 用标准 logging 替换 app.log，使其可独立运行
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(levelname)-8s %(name)s  %(message)s",
        stream=sys.stdout,
    )

    # ------------------------------------------------------------------ #
    # 1. 读取 Skill API Key
    # ------------------------------------------------------------------ #
    _sep("Step 1 · 读取 Skill API Key")

    api_key = os.environ.get("WEREAD_API_KEY", "").strip()
    if not api_key:
        api_key = input("请输入微信读书 Skill API Key（wrk-...）：").strip()

    if not api_key:
        print("[ERROR] 未提供任何 Skill API Key，退出。")
        sys.exit(1)

    # ------------------------------------------------------------------ #
    # 2. 初始化 WereadHelper
    # ------------------------------------------------------------------ #
    _sep("Step 2 · 初始化 WereadHelper")

    def _notify(title: str, body: str) -> None:
        print(f"[NOTIFY] {title}: {body}")

    helper = WereadHelper(api_key=api_key, notify_fn=_notify)
    print(f"Skill API Key 长度: {len(api_key)}")

    # ------------------------------------------------------------------ #
    # 3. 拉取书架
    # ------------------------------------------------------------------ #
    _sep("Step 3 · 拉取书架")

    notebooks = helper.get_notebook_list()
    if not notebooks:
        print("[ERROR] /shelf/sync 返回空，API Key 可能已失效或书架为空，退出。")
        sys.exit(1)

    print(f"[OK] 书架共 {len(notebooks)} 本，按最近阅读时间排序")
    print("\n前 3 条原始字段：")
    for item in notebooks[:3]:
        book = item.get("book") or {}
        print(f"\n  书名      : {book.get('title', '-')}")
        print(f"  bookId    : {book.get('bookId', '-')}")
        print(f"  progress  : {book.get('readingProgress', '(无)')}")
        print(f"  readTime  : {book.get('readingTime', '(无)')}")

    # ------------------------------------------------------------------ #
    # 4. 对前 5 本调用 getprogress，打印原始字段
    # ------------------------------------------------------------------ #
    _sep("Step 4 · getprogress 原始返回（前 5 本）")

    for item in notebooks[:5]:
        book = item.get("book") or {}
        book_id = book.get("bookId", "")
        title = book.get("title", "-")
        if not book_id:
            continue

        info = helper.get_book_progress(book_id)
        if info is None:
            print(f"\n  《{title}》 → getProgress 返回 None（接口失败或被限流）")
            continue

        book_info = info.get("book") if isinstance(info.get("book"), dict) else info
        progress = book_info.get("progress", "(无)")
        reading_time = book_info.get("readingTime", 0)
        chapter_uid = book_info.get("chapterUid", "(无)")
        update_time = book_info.get("updateTime", "(无)")

        print(f"\n  《{title}》 (bookId={book_id})")
        print(f"    progress        : {progress}")
        print(f"    readingTime     : {reading_time}s = {WereadHelper.format_reading_time(reading_time)}")
        print(f"    chapterUid      : {chapter_uid}")
        print(f"    updateTime      : {update_time}")

    # ------------------------------------------------------------------ #
    # 5. 通过 get_recent_books 整合接口获取
    # ------------------------------------------------------------------ #
    _sep("Step 5 · get_recent_books() 整合结果")

    books = helper.get_recent_books(limit=10, include_progress=True)
    print(f"[OK] 整合书籍数：{len(books)}")
    for i, b in enumerate(books, 1):
        print(f"  {i:2d}. 【{b['status']:3s}】{b['title']} - {b['author']}"
              f" | 进度 {b['reading_progress']}%"
              f" | 累计 {WereadHelper.format_reading_time(b['reading_time'])}"
              f" | 读完日期: {b['finished_date'] or '-'}")

    _sep("全部测试通过")


if __name__ == "__main__":
    main()
