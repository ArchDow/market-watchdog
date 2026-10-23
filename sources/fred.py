"""
FRED（圣路易斯联储经济数据库）——免费可自行复核的宏观/信用温度计。

两条路径：
  1. 有 api_key：官方 JSON API（120 req/min）
       https://api.stlouisfed.org/fred/series/observations?series_id=<id>&api_key=<key>
       注意：api_key 是必需参数，官方 API **没有匿名模式**（缺 key 返回 400，
       错误消息 "Variable api_key is not set"，实测 2026-10-23）。
  2. 无 api_key：fredgraph.csv 端点（免 key，无需注册）
       https://fred.stlouisfed.org/graph/fredgraph.csv?id=<id>&cosd=<YYYY-MM-DD>
       返回 CSV 全历史（cosd 可限定起始日减小体积），尾部即最新观测。
       实测 2026-10-23：200 OK，VIXCLS 最新 2026-10-22=19.03。

申请免费 key（推荐，更稳）：https://fred.stlouisfed.org/docs/api/api_key.html

返回最新一条 observation（官方每天更新，盘后）。"异动"检测（如 CCC 利差
跳升 > Xσ）是 V2 功能，V1 按用户"全量推"要求直接把最新值当一条 news 推。

series_id 预设：
  BAMLH0A0HYM2   ICE BofA US High Yield Option-Adjusted Spread（信用整体温度计）
  BAMLH0A3HYC    ICE BofA CCC & Lower OAS（垃圾债最边缘；溯源报告 12.67pp 即此类）
  VIXCLS         VIX 恐慌指数
  SP500          S&P 500
  NASDAQ100      NASDAQ 100 指数水平
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone

import requests


log = logging.getLogger("fred")

API_BASE = "https://api.stlouisfed.org/fred/series/observations"
CSV_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv"
REQUEST_TIMEOUT = 15
MAX_RETRIES = 2
BASE_SLEEP = 0.5
# CSV 路径限定起始日：只拉最近 30 天，避免每次全量下载（节假日最长连停 ~4 天，30 天足够）
CSV_LOOKBACK_DAYS = 30
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _build_item(series_id: str, label: str, date: str,
                val, prev_val) -> dict:
    pct = ""
    if val is not None and prev_val is not None and prev_val != 0:
        pct = f"  (日环比 {(val - prev_val) / prev_val * 100:+.2f}%)"
    label_final = label or series_id
    title = f"FRED · {label_final} · {date} · 值 {val if val is not None else '无数据'}{pct}"
    return {
        "source_label": f"FRED {label_final}",
        "source": "fred",
        "title": title,
        "url": f"https://fred.stlouisfed.org/series/{series_id}",
        "ticker": series_id,
        "company": label_final,
        "form_type": "macroeconomic",
        "timestamp": f"{date}T00:00:00+00:00",
        "series_id": series_id,
        "value": val,
        "prev_value": prev_val,
    }


def _fetch_via_api(series_id: str, api_key: str, label: str) -> list[dict]:
    """官方 JSON API（有 key 才可走）。"""
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 2,  # 最新 2 个点，用来算日环比
    }
    payload = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(API_BASE, params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429:
                wait = BASE_SLEEP * (2 ** attempt)
                log.warning("FRED %s 限流 429，等 %ds", series_id, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            payload = r.json()
            break
        except requests.RequestException as e:
            log.warning("FRED %s attempt %d/%d: %s", series_id, attempt, MAX_RETRIES, e)
            time.sleep(BASE_SLEEP * (2 ** attempt))
    if payload is None:
        log.error("FRED %s API 路径最终未拿到数据", series_id)
        return []

    obs = payload.get("observations", [])
    if not obs:
        log.info("FRED %s 无 observations", series_id)
        return []
    latest, prev = obs[0], (obs[1] if len(obs) > 1 else None)
    date = latest.get("date", "")
    val = None if latest.get("value", ".") == "." else float(latest["value"])
    prev_val = None
    if prev:
        prev_val = None if prev.get("value", ".") == "." else float(prev["value"])
    if not date:
        return []
    return [_build_item(series_id, label, date, val, prev_val)]


def _fetch_via_csv(series_id: str, label: str) -> list[dict]:
    """免 key fallback：fredgraph.csv，取尾部最新 2 个观测。"""
    cosd = (datetime.now(timezone.utc) - timedelta(days=CSV_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    params = {"id": series_id, "cosd": cosd}
    text = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(CSV_BASE, params=params, timeout=REQUEST_TIMEOUT + 5)
            if r.status_code == 429:
                wait = BASE_SLEEP * (2 ** attempt)
                log.warning("FRED %s CSV 限流 429，等 %ds", series_id, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            text = r.text
            break
        except requests.RequestException as e:
            log.warning("FRED %s CSV attempt %d/%d: %s", series_id, attempt, MAX_RETRIES, e)
            time.sleep(BASE_SLEEP * (2 ** attempt))
    if text is None:
        log.error("FRED %s CSV 路径最终未拿到数据", series_id)
        return []

    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    # 首行可能是表头（DATE,...）；按"第一列是否为日期"判断跳过
    if lines and not DATE_RE.match(lines[0].split(",")[0]):
        lines = lines[1:]
    rows = []
    for ln in lines:
        parts = ln.split(",")
        if len(parts) < 2 or not DATE_RE.match(parts[0]):
            continue
        raw = parts[1].strip().strip('"')
        rows.append((parts[0], None if raw in (".", "") else raw))
    if not rows:
        log.info("FRED %s CSV 无有效行", series_id)
        return []

    date, raw_val = rows[-1]
    prev_val = None
    if len(rows) >= 2:
        prev_val = None if rows[-2][1] is None else float(rows[-2][1])
    val = None if raw_val is None else float(raw_val)
    return [_build_item(series_id, label, date, val, prev_val)]


def fetch_latest_observation(series_id: str, api_key: str = "",
                             label: str = "") -> list[dict]:
    if api_key:
        items = _fetch_via_api(series_id, api_key, label)
        if items:
            return items
        log.warning("FRED %s API 路径失败，尝试 CSV fallback", series_id)
    return _fetch_via_csv(series_id, label)


def run_all(config: dict) -> list[dict]:
    src_cfg = config.get("sources", {}).get("fred", {})
    if not src_cfg.get("enabled", True):
        return []

    api_key = (src_cfg.get("api_key") or "").strip()
    series = src_cfg.get("series_ids", [])

    # 预设 label（可选），config 里 series_ids 只给了 id
    label_map = {
        "BAMLH0A0HYM2": "US High Yield OAS",
        "BAMLH0A3HYC": "CCC & Lower OAS（垃圾债最边缘）",
        "VIXCLS": "VIX 恐慌指数",
        "SP500": "S&P 500",
        "NASDAQ100": "NASDAQ 100",
    }

    all_items: list[dict] = []
    for sid in series:
        label = label_map.get(sid, sid)
        all_items.extend(fetch_latest_observation(sid, api_key, label))
        time.sleep(BASE_SLEEP)

    log.info("FRED 本轮共 %d 条候选", len(all_items))
    return all_items
