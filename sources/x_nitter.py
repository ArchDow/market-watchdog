"""
X（Twitter）大 V：Nitter 第三方 RSS 镜像。

X 免费 API 已对大部分爬虫关闭，Nitter 是社区维护的替代方案。
双镜像轮换：一个挂了/403 → 试另一个；两个都挂 → 本轮放弃，不阻塞其他 source。

RSS URL 格式：https://<instance>/<handle>/rss
    https://nitter.privacydev.net/chamath/rss
    https://nitter.poast.org/elonmusk/rss

handle 列表：config.yaml → sources.x_nitter.handles

重要限制：
  1. Nitter 实例稳定性参差不齐，偶发整个镜像宕机 1-2 天。
  2. 推文无 ticker/CIK 字段，ticker 留空，靠 dedup hash 里的 handle 区分不同大 V。
  3. 不做"按关键词过滤"——v1 按用户"全量推"要求直接把每条最新推文当 news 推。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import feedparser
import requests


log = logging.getLogger("x_nitter")

MAX_ENTRIES_PER_HANDLE = 10
REQUEST_TIMEOUT = 15


def fetch_handle(handle: str, instances: list[str]) -> list[dict]:
    out: list[dict] = []
    for inst in instances:
        url = f"{inst.rstrip('/')}/{handle}/rss"
        try:
            # 先 HTTP HEAD 看 RSS 是否存在（Nitter 对不存在 handle 返回 404）
            r = requests.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True,
                              headers={"User-Agent": "market-watchdog/1.0"})
            if r.status_code in (404, 403):
                log.debug("Nitter %s handle %s → %s，换下一个镜像", inst, handle, r.status_code)
                continue
            if r.status_code >= 500:
                log.debug("Nitter %s 服务异常 %s，换下一个镜像", inst, r.status_code)
                continue

            # feedparser.parse(url) 不支持超时；先 requests 拿正文（超时可控）再解析
            resp = requests.get(url, timeout=REQUEST_TIMEOUT,
                                headers={"User-Agent": "market-watchdog/1.0"})
            resp.raise_for_status()
            d = feedparser.parse(resp.text)
            if d.bozo or not d.entries:
                log.debug("Nitter %s handle %s bozo=%s，换下一个镜像", inst, handle, d.bozo_exception)
                continue

            # 拿到 → 就用这个镜像，不再轮换
            for e in d.entries[:MAX_ENTRIES_PER_HANDLE]:
                title = (e.get("title") or e.get("summary") or "(无标题)").strip()
                link = e.get("link", "")
                t = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
                try:
                    ts = datetime(*t[:6], tzinfo=timezone.utc).isoformat() if t else datetime.now(timezone.utc).isoformat()
                except (TypeError, ValueError):
                    ts = datetime.now(timezone.utc).isoformat()

                out.append({
                    "source_label": f"X @{handle}",
                    "source": "x_nitter",
                    "title": title[:200],
                    "url": link,
                    "ticker": "",
                    "company": f"@{handle}",
                    "form_type": "tweet",
                    "timestamp": ts,
                    "handle": handle,
                })
            log.info("X %s (%s) 拿到 %d 条", handle, inst, len(out))
            return out  # 成功镜像就停
        except Exception as ex:
            log.debug("Nitter %s handle %s 异常 %s，换下一个镜像", inst, handle, ex)
            continue

    log.warning("X handle %s 两个镜像都失败，放弃", handle)
    return out


def run_all(config: dict) -> list[dict]:
    src_cfg = config.get("sources", {}).get("x_nitter", {})
    if not src_cfg.get("enabled", True):
        return []

    handles = src_cfg.get("handles", [])
    instances = src_cfg.get("instances", []) or ["https://nitter.privacydev.net"]

    all_items: list[dict] = []
    for h in handles:
        all_items.extend(fetch_handle(h, instances))
        time.sleep(1.0)  # 对镜像也礼貌一点

    log.info("x_nitter 本轮共 %d 条候选", len(all_items))
    return all_items
