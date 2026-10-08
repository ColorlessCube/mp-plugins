"""管理书影音未匹配记录、重试时间和管理员手动关联，不执行网络或豆瓣写入。"""
import hashlib
import time
from typing import Any, Callable, Dict, List, Optional


class MatchingHelper:
    """持久化未匹配候选，保留跨窗口重试所需的最小来源信息。"""

    _MISS_RETRY_SECONDS = 86400
    _ERROR_RETRY_SECONDS = 300

    def __init__(self, save_data_fn: Callable, get_data_fn: Callable):
        """加载未匹配记录与手动关联，不读取凭据或启动同步。"""
        self._save_data = save_data_fn
        state = get_data_fn("douban_matching") or {}
        self.items = dict(state.get("items") or {})
        self.manual = dict(state.get("manual") or {})
        self._contexts = {}

    def _persist(self) -> None:
        """只保存候选标识和处理状态，不保存 HTTP 响应或认证信息。"""
        self._save_data("douban_matching", {"items": self.items, "manual": self.manual})

    @staticmethod
    def make_key(kind: str, identity: str) -> str:
        """生成可用作接口路径的固定标识，避免把标题直接拼进 URL。"""
        return hashlib.sha256(f"{kind}:{identity}".encode()).hexdigest()

    def prepare(self, kind: str, identity: str, source: str, title: str, host: str, candidate: Dict[str, Any]) -> str:
        """记录本轮候选上下文，已延期候选保持原有重试时间而非不断续期。"""
        key = self.make_key(kind, str(identity))
        context = {"kind": kind, "identity": str(identity), "source": source, "title": title,
                   "host": host, "candidate": candidate, "last_seen_at": int(time.time())}
        self._contexts[key] = context
        if key in self.items:
            previous = self.items[key]
            changed = previous.get("title") != title
            self.items[key] = {**previous, **context}
            if changed:
                self.items[key]["retry_after"] = 0
            self._persist()
        return key

    def should_retry(self, key: str) -> bool:
        """手动关联立即可用，其余候选只在延期结束后重新搜索。"""
        return bool(self.manual.get(key)) or (self.items.get(key) or {}).get("retry_after", 0) <= time.time()

    def get_manual(self, key: str) -> Optional[str]:
        """读取管理员已确认的豆瓣条目 ID。"""
        return self.manual.get(key)

    def fail(self, key: str, reason: str, transient: bool = False) -> None:
        """保存实际匹配失败及原因，网络异常短暂延期，明确未命中次日重试。"""
        previous = self.items.get(key) or {}
        context = self._contexts.get(key) or previous
        now = int(time.time())
        self.items[key] = {**previous, **context, "reason": reason, "first_seen_at": previous.get("first_seen_at", now),
                           "attempts": previous.get("attempts", 0) + 1,
                           "retry_after": now + (self._ERROR_RETRY_SECONDS if transient else self._MISS_RETRY_SECONDS)}
        self._persist()

    def matched(self, key: str) -> None:
        """匹配完成后移除未匹配记录，已确认关联继续保留供后续状态变化使用。"""
        if self.items.pop(key, None) is not None:
            self._persist()

    def retry(self, key: str) -> bool:
        """解除一条匹配延期，下次同步再尝试，不触发网络请求或写入。"""
        if key not in self.items:
            return False
        self.items[key]["retry_after"] = 0
        self._persist()
        return True

    def associate(self, key: str, subject_id: str) -> bool:
        """保存管理员确认的关联；仍由后续同步核对来源并按共享预算写入。"""
        if key not in self.items:
            return False
        self.manual[key] = subject_id
        self.items[key].update({"reason": "已关联豆瓣条目，等待下次同步处理", "retry_after": 0})
        self._persist()
        return True

    def merge_candidates(self, kind: str, candidates: List[Dict[str, Any]], identity_fn: Callable) -> List[Dict[str, Any]]:
        """补入离开最近来源窗口的延期候选，当前来源记录始终优先。"""
        result = list(candidates)
        seen = {self.make_key(kind, str(identity_fn(item))) for item in candidates}
        added = 0
        for key, record in self.items.items():
            if record.get("kind") == kind and key not in seen and self.should_retry(key):
                result.append(record["candidate"])
                added += 1
                # 来源窗口外的历史候选逐轮补入，避免积压记录一次触发大量搜索。
                if added >= 5:
                    break
        return result
