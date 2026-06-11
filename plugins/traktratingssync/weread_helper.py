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
from typing import Any, Callable, Dict, List, Optional

import requests

from app.log import logger
from app.utils.http import RequestUtils


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
    _AUTH_FAILURE_NOTIFY_COOLDOWN = 6 * 60 * 60
    _SKILL_VERSION = "1.0.3"

    def __init__(
        self,
        api_key: Optional[str] = None,
        notify_fn: Optional[Callable[[str, str], None]] = None,
    ):
        self._notify = notify_fn or (lambda title, body: None)
        self._auth_failed = False
        self._last_auth_failure_notify_at = 0.0
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

    def _auth_context(self) -> str:
        """返回当前微信读书鉴权上下文摘要。"""
        return f"auth_mode=api_key, key_length={len(self._api_key)}"

    def _notify_auth_failure(self, title: str, message: str, detail: str) -> None:
        """统一记录并通知微信读书鉴权失败。"""
        logger.error("%s 详情: %s", message, detail)
        self._auth_failed = True

        now = time.time()
        if now - self._last_auth_failure_notify_at < self._AUTH_FAILURE_NOTIFY_COOLDOWN:
            logger.warning(
                "微信读书鉴权失败通知已在冷却期内，跳过重复推送: title=%s, cooldown=%ss",
                title, self._AUTH_FAILURE_NOTIFY_COOLDOWN,
            )
            return

        self._last_auth_failure_notify_at = now
        self._notify(title, f"{message}\n{detail}")

    def _sleep_before_request(self, action: str) -> None:
        """请求前随机等待，降低批量同步的固定节奏。"""
        delay = random.uniform(*self._REQUEST_JITTER_RANGE)
        logger.debug("微信读书%s前随机等待 %.2f 秒", action, delay)
        time.sleep(delay)

    def _gateway_call(
        self,
        api_name: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """调用微信读书 Skill Agent API Gateway。"""
        if self._auth_failed:
            logger.warning("微信读书登录态已标记失效，跳过 Skill 请求: api_name=%s", api_name)
            return None
        if not self._api_key:
            return None

        body = {
            "api_name": api_name,
            "skill_version": self._SKILL_VERSION,
        }
        if payload:
            body.update(payload)

        try:
            self._sleep_before_request("Skill API")
            resp = RequestUtils(
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=20,
            ).post_res(self._url_gateway, json=body)
            if resp is None:
                logger.error("微信读书 Skill 请求失败: api_name=%s", api_name)
                return None
            if resp.status_code in (401, 403):
                msg = (
                    f"微信读书 API Key 已失效或无权限（HTTP {resp.status_code}），"
                    "请重新获取 API Key 并更新配置。"
                )
                detail = f"api_name={api_name}, status={resp.status_code}, {self._auth_context()}"
                self._notify_auth_failure("微信读书 API Key 已失效", msg, detail)
                return None
            resp.raise_for_status()
            data = resp.json()
        except requests.HTTPError as e:
            logger.error("微信读书 Skill HTTP 错误: %s", e)
            return None
        except requests.RequestException as e:
            logger.error("微信读书 Skill 请求异常: %s", e)
            return None
        except Exception as e:
            logger.error("微信读书 Skill 处理异常: %s", e, exc_info=True)
            return None

        if not isinstance(data, dict):
            logger.warning("微信读书 Skill 返回非对象响应: api_name=%s", api_name)
            return None

        upgrade_info = data.get("upgrade_info")
        if upgrade_info:
            logger.error("微信读书 Skill 需要升级: %s", upgrade_info.get("message") or upgrade_info)
            return None

        errcode = data.get("errcode")
        errmsg = data.get("errmsg", "")
        if errcode is None and "errCode" in data:
            errcode = data.get("errCode")
            errmsg = data.get("errMsg", "")
        if errcode is not None and errcode != 0:
            if errcode in (-2012, -2010, -1012, 401, 403):
                msg = "微信读书 API Key 已失效或无权限，请重新获取 API Key 并更新配置。"
                detail = (
                    f"api_name={api_name}, errcode={errcode}, "
                    f"errmsg={errmsg}, {self._auth_context()}"
                )
                self._notify_auth_failure("微信读书 API Key 已失效", msg, detail)
            else:
                logger.warning(
                    "微信读书 Skill API 返回错误: api_name=%s, errcode=%s, errmsg=%s",
                    api_name, errcode, errmsg,
                )
            return None

        if isinstance(data.get("data"), dict):
            return data.get("data")
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
        if not data:
            return []
        books = data.get("books", [])
        books.sort(
            key=lambda x: x.get("readUpdateTime") or x.get("updateTime") or 0,
            reverse=True,
        )
        return [{"book": book} for book in books]

    def get_book_progress(self, book_id: str) -> Optional[Dict[str, Any]]:
        """获取单本书当前阅读进度（百分比、阅读时长、最后阅读位置）。"""
        return self._gateway_call("/book/getprogress", {"bookId": book_id})

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
        result: List[Dict[str, Any]] = []

        for item in self.get_notebook_list()[:limit]:
            book = item.get("book", {})
            book_id = book.get("bookId", "")
            result.append(self._build_entry_from_notebook(book_id, book))

        if include_progress:
            for entry in result:
                if self._auth_failed:
                    logger.warning("微信读书登录态已失效，停止继续拉取单本阅读进度")
                    break
                book_id = entry.get("book_id", "")
                if not book_id:
                    continue
                try:
                    progress_info = self.get_book_progress(book_id)
                    if progress_info:
                        self._enrich_with_book_progress(entry, progress_info)
                except Exception as e:
                    logger.debug("获取 %s 阅读详情失败: %s", entry.get("title"), e)

        return result

    # ------------------------------------------------------------------
    # 私有辅助方法
    # ------------------------------------------------------------------

    def _build_entry_from_notebook(self, book_id: str, book: Dict[str, Any]) -> Dict[str, Any]:
        """从 notebook 条目构造统一数据结构"""
        read_update_time = self._safe_int(book.get("readUpdateTime") or book.get("updateTime"))
        finished = book.get("finishReading") == 1
        return {
            "book_id": book_id,
            "title": book.get("title", ""),
            "author": book.get("author", ""),
            "cover": self._normalize_cover(book.get("cover", "")),
            "category": book.get("category", ""),
            "read_update_time": read_update_time,
            "reading_time": 0,
            "reading_progress": 0,
            "status": "读完" if finished else "在读",
            "finished_date": None,
            "weread_url": self._build_weread_url(book_id),
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
            if reading_progress >= 100:
                entry["status"] = "读完"
                finish_time = self._extract_finish_time(book)
                if finish_time > 0:
                    entry["finished_date"] = time.strftime(
                        "%Y-%m-%d",
                        time.localtime(finish_time),
                    )
                elif not entry.get("finished_date"):
                    entry["finished_date"] = None
            elif reading_progress > 0 and entry.get("status") != "读完":
                entry["status"] = "在读"

        update_time = self._extract_read_update_time(book)
        if update_time > 0:
            entry["read_update_time"] = update_time

    @classmethod
    def _extract_progress(cls, payload: Dict[str, Any]) -> Optional[int]:
        """从不同接口结构中提取阅读进度百分比。"""
        candidates = [
            payload.get("readingProgress"),
            payload.get("progress"),
        ]
        reading_detail = payload.get("readingDetail")
        if isinstance(reading_detail, dict):
            candidates.extend([
                reading_detail.get("readingProgress"),
                reading_detail.get("progress"),
            ])
        for value in candidates:
            progress = cls._normalize_progress_value(value)
            if progress is not None:
                return progress
        return None

    @staticmethod
    def _extract_reading_time(payload: Dict[str, Any]) -> Optional[int]:
        for value in (
            payload.get("readingTime"),
            payload.get("recordReadingTime"),
            (payload.get("readingDetail") or {}).get("readingTime") if isinstance(payload.get("readingDetail"), dict) else None,
        ):
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
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
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _normalize_progress_value(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if 0 < number <= 1 and not float(number).is_integer():
            number *= 100
        progress = int(round(number))
        if progress < 0:
            return 0
        if progress > 100:
            return 100
        return progress

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
