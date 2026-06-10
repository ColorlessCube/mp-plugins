# -*- coding: utf-8 -*-
"""
豆瓣书影音档案 Helper（本插件自包含，不依赖 doubanSync 插件）
用于提交「看过/在看」「读过/在读」「听过」状态及评分到豆瓣。
Cookie 需在插件配置中手动填写，失效时通过注入的 notify_fn 通知用户。
"""
import random
import re
import shlex
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

from app.core.config import settings
from app.core.meta import MetaBase
from app.log import logger
from app.utils.http import RequestUtils


class DoubanHelper:
    """豆瓣 Cookie 登录与状态/评分提交。

    Args:
        user_cookie: 从浏览器复制的豆瓣 Cookie 字符串
        notify_fn: Cookie 失效等异常时的通知回调，签名 ``(title: str, body: str) -> None``
    """

    # 豆瓣各域名
    _URL_DOUBAN = "https://www.douban.com/"
    _URL_MOVIE_INTEREST = "https://movie.douban.com/j/subject/{subject_id}/interest"
    _URL_BOOK_INTEREST = "https://book.douban.com/j/subject/{subject_id}/interest"
    _URL_MUSIC_INTEREST = "https://music.douban.com/j/subject/{subject_id}/interest"
    _URL_PODCAST_INTEREST = "https://www.douban.com/j/subject/{subject_id}/interest"
    _URL_SEARCH = "https://www.douban.com/search"
    _URL_PODCAST_SEARCH = "https://www.douban.com/podcast/"
    _URL_SUBJECT_SEARCH = "https://www.douban.com/subject_search"
    _URL_REXXAR_SEARCH = "https://m.douban.com/rexxar/api/v2/search"
    _SEARCH_MIN_INTERVAL = 2.5
    _SEARCH_FORBIDDEN_COOLDOWN = 600
    _REQUEST_JITTER_RANGE = (1.0, 3.0)

    def __init__(
        self,
        user_cookie: Optional[str] = None,
        notify_fn: Optional[Callable[[str, str], None]] = None,
    ):
        self._notify = notify_fn or (lambda title, body: None)

        if user_cookie:
            self.cookies = self._parse_cookie_input(user_cookie)
        else:
            self.cookies = {}
            logger.warning("未配置豆瓣 Cookie，请在插件配置中填写")

        self.headers = {
            "User-Agent": settings.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, sdch",
            "Accept-Language": "zh-CN,zh;q=0.8,en-US;q=0.6,en;q=0.4,en-GB;q=0.2,zh-TW;q=0.2",
            "Connection": "keep-alive",
            "DNT": "1",
            "HOST": "www.douban.com",
        }

        self.cookies.pop("__utmz", None)
        self.cookies.pop("ck", None)
        self._authenticated = False

        if self.cookies:
            self._refresh_ck()
            self.ck = self.cookies.get("ck")
            if self.ck:
                self._authenticated = True
                logger.debug("豆瓣认证成功 ck:%s", self.ck)
            else:
                msg = "豆瓣 Cookie 已失效或填写错误，请重新从浏览器复制 Cookie 或完整 cURL 并更新配置。"
                self._notify_auth_failure("豆瓣 Cookie 已失效", msg, self._auth_context())
        else:
            self.ck = None

        self._last_search_ts = 0.0
        self._search_forbidden_until = 0.0
        self._search_forbidden_count = 0

    @property
    def is_authenticated(self) -> bool:
        """返回豆瓣 Cookie 是否有效（ck 已成功获取）"""
        return self._authenticated

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _refresh_ck(self) -> None:
        """访问豆瓣首页刷新 ck Cookie"""
        self.headers["Cookie"] = ";".join(f"{k}={v}" for k, v in self.cookies.items())
        try:
            self._sleep_before_request("刷新 ck")
            response = requests.get(self._URL_DOUBAN, headers=self.headers, timeout=10)
        except Exception as e:
            logger.warning("刷新豆瓣 ck 请求失败: %s；%s", e, self._auth_context())
            self.cookies["ck"] = ""
            return
        ck_str = response.headers.get("Set-Cookie", "")
        logger.debug("豆瓣 Set-Cookie: %s", ck_str)
        if not ck_str:
            self.cookies["ck"] = ""
            return
        ck = ck_str.split(";")[0].split("=")[1].strip()
        self.cookies["ck"] = "" if ck == '"deleted"' else ck

    def _build_headers(self, referer: str, host: str) -> dict:
        """构造带 Referer / Host / Cookie 的请求头"""
        return {
            **self.headers,
            "Referer": referer,
            "Origin": f"https://{host}",
            "Host": host,
            "Cookie": ";".join(f"{k}={v}" for k, v in self.cookies.items()),
        }

    def _build_search_headers(self, referer: str) -> dict:
        """构造搜索请求头。"""
        return {
            **self.headers,
            "Referer": referer,
            "Host": "www.douban.com",
        }

    def _build_rexxar_headers(self, referer: str) -> dict:
        """构造豆瓣移动端 rexxar 搜索请求头。"""
        headers = self._build_search_headers(referer)
        headers.update({
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Host": "m.douban.com",
            "Origin": "https://www.douban.com",
        })
        return headers

    def _sleep_before_request(self, action: str) -> None:
        """所有豆瓣请求前加随机等待，降低周期任务的突发特征。"""
        delay = random.uniform(*self._REQUEST_JITTER_RANGE)
        logger.debug("豆瓣%s前随机等待 %.2f 秒", action, delay)
        time.sleep(delay)

    def _auth_context(self) -> str:
        """返回当前豆瓣鉴权上下文摘要。"""
        return (
            f"cookie_count={len(self.cookies)}, "
            f"has_dbcl2={bool(self.cookies.get('dbcl2'))}, "
            f"has_ck={bool(self.cookies.get('ck') or getattr(self, 'ck', ''))}, "
            f"has_bid={bool(self.cookies.get('bid'))}, "
            f"user_agent={self.headers.get('User-Agent', '')[:120]}"
        )

    def _notify_auth_failure(self, title: str, message: str, detail: str) -> None:
        """统一记录并通知豆瓣鉴权失败。"""
        logger.error("%s 详情: %s", message, detail)
        self._notify(title, f"{message}\n{detail}")

    @staticmethod
    def _cookie_string_to_dict(cookie_string: str) -> dict:
        """将 Cookie 字符串转为字典。"""
        cookies = {}
        for part in cookie_string.split(";"):
            part = part.strip()
            if "=" in part:
                key, _, value = part.partition("=")
                cookies[key.strip()] = value.strip()
        return cookies

    @classmethod
    def _parse_cookie_input(cls, raw_value: str) -> dict:
        """兼容纯 Cookie 字符串或完整 curl，提取豆瓣 Cookie。"""
        text = (raw_value or "").strip()
        if not text:
            return {}
        if not text.lower().startswith("curl "):
            return cls._cookie_string_to_dict(text)

        normalized = re.sub(r"\\\n\s*", " ", text).strip()
        try:
            parts = shlex.split(normalized)
        except Exception:
            logger.warning("解析豆瓣 cURL 失败，回退为原始 Cookie 字符串")
            return cls._cookie_string_to_dict(text)

        cookie_string = ""
        for i, part in enumerate(parts):
            if part in ("-b", "--cookie") and i + 1 < len(parts):
                cookie_string = parts[i + 1].strip()
                break
            if part in ("-H", "--header") and i + 1 < len(parts):
                header = parts[i + 1]
                if header.lower().startswith("cookie:"):
                    cookie_string = header.split(":", 1)[1].strip()
                    break

        if cookie_string:
            logger.debug("已从豆瓣 cURL 中提取 Cookie")
            return cls._cookie_string_to_dict(cookie_string)

        logger.warning("未能从豆瓣 cURL 中提取 Cookie")
        return {}

    def _throttle_search(self) -> None:
        """对豆瓣搜索做最小间隔，降低连续命中风控的概率。"""
        now = time.time()
        wait = self._last_search_ts + self._SEARCH_MIN_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
            now = time.time()
        self._last_search_ts = now

    def _is_search_blocked(self) -> bool:
        """搜索被 403 熔断后，在冷却窗口内直接跳过，避免继续触发风控。"""
        if self._search_forbidden_until <= time.time():
            return False
        logger.warning(
            "豆瓣搜索处于冷却期，跳过本次请求，%.0f 秒后再试",
            self._search_forbidden_until - time.time(),
        )
        return True

    def _mark_search_response(self, status_code: Optional[int], keyword: str) -> None:
        """记录搜索响应状态，用于 403 熔断。"""
        if status_code == 403:
            self._search_forbidden_count += 1
            if self._search_forbidden_count >= 2:
                self._search_forbidden_until = time.time() + self._SEARCH_FORBIDDEN_COOLDOWN
                logger.warning(
                    "豆瓣搜索连续返回 403，已暂停搜索 %d 秒。通常是频率过高、IP 风控或搜索页反爬，不一定是 Cookie 失效。最后关键词: %s",
                    self._SEARCH_FORBIDDEN_COOLDOWN,
                    keyword,
                )
            return
        self._search_forbidden_count = 0

    def _post_interest(self, url: str, referer: str, host: str, data: dict) -> bool:
        """向豆瓣提交 interest 请求，统一处理响应和 Cookie 失效检测"""
        headers = self._build_headers(referer, host)
        try:
            self._sleep_before_request("提交状态")
            response = requests.post(url=url, headers=headers, data=data, timeout=10)
        except Exception as e:
            logger.error("请求豆瓣失败: %s", e)
            return False
        if not response:
            logger.error("豆瓣未返回内容")
            return False
        if response.status_code == 403:
            msg = "豆瓣返回 403，Cookie 可能已失效、填写错误，或当前请求被风控。请重新复制 Cookie 或完整 cURL 后重试。"
            detail = (
                f"url={url}, host={host}, status=403, body={(response.text or '')[:200]}, "
                f"{self._auth_context()}"
            )
            self._notify_auth_failure("豆瓣 Cookie 已失效", msg, detail)
            return False
        if response.status_code == 200:
            ret = response.json().get("r")
            if isinstance(ret, bool) and ret is False:
                logger.error("豆瓣提交失败（条目未开播或不存在）: url=%s", url)
                return False
            return True
        logger.error("豆瓣返回异常 %s: %s", response.status_code, response.text[:200])
        return False

    def _search_subject(self, keyword: str, cat: str) -> Tuple[Optional[str], Optional[str]]:
        """通用豆瓣搜索（cat=1001图书/1002影视/1003音乐），返回 (title, subject_id)"""
        if self._is_search_blocked():
            return None, None
        self._sleep_before_request("搜索")
        self._throttle_search()
        url = self._URL_SEARCH
        response = RequestUtils(
            headers=self._build_search_headers(referer=url),
            cookies=self.cookies,
            timeout=10,
        ).get_res(
            url=url, params={"cat": cat, "q": keyword}
        )
        self._mark_search_response(getattr(response, "status_code", None), keyword)
        if not response or response.status_code != 200:
            logger.error(
                "搜索 [%s] 失败: HTTP %s%s",
                keyword,
                getattr(response, "status_code", None),
                "（可能是频率过高或搜索页风控）" if getattr(response, "status_code", None) == 403 else "",
            )
            return None, None
        soup = BeautifulSoup(response.text.encode("utf-8"), "lxml")
        for div in soup.find_all("div", class_="title"):
            a_tag = div.find_all("a")
            if not a_tag:
                continue
            a = a_tag[0]
            link = unquote(a.get("href", ""))
            match = re.search(r"subject/(\d+)/", link)
            if match:
                return (a.string or "").strip(), match.group(1)
        logger.debug("豆瓣未找到 [%s] 相关条目 (cat=%s)", keyword, cat)
        return None, None

    def _search_podcast_subject(self, keyword: str) -> Tuple[Optional[str], Optional[str]]:
        """搜索豆瓣播客条目，返回 (title, subject_id)。"""
        if self._is_search_blocked():
            return None, None
        self._sleep_before_request("播客搜索")
        self._throttle_search()

        response = RequestUtils(
            headers=self._build_rexxar_headers(referer=self._URL_SUBJECT_SEARCH),
            cookies=self.cookies,
            timeout=10,
        ).get_res(
            url=self._URL_REXXAR_SEARCH,
            params={
                "q": keyword,
                "type": "podcast",
                "start": 0,
                "count": 5,
                "sort": "relevance",
            },
        )
        self._mark_search_response(getattr(response, "status_code", None), keyword)
        if response and response.status_code == 200:
            try:
                data = response.json()
            except Exception as e:
                logger.warning("解析豆瓣播客搜索 JSON 失败 [%s]: %s", keyword, e)
                data = {}
            douban_title, subject_id = self._parse_podcast_rexxar_result(keyword, data)
            if subject_id:
                return douban_title, subject_id
            logger.debug("豆瓣 rexxar 播客搜索未命中: %s", keyword)
        elif not response or response.status_code != 200:
            logger.error(
                "搜索播客 [%s] 失败: HTTP %s%s",
                keyword,
                getattr(response, "status_code", None),
                "（可能是频率过高或搜索页风控）" if getattr(response, "status_code", None) == 403 else "",
            )
            return None, None

        # 豆瓣播客顶部搜索表单当前指向 subject_search；保留 HTML 解析兜底。
        response = RequestUtils(
            headers=self._build_search_headers(referer=self._URL_SUBJECT_SEARCH),
            cookies=self.cookies,
            timeout=10,
        ).get_res(
            url=self._URL_SUBJECT_SEARCH,
            params={"search_text": keyword},
        )
        self._mark_search_response(getattr(response, "status_code", None), keyword)
        if not response or response.status_code != 200:
            logger.error(
                "搜索播客 [%s] 失败: HTTP %s%s",
                keyword,
                getattr(response, "status_code", None),
                "（可能是频率过高或搜索页风控）" if getattr(response, "status_code", None) == 403 else "",
            )
            return None, None

        soup = BeautifulSoup(response.text.encode("utf-8"), "lxml")
        for a in soup.find_all("a", href=True):
            link = unquote(a.get("href", ""))
            match = re.search(r"/(?:podcast|subject)/(\d+)(?:/|$|\?)", link)
            if not match:
                continue
            title = a.get_text(strip=True) or keyword
            return title, match.group(1)

        match = re.search(r"/(?:podcast|subject)/(\d+)(?:/|$|\?)", response.text)
        if match:
            logger.debug("豆瓣播客搜索命中，但未解析到标题，回退为原始关键词: %s", keyword)
            return keyword, match.group(1)

        logger.debug("豆瓣未找到播客条目: %s", keyword)
        return None, None

    @staticmethod
    def _parse_podcast_rexxar_result(
        keyword: str,
        data: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[str]]:
        """从豆瓣 rexxar 搜索结果中提取播客 subject。"""
        subjects = data.get("subjects") if isinstance(data, dict) else {}
        items = subjects.get("items") if isinstance(subjects, dict) else []
        if not isinstance(items, list):
            return None, None

        for item in items:
            if not isinstance(item, dict):
                continue
            target = item.get("target") if isinstance(item.get("target"), dict) else {}
            layout = item.get("layout") or item.get("target_type")
            uri = str(target.get("uri") or target.get("url") or "")
            if layout != "podcast" and "/podcast/" not in uri and "podcast/" not in uri:
                continue
            subject_id = str(target.get("id") or "")
            if not subject_id:
                match = re.search(r"podcast/(\d+)", uri)
                subject_id = match.group(1) if match else ""
            if subject_id:
                return (target.get("title") or keyword), subject_id
        return None, None

    @staticmethod
    def _podcast_search_candidates(title: str) -> List[str]:
        """生成播客搜索候选词，兼容小宇宙标题中的副标题和官方后缀。"""
        candidates = [title]
        for sep in ("｜", "|", " - ", "—", "–"):
            if sep in title:
                candidates.append(title.split(sep, 1)[0].strip())
        if title.endswith("official"):
            candidates.append(title[:-8].strip())
        return [item for idx, item in enumerate(candidates) if item and item not in candidates[:idx]]

    # ------------------------------------------------------------------
    # 搜索接口
    # ------------------------------------------------------------------

    def get_subject_id(
        self,
        title: Optional[str] = None,
        meta: Optional[MetaBase] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """搜索影视条目，返回 (subject_name, subject_id)"""
        if not title and meta:
            title = meta.title
        if not title:
            return None, None
        return self._search_subject(title, "1002")

    def get_book_subject_id(
        self,
        title: Optional[str] = None,
        author: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """搜索图书条目，返回 (subject_name, subject_id)。

        搜索策略（逐级 fallback，找到即返回）：
        1. 「书名 + 作者」（精度最高）
        2. 纯书名（去掉作者，兼容作者名不一致的情况）
        3. 书名逐字截断（每次去掉最后一个字，最短保留 4 字），
           用于处理微信读书书名含版本号/括号等后缀的情况
        """
        if not title:
            return None, None

        # 策略 1：书名 + 作者
        if author:
            result = self._search_subject(f"{title} {author}", "1001")
            if result[1]:
                return result
            logger.debug("豆瓣图书「书名+作者」未命中，降级为纯书名: %s", title)

        # 策略 2：纯书名
        result = self._search_subject(title, "1001")
        if result[1]:
            return result
        logger.debug("豆瓣图书纯书名未命中，尝试截断搜索: %s", title)

        # 策略 3：书名逐字截断（最短保留 4 字）
        for length in range(len(title) - 1, 3, -1):
            short_title = title[:length]
            result = self._search_subject(short_title, "1001")
            if result[1]:
                logger.debug("豆瓣图书截断命中 [%s → %s]", title, short_title)
                return result

        return None, None

    def get_music_subject_id(
        self,
        title: Optional[str] = None,
        artist: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """搜索音乐条目，返回 (subject_name, subject_id)。

        保守策略：只发起一次搜索。
        - 有艺术家时搜「专辑名 + 艺术家」
        - 无艺术家时搜纯专辑名
        """
        if not title:
            return None, None

        if artist:
            return self._search_subject(f"{title} {artist}", "1003")
        return self._search_subject(title, "1003")

    # ------------------------------------------------------------------
    # 状态提交接口
    # ------------------------------------------------------------------

    def set_watching_status(
        self,
        subject_id: str,
        status: str = "do",
        private: bool = True,
        rating: Optional[int] = None,
    ) -> bool:
        """设置豆瓣影视观看状态（wish/do/collect），可选 1–5 星评分"""
        url = self._URL_MOVIE_INTEREST.format(subject_id=subject_id)
        data = {
            "ck": self.ck,
            "interest": status,
            "rating": str(rating) if rating is not None and 1 <= rating <= 5 else "",
            "foldcollect": "U",
            "tags": "",
            "comment": "",
        }
        if private:
            data["private"] = "on"
        return self._post_interest(
            url,
            referer=f"https://movie.douban.com/subject/{subject_id}/",
            host="movie.douban.com",
            data=data,
        )

    def set_book_status(
        self,
        subject_id: str,
        status: str = "do",
        private: bool = True,
        rating: Optional[int] = None,
    ) -> bool:
        """设置豆瓣图书状态（wish/do/collect），可选 1–5 星评分"""
        url = self._URL_BOOK_INTEREST.format(subject_id=subject_id)
        data = {
            "ck": self.ck,
            "interest": status,
            "rating": str(rating) if rating is not None and 1 <= rating <= 5 else "",
            "tags": "",
            "comment": "",
        }
        if private:
            data["private"] = "on"
        return self._post_interest(
            url,
            referer=f"https://book.douban.com/subject/{subject_id}/",
            host="book.douban.com",
            data=data,
        )

    def set_music_status(
        self,
        subject_id: str,
        status: str = "do",
        private: bool = True,
        rating: Optional[int] = None,
    ) -> bool:
        """设置豆瓣音乐状态（wish/do/collect），可选 1–5 星评分"""
        url = self._URL_MUSIC_INTEREST.format(subject_id=subject_id)
        data = {
            "ck": self.ck,
            "interest": status,
            "rating": str(rating) if rating is not None and 1 <= rating <= 5 else "",
            "tags": "",
            "comment": "",
        }
        if private:
            data["private"] = "on"
        return self._post_interest(
            url,
            referer=f"https://music.douban.com/subject/{subject_id}/",
            host="music.douban.com",
            data=data,
        )

    def get_podcast_subject_id(self, title: str) -> Tuple[Optional[str], Optional[str]]:
        """搜索豆瓣播客条目，返回 (豆瓣标题, subject_id)。
        
        Args:
            title: 播客名称
            
        Returns:
            (豆瓣标题, subject_id) 或 (None, None)
        """
        if not title:
            return None, None

        for keyword in self._podcast_search_candidates(title):
            douban_title, subject_id = self._search_podcast_subject(keyword)
            if subject_id:
                if keyword != title:
                    logger.debug("豆瓣播客候选词命中 [%s → %s]", title, keyword)
                return douban_title, subject_id

        logger.debug("豆瓣播客搜索未找到: %s", title)
        return None, None

    def set_podcast_status(
        self,
        subject_id: str,
        status: str = "do",
        private: bool = True,
        rating: Optional[int] = None,
    ) -> bool:
        """设置豆瓣播客状态（wish/do/collect），可选 1–5 星评分"""
        url = self._URL_PODCAST_INTEREST.format(subject_id=subject_id)
        data = {
            "ck": self.ck,
            "interest": status,
            "rating": str(rating) if rating is not None and 1 <= rating <= 5 else "",
            "tags": "",
            "comment": "",
        }
        if private:
            data["private"] = "on"
        return self._post_interest(
            url,
            referer=f"https://www.douban.com/subject/{subject_id}/",
            host="www.douban.com",
            data=data,
        )
