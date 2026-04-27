# -*- coding: utf-8 -*-
"""
微信读书 API Helper
用于查询用户最近阅读的书籍及阅读进度。
通过阅读页 cURL 复用真实浏览器请求上下文，避免仅凭 Cookie 无法访问进度接口。

直接运行本文件可快速测试：
    python weread_helper.py
"""
import hashlib
import random
import re
import shlex
import time
from typing import Any, Callable, Dict, List, Optional

import requests
from requests.utils import cookiejar_from_dict

from app.log import logger


class WereadHelper:
    """微信读书 API 封装类。

    Args:
        curl_string: 从浏览器复制的 ``web/book/read`` 完整 cURL 字符串
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

    def __init__(
        self,
        curl_string: Optional[str] = None,
        notify_fn: Optional[Callable[[str, str], None]] = None,
    ):
        self._notify = notify_fn or (lambda title, body: None)
        self._auth_failed = False
        self._last_auth_failure_notify_at = 0.0

        # 实例级 URL 常量（便于测试替换）
        self._base_url = "https://weread.qq.com"
        self._url_notebooks = f"{self._base_url}/api/user/notebook"
        self._url_book_progress = f"{self._base_url}/web/book/getProgress"
        self._url_book_info = f"{self._base_url}/web/book/info"

        self.session = requests.Session()
        if curl_string:
            parsed = self._parse_curl_string(curl_string)
            self.session.cookies = self._parse_cookie_string(parsed.get("cookie", ""))
            self.session.headers.update(parsed.get("headers", {}))
        else:
            logger.warning("未提供微信读书阅读页 cURL，个人数据接口将无法使用")

        self.session.headers.update({
            "User-Agent": self.session.headers.get(
                "User-Agent",
                (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            ),
            "Accept": self.session.headers.get("Accept", "application/json, text/plain, */*"),
            "Accept-Language": self.session.headers.get("Accept-Language", "zh-CN,zh;q=0.9"),
            "Referer": self.session.headers.get("Referer", "https://weread.qq.com/"),
            "Origin": self.session.headers.get("Origin", "https://weread.qq.com"),
        })

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_cookie_string(cookie_string: str) -> requests.cookies.RequestsCookieJar:
        """将浏览器复制的 Cookie 字符串转为 RequestsCookieJar。

        使用手动 split 解析，避免 SimpleCookie 对含特殊字符的字段静默丢弃。
        """
        cookies_dict: Dict[str, str] = {}
        for part in cookie_string.split(";"):
            part = part.strip()
            if "=" in part:
                key, _, value = part.partition("=")
                cookies_dict[key.strip()] = value.strip()
        return cookiejar_from_dict(cookies_dict, cookiejar=None, overwrite=True)

    @staticmethod
    def _parse_curl_string(curl_string: str) -> Dict[str, Any]:
        """解析浏览器复制的 cURL，提取 Cookie 与关键请求头。"""
        if not curl_string:
            return {"cookie": "", "headers": {}}

        normalized = re.sub(r"\\\n\s*", " ", curl_string).strip()
        parts = shlex.split(normalized)
        headers: Dict[str, str] = {}
        cookie_string = ""

        i = 0
        while i < len(parts):
            part = parts[i]
            if part in ("-H", "--header") and i + 1 < len(parts):
                header = parts[i + 1]
                if ":" in header:
                    key, value = header.split(":", 1)
                    key = key.strip()
                    value = value.strip()
                    if key.lower() == "cookie":
                        cookie_string = value
                    else:
                        headers[key] = value
                i += 2
                continue
            if part in ("-b", "--cookie") and i + 1 < len(parts):
                cookie_string = parts[i + 1].strip()
                i += 2
                continue
            i += 1

        # 只保留进度接口必需的浏览器上下文，避免带入无关噪音头。
        allow_headers = {
            "Accept",
            "Accept-Language",
            "Content-Type",
            "Origin",
            "Referer",
            "User-Agent",
            "x-wrpa-0",
        }
        filtered = {k: v for k, v in headers.items() if k in allow_headers}
        return {"cookie": cookie_string, "headers": filtered}

    def _refresh_session(self) -> None:
        """访问首页以刷新 Session，防止登录态过期"""
        if self._auth_failed:
            logger.debug("微信读书登录态已标记失效，跳过刷新 Session")
            return
        try:
            self._sleep_before_request("刷新 Session")
            self.session.get(self._base_url, timeout=10)
        except Exception:
            pass

    def _auth_context(self) -> str:
        """返回当前微信读书鉴权上下文摘要。"""
        cookie_keys = sorted(self.session.cookies.keys())
        return (
            f"cookie_count={len(cookie_keys)}, "
            f"has_wr_skey={'wr_skey' in self.session.cookies}, "
            f"has_wr_vid={'wr_vid' in self.session.cookies}, "
            f"has_wr_rt={'wr_rt' in self.session.cookies}, "
            f"has_x_wrpa_0={bool(self.session.headers.get('x-wrpa-0'))}, "
            f"referer={self.session.headers.get('Referer', '')[:120]}"
        )

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

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """GET 请求封装，自动检测登录态失效并通知，失败返回 None"""
        if self._auth_failed:
            logger.warning("微信读书登录态已标记失效，跳过请求: url=%s", url)
            return None

        try:
            self._sleep_before_request("GET")
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, dict):
                errcode = data.get("errcode")
                errmsg = data.get("errmsg", "")
                if errcode is None and "errCode" in data:
                    errcode = data.get("errCode")
                    errmsg = data.get("errMsg", "")

                if errcode is not None and errcode != 0:
                    # -2012 / -2010 通常表示未登录或登录态失效
                    if errcode in (-2012, -2010, -1012):
                        msg = "微信读书登录态已失效或 cURL 鉴权上下文不完整，请重新从浏览器阅读页复制完整 cURL 并更新配置。"
                        detail = (
                            f"url={url}, errcode={errcode}, errmsg={errmsg}, "
                            f"{self._auth_context()}"
                        )
                        self._notify_auth_failure("微信读书登录态已失效", msg, detail)
                    else:
                        logger.warning(
                            "微信读书 API 返回错误: url=%s, errcode=%s, errmsg=%s",
                            url, errcode, errmsg,
                        )
                    return None

                if "info" in data and isinstance(data.get("info"), dict):
                    return data.get("info")
            return data

        except requests.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code in (401, 403):
                msg = f"微信读书登录态已失效（HTTP {status_code}）或 cURL 鉴权上下文不完整，请重新从浏览器阅读页复制完整 cURL 并更新配置。"
                detail = (
                    f"url={url}, status={status_code}, body={(e.response.text or '')[:200]}, "
                    f"{self._auth_context()}"
                )
                self._notify_auth_failure("微信读书登录态已失效", msg, detail)
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

    def get_book_progress(self, book_id: str) -> Optional[Dict[str, Any]]:
        """获取单本书当前阅读进度（百分比、阅读时长、最后阅读位置）。"""
        return self._get(self._url_book_progress, params={"bookId": book_id})

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

        当前实现固定使用 ``/api/user/notebook`` 获取最近交互书单，
        再逐本调用 ``/web/book/getProgress`` 获取进度。

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
                if not entry.get("finished_date"):
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
    """交互式测试 WereadHelper 各步骤，方便诊断 cURL 问题。"""
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
    # 1. 读取 cURL
    # ------------------------------------------------------------------ #
    _sep("Step 1 · 读取 cURL")

    curl_str = os.environ.get("WEREAD_CURL", "").strip()
    if not curl_str:
        print("未通过环境变量 WEREAD_CURL 传入阅读页 cURL，请手动粘贴（回车两次结束）：")
        lines = []
        while True:
            line = input()
            if line == "":
                break
            lines.append(line)
        curl_str = "\n".join(lines).strip()

    if not curl_str:
        print("[ERROR] 未提供任何 cURL，退出。")
        sys.exit(1)

    # ------------------------------------------------------------------ #
    # 2. 解析 cURL
    # ------------------------------------------------------------------ #
    _sep("Step 2 · 解析 cURL")

    parsed = WereadHelper._parse_curl_string(curl_str)
    cookie_str = parsed.get("cookie", "")
    header_map = parsed.get("headers", {})
    print(f"解析到 Cookie 长度: {len(cookie_str)}")
    print(f"解析到关键请求头: {sorted(header_map.keys())}")

    if "x-wrpa-0" not in header_map:
        print("\n[ERROR] cURL 中不含 x-wrpa-0，请从阅读页重新复制完整 web/book/read 请求。")
        sys.exit(1)

    # ------------------------------------------------------------------ #
    # 3. 初始化 WereadHelper
    # ------------------------------------------------------------------ #
    _sep("Step 3 · 初始化 WereadHelper")

    def _notify(title: str, body: str) -> None:
        print(f"[NOTIFY] {title}: {body}")

    helper = WereadHelper(curl_string=curl_str, notify_fn=_notify)
    jar = helper.session.cookies
    key_fields = ["wr_skey", "wr_vid", "wr_rt", "wr_fp"]
    print("关键 Cookie 字段验证：")
    for f in key_fields:
        val = jar.get(f, default="(未找到)")
        display = (val[:20] + "...") if isinstance(val, str) and len(val) > 20 else val
        print(f"  {f:20s} = {display}")

    # ------------------------------------------------------------------ #
    # 4. 拉取 notebook
    # ------------------------------------------------------------------ #
    _sep("Step 4 · 拉取 notebook")

    notebooks = helper.get_notebook_list()
    if not notebooks:
        print("[ERROR] notebook 接口返回空，登录态可能已失效，退出。")
        sys.exit(1)

    print(f"[OK] notebook 共 {len(notebooks)} 本，按最近交互时间排序")
    print("\n前 3 条原始字段（notebook 返回的字段）：")
    for item in notebooks[:3]:
        book = item.get("book") or {}
        print(f"\n  书名      : {book.get('title', '-')}")
        print(f"  bookId    : {book.get('bookId', '-')}")
        print(f"  sort      : {item.get('sort', '(无)')}")
        print(f"  noteCount : {item.get('noteCount', '(无)')}")

    # ------------------------------------------------------------------ #
    # 5. 对前 5 本调用 getProgress，打印原始字段
    # ------------------------------------------------------------------ #
    _sep("Step 5 · getProgress 原始返回（前 5 本）")

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
    # 6. 通过 get_recent_books 整合接口获取
    # ------------------------------------------------------------------ #
    _sep("Step 6 · get_recent_books() 整合结果")

    books = helper.get_recent_books(limit=10, include_progress=True)
    print(f"[OK] 整合书籍数：{len(books)}")
    for i, b in enumerate(books, 1):
        print(f"  {i:2d}. 【{b['status']:3s}】{b['title']} - {b['author']}"
              f" | 进度 {b['reading_progress']}%"
              f" | 累计 {WereadHelper.format_reading_time(b['reading_time'])}"
              f" | 读完日期: {b['finished_date'] or '-'}")

    _sep("全部测试通过 ✅")


if __name__ == "__main__":
    main()
