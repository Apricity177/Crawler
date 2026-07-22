# AI 招采商机挖掘与内网看板

本工具每天从配置的招采网站检索最近 1 天公告，用 AI 筛选 AI 相关商机、抽取字段并匹配公司业务。结果会在内网网页中展示，方便同事共同查看。

## 部署：按这四步操作

> 请选择一台能在工作日持续开机、并接入公司内网的电脑作为运行机器。无需购买服务器；访问机器需要和这台机器处于同一公司网络或公司 VPN。

### 1. 配置模型密钥

在项目目录执行：

```bash
cp .env.example .env
```

编辑本机 `.env`，至少填写：

```dotenv
SOPHON_API_KEY=你的密钥
SOPHON_API_BASE_URL=https://sophon-api.vzoom.com/ai/v1
SOPHON_MODEL=qwen-core
SOPHON_TRUST_ENV=false
```

验证模型可用：

```bash
python3 self_evolving_agent_crawler.py --env-file .env --config config.example.json --check-ai
```

看到 `[ai-check] ok` 后再继续。`.env` 和 `.env.deepseek` 都只保留在本机，已被 Git 忽略，禁止提交或发送给他人。

### 2. 配置要监控的网站

编辑 [config.example.json](config.example.json)：

```json
{
  "urls": [
    "https://www.ggzy.gov.cn/",
    "https://example.com/"
  ],
  "days": 1,
  "pages": 3,
  "output_dir": "data/other_sites"
}
```

- `urls`：要监控的招采网站；可填写多个。
- `days`：保持 `1`，表示每日只查最近 1 天的新公告。
- `pages`：每个内置关键词查询的页数范围；数字越大，耗时和模型调用量越高。
- `output_dir`：结果保存位置，日常无需修改。

程序内置 AI、人工智能、大模型、AIGC、智能体、智能问答等关键词，无需另配。

### 3. 启动内网网页和每日自动更新

在项目目录运行下面这条命令，并保持终端和电脑开机：

```bash
python3 opportunity_dashboard.py --host 0.0.0.0 --port 8000 --daily-at 08:30
```

含义：网页服务使用 8000 端口；每天北京时间 08:30 自动运行一次爬虫。

首次部署时，如想立即采集一次，可另开一个终端运行：

```bash
python3 self_evolving_agent_crawler.py --env-file .env --config config.example.json
```

### 4. 访问地址

在运行机器的终端执行：

```bash
ipconfig getifaddr en0
```

假设输出为 `192.168.1.23`，应访问：

```text
http://192.168.1.23:8000
```


## 网页内容与筛选规则

- 以列表形式展示商机，按采集时间排序。
- 可按网址、与公司业务匹配度、招标单位行业筛选，也可搜索项目、单位、编号和采购内容。
- 网址筛选项完整显示 `config.example.json` 中的所有网站；尚未采集到商机的网站显示 `0`。
- 仅保留最近 30 天：网页、CSV、SQLite 历史库和逐条商机 JSON 都会自动清除 30 天前的数据。
- 可导出当前筛选结果为 CSV；公告内容以来源网站为准。

## 常用维护命令

```bash
# 只导入已有 JSON 到历史库，不启动网页
python3 opportunity_dashboard.py --import-only

# 改用 8080 端口
python3 opportunity_dashboard.py --host 0.0.0.0 --port 8080 --daily-at 08:30

# 普通图片验证码时，保存图片并在终端手工输入验证码
python3 self_evolving_agent_crawler.py --env-file .env --config config.example.json --manual-verification
```

程序不会绕过短信验证、滑块验证码、人机验证或 WAF。需要登录的网站可把账号密码放在本机 Excel `招标网站汇总及账号密码-提供给AI.xlsx`（表头：`网址`、`账号`、`密码`）；该文件也已被 Git 忽略。

## 数据文件

```text
data/other_sites/opportunities/*.json       每条商机的原始结构化记录
data/other_sites/opportunities_summary.csv  本次采集摘要
data/opportunities.db                       网页查询使用的历史库
data/other_sites/run_status.json            最近一次任务状态
```

不要将当前无登录保护的 8000 端口直接暴露到公网。如需让公司外部人员访问，应使用公司 VPN 或在具备 HTTPS 与登录保护的公司服务器上部署。
