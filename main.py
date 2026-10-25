"""
Market Watchdog — 入口。

流程：
  加载 config.yaml → 初始化去重 store → 构造 notifier → 拉各信源 → 合并去重 → 推送 → 持久化。

本地运行（需要先 export 三个环境变量，或写进 .env）：
  cd market-watchdog
  pip install -r requirements.txt
  export LARK_APP_ID=xxx LARK_APP_SECRET=xxx LARK_OPEN_ID=xxx
  python main.py

GitHub Actions 上：5 分钟 cron 定时跑，state.json 每次 run 末尾 git commit 回推。
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml

# 让 sources/ 和 notifier/ 能作为顶层 package 导入
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from dedup import DedupStore, filter_new  # noqa: E402
from notifier.feishu import build_from_config  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    log.info("config loaded from %s", path)
    return cfg


def run_source_safe(module_name: str, cfg: dict) -> list[dict]:
    """
    动态 import 一个 source 模块，调用它的 run_all(cfg)。
    任何 source 崩了都被吞掉，返回空列表，不影响其他 source。
    """
    try:
        mod = importlib.import_module(module_name)
        fn = getattr(mod, "run_all", None)
        if not callable(fn):
            log.warning("module %s 没有 run_all()，跳过", module_name)
            return []
        items = fn(cfg) or []
        log.info("source %s 返回 %d 条候选", module_name, len(items))
        return items
    except Exception as e:
        log.error("source %s 崩溃: %s\n%s", module_name, e, traceback.format_exc())
        return []


def main() -> int:
    cfg_path = HERE / "config.yaml"
    cfg = load_config(cfg_path)

    dedup_window = int(cfg.get("thresholds", {}).get("dedup_window_seconds", 3600))
    state_path = HERE / "state.json"
    store = DedupStore(state_path=str(state_path), dedup_window_seconds=dedup_window)

    log.info("dedup store loaded: %d hashes in window=%ds", store.count(), dedup_window)

    # ---- 构造 notifier ----
    notifier = build_from_config(cfg)
    log.info("feishu notifier: %s", "ok" if notifier else "disabled / missing creds")

    # ---- 拉所有信源（顺序固定，每个独立 try/except）----
    source_modules = [
        "sources.sec_edgar",
        "sources.company_ir",
        "sources.fred",
        "sources.rss_feeds",
        "sources.x_nitter",
    ]

    all_items: list[dict] = []
    for mod in source_modules:
        items = run_source_safe(mod, cfg)
        all_items.extend(items)

    log.info("全部信源合计 %d 条候选", len(all_items))

    # ---- 去重 ----
    new_items = filter_new(all_items, store)
    log.info("去重后 %d 条需要推送", len(new_items))

    # ---- 按时间排序（最新先推；时间解析失败的排最后）----
    def _sort_key(it: dict):
        ts = (it.get("timestamp") or "").replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    new_items.sort(key=_sort_key, reverse=True)

    # ---- 推送 ----
    push_ok, push_fail = 0, 0
    if notifier and new_items:
        for it in new_items:
            try:
                notifier.send(it)
                push_ok += 1
            except Exception as e:
                push_fail += 1
                log.error("推送失败: %s (%s)", it.get("title", ""), e)
        log.info("推送完成 ok=%d fail=%d", push_ok, push_fail)

    # ---- 持久化（只把这次尝试过推送的 new_items 标记为已推）----
    store.mark_pushed(new_items)
    store.save()

    # ---- 输出总结 JSON（供 GitHub Actions step summary / artifact）----
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidates_total": len(all_items),
        "new_after_dedup": len(new_items),
        "push_ok": push_ok,
        "push_fail": push_fail,
        "sources": source_modules,
    }
    summary_path = HERE / "last_run_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("summary 写入 %s", summary_path)
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    # 有新推送 → 退出码 0；全是老消息（无新内容）也退出码 0；
    # 只有致命错误（config 加载失败）才退出码 1，靠 try/except 在外层处理。
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log.critical("main 崩溃: %s\n%s", e, traceback.format_exc())
        sys.exit(1)
