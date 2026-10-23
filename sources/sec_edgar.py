"""
SEC EDGAR 8-K / 6-K / SC 13D / SC 13G / DEF 14A 抓取。

官方数据源：https://data.sec.gov/submissions/CIK{cik}.json
限流：10 请求 / 分钟 / IP —— 调用方必须在每个 CIK 之间 sleep。
CIK 格式：零填充到 10 位（如 1326801 → 0001326801）。

为什么先抓 8-K？
  10-K / 10-Q 是常规季报，信噪比低。
  8-K 是"material current report"——合并/收购/违约/盈利指引/重大风险事件。
  6-K 是外国发行人 current report（含 CoreWeave 这类 Delaware 关联实体）。
  SC 13D / 13G 是 5%+ 股权变动，也是重大信号。
  DEF 14A 是 proxy statement（年报前的董事会提案）。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests


log = logging.getLogger("sec_edgar")


EDGAR_BASE = "https://data.sec.gov"
# SEC 要求 User-Agent；不给会被 403。改成你自己的邮箱。
DEFAULT_USER_AGENT = "market-watchdog/1.0 (your@email.com)"
REQUEST_TIMEOUT = 15

# 最多最近多少天的 filings 才纳入候选（默认 3，跑得勤可放 2）
DEFAULT_MAX_DAYS_BACK = 3

# 每个 CIK 之间 sleep 的基础秒数；加上指数退避留余量
BASE_SLEEP = 8
MAX_RETRIES = 3


def _pad_cik(cik: str) -> str:
    cik = str(cik).strip().zfill(10)
    return cik


def _parse_filing_date(s: str) -> Optional[datetime]:
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
        return d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def fetch_recent_filings(
    cik: str,
    ticker: str = "",
    company_name: str = "",
    forms: Optional[list[str]] = None,
    max_days_back: int = DEFAULT_MAX_DAYS_BACK,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[dict]:
    """
    返回规范化的条目列表。每条：
    {
      "source_label": "SEC EDGAR 8-K",
      "source": "sec_edgar",
      "title": "8-K · Meta Platforms · 0001326801 · 2026-10-22",
      "url": "https://www.sec.gov/Archives/edgar/data/1326801/...",
      "ticker": "META",
      "cik": "0001326801",
      "company": "Meta Platforms Inc.",
      "form_type": "8-K",
      "timestamp": "2026-10-22T00:00:00+00:00",  # filingDate 是本地 00:00，统一 UTC
      "filing_date": "2026-10-22",
    }
    """
    forms = forms or ["8-K"]
    cik_padded = _pad_cik(cik)
    url = f"{EDGAR_BASE}/submissions/CIK{cik_padded}.json"
    headers = {"User-Agent": user_agent}

    payload = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = BASE_SLEEP * (2 ** attempt)
                log.warning("CIK %s 被限流/服务错误 (%s)，等 %ds 重试 %d/%d",
                            cik_padded, resp.status_code, wait, attempt, MAX_RETRIES)
                time.sleep(wait)
                continue
            if resp.status_code == 404:
                log.info("CIK %s 在 EDGAR 不存在（可能未上市或 CIK 填错），跳过", cik_padded)
                return []
            resp.raise_for_status()
            payload = resp.json()
            break
        except requests.RequestException as e:
            log.warning("CIK %s 请求失败（attempt %d/%d）：%s",
                        cik_padded, attempt, MAX_RETRIES, e)
            time.sleep(BASE_SLEEP * (2 ** attempt))
    if payload is None:
        log.error("CIK %s 最终仍未拿到数据，放弃", cik_padded)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days_back)
    recent = payload.get("filings", {}).get("recent", {})
    forms_list = recent.get("form", [])
    dates = recent.get("filingDate", [])
    acc_nums = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])

    out: list[dict] = []
    for i in range(len(forms_list)):
        form = forms_list[i]
        if form not in forms:
            continue
        d = _parse_filing_date(dates[i]) if i < len(dates) else None
        if d is None or d < cutoff:
            continue

        acc = acc_nums[i] if i < len(acc_nums) else ""
        doc = docs[i] if i < len(docs) else ""
        # 归档 URL：https://www.sec.gov/Archives/edgar/data/<原 CIK，无零填充>/<accession>/<doc>
        cik_plain = cik_padded.lstrip("0") or cik_padded
        archive_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_plain}/{acc.replace('-','')}/{doc}"
            if acc and doc else f"https://www.sec.gov/Archives/edgar/data/{cik_plain}/{acc}"
        )

        out.append({
            "source_label": f"SEC EDGAR {form}",
            "source": "sec_edgar",
            "title": f"{form} · {company_name or ticker or cik_padded} · {cik_padded} · {d.strftime('%Y-%m-%d')}",
            "url": archive_url,
            "ticker": ticker or "",
            "cik": cik_padded,
            "company": company_name or "",
            "form_type": form,
            "timestamp": d.isoformat(),
            "filing_date": d.strftime("%Y-%m-%d"),
        })

    log.info("CIK %s 拿到 %d 份 filings（form in %s，%d 天内）",
             cik_padded, len(out), forms, max_days_back)
    return out


# ------------------------------------------------------------
# 整批：对 config.yaml 里所有有 CIK 的 ticker 逐一抓
# ------------------------------------------------------------
def run_all(config: dict) -> list[dict]:
    src_cfg = config.get("sources", {}).get("sec_edgar", {})
    if not src_cfg.get("enabled", True):
        return []

    forms = src_cfg.get("form_types", ["8-K"])
    ua = src_cfg.get("user_agent", DEFAULT_USER_AGENT)
    interval = int(src_cfg.get("per_ticker_interval_seconds", BASE_SLEEP))
    tickers_cfg = config.get("tickers", {})

    # 把所有 ticker 组拍平（yaml.safe_load 保持文档顺序，v1_core 在最前）
    flat: list[dict] = []
    for grp, items in tickers_cfg.items():
        flat.extend(items or [])

    all_filings: list[dict] = []
    for idx, t in enumerate(flat):
        cik = str(t.get("cik", "")).strip()
        if not cik:
            continue
        ticker = t.get("ticker", "")
        name = t.get("name", "")
        log.info("[SEC %d/%d] %s %s CIK=%s", idx+1, len(flat), name, ticker, cik)
        filings = fetch_recent_filings(
            cik=cik,
            ticker=ticker,
            company_name=name,
            forms=forms,
            user_agent=ua,
        )
        all_filings.extend(filings)
        if idx < len(flat) - 1:
            time.sleep(interval)  # 遵守限流

    log.info("SEC EDGAR 本轮共 %d 份 filings 候选", len(all_filings))
    return all_filings
