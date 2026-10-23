# market-watchdog

实时资讯抓取 + **飞书单聊推送**的无服务器程序。信源：SEC EDGAR 8-K、FRED 信用温度计、中文快讯（财联社/金十，经 RSSHub），以及当前不可用的 X 大 V（Nitter 镜像）——实测状态见下方表格。

> **company_ir（公司 IR 官网）为何缺席？** 实测 2026-10-23：Meta / CoreWeave 的 IR RSS
> 返回 429 Cloudflare JS 盾（无头 HTTP 客户端无法通过），NVIDIA 旧 URL 已 404 且新域名
> 同样有盾。这些公司的重大公告已由 SEC EDGAR 8-K 覆盖；如你有无盾的 IR RSS，
> 在 `config.yaml → sources.company_ir` 加回即可。

> **V1 实际可用的信源只有 3 个（2026-10-24 实测）**：
> | 信源 | 状态 | 证据 |
> |---|---|---|
> | SEC EDGAR | ✅ 可用 | Actions 首跑拿到 5 份 filings |
> | FRED | ✅ 可用 | 5 条（含 fredgraph.csv 免 key 回退） |
> | 中文快讯（RSSHub） | ✅ 可用 | 经 `hub.slarker.me` 命中，财联社 20 条 + 金十 2 条 |
> | x_nitter（X 大 V） | ❌ **当前不可用** | `xcancel.com` / `nitter.privacydev.net` / `nitter.poast.org` 三镜像全部失败，本轮 0 条。Nitter 生态已基本停摆，X 官方 API 需付费，暂无免费替代 |
> | company_ir | ⛔ V1 禁用 | Cloudflare JS 盾 |
>
> RSSHub 公共实例的失败形态会变（2026-10-23 大陆超时 → 2026-10-24 Actions 上 403），
> 因此 `config.yaml` 里每个 feed 用 `urls:` 列表做多镜像顺序回退（回退实现在
> `sources/rss_feeds.py`，不是配置层挂多条——配置层挂多条会因 `source_label` 不同
> 产生不同 hash 而重复推送同一条新闻）。

## 快速开始

### 1. 本地运行（Windows / macOS / Linux）

```bash
# 克隆或把整个 market-watchdog 目录放到本机
cd market-watchdog

# Python >= 3.10
pip install -r requirements.txt

# 临时设置飞书凭据（测试用）
export LARK_APP_ID=cli_xxxxxxxxxxxxxxxxx
export LARK_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export LARK_OPEN_ID=ou_xxxxxxxxxxxxxxxxxxxxxx

python main.py
```

首次运行会创建 `state.json`（空的），推 0 条；第二次开始才会有"新条目"被推送。

### 2. GitHub Actions 5 分钟定时跑（推荐）

1. 把 `market-watchdog/` 目录**里面的内容**（`main.py`、`config.yaml`、`sources/`、`.github/` 等）推到你自己的 GitHub repo **根目录**——workflow 里按 repo 根做 checkout，不要多套一层 `market-watchdog/` 目录。
2. 在 repo 的 **Settings → Secrets and variables → Actions** 里加三个 secret：

   | Secret | 是什么 |
   |---|---|
   | `LARK_APP_ID` | 飞书开放平台应用的 App ID（`cli_` 开头） |
   | `LARK_APP_SECRET` | 同一个应用的 App Secret |
   | `LARK_OPEN_ID` | 你自己的飞书单聊 open_id（`ou_` 开头） |

3. Actions 里出现 `market-watchdog` 工作流，`Enable workflow`，手动触发一次看是否推成功。之后每 5 分钟自动跑。

> **额度提醒**：public repo 的 Actions 免费；**private repo 免费层只有 2000 分钟/月**，
> 而每 5 分钟一次 × 每次约 1-2 分钟 ≈ 8000+ 分钟/月，会耗尽额度。private 用户请把
> cron 降到 `*/30`（每 30 分钟），或改用 public repo（本项目代码无敏感信息；
> 飞书凭据全部放 Secrets，`state.json` 也无害）。

### 3. 飞书 bot 注册三要素怎么拿

#### App ID + App Secret
1. 打开 https://open.feishu.cn/app → 创建"自建应用"
2. 在应用的 **凭证与基础信息** 页拿到 `App ID` 和 `App Secret`
3. **权限** 里勾上：
   - `im:message` 或 `im:message:send_as_bot`（发消息）
   - `im:chat:readonly` （可选）
4. **版本管理与发布** → 创建版本 → 申请发布 / 或自己**在飞书搜索"你的应用名"把它加到你自己的 tenant 里**
5. **可用范围** 设置成你自己一个人即可（避免发给别人）

#### 自己的 open_id
两种方法任选：

```bash
# 方法 A：用 lark-cli（本地已安装）
lark-cli contact +get-user --as user
# 输出里会有 open_id 字段

# 方法 B：飞书浏览器 → 点自己头像 → URL 里有 open_id 参数
```

拿到 open_id 后，**先给自己发一条测试**（在 open-apis 调试台里手动调一次 `im/v1/messages?receive_id_type=open_id`），确认通了再配进 GitHub Secrets。

## 项目结构

```
market-watchdog/
├── config.yaml                  # 全部配置（标的池 / 信源开关 / 推送目标 / 阈值）
├── main.py                      # 入口：调度各 source → 去重 → 推送 → 持久化
├── dedup.py                     # hash 去重 + state.json 持久化（原子写）
├── requirements.txt
├── state.json                   # 运行时生成：已推条目 hash 集合
├── last_run_summary.json        # 每次 run 的 JSON 总结（artifact）
├── sources/
│   ├── __init__.py
│   ├── sec_edgar.py             # SEC EDGAR 8-K/6-K/SC 13D 抓取
│   ├── company_ir.py            # 公司 IR 官网最新稿（V1 禁用：被 Cloudflare 盾挡）
│   ├── fred.py                  # FRED 宏观/信用温度计（无 key 自动走 fredgraph.csv）
│   ├── rss_feeds.py             # 财联社/金十中文快讯（经 RSSHub 公共路由）
│   └── x_nitter.py              # X 大 V（Nitter RSS 双镜像）
├── notifier/
│   └── feishu.py                # 飞书单聊推送（原生 OpenAPI）
└── .github/workflows/watchdog.yml   # GitHub Actions 5 分钟定时工作流
```

## 标的池维护

`config.yaml` 的 `tickers:` 节点里维护所有 CIK / ticker：

| 字段 | 含义 | 来源 |
|---|---|---|
| `name` | 公司全名 | 自由填写 |
| `ticker` | trading symbol | 如 META / NVDA / TSLA |
| `cik` | SEC EDGAR 公司 ID，**零填充到 10 位** | 查 https://www.sec.gov/edgar/searchedgar/companysearch.html |
| `sector` | 板块标签 | 自由填写 |

- **只有上市到美股/SEC 备案的公司才有 CIK**。A 股/港股/日韩台股大部分 ticker 在 SEC 无 CIK（除非有 ADR），`cik` 字段留空 —— 这类标的仍会被 FRED / RSS / X 大 V 渠道覆盖。
- **CoreWeave 已于 2026-03 IPO（NASDAQ: CRWV）**，CIK 1769628，8-K 正常可查。
- **动态成交额排行**（A 股前 50 / 港股前 50 / 韩股前 50 …）需要从东方财富 / TWSE / KRX 等交易所 API 动态拉，是 v2 功能。v1 先用 well-known 成分硬编码。

## 限流与稳定性

| 信源 | 限流 | 备注 |
|---|---|---|
| SEC EDGAR | **10 req/min/IP** | `user_agent` header 必填；代码里 CIK 之间强制 sleep |
| FRED 官方 API | 120 req/min（需 key） | key 是**必需**参数，官方 API 无匿名模式（缺 key 返回 400） |
| FRED CSV（无 key 自动走） | 宽松 | `fredgraph.csv` 免 key 端点，拉最近 30 天，尾部即最新值 |
| 飞书 OpenAPI | 200 req/min/租户 | 市场异动量级远低于这个 |
| Nitter RSS | 不稳定 | 两个镜像轮换，一个挂了自动切下一个 |
| RSSHub 公共实例 | 宽松 | 财联社直连 API 需签名（无官方文档）→ 走 rsshub.app 路由；大陆直连 ConnectTimeout，以 Actions（美国节点）日志为准 |

## state.json 持久化机制

GitHub Actions runner 每次 run 完销毁，跨次状态靠 `git commit` 回推 `state.json`：
- `DedupStore` 在本地把新推条目的 hash 写进 `state.json`
- workflow 的 `git diff --cached --quiet` 判断这次 run 是否真有变更
- 有变更才 `git commit && git push`，没有变更不产生空 commit

## 常见问题

**Q: Nitter RSS 全挂了？**
A: Nitter 是第三方社区镜像，稳定性随运气。先换 `config.yaml → sources.x_nitter.instances` 里的另一个镜像，再不行就注释掉整个 `x_nitter` 节。

**Q: 飞书凭据配了但还是不推？**
A: workflow log 里会有 `feishu token 刷新成功` / `飞书单聊推送成功 message_id=om_xxx` 字样。凭据缺一个会在 `main.py` 启动时打印 `feishu notifier: disabled / missing creds`，程序继续跑但跳过推送（去重仍生效）。

**Q: 想加阈值（只推 CDS 跳升 > X 的新闻）？**
A: v1 按你要求全量推。阈值逻辑在 `main.py` 里加 `if thresholds.get("min_severity") == "high": ...` 即可，或在各 source 的 `run_all()` 里加关键词预过滤。

**Q: 飞书单聊太吵了？**
A: 把 `notifier.feishu.enabled` 改成 `false`，程序继续跑，`last_run_summary.json` 和 Actions step summary 里仍然能看到每次 run 有多少新条目。或者把自己拉进一个新建的飞书群，改 config 里的推送目标为 `chat_id`。

**Q: GitHub Actions 免费层 cron 真能准时吗？**
A: 实际触发间隔在 5–10 分钟之间（runner 排队 + 启动）。对"分钟级盯盘"够用，对"秒级抢发"不够；秒级需要自建 VPS 常驻进程 + X/Sec EDGAR 的更高频 API。
