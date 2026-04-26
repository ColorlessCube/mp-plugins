# -*- coding: utf-8 -*-
"""
小宇宙 FM 播客 Helper
用于查询用户最近听取的播客单集及播客信息。
Token 失效时通过注入的 notify_fn 通知用户。

直接运行本文件可快速测试：
    python xiaoyuzhou_helper.py
"""
import random
import re
import shlex
import time
from typing import Any, Callable, Dict, List, Optional, Set

import requests

from app.log import logger


class XiaoyuzhouHelper:
    """小宇宙 FM API 封装类。

    实测 /v1/episode-played/list-history 响应格式：
        { "data": [ { "episode": {..., "eid", "pid", "title", "duration", "description"}, "podcast": {"pid": "..."} }, ... ], "loadMoreKey": ... }

    注意：
    - podcast 对象只含 pid，无 title / image；需单独调用 /v1/podcast/get 获取
    - list-history 不返回 playedAt（听取时间），该字段不可用

    Args:
        access_token: 小宇宙 API 认证令牌（x-jike-access-token），从浏览器 Cookie 中复制
        notify_fn: Token 失效等异常时的通知回调，签名 ``(title: str, body: str) -> None``
    """

    _BASE_URL = "https://api.xiaoyuzhoufm.com"
    _URL_HISTORY = f"{_BASE_URL}/v1/episode-played/list-history"
    _URL_EPISODE_DETAIL = f"{_BASE_URL}/v1/episode/get"
    _URL_PODCAST_DETAIL = f"{_BASE_URL}/v1/podcast/get"
    _URL_PLAYBACK_PROGRESS = f"{_BASE_URL}/v1/playback-progress/list"
    _REQUEST_JITTER_RANGE = (0.8, 2.0)

    # 已听完判定阈值（已播放秒数 / 总时长 >= 此值则视为听完）
    FINISHED_THRESHOLD = 0.90

    # 模拟 iOS 客户端请求头（对标 xyz 项目）
    _BASE_HEADERS = {
        "Host": "api.xiaoyuzhoufm.com",
        "User-Agent": "Xiaoyuzhou/2.57.1 (build:1576; iOS 17.4.1)",
        "Market": "AppStore",
        "App-BuildNo": "1576",
        "App-Version": "2.57.1",
        "OS": "ios",
        "OS-Version": "17.4.1",
        "Model": "iPhone14,2",
        "Manufacturer": "Apple",
        "BundleID": "app.podcast.cosmos",
        "Accept": "*/*",
        "Accept-Language": "zh-Hans-CN;q=1.0, zh-Hant-TW;q=0.9",
        "Content-Type": "application/json",
        "Connection": "keep-alive",
        "WifiConnected": "true",
        "app-permissions": "4",
        "x-custom-xiaoyuzhou-app-dev": "",
        "Timezone": "Asia/Shanghai",
    }

    def __init__(
        self,
        access_token: Optional[str] = None,
        notify_fn: Optional[Callable[[str, str], None]] = None,
    ):
        self._notify = notify_fn or (lambda title, body: None)
        self._access_token = self._extract_access_token(access_token)

        if not self._access_token:
            logger.warning("未提供小宇宙 access_token，个人数据接口将无法使用")
        else:
            logger.debug("小宇宙 access_token 已设置，长度 %d", len(self._access_token))

        self.session = requests.Session()
        self.session.headers.update(self._BASE_HEADERS)
        self.session.headers["x-jike-access-token"] = self._access_token

    # ------------------------------------------------------------------
    # 内部请求封装
    # ------------------------------------------------------------------

    def _now_iso(self) -> str:
        """生成当前时间的 ISO 8601 字符串，供 Local-Time 请求头使用。"""
        return time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime())

    def _sleep_before_request(self, action: str) -> None:
        """请求前随机等待，降低周期同步的固定节奏。"""
        delay = random.uniform(*self._REQUEST_JITTER_RANGE)
        logger.debug("小宇宙%s前随机等待 %.2f 秒", action, delay)
        time.sleep(delay)

    @staticmethod
    def _extract_cookie_value(cookie_string: str, key: str) -> str:
        """从 Cookie 字符串中提取指定字段值。"""
        if not cookie_string or not key:
            return ""
        match = re.search(rf"(?:^|;\s*){re.escape(key)}=([^;]+)", cookie_string)
        return match.group(1).strip() if match else ""

    @classmethod
    def _extract_access_token(cls, raw_value: Optional[str]) -> str:
        """兼容纯 token 或完整 curl，提取 x-jike-access-token。"""
        text = (raw_value or "").strip()
        if not text:
            return ""

        if not text.lower().startswith("curl "):
            if text.startswith("x-jike-access-token="):
                return text.split("=", 1)[1].strip()
            return text

        normalized = re.sub(r"\\\n\s*", " ", text).strip()
        try:
            parts = shlex.split(normalized)
        except Exception:
            logger.warning("解析小宇宙 cURL 失败，回退为原始文本")
            return text

        for i, part in enumerate(parts):
            if part in ("-b", "--cookie") and i + 1 < len(parts):
                cookie_string = parts[i + 1].strip()
                token = cls._extract_cookie_value(cookie_string, "x-jike-access-token")
                if token:
                    logger.debug("已从小宇宙 cURL Cookie 中提取 x-jike-access-token")
                    return token
                jt = cls._extract_cookie_value(cookie_string, "_jt")
                if jt:
                    match = re.search(r'"accessToken":"([^"]+)"', jt)
                    if match:
                        logger.debug("已从小宇宙 cURL 的 _jt 字段中提取 accessToken")
                        return match.group(1).strip()

        logger.warning("未能从小宇宙 cURL 中提取 x-jike-access-token")
        return ""

    def _handle_auth_error(self, method: str, url: str) -> None:
        """统一处理 401，明确提示 Token 问题并通知用户。"""
        msg = (
            "小宇宙 Token 失效或填写错误，请重新从浏览器复制 x-jike-access-token，"
            "或直接粘贴包含该字段的完整 cURL。"
        )
        detail = (
            f"method={method}, url={url}, "
            f"has_token={bool(self._access_token)}, token_length={len(self._access_token)}"
        )
        logger.error("小宇宙 %s 请求失败: %s (HTTP 401)；%s；%s", method, url, msg, detail)
        self._notify("小宇宙 Token 已失效", f"{msg}\n{detail}")

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """GET 请求封装。"""
        try:
            self._sleep_before_request("GET")
            self.session.headers["Local-Time"] = self._now_iso()
            resp = self.session.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401:
                self._handle_auth_error("GET", url)
                return None
            logger.error("小宇宙 GET 请求失败: %s (HTTP %d)", url, resp.status_code)
            return None
        except Exception as e:
            logger.error("小宇宙 GET 请求异常: %s", e)
            return None

    def _post(self, url: str, data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """POST 请求封装。"""
        try:
            self._sleep_before_request("POST")
            self.session.headers["Local-Time"] = self._now_iso()
            resp = self.session.post(url, json=data or {}, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401:
                self._handle_auth_error("POST", url)
                return None
            logger.error("小宇宙 POST 请求失败: %s (HTTP %d)", url, resp.status_code)
            return None
        except Exception as e:
            logger.error("小宇宙 POST 请求异常: %s", e)
            return None

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def get_recent_episodes(self, limit: int = 50) -> List[Dict[str, Any]]:
        """获取最近听取的播客单集列表，并自动回填播客名称和播放进度。

        流程：
        1. 调用 /v1/episode-played/list-history 拿单集列表（只含 pid，无播客名）
        2. 对列表中出现的去重 pid 批量调用 /v1/podcast/get，获取播客名和封面
        3. 将播客信息回填到各单集
        4. 批量调用 /v1/playback-progress/list 查询各单集播放进度，计算是否听完

        Args:
            limit: 最多返回几条记录

        Returns:
            单集列表，每项含：
                episode_id / title / podcast_id / podcast_name / duration / description
                / cover_url / played_at / listen_pct / is_finished
        """
        raw = self._post(self._URL_HISTORY, {})
        if not raw:
            return []

        # 响应格式: { "data": [...], "loadMoreKey": ... }
        episodes_data: List[Dict] = raw.get("data", []) if isinstance(raw, dict) else raw
        if not episodes_data:
            return []

        # ── Step 1：解析基本字段 ──────────────────────────────────────────
        result: List[Dict[str, Any]] = []
        for ep in episodes_data[:limit]:
            if not isinstance(ep, dict):
                continue
            episode = ep.get("episode", {})
            podcast = ep.get("podcast", {})
            # podcast 对象只含 pid，title/image 需单独获取
            podcast_id = podcast.get("pid", "") or episode.get("pid", "")
            result.append({
                "episode_id": episode.get("eid", ""),
                "title": episode.get("title", ""),
                "podcast_id": podcast_id,
                "podcast_name": "",   # 将在 Step 2 回填
                "duration": episode.get("duration", 0),
                "description": episode.get("description", ""),
                "cover_url": "",      # 将在 Step 2 回填
                "played_at": "",      # list-history 不返回听取时间
                "listen_pct": 0.0,    # 将在 Step 3 回填（已听百分比，0~1）
                "is_finished": False, # 将在 Step 3 回填（True = 已听完）
            })

        # ── Step 2：批量获取去重 pid 的播客详情，回填播客名和封面 ──────────
        unique_pids: Set[str] = {ep["podcast_id"] for ep in result if ep["podcast_id"]}
        podcast_info: Dict[str, Dict[str, str]] = {}
        for pid in unique_pids:
            detail = self.get_podcast_detail(pid)
            if detail:
                podcast_info[pid] = {
                    "podcast_name": detail.get("title", ""),
                    "cover_url": detail.get("cover_url", ""),
                }

        for ep in result:
            info = podcast_info.get(ep["podcast_id"], {})
            ep["podcast_name"] = info.get("podcast_name", "")
            ep["cover_url"] = info.get("cover_url", "")

        # ── Step 3：批量查询播放进度，计算是否听完 ──────────────────────────
        eids = [ep["episode_id"] for ep in result if ep["episode_id"]]
        if eids:
            progress_map = self.get_playback_progress(eids)
            for ep in result:
                eid = ep["episode_id"]
                progress_sec = progress_map.get(eid, 0)
                duration = ep["duration"] or 0
                if duration > 0 and progress_sec > 0:
                    pct = progress_sec / duration
                    ep["listen_pct"] = round(pct, 4)
                    ep["is_finished"] = pct >= self.FINISHED_THRESHOLD
                elif progress_sec < 0:
                    # 部分客户端以 -1 表示已听完
                    ep["listen_pct"] = 1.0
                    ep["is_finished"] = True

        return result

    def get_playback_progress(self, eids: List[str]) -> Dict[str, int]:
        """批量查询单集的播放进度。

        调用 /v1/playback-progress/list 接口，返回各单集已播放秒数。

        返回字段说明（根据 xyz 项目推断）：
            - progress: 已播放秒数（int）；可能为 -1 表示已听完
            - 若某 eid 无记录（从未播放），则不出现在返回字典中

        Args:
            eids: 单集 ID 列表，每次最多建议 50 条

        Returns:
            { eid: progress_seconds } 字典，未找到的 eid 不包含在内
        """
        if not eids:
            return {}

        # 接口每次最多查询 50 条，超出时分批请求
        result: Dict[str, int] = {}
        batch_size = 50
        for i in range(0, len(eids), batch_size):
            batch = eids[i: i + batch_size]
            raw = self._post(self._URL_PLAYBACK_PROGRESS, {"eids": batch})
            if not raw:
                logger.warning("小宇宙播放进度接口无响应，跳过批次 %d", i // batch_size)
                continue

            # 响应格式推断：{ "data": [ { "eid": "...", "progress": 1234, ... }, ... ] }
            # 也可能直接是列表格式，兼容两种情况
            data = raw.get("data", raw) if isinstance(raw, dict) else raw
            if not isinstance(data, list):
                logger.warning("小宇宙播放进度响应格式未知: %s", type(data))
                continue

            for item in data:
                if not isinstance(item, dict):
                    continue
                eid = item.get("eid", "")
                progress = item.get("progress", None)
                if eid and progress is not None:
                    try:
                        result[eid] = int(progress)
                    except (TypeError, ValueError):
                        pass

        return result

    def get_podcast_detail(self, podcast_id: str) -> Optional[Dict[str, Any]]:
        """获取播客详细信息。

        Returns:
            { podcast_id, title, description, cover_url, author }
        """
        if not podcast_id:
            return None
        raw = self._get(self._URL_PODCAST_DETAIL, params={"pid": podcast_id})
        if not raw:
            return None
        data = raw.get("data", {})
        if not isinstance(data, dict):
            return None
        # cover 字段：image.picUrl 或 image（字符串）
        image = data.get("image", {})
        cover_url = image.get("picUrl", "") if isinstance(image, dict) else str(image or "")
        return {
            "podcast_id": data.get("pid", ""),
            "title": data.get("title", ""),
            "description": data.get("description", ""),
            "cover_url": cover_url,
            "author": data.get("author", ""),
        }

    def get_episode_detail(self, episode_id: str) -> Optional[Dict[str, Any]]:
        """获取单集详细信息。

        Returns:
            { episode_id, title, duration, description, pub_date }
        """
        if not episode_id:
            return None
        raw = self._get(self._URL_EPISODE_DETAIL, params={"eid": episode_id})
        if not raw:
            return None
        data = raw.get("data", {})
        if not isinstance(data, dict):
            return None
        return {
            "episode_id": data.get("eid", ""),
            "title": data.get("title", ""),
            "duration": data.get("duration", 0),
            "description": data.get("description", ""),
            "pub_date": data.get("pubDate", ""),
        }

    @staticmethod
    def format_duration(seconds: int) -> str:
        """将秒数格式化为可读时长字符串。"""
        if not isinstance(seconds, int) or seconds < 0:
            return "-"
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        if h > 0:
            return f"{h} 小时 {m} 分钟"
        if m > 0:
            return f"{m} 分钟 {s} 秒"
        return f"{s} 秒"


# ---------------------------------------------------------------------------
# 本地测试入口（直接 python xiaoyuzhou_helper.py 运行）
# ---------------------------------------------------------------------------

def _print_sep(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def main() -> None:
    """交互式测试 XiaoyuzhouHelper 各步骤，方便诊断 Token 问题。"""
    import os
    import sys
    import logging
    import json

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(levelname)-8s %(name)s  %(message)s",
        stream=sys.stdout,
    )

    # ------------------------------------------------------------------ #
    # Step 1 · 读取 access_token
    # ------------------------------------------------------------------ #
    _print_sep("Step 1 · 读取 x-jike-access-token")

    token = os.environ.get("XIAOYUZHOU_TOKEN", "").strip()
    if not token:
        print("未通过环境变量 XIAOYUZHOU_TOKEN 传入 Token，请手动粘贴：")
        token = input().strip()

    if not token:
        print("[ERROR] 未提供任何 Token，退出。")
        sys.exit(1)

    display = token[:20] + "..." if len(token) > 20 else token
    print(f"[OK] Token 已获取，长度 {len(token)} 字符  ({display})")

    # ------------------------------------------------------------------ #
    # Step 2 · 初始化 Helper
    # ------------------------------------------------------------------ #
    _print_sep("Step 2 · 初始化 XiaoyuzhouHelper")

    def _notify(title: str, body: str) -> None:
        print(f"[NOTIFY] {title}: {body}")

    helper = XiaoyuzhouHelper(access_token=token, notify_fn=_notify)
    print(f"[OK] access_token 设置成功: {bool(helper._access_token)}")

    # ------------------------------------------------------------------ #
    # Step 3 · 拉取最近收听列表（含自动回填播客名 + 播放进度）
    # ------------------------------------------------------------------ #
    _print_sep("Step 3 · 拉取最近听取的播客（含播客名回填 + 播放进度）")

    episodes = helper.get_recent_episodes(limit=10)
    if not episodes:
        print("[ERROR] 拉取播客列表失败，Token 可能已失效，退出。")
        sys.exit(1)

    print(f"[OK] 共拉取 {len(episodes)} 条记录")
    print()
    for i, ep in enumerate(episodes[:5], 1):
        listen_pct = ep.get('listen_pct', 0.0)
        is_finished = ep.get('is_finished', False)
        status_str = "✅ 已听完" if is_finished else f"🔄 听了 {listen_pct*100:.0f}%"
        print(f"  {i}. 《{ep.get('title', '-')}》")
        print(f"     播客: {ep.get('podcast_name', '-')}")
        print(f"     时长: {XiaoyuzhouHelper.format_duration(ep.get('duration', 0))}")
        print(f"     状态: {status_str}")
        print(f"     封面: {'有' if ep.get('cover_url') else '无'}")

    # ------------------------------------------------------------------ #
    # Step 4 · 单独测试 get_playback_progress（原始响应）
    # ------------------------------------------------------------------ #
    _print_sep("Step 4 · 单独测试 get_playback_progress（原始响应）")

    eids = [ep["episode_id"] for ep in episodes[:5] if ep.get("episode_id")]
    if eids:
        progress_map = helper.get_playback_progress(eids)
        print(f"[OK] 查询 {len(eids)} 条，有进度记录 {len(progress_map)} 条")
        for eid, sec in progress_map.items():
            title = next((ep["title"] for ep in episodes if ep["episode_id"] == eid), eid)
            print(f"  《{title}》 → 已播放 {sec} 秒 ({XiaoyuzhouHelper.format_duration(sec)})")
    else:
        print("[SKIP] 无可用 eid")

    # ------------------------------------------------------------------ #
    # Step 5 · 获取单集详情
    # ------------------------------------------------------------------ #
    _print_sep("Step 5 · 获取单集详情（前 3 条）")

    for ep in episodes[:3]:
        eid = ep.get("episode_id", "")
        detail = helper.get_episode_detail(eid)
        if detail:
            print(f"\n  《{detail.get('title', '-')}》")
            print(f"    时长: {XiaoyuzhouHelper.format_duration(detail.get('duration', 0))}")
            print(f"    发布: {detail.get('pub_date', '-')[:10]}")
        else:
            print(f"\n  《{ep.get('title', '-')}》 → 获取详情失败")

    _print_sep("全部测试通过 ✅")


if __name__ == "__main__":
    main()
