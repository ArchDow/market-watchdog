"""
去重 + 状态持久化。

GitHub Actions 每次 run 销毁容器，跨次状态要持久化。
这里采用最简单方案：每次 run 末尾 git commit 回推 state.json 到 repo 本身。
比 GitHub Artifact 更稳（Artifact 90 天过期 / 读回麻烦）。

State schema:
{
  "pushed": {
    "<sha256(title+url+ticker+ts)>": "<ISO8601 timestamp when pushed>"
  },
  "meta": {
    "last_commit": "<git sha or empty>",
    "version": 1
  }
}
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Iterable


DEFAULT_STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")

# state.json 容量上限：超过后按「标记时间最旧优先」淘汰。
# 7 天窗口 × 中文快讯约 500 条/天 ≈ 3500 条，远低于此上限；这是防御性兜底。
MAX_STATE_ENTRIES = 20000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def content_hash(item: dict) -> str:
    """
    给一条抓取到的条目生成稳定 hash 键。
    item 至少要有 title + url；ticker / timestamp / source_label 可选。
    """
    blob = (
        f"{item.get('source_label','')}|"
        f"{item.get('title','')}|"
        f"{item.get('url','')}|"
        f"{item.get('ticker','')}|"
        f"{item.get('timestamp','')}"
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class DedupStore:
    """加载 / 查询 / 追加 / 保存 已推送条目的 hash 集合。"""

    def __init__(self, state_path: str = DEFAULT_STATE_PATH,
                 dedup_window_seconds: int = 3600):
        self.state_path = state_path
        self.window = dedup_window_seconds
        self.data: dict = self._load()
        self._dirty = False

    # ------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------
    def _load(self) -> dict:
        if not os.path.exists(self.state_path):
            return {"pushed": {}, "meta": {"last_commit": "", "version": 1}}
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            # 损坏 → 从空开始，不能让 state 本身导致崩溃
            return {"pushed": {}, "meta": {"last_commit": "", "version": 1}}

    def save(self) -> bool:
        """原子写 state.json。返回 True 表示有变更。"""
        if not self._dirty:
            return False
        # 清理超出 dedup_window 的旧条目，避免文件无限增长
        cutoff = time.time() - self.window
        kept = {}
        for h, iso_ts in self.data["pushed"].items():
            try:
                t = datetime.fromisoformat(iso_ts).timestamp()
            except ValueError:
                t = 0
            if t >= cutoff:
                kept[h] = iso_ts
        # 容量兜底：超上限时淘汰最旧的
        if len(kept) > MAX_STATE_ENTRIES:
            kept = dict(sorted(kept.items(), key=lambda kv: kv[1])[-MAX_STATE_ENTRIES:])
        self.data["pushed"] = kept
        self.data["meta"]["last_commit"] = ""  # workflow 会 git commit 填这个

        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.state_path)
        self._dirty = False
        return True

    # ------------------------------------------------------------
    # 查询 + 追加
    # ------------------------------------------------------------
    def is_new(self, item: dict) -> bool:
        """True = 这条之前没推过（或已超过 dedup window）。"""
        h = content_hash(item)
        ts_iso = self.data["pushed"].get(h)
        if not ts_iso:
            return True
        try:
            t = datetime.fromisoformat(ts_iso).timestamp()
        except ValueError:
            return True
        return time.time() - t > self.window

    def mark_pushed(self, items: Iterable[dict]) -> int:
        """把 items 全部标记为已推送；返回新加入的数量。"""
        n = 0
        for it in items:
            h = content_hash(it)
            if h not in self.data["pushed"]:
                self.data["pushed"][h] = _now_iso()
                self._dirty = True
                n += 1
        return n

    def count(self) -> int:
        return len(self.data["pushed"])


def filter_new(items: list[dict], store: DedupStore) -> list[dict]:
    """从候选列表里筛出"这条在 dedup window 内没推过"的条目。"""
    return [it for it in items if store.is_new(it)]
