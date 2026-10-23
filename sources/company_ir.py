"""
公司 IR（Investor Relations）官网最新稿。

两种形态：
  1. 有 RSS —— 直接喂给 feedparser（和 rss_feeds 同逻辑）
  2. 无 RSS —— 请求 IR 新闻列表页 HTML，用正则抓最新几条 <a> 标题 + link

不引入 BeautifulSoup 减少依赖；正则够用（只抓最新 5 条，结构稳定的网站不会有问题）。
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import feedparser
import requests


log = logging.getLogger("company_ir")

REQUEST_TIMEOUT = 15
MAX_ENTRIES = 5
MAX_RETRIES = 2
BASE_SLEEP = 0.5


def fetch_rss(url: str, label: str) -> list[dict]:
    out: list[dict] = []
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # feedparser.parse(url) 不支持超时；先 requests 拿正文（超时可控）再解析
            r = requests.get(url, timeout=REQUEST_TIMEOUT, headers={
                "User-Agent": "Mozilla/5.0 (compatible; market-watchdog/1.0)"})
            r.raise_for_status()
            d = feedparser.parse(r.text)
            if d.bozo and not d.entries:
                log.warning("IR RSS %s bozo=%s attempt %d/%d", label, d.bozo_exception, attempt, MAX_RETRIES)
                time.sleep(1.0 * (2 ** attempt))
                continue
            break
        except Exception as e:
            log.warning("IR RSS %s attempt %d/%d: %s", label, attempt, MAX_RETRIES, e)
            time.sleep(1.0 * (2 ** attempt))
    else:
        log.error("IR RSS %s 最终拉失败", label)
        return out

    for e in d.entries[:MAX_ENTRIES]:
        title = (e.get("title") or "").strip()
        if not title:
            continue
        link = e.get("link", "")
        t = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
        try:
            ts = datetime(*t[:6], tzinfo=timezone.utc).isoformat() if t else datetime.now(timezone.utc).isoformat()
        except (TypeError, ValueError):
            ts = datetime.now(timezone.utc).isoformat()
        out.append({
            "source_label": label,
            "source": "company_ir",
            "title": title,
            "url": link,
            "ticker": "",
            "company": label,
            "form_type": "press_release",
            "timestamp": ts,
        })
    log.info("IR RSS %s %d 条", label, len(out))
    return out


def _make_abs(url: str, base: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, url)


def fetch_html_latest(url: str, label: str) -> list[dict]:
    out: list[dict] = []
    headers = {"User-Agent": "market-watchdog/1.0 (Windows NT 10.0; Win64; x64)"}
    html = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            html = r.text
            break
        except requests.RequestException as e:
            log.warning("IR HTML %s attempt %d/%d: %s", label, attempt, MAX_RETRIES, e)
            time.sleep(1.0 * (2 ** attempt))
    if html is None:
        log.error("IR HTML %s 最终拉失败", label)
        return out

    # 宽松正则：抓所有 <a href="...">...</a>，文本长度在 10-120 之间的"像标题"的条目
    # 然后去重 + 取前 MAX_ENTRIES
    seen = set()
    # 常见新闻列表里链接文本在 <h2>/<h3>/<h4>/<li>/<article> 内
    pattern = re.compile(
        r'<a\s+[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>\s*([^<]{10,150}?)\s*</a>',
        re.IGNORECASE,
    )
    for m in pattern.finditer(html):
        href = m.group(1).strip()
        text = m.group(2).strip()
        # 过滤导航/隐私政策等
        if not text or len(text) < 12:
            continue
        if any(kw in text.lower() for kw in ["privacy", "cookie", "terms", "investor relations", "contact", "login", "search", "skip"]):
            continue
        if href in seen:
            continue
        seen.add(href)
        abs_url = _make_abs(href, url)
        out.append({
            "source_label": label,
            "source": "company_ir",
            "title": text,
            "url": abs_url,
            "ticker": "",
            "company": label,
            "form_type": "press_release",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        if len(out) >= MAX_ENTRIES:
            break

    log.info("IR HTML %s %d 条", label, len(out))
    return out


def run_all(config: dict) -> list[dict]:
    src_cfg = config.get("sources", {}).get("company_ir", {})
    if not src_cfg.get("enabled", True):
        return []

    feeds = src_cfg.get("feeds", [])
    all_items: list[dict] = []
    for f in feeds:
        url = f.get("url", "")
        label = f.get("label", "unknown")
        html_mode = bool(f.get("html", False))
        if not url:
            continue
        if html_mode:
            all_items.extend(fetch_html_latest(url, label))
        else:
            all_items.extend(fetch_rss(url, label))
        time.sleep(BASE_SLEEP)

    log.info("company_ir 本轮共 %d 条候选", len(all_items))
    return all_items
