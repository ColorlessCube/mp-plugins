# -*- coding: utf-8 -*-
"""
Trakt 观看记录/评分 → 豆瓣 同步测试脚本（单文件，无 MoviePilot 依赖）
用法：填写下方参数后执行  python test_trakt_to_douban_sync.py

支持测试两部分：
- Trakt 电影评分 → 豆瓣「看过」+ 评分
- Trakt 播放进度（未看完电影 / 剧集，基于 /sync/playback）→ 豆瓣「在看」（不写评分）
"""
import re
from http.cookies import SimpleCookie
from typing import Optional
from urllib.parse import unquote, quote

import requests
from bs4 import BeautifulSoup
import time

# ========== 请填写以下参数 ==========
TRAKT_USERNAME = "ialex-cube"           # Trakt 用户名，如 ialex-cube
TRAKT_CLIENT_ID = "d1d818e084a4e4281fbd635d298512186b17f1e47077efc10827b222807e2b37"          # 在 https://trakt.tv/oauth/applications 创建应用获取
TRAKT_CLIENT_SECRET = ""                # Trakt 应用的 client_secret，用于设备码流程获取 Access Token（可选）
TRAKT_ACCESS_TOKEN = ""                 # 如你已有长期可用的 Access Token，可直接填在这里（否则留空走设备码流程）
DOUBAN_COOKIE = 'll="118163"; bid=eQOD1peo1rI; _vwo_uuid_v2=D45E8A81B24075C95A00692C6A8990A94|3feaca0d8355fd38742346408b2320b4; __utmz=30149280.1748934222.5.2.utmcsr=google|utmccn=(organic)|utmcmd=organic|utmctr=(not%20provided); __utmv=30149280.24643; __utmc=30149280; push_noty_num=0; push_doumail_num=0; __utma=30149280.1599151569.1747632734.1769608932.1771981945.13; __utmt=1; dbcl2="246439528:RTWJIH3Prwc"; ck=zm2z; ap_v=0,6.0; frodotk_db="22f29ed22d753fd768d7dc76885fbf85"; __utmb=30149280.3.10.1771981945'            # 豆瓣 Cookie 字符串（用于提交「看过」和评分）
PRIVATE = False                # 豆瓣标记为仅自己可见
SYNC_LIMIT = 5                # 本次最多同步条数（测试时可设小一点，0 表示不限制）
# =====================================

TRAKT_API_BASE = "https://api.trakt.tv"
TRAKT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "trakt-api-version": "2",
    "trakt-api-key": TRAKT_CLIENT_ID,
}
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def trakt_rating_to_douban(trakt_rating: int) -> int:
    """Trakt 1-10 → 豆瓣 1-5 星"""
    if trakt_rating <= 0:
        return 1
    return max(1, min(5, round(trakt_rating / 2)))


def fetch_trakt_ratings() -> list:
    """从 Trakt 拉取用户电影评分列表"""
    if not TRAKT_USERNAME or not TRAKT_CLIENT_ID:
        print("未配置 TRAKT_USERNAME 或 TRAKT_CLIENT_ID")
        return []
    url = f"{TRAKT_API_BASE}/users/{TRAKT_USERNAME}/ratings/movies"
    try:
        r = requests.get(url, headers=TRAKT_HEADERS, timeout=30)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return data
            print("Trakt 返回格式异常，期望数组")
            return []
        if r.status_code == 429:
            print("Trakt 触发频率限制(429)，请稍后再试")
            return []
        if r.status_code in (403, 404):
            print(f"Trakt 返回 {r.status_code}，请检查用户名或 Client ID")
            return []
        print(f"Trakt API 异常: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"拉取 Trakt 失败: {e}")
    return []


def get_trakt_access_token() -> Optional[str]:
    """获取 Trakt Access Token：
    - 如 TRAKT_ACCESS_TOKEN 已填写，直接返回；
    - 否则使用 device code 流程，引导用户在浏览器确认一次。
    """
    global TRAKT_ACCESS_TOKEN
    if TRAKT_ACCESS_TOKEN:
        return TRAKT_ACCESS_TOKEN
    if not TRAKT_CLIENT_ID or not TRAKT_CLIENT_SECRET:
        print("未配置 TRAKT_ACCESS_TOKEN，且缺少 TRAKT_CLIENT_SECRET，无法自动获取 Access Token。")
        return None

    device_code_url = f"{TRAKT_API_BASE}/oauth/device/code"
    token_url = f"{TRAKT_API_BASE}/oauth/device/token"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": TRAKT_CLIENT_ID,
    }
    try:
        resp = requests.post(device_code_url, json={"client_id": TRAKT_CLIENT_ID}, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"获取设备码失败: {resp.status_code} {resp.text[:200]}")
            return None
        data = resp.json()
        device_code = data["device_code"]
        user_code = data["user_code"]
        verification_url = data["verification_url"]
        interval = int(data.get("interval", 5))
        expires_in = int(data.get("expires_in", 600))

        print("\n========== Trakt 设备授权 ==========")
        print(f"请在浏览器打开：{verification_url}")
        print(f"并输入授权码：{user_code}")
        print("授权完成后，脚本会自动继续。")

        start = time.time()
        while time.time() - start < expires_in:
            time.sleep(interval)
            try:
                token_resp = requests.post(
                    token_url,
                    json={
                        "code": device_code,
                        "client_id": TRAKT_CLIENT_ID,
                        "client_secret": TRAKT_CLIENT_SECRET,
                    },
                    headers=headers,
                    timeout=10,
                )
            except Exception as e:
                print(f"轮询 Access Token 失败: {e}")
                continue

            if token_resp.status_code == 200:
                try:
                    token_data = token_resp.json()
                except Exception as e:
                    print(f"解析 Access Token 响应失败: {e} {token_resp.text[:200]}")
                    return None
                access_token = token_data.get("access_token")
                if access_token:
                    TRAKT_ACCESS_TOKEN = access_token
                    print("Trakt Access Token 获取成功。你可以将其填入脚本顶部的 TRAKT_ACCESS_TOKEN 以便下次直接使用。")
                    print(f"Access Token: {access_token}")
                    return access_token
                print("Trakt 返回内容中未找到 access_token")
                return None

            # 400 错误时，根据 error 字段判断是否继续轮询
            if token_resp.status_code == 400:
                try:
                    err = (token_resp.json().get("error") or "").lower()
                except Exception:
                    # 有些情况下 Trakt 可能返回空响应或非 JSON，视为仍在等待授权
                    continue
                if err in ("authorization_pending", "slow_down"):
                    continue
                print(f"获取 Access Token 失败: {err}")
                return None

            print(f"获取 Access Token 返回异常: {token_resp.status_code} {token_resp.text[:200]}")
            return None

        print("设备授权超时，请重新运行脚本获取设备码。")
        return None
    except Exception as e:
        print(f"设备码流程异常: {e}")
        return None


def douban_search_subject_id(title: str, year: Optional[int] = None) -> Optional[str]:
    """豆瓣搜索电影，返回第一条的 subject_id"""
    q = f"{title}" if not year else f"{title} {year}"
    url = f"https://www.douban.com/search?cat=1002&q={quote(q)}"
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text.encode("utf-8"), "lxml")
        for div in soup.find_all("div", class_="title"):
            a = div.find("a")
            if not a:
                continue
            href = unquote(a.get("href", ""))
            if "subject/" in href:
                m = re.search(r"subject/(\d+)/", href)
                if m:
                    return m.group(1)
    except Exception as e:
        print(f"  豆瓣搜索异常: {e}")
    return None


def douban_set_watching(cookies_dict: dict, ck: str, subject_id: str, rating: int, private: bool) -> bool:
    """提交豆瓣「看过」+ 评分"""
    url = f"https://movie.douban.com/j/subject/{subject_id}/interest"
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": f"https://movie.douban.com/subject/{subject_id}/",
        "Origin": "https://movie.douban.com",
        "Host": "movie.douban.com",
        "Cookie": ";".join([f"{k}={v}" for k, v in cookies_dict.items()]),
    }
    data = {
        "ck": ck,
        "interest": "collect",
        "rating": str(rating),
        "foldcollect": "U",
        "tags": "",
        "comment": "",
        "private": "on" if private else "",
    }
    try:
        r = requests.post(url, headers=headers, data=data, timeout=10)
        if r.status_code == 200:
            ret = r.json().get("r")
            if ret is False:
                print(f"    豆瓣返回未开播或失败")
                return False
            return True
        print(f"    豆瓣返回: {r.status_code} {r.text[:150]}")
    except Exception as e:
        print(f"    请求豆瓣失败: {e}")
    return False


def douban_set_do(cookies_dict: dict, ck: str, subject_id: str, private: bool) -> bool:
    """提交豆瓣「在看」（不带评分）"""
    url = f"https://movie.douban.com/j/subject/{subject_id}/interest"
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": f"https://movie.douban.com/subject/{subject_id}/",
        "Origin": "https://movie.douban.com",
        "Host": "movie.douban.com",
        "Cookie": ";".join([f"{k}={v}" for k, v in cookies_dict.items()]),
    }
    data = {
        "ck": ck,
        "interest": "do",
        "rating": "",
        "foldcollect": "U",
        "tags": "",
        "comment": "",
        "private": "on" if private else "",
    }
    try:
        r = requests.post(url, headers=headers, data=data, timeout=10)
        if r.status_code == 200:
            ret = r.json().get("r")
            if ret is False:
                print(f"    豆瓣返回未开播或失败")
                return False
            return True
        print(f"    豆瓣返回: {r.status_code} {r.text[:150]}")
    except Exception as e:
        print(f"    请求豆瓣失败: {e}")
    return False


def main():
    print("========== Trakt → 豆瓣 同步测试 ==========")
    if not DOUBAN_COOKIE:
        print("请填写 DOUBAN_COOKIE 后再运行")
        return

    # 解析豆瓣 Cookie 并刷新 ck
    cookie = SimpleCookie(DOUBAN_COOKIE)
    cookies_dict = {k: v.value for k, v in cookie.items()}
    cookies_dict.pop("__utmz", None)
    cookies_dict.pop("ck", None)
    headers = {"User-Agent": USER_AGENT}
    headers["Cookie"] = ";".join([f"{k}={v}" for k, v in cookies_dict.items()])
    try:
        r = requests.get("https://www.douban.com/", headers=headers, timeout=10)
        ck_str = r.headers.get("Set-Cookie", "")
        if ck_str:
            ck = ck_str.split(";")[0].split("=", 1)[-1].strip()
            if ck != '"deleted"':
                cookies_dict["ck"] = ck
        if not cookies_dict.get("ck"):
            print("获取豆瓣 ck 失败，请检查 Cookie 是否有效")
            return
    except Exception as e:
        print(f"请求豆瓣获取 ck 失败: {e}")
        return

    items = fetch_trakt_ratings()
    if not items:
        print("未获取到 Trakt 评分列表")
        return
    print(f"Trakt 共 {len(items)} 条电影评分")

    limit = SYNC_LIMIT if SYNC_LIMIT > 0 else len(items)
    success = 0
    fail = 0
    for i, item in enumerate(items):
        if i >= limit:
            break
        movie = item.get("movie") or {}
        ids = movie.get("ids") or {}
        title = movie.get("title", "未知")
        year = movie.get("year")
        rating = int(item.get("rating") or 0)
        douban_star = trakt_rating_to_douban(rating)
        print(f"\n[{i+1}/{limit}] {title} ({year}) Trakt {rating} → 豆瓣 {douban_star} 星")

        subject_id = douban_search_subject_id(title, year)
        if not subject_id:
            print(f"  未找到豆瓣条目，跳过")
            fail += 1
            continue
        print(f"  豆瓣 subject_id: {subject_id}")

        if douban_set_watching(cookies_dict, cookies_dict.get("ck", ""), subject_id, douban_star, PRIVATE):
            print(f"  同步成功")
            success += 1
        else:
            fail += 1

    print("\n========== 评分同步结束 ==========")
    print(f"成功: {success}, 失败: {fail}")

    # 测试：基于 Trakt 播放进度（未看完列表） → 豆瓣「在看」
    print("\n========== 测试 Trakt 未看完列表 → 豆瓣「在看」 ==========")
    access_token = get_trakt_access_token()
    if not access_token:
        print("未能获取 Trakt Access Token，跳过未看完列表 → 在看 测试。")
        print("========== 全部结束 ==========")
        return

    playback_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": TRAKT_CLIENT_ID,
        "Authorization": f"Bearer {access_token}",
    }

    def fetch_playback(path: str) -> list:
        url = f"{TRAKT_API_BASE}{path}"
        try:
            r = requests.get(url, headers=playback_headers, timeout=20)
            if r.status_code == 204:
                return []
            if r.status_code == 200:
                data = r.json()
                return data if isinstance(data, list) else []
            print(f"Trakt 播放进度接口 {path} 返回 {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"拉取 Trakt 播放进度 {path} 失败: {e}")
        return []

    movies = fetch_playback("/sync/playback/movies")
    episodes = fetch_playback("/sync/playback/episodes")

    if not movies and not episodes:
        print("Trakt 播放进度列表为空，无未看完的条目。")
        print("========== 全部结束 ==========")
        return

    # 电影：直接按 movie 匹配豆瓣并标记在看
    for item in movies:
        progress = item.get("progress")
        if isinstance(progress, (int, float)) and progress >= 100:
            continue
        movie = item.get("movie") or {}
        title = movie.get("title", "未知")
        year = movie.get("year")
        print(f"\nTrakt 未看完电影: {title} ({year}) progress={progress}")
        subject_id = douban_search_subject_id(title, year)
        if not subject_id:
            print("  未找到豆瓣条目，跳过")
            continue
        print(f"  豆瓣 subject_id: {subject_id}")
        if douban_set_do(cookies_dict, cookies_dict.get("ck", ""), subject_id, PRIVATE):
            print("  在看状态同步成功")
        else:
            print("  在看状态同步失败")

    # 剧集：使用 show 信息按整部剧标记在看
    for item in episodes:
        progress = item.get("progress")
        if isinstance(progress, (int, float)) and progress >= 100:
            continue
        show = item.get("show") or {}
        if not show:
            continue
        title = show.get("title", "未知")
        year = show.get("year")
        print(f"\nTrakt 未看完剧集: {title} ({year}) progress={progress}")
        subject_id = douban_search_subject_id(title, year)
        if not subject_id:
            print("  未找到豆瓣条目，跳过")
            continue
        print(f"  豆瓣 subject_id: {subject_id}")
        if douban_set_do(cookies_dict, cookies_dict.get("ck", ""), subject_id, PRIVATE):
            print("  在看状态同步成功")
        else:
            print("  在看状态同步失败")

    print("\n========== 全部结束 ==========")


if __name__ == "__main__":
    main()
