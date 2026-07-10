# 商机挖掘爬虫

这是一个面向采购、招标、中标、成交、合同公告等公开网页的 AI 商机挖掘爬虫。

当前使用方式已经简化：**只需要在 `config.example.json` 里改网址和输出目录**。爬虫会自动调用 `.env` 里的模型接口，让 AI 判断页面是否符合采购、招标、商机线索要求。

## 1. 配好模型 Key

复制一份 env 文件：

```bash
cp .env.example .env
```

编辑 `.env`：

```dotenv
SOPHON_API_KEY=你的 key
SOPHON_API_BASE_URL=https://sophon-api.vzoom.com/ai/v1
SOPHON_MODEL=qwen-core
SOPHON_VERIFY_SSL=true
SOPHON_MAX_RETRIES=2
SOPHON_RETRY_DELAY_SECONDS=1.5
SOPHON_MAX_FAILURES=3
SOPHON_TEMPERATURE=0.75
SOPHON_MAX_TOKENS=1800
SOPHON_TRUST_ENV=false
# SOPHON_PROXY_URL=http://127.0.0.1:7890
```

如果开 VPN 后 Sophon API 不能用，优先保持：

```dotenv
SOPHON_TRUST_ENV=false
```

这个配置会让 Sophon API 请求不自动读取系统里的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 等代理环境变量。很多 VPN 会注入这些变量，导致 API 请求被错误代理。

如果 Sophon API 必须走某个本地代理，再显式打开：

```dotenv
SOPHON_PROXY_URL=http://127.0.0.1:7890
```

注意 `.env` 里每一项都要单独一行，不能把两个配置写在同一行。

检查 AI 是否能连通：

```bash
python3 self_evolving_agent_crawler.py --env-file .env --config config.example.json --check-ai
```

看到 `[ai-check] ok` 就可以运行。

## 2. 改网址和输出目录

打开 `config.example.json`，默认内容很短：

```json
{
  "seeds": [
    "https://www.ggzy.gov.cn/"
  ],
  "recent_days": 1,
  "max_depth": 3,
  "safety_max_pages": 1000,
  "output_dir": "data/ggzy_gov"
}
```

换任务时，主要改 `seeds`、`recent_days`、`max_depth`、`safety_max_pages` 和 `output_dir`：

```json
{
  "seeds": [
    "https://你要爬的网站/"
  ],
  "recent_days": 7,
  "max_depth": 3,
  "safety_max_pages": 1000,
  "output_dir": "data/你的任务名"
}
```

字段含义：

- `seeds`：起始网址，可以放一个或多个。
- `recent_days`：挖掘最近几天发布的商机。例如 `7` 表示只保留最近 7 天内发布的招标采购信息。
- `max_depth`：从起始网址最多往下点几层链接。
- `safety_max_pages`：单次运行最多保存多少个页面。这是安全上限，不是业务筛选条件。
- `output_dir`：本次挖掘任务的输出目录。不同任务建议使用不同目录，避免结果和状态混在一起。

如果二次运行没有抓到新页面，通常不是 `safety_max_pages` 太小，而是 `crawler_state.json` 里已经记录了已访问 URL，或者当前待爬链接已经爬完。需要继续扩大范围时，优先调大 `max_depth`，再调大 `safety_max_pages`。

## 3. 运行爬虫

```bash
python3 self_evolving_agent_crawler.py --env-file .env --config config.example.json
```

如果你直接运行脚本，也会默认读取 `.env` 和 `config.example.json`：

```bash
python3 self_evolving_agent_crawler.py
```

## 4. 输出在哪里

输出会保存到配置里的 `output_dir`。

例如当前配置：

```json
"output_dir": "data/ggzy_gov"
```

会输出到：

```text
data/ggzy_gov/
```

主要看这几个文件：

```text
data/<任务名>/pages/*.json
data/<任务名>/opportunities/*.json
data/<任务名>/opportunities_structured.json
data/<任务名>/opportunities_structured.txt
data/<任务名>/opportunities_summary.csv
data/<任务名>/opportunities_summary.jsonl
data/<任务名>/crawler_state.json
```

其中：

- `opportunities_structured.json`：最推荐查看的干净结构化商机结果，字段少，金额统一为“元”，日期统一格式。
- `opportunities_structured.txt`：同样的结果，每个参数单独一行，适合人工快速扫。
- `opportunities_summary.csv`：适合用 Excel 打开。
- `opportunities_summary.jsonl`：适合后续导入程序、数据库或再交给模型分析。
- `pages/*.json`：所有保存的网页内容和 AI 判断，主要用于排查。
- `opportunities/*.json`：单页命中留档，最终汇总会从 `pages/` 重新生成，避免旧结果污染。
- `crawler_state.json`：爬虫状态，包括已访问 URL、待爬队列、自适应延迟、阻塞冷却和抽取策略权重。

`crawler_state.json` 里比较重要的字段：

- `seen_urls`：已经访问过的 URL。二次运行会跳过这些 URL。
- `pending_frontier`：还没爬完的待爬队列。脚本退出后，下次会从这里继续。
- `domain_delay`：每个域名的自适应抓取延迟。
- `domain_block_count`：每个域名触发阻塞的次数。
- `domain_blocked_until`：某个域名被暂停到什么时候。

## 5. AI 会判断什么

AI 会判断页面是否包含可跟进的采购、招标、商机线索，并提取：

- 采购人 / 招标人 / 相关单位
- 预算或项目规模，并统一为 `金额元`
- 截止时间 / 开标时间
- 联系人
- 联系方式
- 地址
- 来源链接

最终结构化结果会再做一次规则过滤：

- `发布日期` 必须在 `recent_days` 范围内。
- `金额元` 必须大于 0。
- 没有金额、日期太旧、日期缺失或无法解析的页面，会保留在 `pages/` 里，但不会进入 `opportunities_structured.json`。

内部仍会用阶段辅助判断和去重，但不会写入最终 `opportunities_structured.json`。内部阶段包括：

- `tender_notice`：招标公告、公开招标、资格预审。
- `procurement_notice`：采购公告、竞争性磋商、竞争性谈判、询价、单一来源采购。
- `award_candidate`：中标候选人、成交候选人公示。
- `award_result`：中标结果、成交公告。
- `contract_notice`：合同公告。
- `market_research`：首页、列表页、搜索页，只作为继续爬取入口。
- `not_opportunity`：不是商机。
- `policy_info` / `credit_info`：规则兜底模式识别出的政策或信用信息页。

只有具体项目详情页会进入 `opportunities/`。首页、列表页、搜索页、政策法规、信用信息等页面会保留在 `pages/`，但不会混进最终商机结果。

## 6. 可选高级配置

一般不用写。如果需要调整模型参数、商机分数、页面上限或阻塞策略，可以在 `config.example.json` 里额外加：

```json
{
  "seeds": [
    "https://www.ggzy.gov.cn/"
  ],
  "recent_days": 7,
  "max_depth": 3,
  "safety_max_pages": 1000,
  "output_dir": "data/my_opportunities",
  "ai": {
    "enabled": true,
    "model": "qwen-core"
  },
  "opportunity": {
    "min_score": 0.6
  },
  "block_cooldown_seconds": 1800,
  "max_domain_blocks": 3
}
```

常见调整：

```json
{
  "recent_days": 7,
  "max_depth": 4,
  "safety_max_pages": 2000
}
```

含义：

- `recent_days` 越大，最终结果保留的发布日期范围越大。
- `max_depth` 越大，能从入口页继续深入更多层链接。
- `safety_max_pages` 越大，单次运行允许保存的页面越多。

注意：`safety_max_pages` 只是防止无限爬取的安全阀。它不会让爬虫凭空发现更多链接；真正影响发现范围的是 `seeds`、`max_depth`、站点页面链接结构和已有状态。

如果想重新开始一个任务，最推荐换一个 `output_dir`：

```json
{
  "seeds": [
    "https://www.ggzy.gov.cn/"
  ],
  "recent_days": 7,
  "max_depth": 3,
  "safety_max_pages": 1000,
  "output_dir": "data/ggzy_gov_new_run"
}
```

如果继续使用同一个 `output_dir`，爬虫会读取已有 `crawler_state.json` 并自动续爬。

## 7. 二次运行和续爬

爬虫不是每次都从零开始。它会保存状态：

```text
data/<任务名>/crawler_state.json
```

二次运行时：

- 已经访问过的 URL 会从 `seen_urls` 跳过。
- 上次没爬完的 URL 会从 `pending_frontier` 恢复。
- 如果旧状态没有 `pending_frontier`，爬虫会尝试从已有 `pages/*.json` 的 `links` 里重建待爬队列。
- 最终结果会重新从已有 `pages/*.json` 生成。

所以看到下面日志是正常的：

```text
[summary] opportunities=8 ...
[done] saved_pages=0 seen=50 ...
```

它表示这次没有保存新页面，只是重新生成了汇总结果。

如果想继续扩大当前任务，可以调大：

```json
{
  "max_depth": 4,
  "safety_max_pages": 2000
}
```

如果想完全重新爬一次，不复用旧状态，建议换新的 `output_dir`。

如果运行中按 `Ctrl+C` 终止，爬虫会先做收尾：

- 保存当前 `pending_frontier`。
- 保存 `crawler_state.json`。
- 重新生成 `opportunities_structured.json`、`opportunities_structured.txt`、`opportunities_summary.csv` 和 `opportunities_summary.jsonl`。

因此中断后也能看到截至当前已抓页面的最新汇总结果。

## 8. 自适应阻塞处理

爬虫内置了合规的自适应阻塞处理，不做验证码绕过、封禁规避或自动换 IP。

当页面或 HTTP 状态出现下面情况时，爬虫会把页面标记为 `blocked`，不会交给 AI 作为商机分析：

- `403`：无权限或被拒绝。
- `429`：请求过于频繁。
- `401` 或登录提示：需要登录。
- 页面包含验证码、人机验证、安全验证。
- 页面包含访问过于频繁、请稍后再试等频控提示。

触发后会自动：

- 在 `pages/*.json` 里记录 `blocked_reason`。
- 在 `crawler_state.json` 里记录 `domain_block_count` 和 `domain_blocked_until`。
- 增加该域名的抓取延迟。
- 暂停该域名一段时间，避免继续冲击网站。

可选高级参数：

```json
{
  "block_cooldown_seconds": 1800,
  "max_domain_blocks": 3
}
```

一般不用配置。默认策略已经足够保守。

## 9. 边界

这个爬虫适合低频、合规地分析公开网页。默认遵守 `robots.txt`，不会绕过登录、验证码、付费墙或访问控制。对搜索引擎、社交平台、登录态网站和强反爬站点，应优先使用官方 API 或授权数据源。
