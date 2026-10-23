"""
飞书单聊推送（原生 Lark OpenAPI，不依赖 lark-cli）。

为什么走原生而不是 lark-cli？
  GitHub Actions runner 需要预装 lark-cli（额外步骤 + 维护），原生 Python + requests
  只需 pip install requests，且 token 缓存/重试/卡片构造全部可控。

关键 API:
  1. 换 token   POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
       body: {"app_id","app_secret"}   → tenant_access_token（7200s 有效）
  2. 发消息    POST https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id
       Authorization: Bearer <token>
       body: {"receive_id","msg_type":"interactive","content":<card_json_string>}

官方频控：发消息接口有频控策略（触发时返回 99991400/11232/11233，见官方频控文档
https://open.feishu.cn/document/ukTMukTMukTM/uUzN04SN3QjL1cDN）—— 市场异动量级（几十条/天）
远低于任何频控阈值，无需批量合并。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

import requests


log = logging.getLogger("feishu")


TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

MAX_RETRIES = 3
RETRY_BASE = 1.0  # seconds


class FeishuNotifier:
    def __init__(self, app_id: str, app_secret: str, open_id: str):
        if not all([app_id, app_secret, open_id]):
            raise ValueError("FeishuNotifier 需要 app_id + app_secret + open_id 全量")
        self.app_id = app_id
        self.app_secret = app_secret
        self.open_id = open_id
        self._token: Optional[str] = None
        self._token_expires_at: float = 0

    # ------------------------------------------------------------
    # Token 管理（lazy + 缓存 + 提前 60s 刷新）
    # ------------------------------------------------------------
    def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = requests.post(TOKEN_URL, json={
                    "app_id": self.app_id,
                    "app_secret": self.app_secret,
                }, timeout=10)
                r.raise_for_status()
                payload = r.json()
                if payload.get("code", 0) != 0:
                    raise RuntimeError(f"换 token 失败: {payload}")
                self._token = payload["tenant_access_token"]
                self._token_expires_at = now + payload.get("expire", 7200)
                log.info("飞书 token 刷新成功，有效期 %ds", payload.get("expire", 7200))
                return self._token
            except requests.RequestException as e:
                wait = RETRY_BASE * (2 ** attempt)
                log.warning("换 token 失败 attempt %d/%d: %s，等 %ds 重试",
                            attempt, MAX_RETRIES, e, wait)
                time.sleep(wait)
        raise RuntimeError("飞书 token 最终未获取到")

    # ------------------------------------------------------------
    # 卡片 payload
    # ------------------------------------------------------------
    @staticmethod
    def build_card(item: dict) -> dict:
        """
        构造简洁的 interactive card。不追求复杂布局，信息优先。
        item 字段来源：sec_edgar / rss_feeds / company_ir / x_nitter 统一规范化。
        """
        source_label = item.get("source_label", "market-watchdog")
        title = item.get("title", "(无标题)")
        url = item.get("url", "")
        ticker = item.get("ticker", "")
        company = item.get("company", "")
        form_type = item.get("form_type", "")
        timestamp = item.get("timestamp", "")

        ticker_info_parts = [p for p in [
            f"📈 {ticker}" if ticker else None,
            f"🏢 {company}" if company else None,
            f"📋 {form_type}" if form_type else None,
        ] if p]
        ticker_info = "  ·  ".join(ticker_info_parts) or "(标的未匹配)"

        elements = [
            {"tag": "div",
             "text": {"tag": "lark_md",
                      "content": f"**{title}**\n\n{ticker_info}\n\n🕐 {timestamp}"}},
        ]
        if url:
            elements.append({
                "tag": "action",
                "actions": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": "打开原文"},
                     "url": url, "type": "primary"}
                ]
            })
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text",
                           "content": f"source: {source_label} · market-watchdog v1"}]
        })

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text",
                          "content": f"🔔 市场异动 · {source_label}"},
                "template": "red" if any(k in title.upper() for k in ["DEFAULT", "BANKRUPTCY", "FORCE MAJEURE"]) else "blue",
            },
            "elements": elements,
        }

    # ------------------------------------------------------------
    # 推送
    # ------------------------------------------------------------
    def send(self, item: dict) -> dict:
        """
        推一条。返回飞书 API 原始响应（含 message_id）。失败抛异常。
        """
        card = self.build_card(item)
        body = {
            "receive_id": self.open_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        for attempt in range(1, MAX_RETRIES + 1):
            # 每轮重新取：上一轮 99991668 置 None 后这里才会真正重换 token
            token = self._get_token()
            try:
                r = requests.post(
                    MSG_URL + "?receive_id_type=open_id",
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"},
                    json=body,
                    timeout=15,
                )
                payload = r.json() if r.content else {}
                code = payload.get("code", 0)
                # 错误码语义（官方 FAQ + 端点实测 2026-10-23，详见飞书配置指南）：
                #   99991663 = Invalid token（token 过期/伪造/不属于当前应用）→ 刷新重试
                #   99991668 = token 失效（官方 FAQ 未收录；按刷新处理无害）
                #   99991400 / 11232 / 11233 = 触发频控（真正的限流码）→ 退避重试
                if code in (99991663, 99991668):
                    self._token = None  # 强制刷新后重试
                    wait = RETRY_BASE * (2 ** attempt)
                    log.warning("飞书 token 失效 attempt %d/%d code=%s，刷新重试（等 %ds）",
                                attempt, MAX_RETRIES, code, wait)
                    time.sleep(wait)
                    continue
                if r.status_code >= 500 or code in (99991400, 11232, 11233):
                    wait = RETRY_BASE * (2 ** attempt)
                    log.warning("飞书推送被限流/5xx attempt %d/%d code=%s，等 %ds",
                                attempt, MAX_RETRIES, code, wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                if payload.get("code", 0) != 0:
                    raise RuntimeError(f"飞书业务码异常: {payload}")
                log.info("飞书单聊推送成功 message_id=%s",
                         payload.get("data", {}).get("message_id"))
                return payload
            except requests.RequestException as e:
                wait = RETRY_BASE * (2 ** attempt)
                log.warning("飞书推送网络错误 attempt %d/%d: %s，等 %ds",
                            attempt, MAX_RETRIES, e, wait)
                time.sleep(wait)
        raise RuntimeError(f"飞书推送最终失败: {item.get('title','')}")


# ------------------------------------------------------------
# 工厂：从 config + env 构造
# ------------------------------------------------------------
def build_from_config(cfg: dict) -> Optional[FeishuNotifier]:
    fei_cfg = cfg.get("notifier", {}).get("feishu", {})
    if not fei_cfg.get("enabled", True):
        return None
    app_id = os.environ.get(fei_cfg.get("app_id_env", "LARK_APP_ID"), "").strip()
    app_secret = os.environ.get(fei_cfg.get("app_secret_env", "LARK_APP_SECRET"), "").strip()
    open_id = os.environ.get(fei_cfg.get("open_id_env", "LARK_OPEN_ID"), "").strip()
    if not all([app_id, app_secret, open_id]):
        log.warning("飞书凭据不全（app_id=%s app_secret=%s open_id=%s），跳过推送",
                    "ok" if app_id else "MISSING",
                    "ok" if app_secret else "MISSING",
                    "ok" if open_id else "MISSING")
        return None
    return FeishuNotifier(app_id, app_secret, open_id)
