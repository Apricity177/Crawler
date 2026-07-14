# AI 招标采购信息挖掘脚本


默认流程已经简化：

1. 用内置关键词搜索招采网站。
2. 从搜索结果里拿详情页链接。
3. 抓取详情页正文。
4. 调用模型 API 判断是否真的是 AI 相关招标采购信息。
5. 由模型 API 抽取项目名称、采购单位、金额、时间、联系人、联系方式、地址、项目编号和来源链接。
6. 输出结构化结果。

默认运行时，如果 `.env` 里的模型 API 不可用，脚本不会用规则结果冒充 AI 结果。只有在配置里显式设置 `"ai": {"enabled": false}` 时，才会走规则兜底。

项目现在支持多站点商机挖掘：用户只需要在配置里填写招采网站网址，程序会自动选择合适的搜索和抽取方式。候选详情页抓取、AI 判断、字段抽取和结果输出共用同一套流程。

内置搜索关键词：

```text
AI、人工智能、大模型、大语言模型、生成式AI、AIGC、智能体、智能问答、智能客服、知识图谱、机器学习、深度学习、算法模型、模型训练、自然语言处理、NLP、计算机视觉、图像识别、语音识别、智能分析
```

## 1. 配置 API

默认 Sophon 配置可以继续放在 `.env`：

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
```

如果开 VPN 后 Sophon API 不能用，优先保留：

```dotenv
SOPHON_TRUST_ENV=false
```

如果 Sophon API 必须走本地代理，再显式加：

```dotenv
SOPHON_PROXY_URL=http://127.0.0.1:7890
```

检查 API 是否可用：

```bash
python3 self_evolving_agent_crawler.py --env-file .env --config config.example.json --check-ai
```

看到 `[ai-check] ok` 就可以运行。

代码会优先读取通用 `AI_*` 配置；如果没有 `AI_*`，再读取 `SOPHON_*` 配置。因此公司环境可以继续使用 `.env` 里的 Sophon 配置，也可以用 `AI_API_KEY`、`AI_API_BASE_URL`、`AI_MODEL` 接入其他 OpenAI-compatible 模型服务。

默认运行也会先做一次 AI 预检。如果模型接口不可用，脚本会直接停止，不会继续搜索和抓取页面。这样可以避免出现“搜索到了很多候选，但因为 AI 不通全部跳过”的假进度。

## 2. 配置任务

`config.example.json` 面向日常使用，只保留必须参数：

```json
{
  "urls": [
    "https://www.ggzy.gov.cn/"
  ],
  "days": 7,
  "pages": [
    1,
    3
  ],
  "output_dir": "data/ggzy"
}
```

字段含义：

- `urls`：要挖掘的招标采购网站网址，可以填一个或多个。
- `days`：时间范围。例如 `7` 表示只看最近 7 天的信息。
- `pages`：页码范围。例如 `[1, 3]` 表示每个关键词搜索第 1 到第 3 页。
- `output_dir`：输出目录。不同任务建议换不同目录，避免结果混在一起。

`[warn] ... _ssl.c:993: The handshake operation timed out` 通常是目标站点或当前网络在 HTTPS 握手阶段超时，不代表程序逻辑失败。脚本会自动重试；如果偶尔出现，可以先不用管。如果连续很多关键词都超时，再换网络或稍后重跑。

一般只需要改：

```json
{
  "urls": [
    "https://你要挖掘的招采网站/"
  ],
  "days": 30,
  "pages": [
    1,
    5
  ],
  "output_dir": "data/my_ai_tenders"
}
```

多网站任务也只是多填几个网址：

```json
{
  "urls": [
    "https://www.ggzy.gov.cn/",
    "https://example.com/"
  ],
  "days": 7,
  "pages": [
    1,
    3
  ],
  "output_dir": "data/ai_tenders"
}
```

默认不需要配置关键词。程序内置了 AI、人工智能、大模型、生成式 AI、AIGC、智能体等关键词。

## 3. 运行

```bash
python3 self_evolving_agent_crawler.py --env-file .env --config config.example.json
```

也可以直接运行，默认读取 `.env` 和 `config.example.json`：

```bash
python3 self_evolving_agent_crawler.py
```

运行时会看到类似日志：

```text
[search] site=ggzy keyword=人工智能 page=1 records=10
[search] candidates=23
[fetch] 1/23 https://www.ggzy.gov.cn/...
[hit] 某人工智能平台采购项目
[done] opportunities=3 output_dir=data/ggzy
```

## 4. 输出

主要看这四类文件：

```text
data/ggzy/opportunities/*.json
data/ggzy/opportunities_structured.json
data/ggzy/opportunities_structured.txt
data/ggzy/opportunities_summary.csv
```

脚本现在是边挖掘边写入：每命中一个详情页，就会立刻写入一份 `opportunities/*.json`，并刷新 `opportunities_structured.json`、`opportunities_structured.txt` 和 `opportunities_summary.csv`。不用等全部跑完再看结果。

字段尽量保持简洁：

```json
{
  "项目名称": "某人工智能平台采购项目",
  "采购单位": "某单位",
  "代理机构": "某招标代理有限公司",
  "金额元": 1200000,
  "截止时间": "2026-07-20 09:30:00",
  "发布日期": "2026-07-13",
  "联系人": "张三",
  "联系方式": "010-12345678",
  "地址": "北京市...",
  "项目编号": "AI-2026-001",
  "来源链接": "https://example.com/..."
}
```

## 5. 代码结构

当前代码已经删掉旧的 frontier 爬虫、自进化策略权重、robots 缓存、跨站递归等复杂逻辑，保留清晰的 AI 招标采购挖掘主流程：

```text
读取配置 -> 按站点搜索关键词 -> 抓详情页 -> AI 判断和抽取 -> 增量写入结果
```

新增站点时，优先只在 `urls` 里加网址。确实遇到某个网站搜索入口很特殊时，再在代码里补自动适配规则，不把这些实现细节暴露给普通用户。
