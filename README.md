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
  "recent_days": 7,
  "max_depth": 2,
  "output_dir": "data/ggzy_gov"
}
```

换任务时，主要改 `seeds`、`recent_days` 和 `output_dir`：

```json
{
  "seeds": [
    "https://你要爬的网站/"
  ],
  "recent_days": 7,
  "max_depth": 2,
  "output_dir": "data/你的任务名"
}
```

字段含义：

- `seeds`：起始网址，可以放一个或多个。
- `recent_days`：挖掘最近几天发布的商机。例如 `7` 表示只保留最近 7 天内发布的招标采购信息。
- `max_depth`：从起始网址最多往下点几层链接。
- `output_dir`：本次挖掘任务的输出目录。不同任务建议使用不同目录，避免结果和状态混在一起。

爬虫不再用 `max_pages` 定义挖掘范围。代码里保留了一个内部安全上限 `safety_max_pages`，只用于防止异常站点无限爬取，正常任务不用配置。

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
- `crawler_state.json`：爬虫状态，包括已访问 URL、自适应延迟和抽取策略权重。

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

一般不用写。如果需要调整模型参数或商机分数，可以在 `config.example.json` 里额外加：

```json
{
  "seeds": [
    "https://www.ggzy.gov.cn/"
  ],
  "recent_days": 7,
  "max_depth": 2,
  "output_dir": "data/my_opportunities",
  "ai": {
    "enabled": true,
    "model": "qwen-core"
  },
  "opportunity": {
    "min_score": 0.6
  }
}
```

但默认情况下，不需要配置 `keywords`、`opportunity`、`ai` 或 `state_path`。

## 7. 自适应阻塞处理

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

## 8. 边界

这个爬虫适合低频、合规地分析公开网页。默认遵守 `robots.txt`，不会绕过登录、验证码、付费墙或访问控制。对搜索引擎、社交平台、登录态网站和强反爬站点，应优先使用官方 API 或授权数据源。
