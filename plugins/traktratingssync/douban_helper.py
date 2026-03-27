# -*- coding: utf-8 -*-
"""
豆瓣书影音档案 Helper（本插件自包含，不依赖 doubanSync 插件）
用于提交「看过/在看」「读过/在读」「听过」状态及评分到豆瓣。
Cookie 需在插件配置中手动填写，失效时通过注入的 notify_fn 通知用户。
"""
import re
from typing import Callable, List, Optional, Tuple
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

    def __init__(
        self,
        user_cookie: Optional[str] = None,
        notify_fn: Optional[Callable[[str, str], None]] = None,
    ):
        self._notify = notify_fn or (lambda title, body: None)

        if user_cookie:
            # 手动 split 解析，兼容浏览器复制的 Cookie 字符串
            # SimpleCookie 对值含特殊字符的字段会静默跳过，导致关键字段丢失
            self.cookies = {}
            for part in user_cookie.split(";"):
                part = part.strip()
                if "=" in part:
                    key, _, value = part.partition("=")
                    self.cookies[key.strip()] = value.strip()
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
                msg = "豆瓣 Cookie 已失效，请重新从浏览器复制并更新配置。"
                logger.error(msg)
                self._notify("豆瓣 Cookie 已失效", msg)
        else:
            self.ck = None

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
            response = requests.get(self._URL_DOUBAN, headers=self.headers, timeout=10)
        except Exception as e:
            logger.warning("刷新豆瓣 ck 请求失败: %s", e)
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

    def _post_interest(self, url: str, referer: str, host: str, data: dict) -> bool:
        """向豆瓣提交 interest 请求，统一处理响应和 Cookie 失效检测"""
        headers = self._build_headers(referer, host)
        try:
            response = requests.post(url=url, headers=headers, data=data, timeout=10)
        except Exception as e:
            logger.error("请求豆瓣失败: %s", e)
            return False
        if not response:
            logger.error("豆瓣未返回内容")
            return False
        if response.status_code == 403:
            msg = "豆瓣返回 403，Cookie 可能已失效，请重新从浏览器复制并更新配置。"
            logger.error(msg)
            self._notify("豆瓣 Cookie 已失效", msg)
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
        url = self._URL_SEARCH
        response = RequestUtils(headers=self.headers, timeout=10).get_res(
            url=url, params={"cat": cat, "q": keyword}
        )
        if not response or response.status_code != 200:
            logger.error("搜索 [%s] 失败: HTTP %s", keyword, getattr(response, "status_code", None))
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

        搜索策略（逐级 fallback，找到即返回）：
        1. 「专辑名 + 艺术家」（精度最高）
        2. 纯专辑名（去掉艺术家，兼容艺术家名在豆瓣/网易云不一致的情况）
        """
        if not title:
            return None, None

        # 策略 1：专辑名 + 艺术家
        if artist:
            result = self._search_subject(f"{title} {artist}", "1003")
            if result[1]:
                return result
            logger.debug("豆瓣音乐「专辑+艺术家」未命中，降级为纯专辑名: %s", title)

        # 策略 2：纯专辑名
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

        # 直接搜索播客名称
        douban_title, subject_id = self._search_subject(title, cat="podcast")
        if subject_id:
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
