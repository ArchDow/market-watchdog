"""
RSS 信源（金十/财联社等中文快讯，经 RSSHub 公共实例）。

feedparser 解析 RSSHub 路由（免费、无需账号）。

为什么财联社不直连？（实测 2026-10-23）
  - www.cls.cn 的 web API（/api/sw、/v1/roll/get_roll_list 等）全部要求签名，
    返回 {"error":"签名错误"}，且无官方文档——凭印象编签名算法不可取；
  - telegraph 页面是 Next.js CSR，__NEXT_DATA__ 里没有电报列表数据。
  → 走 RSSHub 公共路由 rsshub.app/cls/telegraph（RSSHub 的 cls 路由开源可查）。
  注意：rsshub.app 在中国大陆直连 ConnectTimeout（实测），本地跑不通是预期；
  GitHub Actions（美国 runner）可达性以部署后日志为准。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import feedparser
import requests


log = logging.getLogger("rss_feeds")

REQUEST_TIMEOUT = 15
MAX_ENTRIES_PER_FEED = 40
MAX_RETRIES = 2


def _parse_dt(entry) -> str:
    """feedparser 的 entry 有 published_parsed / updated_parsed，转 ISO8601 UTC。"""
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not t:
        return datetime.now(timezone.utc).isoformat()
    try:
        return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()


def fetch_rss_feed(urls, label: str) -> list[dict]:
    """按顺序尝试多个镜像 URL，第一个成功即用。

    为什么要多镜像回退而不是在 config 里挂多条 feed：
    content_hash = sha256(source_label|title|url|ticker|ts)，同一条新闻挂两个镜像
    会产生两条不同 hash → 重复推送。回退放在代码里，同一 label 只产出一份。
    """
    if isinstance(urls, str):
        urls = [urls]
    d = None
    for url in urls:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # feedparser.parse(url) 不支持超时；先 requests 拿正文（超时可控）再解析
                r = requests.get(url, timeout=REQUEST_TIMEOUT, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; market-watchdog/1.0)"})
                r.raise_for_status()
                parsed = feedparser.parse(r.text)
                if parsed.bozo and not parsed.entries:
                    log.warning("RSS %s bozo=%s attempt %d/%d (%s)",
                                label, parsed.bozo_exception, attempt, MAX_RETRIES, url)
                    time.sleep(1.0 * (2 ** attempt))
                    continue
                d = parsed
                log.info("RSS %s 镜像命中 %s", label, url)
                break
            except Exception as e:
                log.warning("RSS %s 请求异常 attempt %d/%d (%s): %s",
                            label, attempt, MAX_RETRIES, url, e)
                time.sleep(1.0 * (2 ** attempt))
        if d is not None:
            break
    if d is None:
        log.error("RSS %s 全部镜像均失败：%s", label, urls)
        return []

    out: list[dict] = []
    for e in d.entries[:MAX_ENTRIES_PER_FEED]:
        title = (e.get("title") or "(无标题)").strip()
        link = e.get("link", "")
        published = _parse_dt(e)
        summary = (e.get("summary") or "")[:160].replace("\n", " ")
        out.append({
            "source_label": label,
            "source": "rss_feeds",
            "title": title,
            "url": link,
            "ticker": "",
            "company": "",
            "form_type": "",
            "timestamp": published,
            "summary": summary,
        })
    log.info("RSS %s 拿到 %d 条", label, len(out))
    return out


def run_all(config: dict) -> list[dict]:
    src_cfg = config.get("sources", {}).get("rss_feeds", {})
    if not src_cfg.get("enabled", True):
        return []

    all_items: list[dict] = []
    for f in src_cfg.get("feeds", []):
        # urls 支持多镜像顺序回退；url 单值写法保持兼容
        urls = f.get("urls") or ([f["url"]] if f.get("url") else [])
        label = f.get("label", "unknown")
        if not urls:
            continue
        all_items.extend(fetch_rss_feed(urls, label))

    log.info("rss_feeds 本轮共 %d 条候选", len(all_items))
    return all_items
