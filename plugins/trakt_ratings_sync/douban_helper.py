# -*- coding: utf-8 -*-
"""
豆瓣书影音档案 Helper（本插件自包含，不依赖 doubanSync 插件）
用于提交「看过」状态及评分到豆瓣。
"""
import re
from typing import List, Optional, Tuple
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup
from http.cookies import SimpleCookie

from app.core.config import settings
from app.core.meta import MetaBase
from app.helper.cookiecloud import CookieCloudHelper
from app.log import logger
from app.utils.http import RequestUtils


class DoubanHelper:
    """豆瓣 Cookie 登录与状态/评分提交"""

    def __init__(self, user_cookie: Optional[str] = None):
        if not user_cookie:
            self.cookiecloud = CookieCloudHelper()
            cookie_dict, msg = self.cookiecloud.download()
            if cookie_dict is None:
                logger.error(f"获取cookiecloud数据错误 {msg}")
            self.cookies = cookie_dict.get("douban.com") if cookie_dict else None
        else:
            self.cookies = user_cookie
        if not self.cookies:
            self.cookies = {}
        else:
            self.cookies = {k: v.value for k, v in SimpleCookie(self.cookies).items()}

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
        self.set_ck()
        self.ck = self.cookies.get("ck")
        logger.debug(f"ck:{self.ck} cookie:{self.cookies}")
        if not self.cookies:
            logger.error("cookie获取为空，请检查插件配置或cookie cloud")
        if not self.ck:
            logger.error("请求ck失败，请检查传入的cookie登录状态")

    def set_ck(self) -> None:
        """刷新豆瓣 ck"""
        self.headers["Cookie"] = ";".join([f"{key}={value}" for key, value in self.cookies.items()])
        response = requests.get("https://www.douban.com/", headers=self.headers, timeout=10)
        ck_str = response.headers.get("Set-Cookie", "")
        logger.debug(ck_str)
        if not ck_str:
            self.cookies["ck"] = ""
            return
        cookie_parts = ck_str.split(";")
        ck = cookie_parts[0].split("=")[1].strip()
        logger.debug(ck)
        if ck == '"deleted"':
            self.cookies["ck"] = ""
        else:
            self.cookies["ck"] = ck

    def get_subject_id(self, title: Optional[str] = None, meta: Optional[MetaBase] = None) -> Tuple[Optional[str], Optional[str]]:
        """根据标题在豆瓣搜索，返回 (subject_name, subject_id)"""
        if not title and meta:
            title = meta.title
        if not title:
            return None, None
        url = f"https://www.douban.com/search?cat=1002&q={title}"
        response = RequestUtils(headers=self.headers, timeout=10).get_res(url=url)
        if not response or response.status_code != 200:
            logger.error(f"搜索 {title} 失败 状态码：{getattr(response, 'status_code', None)}")
            return None, None
        soup = BeautifulSoup(response.text.encode("utf-8"), "lxml")
        title_divs = soup.find_all("div", class_="title")
        subject_items: List[dict] = []
        for div in title_divs:
            item = {}
            a_tag = div.find_all("a")[0]
            item["title"] = (a_tag.string or "").strip()
            link = unquote(a_tag.get("href", ""))
            if "subject/" in link:
                match = re.search(r"subject/(\d+)/", link)
                if match:
                    item["subject_id"] = match.group(1)
            subject_items.append(item)
        if not subject_items:
            logger.error(f"找不到 {title} 相关条目")
            return None, None
        first = subject_items[0]
        return first.get("title"), first.get("subject_id")

    def set_watching_status(
        self,
        subject_id: str,
        status: str = "do",
        private: bool = True,
        rating: Optional[int] = None,
    ) -> bool:
        """设置豆瓣观看状态（想看/在看/看过），可选 1–5 星评分"""
        self.headers["Referer"] = f"https://movie.douban.com/subject/{subject_id}/"
        self.headers["Origin"] = "https://movie.douban.com"
        self.headers["Host"] = "movie.douban.com"
        self.headers["Cookie"] = ";".join([f"{key}={value}" for key, value in self.cookies.items()])
        data_json = {
            "ck": self.ck,
            "interest": "do",
            "rating": "",
            "foldcollect": "U",
            "tags": "",
            "comment": "",
        }
        if private:
            data_json["private"] = "on"
        data_json["interest"] = status
        if rating is not None and 1 <= rating <= 5:
            data_json["rating"] = str(rating)
        try:
            response = requests.post(
                url=f"https://movie.douban.com/j/subject/{subject_id}/interest",
                headers=self.headers,
                data=data_json,
                timeout=10,
            )
        except Exception as e:
            logger.error(f"请求豆瓣失败: {e}")
            return False
        if not response:
            logger.error("豆瓣未返回内容")
            return False
        if response.status_code == 200:
            ret = response.json().get("r")
            ok = False if (isinstance(ret, bool) and ret is False) else True
            if ok:
                return True
            logger.error(f"douban_id: {subject_id} 未开播")
            return False
        logger.error(response.text)
        return False
