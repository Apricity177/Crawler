#!/usr/bin/env python3
"""A responsible AI opportunity-mining crawler.

The crawler adapts three things from feedback:
- per-domain crawl delay
- URL frontier priority
- extraction strategy weights

When AI is enabled, each page is also judged for business opportunity signals,
and that judgement guides which links should be crawled next.

It intentionally avoids bypass behavior such as CAPTCHA solving, auth probing,
or ban evasion. Keep seeds narrow.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import json
import math
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from typing import Any


HTML_TYPES = ("text/html", "application/xhtml+xml")
FINAL_OPPORTUNITY_STAGES = {
    "tender_notice",
    "procurement_notice",
    "award_candidate",
    "award_result",
    "contract_notice",
}
NON_FINAL_STAGES = {
    "market_research",
    "not_opportunity",
    "policy_info",
    "credit_info",
}
ALL_BUSINESS_STAGES = FINAL_OPPORTUNITY_STAGES | NON_FINAL_STAGES
MISSING_FIELD_MARKERS = (
    "暂无",
    "未明确",
    "未提供",
    "无数据",
    "没有提供",
    "需进一步",
    "进一步查看",
    "详见",
    "null",
    "none",
)
DEFAULT_KEYWORDS = [
    "招标",
    "采购",
    "中标",
    "成交",
    "公告",
    "项目",
    "预算",
    "投标",
    "开标",
    "代理机构",
    "采购人",
    "招标人",
]
DEFAULT_OPPORTUNITY_SIGNALS = [
    "招标公告",
    "采购公告",
    "竞争性磋商",
    "竞争性谈判",
    "询价公告",
    "资格预审",
    "中标候选人",
    "中标结果",
    "成交公告",
    "合同公告",
    "工程建设",
    "政府采购",
    "货物",
    "服务",
    "预算金额",
    "最高限价",
    "项目编号",
    "采购人",
    "招标人",
    "代理机构",
    "联系方式",
    "开标时间",
    "投标截止",
    "报名时间",
]
DEFAULT_NEGATIVE_SIGNALS = [
    "政策法规",
    "办事指南",
    "操作手册",
    "政策解读",
    "管理办法",
    "实施意见",
    "履行指引",
    "交易诚信",
    "信用信息",
    "奖励信息",
    "平台动态",
    "新闻",
    "登录",
    "注册",
    "验证码",
    "招聘",
]
SKIP_EXTENSIONS = {
    ".7z",
    ".avi",
    ".css",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".svg",
    ".tar",
    ".webp",
    ".xls",
    ".xlsx",
    ".zip",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_url(url: str, base_url: str | None = None) -> str | None:
    url = str(url).strip()
    if not url:
        return None
    try:
        if base_url:
            url = urllib.parse.urljoin(base_url, url)
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc or re.search(r"[\s\u4e00-\u9fff，。；；（）()]", parsed.netloc):
        return None
    path = parsed.path or "/"
    if os.path.splitext(path.lower())[1] in SKIP_EXTENSIONS:
        return None
    parsed = parsed._replace(fragment="", path=path)
    return urllib.parse.urlunparse(parsed)


def domain_of(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower()


def scope_domain_of(url: str) -> str:
    domain = domain_of(url)
    if domain.startswith("www."):
        return domain[4:]
    return domain


def host_matches(host: str, scope: str) -> bool:
    host = host.lower()
    scope = scope.lower()
    return host == scope or host.endswith("." + scope)


def tokenize(text: str) -> list[str]:
    return re.findall(r"[\w-]+", text.lower())


def text_quality(text: str, keywords: list[str]) -> float:
    words = tokenize(text)
    if not words:
        return 0.0

    unique_ratio = len(set(words)) / max(len(words), 1)
    length_score = min(len(words) / 700.0, 1.0)
    keyword_hits = sum(text.lower().count(keyword.lower()) for keyword in keywords)
    keyword_score = min(keyword_hits / 8.0, 1.0) if keywords else 0.2

    return round((0.45 * length_score) + (0.35 * unique_ratio) + (0.20 * keyword_score), 4)


def looks_like_html(body: bytes) -> bool:
    prefix = body[:1000].lstrip().lower()
    return prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html") or b"<html" in prefix[:300]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_env_file(path: str = ".env", override: bool = False) -> dict[str, str]:
    loaded: dict[str, str] = {}
    if not path or not os.path.exists(path):
        return loaded

    with open(path, "r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                raise ValueError(f"invalid env line {line_number} in {path}: expected KEY=VALUE")

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"invalid env key {key!r} on line {line_number} in {path}")
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if override or key not in os.environ:
                os.environ[key] = value
            loaded[key] = value
    return loaded


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)

    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("AI response did not contain a JSON object")
    parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("AI response JSON must be an object")
    return parsed


def extract_urls_from_text(text: str) -> list[str]:
    urls: list[str] = []
    patterns = [
        r"""["']((?:https?://|/)[^"'<> \t\r\n，。；;）)]+)["']""",
        r"""(https?://[^\s"'<>，。；;）)]+)""",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            url = match.group(1)
            if url not in urls:
                urls.append(url)
    return urls


@dataclass
class AIConfig:
    enabled: bool = True
    base_url: str = "https://sophon-api.vzoom.com/ai/v1"
    model: str = "qwen-core"
    api_key_env: str = "SOPHON_API_KEY"
    timeout: float = 30.0
    max_input_chars: int = 12000
    temperature: float = 0.1
    max_tokens: int | None = None
    max_retries: int = 2
    retry_delay_seconds: float = 1.5
    verify_ssl: bool = True
    trust_env: bool = True
    max_failures: int = 3

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AIConfig":
        if not data:
            config = cls()
        else:
            config = cls(**data)
        if "SOPHON_MAX_FAILURES" in os.environ:
            config.max_failures = int(os.environ["SOPHON_MAX_FAILURES"])
        if "SOPHON_TEMPERATURE" in os.environ:
            config.temperature = float(os.environ["SOPHON_TEMPERATURE"])
        if "SOPHON_MAX_TOKENS" in os.environ:
            config.max_tokens = int(os.environ["SOPHON_MAX_TOKENS"])
        if "SOPHON_TRUST_ENV" in os.environ:
            config.trust_env = env_bool("SOPHON_TRUST_ENV", config.trust_env)
        return config


@dataclass
class OpportunityConfig:
    goal: str = "判断公开网页是否包含可跟进的采购、招标、中标、成交、合同公告等商机线索，并提取采购人、招标人、预算、截止时间、联系方式和建议动作。"
    target_customers: list[str] = field(
        default_factory=lambda: [
            "政府采购单位",
            "事业单位",
            "国企央企",
            "工程建设单位",
            "招标代理机构",
            "B2B销售团队",
        ]
    )
    signals: list[str] = field(default_factory=lambda: list(DEFAULT_OPPORTUNITY_SIGNALS))
    negative_signals: list[str] = field(default_factory=lambda: list(DEFAULT_NEGATIVE_SIGNALS))
    min_score: float = 0.6

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "OpportunityConfig":
        if not data:
            return cls()
        return cls(**data)


@dataclass
class CrawlerConfig:
    seeds: list[str]
    keywords: list[str] = field(default_factory=lambda: list(DEFAULT_KEYWORDS))
    opportunity: OpportunityConfig = field(default_factory=OpportunityConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    recent_days: int = 7
    safety_max_pages: int = 300
    max_depth: int = 2
    output_dir: str = ""
    state_path: str = ""
    user_agent: str = "AIOpportunityCrawler/0.1 (+mailto:you@example.com)"
    obey_robots: bool = True
    request_timeout: float = 10.0
    min_delay_seconds: float = 2.0
    max_delay_seconds: float = 45.0
    block_cooldown_seconds: float = 1800.0
    max_domain_blocks: int = 3
    max_response_bytes: int = 3_000_000
    proxy_url: str | None = None
    scope_domains: list[str] = field(default_factory=list, init=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CrawlerConfig":
        if "seeds" not in data or not data["seeds"]:
            raise ValueError("config.seeds must contain at least one URL")

        data = dict(data)
        data.pop("max_pages", None)
        data["opportunity"] = OpportunityConfig.from_dict(data.get("opportunity"))
        data["ai"] = AIConfig.from_dict(data.get("ai"))
        config = cls(**data)
        normalized_seeds = []
        for seed in config.seeds:
            normalized = canonical_url(seed)
            if not normalized:
                raise ValueError(f"unsupported seed URL: {seed}")
            normalized_seeds.append(normalized)
        config.seeds = normalized_seeds
        config.scope_domains = sorted({scope_domain_of(seed) for seed in config.seeds})
        if not config.keywords:
            config.keywords = list(DEFAULT_KEYWORDS)
        if not config.output_dir:
            config.output_dir = f"data/{slug_from_url(config.seeds[0])}_opportunities"
        if not config.state_path:
            config.state_path = os.path.join(config.output_dir, "crawler_state.json")

        if config.min_delay_seconds < 0:
            raise ValueError("min_delay_seconds must be >= 0")
        if config.max_delay_seconds < config.min_delay_seconds:
            raise ValueError("max_delay_seconds must be >= min_delay_seconds")
        if config.recent_days <= 0:
            raise ValueError("recent_days must be positive")
        if config.safety_max_pages <= 0:
            raise ValueError("safety_max_pages must be positive")
        if config.max_depth < 0:
            raise ValueError("max_depth must be >= 0")
        if config.block_cooldown_seconds < 0:
            raise ValueError("block_cooldown_seconds must be >= 0")
        if config.max_domain_blocks <= 0:
            raise ValueError("max_domain_blocks must be positive")
        config.opportunity.min_score = clamp(config.opportunity.min_score, 0.0, 1.0)
        return config

    def to_dict(self) -> dict[str, Any]:
        return {
            "seeds": self.seeds,
            "keywords": self.keywords,
            "opportunity": {
                "goal": self.opportunity.goal,
                "target_customers": self.opportunity.target_customers,
                "signals": self.opportunity.signals,
                "negative_signals": self.opportunity.negative_signals,
                "min_score": self.opportunity.min_score,
            },
            "ai": {
                "enabled": self.ai.enabled,
                "base_url": self.ai.base_url,
                "model": self.ai.model,
                "api_key_env": self.ai.api_key_env,
                "timeout": self.ai.timeout,
                "max_input_chars": self.ai.max_input_chars,
                "temperature": self.ai.temperature,
                "max_tokens": self.ai.max_tokens,
                "max_retries": self.ai.max_retries,
                "retry_delay_seconds": self.ai.retry_delay_seconds,
                "verify_ssl": self.ai.verify_ssl,
                "trust_env": self.ai.trust_env,
                "max_failures": self.ai.max_failures,
            },
            "recent_days": self.recent_days,
            "safety_max_pages": self.safety_max_pages,
            "max_depth": self.max_depth,
            "output_dir": self.output_dir,
            "state_path": self.state_path,
            "user_agent": self.user_agent,
            "obey_robots": self.obey_robots,
            "request_timeout": self.request_timeout,
            "min_delay_seconds": self.min_delay_seconds,
            "max_delay_seconds": self.max_delay_seconds,
            "block_cooldown_seconds": self.block_cooldown_seconds,
            "max_domain_blocks": self.max_domain_blocks,
            "max_response_bytes": self.max_response_bytes,
            "proxy_url": self.proxy_url,
        }


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.title_parts: list[str] = []
        self.description_parts: list[str] = []
        self.text_by_strategy: dict[str, list[str]] = {
            "semantic": [],
            "paragraph": [],
            "body": [],
        }
        self.stack: list[str] = []
        self.current_anchor: str | None = None
        self.anchor_parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = {key.lower(): value for key, value in attrs if value is not None}

        if tag in {"script", "style", "noscript"}:
            self.skip_depth += 1

        if tag == "a" and (attr.get("href") or attr.get("onclick")):
            href = attr.get("href") or ""
            onclick_urls = extract_urls_from_text(attr.get("onclick", ""))
            if href.lower().startswith(("javascript:", "#")) and onclick_urls:
                href = onclick_urls[0]
            self.current_anchor = href
            self.anchor_parts = []

        if tag == "meta":
            name = (attr.get("name") or attr.get("property") or "").lower()
            if name in {"description", "og:description"} and attr.get("content"):
                self.description_parts.append(attr["content"].strip())

        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag == "a" and self.current_anchor:
            anchor_text = " ".join(self.anchor_parts).strip()
            self.links.append((self.current_anchor, anchor_text))
            self.current_anchor = None
            self.anchor_parts = []

        if tag in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1

        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return

        if self.current_anchor is not None:
            self.anchor_parts.append(text)

        current_tag = self.stack[-1] if self.stack else ""
        if current_tag == "title":
            self.title_parts.append(text)

        if "body" in self.stack:
            self.text_by_strategy["body"].append(text)
        if any(tag in self.stack for tag in ("article", "main")):
            self.text_by_strategy["semantic"].append(text)
        if current_tag in {"p", "li", "blockquote", "td", "h1", "h2", "h3"}:
            self.text_by_strategy["paragraph"].append(text)

    @property
    def title(self) -> str:
        return " ".join(self.title_parts).strip()

    @property
    def description(self) -> str:
        return " ".join(self.description_parts).strip()

    def candidates(self) -> dict[str, str]:
        return {
            name: re.sub(r"\s+", " ", " ".join(parts)).strip()
            for name, parts in self.text_by_strategy.items()
        }


@dataclass(order=True)
class FrontierItem:
    priority: float
    url: str = field(compare=False)
    depth: int = field(compare=False)
    parent_score: float = field(compare=False, default=0.0)


class AgentMemory:
    def __init__(self, path: str, min_delay: float) -> None:
        self.path = path
        self.seen_urls: set[str] = set()
        self.pending_frontier: list[dict[str, Any]] = []
        self.domain_delay: dict[str, float] = {}
        self.domain_block_count: dict[str, int] = {}
        self.domain_blocked_until: dict[str, float] = {}
        self.strategy_weights: dict[str, float] = {
            "semantic": 0.45,
            "paragraph": 0.35,
            "body": 0.20,
        }
        self.total_pages = 0
        self.total_reward = 0.0
        self.min_delay = min_delay

    @classmethod
    def load(cls, path: str, min_delay: float) -> "AgentMemory":
        memory = cls(path, min_delay)
        if not os.path.exists(path):
            return memory

        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)

        memory.seen_urls = set(raw.get("seen_urls", []))
        memory.pending_frontier = [
            item
            for item in raw.get("pending_frontier", [])
            if isinstance(item, dict) and item.get("url")
        ]
        memory.domain_delay = {
            str(key): float(value)
            for key, value in raw.get("domain_delay", {}).items()
        }
        memory.domain_block_count = {
            str(key): int(value)
            for key, value in raw.get("domain_block_count", {}).items()
        }
        memory.domain_blocked_until = {
            str(key): float(value)
            for key, value in raw.get("domain_blocked_until", {}).items()
        }
        memory.strategy_weights.update(
            {
                str(key): float(value)
                for key, value in raw.get("strategy_weights", {}).items()
            }
        )
        memory.total_pages = int(raw.get("total_pages", 0))
        memory.total_reward = float(raw.get("total_reward", 0.0))
        return memory

    def save(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": utc_now(),
            "seen_urls": sorted(self.seen_urls),
            "pending_frontier": self.pending_frontier,
            "domain_delay": self.domain_delay,
            "domain_block_count": self.domain_block_count,
            "domain_blocked_until": self.domain_blocked_until,
            "strategy_weights": self.strategy_weights,
            "total_pages": self.total_pages,
            "total_reward": round(self.total_reward, 4),
            "average_reward": round(self.total_reward / max(self.total_pages, 1), 4),
        }
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    def choose_extraction(self, candidates: dict[str, str], keywords: list[str]) -> tuple[str, str, float]:
        best_name = "body"
        best_text = candidates.get("body", "")
        best_score = -1.0

        for name, text in candidates.items():
            reward = text_quality(text, keywords)
            weight = self.strategy_weights.get(name, 0.0)
            score = (0.75 * reward) + (0.25 * weight)
            if score > best_score:
                best_name = name
                best_text = text
                best_score = score

        return best_name, best_text, text_quality(best_text, keywords)

    def evolve_after_page(
        self,
        domain: str,
        status: int,
        extraction_strategy: str | None,
        reward: float,
        config: CrawlerConfig,
        blocked_reason: str = "",
        retry_after_seconds: float | None = None,
    ) -> None:
        old_delay = self.domain_delay.get(domain, config.min_delay_seconds)
        if blocked_reason:
            block_count = self.domain_block_count.get(domain, 0) + 1
            self.domain_block_count[domain] = block_count
            multiplier = min(2 ** min(block_count - 1, 4), 16)
            cooldown = retry_after_seconds or (config.block_cooldown_seconds * multiplier)
            self.domain_blocked_until[domain] = round(time.time() + cooldown, 3)
            next_delay = old_delay * 2.0
        elif status == 200 and reward >= 0.25:
            self.domain_block_count[domain] = 0
            self.domain_blocked_until.pop(domain, None)
            next_delay = old_delay * 0.92
        elif status in {429, 503}:
            next_delay = old_delay * 2.0
        elif status in {403, 408, 500, 502, 504}:
            next_delay = old_delay * 1.35
        else:
            next_delay = old_delay * 1.10
        self.domain_delay[domain] = round(
            clamp(next_delay, config.min_delay_seconds, config.max_delay_seconds),
            3,
        )

        if extraction_strategy:
            current = self.strategy_weights.get(extraction_strategy, 0.0)
            self.strategy_weights[extraction_strategy] = round((0.85 * current) + (0.15 * reward), 4)

            # Tiny mutation keeps a weak strategy from becoming impossible to retry.
            for name in list(self.strategy_weights):
                jitter = random.uniform(-0.01, 0.01)
                self.strategy_weights[name] = round(clamp(self.strategy_weights[name] + jitter, 0.05, 1.0), 4)

        self.total_pages += 1
        self.total_reward += reward

    def domain_pause_remaining(self, domain: str) -> float:
        blocked_until = self.domain_blocked_until.get(domain, 0.0)
        return max(0.0, blocked_until - time.time())


class ChatAIClient:
    def __init__(self, config: AIConfig, proxy_url: str | None = None) -> None:
        self.config = config
        self.api_key = os.environ.get(config.api_key_env, "")
        if self.api_key.strip() in {"", "replace_with_your_key", "your_key", "你的 key"}:
            self.api_key = ""
        self.base_url = os.environ.get("SOPHON_API_BASE_URL", config.base_url)
        self.model = os.environ.get("SOPHON_MODEL", config.model)
        self.verify_ssl = env_bool("SOPHON_VERIFY_SSL", config.verify_ssl)
        self.temperature = float(os.environ.get("SOPHON_TEMPERATURE", str(config.temperature)))
        raw_max_tokens = os.environ.get("SOPHON_MAX_TOKENS")
        self.max_tokens = int(raw_max_tokens) if raw_max_tokens else config.max_tokens
        self.max_retries = int(os.environ.get("SOPHON_MAX_RETRIES", str(config.max_retries)))
        self.retry_delay_seconds = float(
            os.environ.get("SOPHON_RETRY_DELAY_SECONDS", str(config.retry_delay_seconds))
        )
        self.trust_env = env_bool("SOPHON_TRUST_ENV", config.trust_env)
        self.proxy_url = os.environ.get("SOPHON_PROXY_URL") or proxy_url
        self.opener = self._build_opener()

    def _build_opener(self) -> urllib.request.OpenerDirector:
        handlers: list[urllib.request.BaseHandler] = [
            urllib.request.HTTPSHandler(context=self._ssl_context())
        ]
        if self.proxy_url:
            handlers.append(
                urllib.request.ProxyHandler(
                    {
                        "http": self.proxy_url,
                        "https": self.proxy_url,
                    }
                )
            )
        elif not self.trust_env:
            handlers.append(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)

    def _ssl_context(self) -> ssl.SSLContext:
        if not self.verify_ssl:
            return ssl._create_unverified_context()
        try:
            import certifi  # type: ignore

            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            return ssl.create_default_context()

    @property
    def available(self) -> bool:
        return self.config.enabled and bool(self.api_key)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    @property
    def masked_key(self) -> str:
        if not self.api_key:
            return "<missing>"
        if len(self.api_key) <= 10:
            return "***"
        return f"{self.api_key[:6]}...{self.api_key[-4:]}"

    def chat_text(self, messages: list[dict[str, str]]) -> str:
        if not self.available:
            raise RuntimeError(f"AI is enabled but ${self.config.api_key_env} is not set")

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        raw = self.post_json(payload)
        data = json.loads(raw.decode("utf-8"))
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"unexpected AI response format: {data}") from exc

        if isinstance(content, list):
            return "\n".join(str(item.get("text", item)) for item in content)
        return str(content)

    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        return extract_json_object(self.chat_text(messages))

    def post_json(self, payload: dict[str, Any]) -> bytes:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error: Exception | None = None
        attempts = max(1, self.max_retries + 1)

        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(
                self.endpoint,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Connection": "close",
                    "User-Agent": "AIOpportunityCrawler/0.1",
                },
                method="POST",
            )
            try:
                with self.opener.open(request, timeout=self.config.timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="replace")[:1000]
                raise RuntimeError(f"AI HTTP {exc.code} from {self.endpoint}: {error_body}") from exc
            except Exception as exc:
                last_error = exc
                if attempt < attempts:
                    print(
                        f"[warn] AI request failed attempt={attempt}/{attempts}: {exc}; retrying...",
                        file=sys.stderr,
                    )
                    time.sleep(self.retry_delay_seconds)

        raise RuntimeError(f"AI request failed after {attempts} attempts: {last_error}") from last_error


class SelfEvolvingCrawler:
    def __init__(self, config: CrawlerConfig) -> None:
        self.config = config
        self.memory = AgentMemory.load(config.state_path, config.min_delay_seconds)
        self.frontier: list[FrontierItem] = []
        self.enqueued: set[str] = set()
        self.robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self.last_request_at: dict[str, float] = {}
        self.opener = self._build_opener(config.proxy_url)
        self.ai_client = ChatAIClient(config.ai, proxy_url=config.proxy_url)
        self.ai_failure_count = 0
        self.ai_disabled_for_run = False
        if config.ai.enabled and not self.ai_client.available:
            print(f"[warn] AI enabled but ${config.ai.api_key_env} is not set; using rule-based fallback.")

    def _build_opener(self, proxy_url: str | None) -> urllib.request.OpenerDirector:
        handlers: list[urllib.request.BaseHandler] = []
        handlers.append(urllib.request.HTTPSHandler(context=self._ssl_context()))
        if proxy_url:
            handlers.append(
                urllib.request.ProxyHandler(
                    {
                        "http": proxy_url,
                        "https": proxy_url,
                    }
                )
            )
        return urllib.request.build_opener(*handlers)

    def _ssl_context(self) -> ssl.SSLContext:
        try:
            import certifi  # type: ignore

            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            return ssl.create_default_context()

    def run(self) -> None:
        os.makedirs(self.config.output_dir, exist_ok=True)
        os.makedirs(os.path.join(self.config.output_dir, "pages"), exist_ok=True)
        os.makedirs(os.path.join(self.config.output_dir, "opportunities"), exist_ok=True)

        self.restore_pending_frontier()
        for seed in self.config.seeds:
            self.enqueue(seed, depth=0, parent_score=1.0, anchor_text="")
        if not self.frontier:
            rebuilt = self.rebuild_frontier_from_saved_pages()
            if rebuilt:
                print(f"[frontier] rebuilt={rebuilt} from saved pages")

        saved = 0
        deferred_items: list[FrontierItem] = []
        interrupted = False
        current_item: FrontierItem | None = None
        try:
            while self.frontier and saved < self.config.safety_max_pages:
                current_item = heapq.heappop(self.frontier)
                item = current_item
                if item.url in self.memory.seen_urls:
                    current_item = None
                    continue
                if item.depth > self.config.max_depth:
                    current_item = None
                    continue
                if not self.in_scope(item.url):
                    current_item = None
                    continue
                if self.config.obey_robots and not self.robot_allowed(item.url):
                    self.memory.seen_urls.add(item.url)
                    current_item = None
                    continue

                domain = domain_of(item.url)
                pause_remaining = self.memory.domain_pause_remaining(domain)
                if pause_remaining > 0:
                    deferred_items.append(item)
                    print(
                        f"[pause] domain={domain} remaining={pause_remaining:.0f}s reason=blocked_cooldown",
                        file=sys.stderr,
                    )
                    current_item = None
                    continue
                self.wait_for_domain(domain)
                print(f"[fetch] depth={item.depth} url={item.url}")
                result = self.fetch(item.url)
                if int(result.get("status", 0)) != 0:
                    self.memory.seen_urls.add(item.url)

                strategy = None
                reward = 0.0
                blocked_reason = str(result.get("blocked_reason", ""))
                if blocked_reason:
                    saved += 1
                    page = {
                        "url": item.url,
                        "depth": item.depth,
                        "fetched_at": utc_now(),
                        "status": result["status"],
                        "content_type": result.get("content_type", ""),
                        "title": "",
                        "description": "",
                        "extraction_strategy": None,
                        "reward": 0.0,
                        "page_score": 0.0,
                        "blocked_reason": blocked_reason,
                        "ai_analysis": {
                            "is_opportunity": False,
                            "opportunity_score": 0.0,
                            "business_stage": "not_opportunity",
                            "page_type": "blocked",
                            "blocked_reason": blocked_reason,
                        },
                        "text": result.get("html", "")[:2000],
                        "links": [],
                    }
                    self.save_page(page)
                    print(f"[blocked] domain={domain} reason={blocked_reason} url={item.url}", file=sys.stderr)
                elif result.get("html"):
                    parser = PageParser()
                    parser.feed(result["html"])
                    strategy, text, reward = self.memory.choose_extraction(parser.candidates(), self.config.keywords)
                    link_pairs = list(parser.links)
                    link_pairs.extend((href, "") for href in extract_urls_from_text(result["html"]))
                    links = []
                    seen_page_links: set[str] = set()
                    for href, _anchor in link_pairs:
                        canonical = canonical_url(href, item.url)
                        if canonical and canonical not in seen_page_links:
                            links.append(canonical)
                            seen_page_links.add(canonical)
                    ai_analysis = self.analyze_page_with_ai(
                        url=item.url,
                        title=parser.title,
                        description=parser.description,
                        text=text,
                        links=links,
                    )
                    if ai_analysis is None:
                        ai_analysis = self.rule_opportunity_analysis(
                            url=item.url,
                            title=parser.title,
                            description=parser.description,
                            text=text,
                            links=links,
                        )
                    ai_analysis = self.refine_opportunity_analysis(
                        url=item.url,
                        title=parser.title,
                        text=text,
                        links=links,
                        analysis=ai_analysis,
                    )
                    page_score = self.page_score(parser.title, parser.description, text, reward, ai_analysis)

                    saved += 1
                    page = {
                        "url": item.url,
                        "depth": item.depth,
                        "fetched_at": utc_now(),
                        "status": result["status"],
                        "content_type": result.get("content_type", ""),
                        "title": parser.title,
                        "description": parser.description,
                        "extraction_strategy": strategy,
                        "reward": reward,
                        "page_score": page_score,
                        "ai_analysis": ai_analysis,
                        "text": text,
                        "links": links,
                    }
                    self.save_page(page)
                    self.save_opportunity(page)

                    for href, anchor_text in link_pairs:
                        url = canonical_url(href, item.url)
                        if not url:
                            continue
                        self.enqueue(
                            url,
                            depth=item.depth + 1,
                            parent_score=page_score,
                            anchor_text=anchor_text,
                            ai_analysis=ai_analysis,
                        )

                self.memory.evolve_after_page(
                    domain=domain,
                    status=int(result.get("status", 0)),
                    extraction_strategy=strategy,
                    reward=reward,
                    config=self.config,
                    blocked_reason=blocked_reason,
                    retry_after_seconds=result.get("retry_after_seconds"),
                )
                self.memory.save()
                current_item = None
        except KeyboardInterrupt:
            interrupted = True
            print("[interrupt] stopping after current signal; saving state and summaries...", file=sys.stderr)
        finally:
            if current_item is not None and current_item.url not in self.memory.seen_urls:
                heapq.heappush(self.frontier, current_item)
            for item in deferred_items:
                heapq.heappush(self.frontier, item)
            self.save_pending_frontier()
            self.memory.save()
            self.write_opportunity_summaries()

        status = "interrupted" if interrupted else "done"
        print(f"[{status}] saved_pages={saved} seen={len(self.memory.seen_urls)} state={self.config.state_path}")

    def restore_pending_frontier(self) -> None:
        pending = list(self.memory.pending_frontier)
        self.memory.pending_frontier = []
        restored = 0
        for item in pending:
            url = canonical_url(str(item.get("url", "")))
            if not url or url in self.memory.seen_urls or url in self.enqueued:
                continue
            depth = int(item.get("depth", 0))
            if depth > self.config.max_depth or not self.in_scope(url):
                continue
            parent_score = float(item.get("parent_score", 0.0))
            priority = float(item.get("priority", -self.url_score(url, "", parent_score, depth)))
            heapq.heappush(self.frontier, FrontierItem(priority=priority, url=url, depth=depth, parent_score=parent_score))
            self.enqueued.add(url)
            restored += 1
        if restored:
            print(f"[frontier] restored={restored} from state")

    def save_pending_frontier(self) -> None:
        pending: list[dict[str, Any]] = []
        seen_pending: set[str] = set()
        for item in sorted(self.frontier):
            if item.url in self.memory.seen_urls or item.url in seen_pending:
                continue
            pending.append(
                {
                    "url": item.url,
                    "depth": item.depth,
                    "parent_score": item.parent_score,
                    "priority": item.priority,
                }
            )
            seen_pending.add(item.url)
        self.memory.pending_frontier = pending[: self.config.safety_max_pages * 3]

    def rebuild_frontier_from_saved_pages(self) -> int:
        pages_dir = os.path.join(self.config.output_dir, "pages")
        if not os.path.isdir(pages_dir):
            return 0
        rebuilt = 0
        for name in sorted(os.listdir(pages_dir)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(pages_dir, name), "r", encoding="utf-8") as handle:
                    page = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            page_depth = int(page.get("depth", 0))
            next_depth = page_depth + 1
            if next_depth > self.config.max_depth:
                continue
            parent_score = float(page.get("page_score", 0.0))
            links = page.get("links", [])
            if not isinstance(links, list):
                continue
            for raw_link in links:
                url = canonical_url(str(raw_link), str(page.get("url", "")))
                if not url or url in self.memory.seen_urls or url in self.enqueued or not self.in_scope(url):
                    continue
                self.enqueue(url, depth=next_depth, parent_score=parent_score, anchor_text="")
                rebuilt += 1
        return rebuilt

    def in_scope(self, url: str) -> bool:
        host = domain_of(url)
        return any(host_matches(host, scope) for scope in self.config.scope_domains)

    def robot_allowed(self, url: str) -> bool:
        host = domain_of(url)
        if host not in self.robots:
            robots_url = urllib.parse.urlunparse((urllib.parse.urlparse(url).scheme, host, "/robots.txt", "", "", ""))
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(robots_url)
            try:
                request = urllib.request.Request(robots_url, headers={"User-Agent": self.config.user_agent})
                with self.opener.open(request, timeout=self.config.request_timeout) as response:
                    body = response.read(self.config.max_response_bytes)
                parser.parse(body.decode("utf-8", errors="replace").splitlines())
            except Exception:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse([])
            self.robots[host] = parser
        return self.robots[host].can_fetch(self.config.user_agent, url)

    def wait_for_domain(self, domain: str) -> None:
        delay = self.memory.domain_delay.get(domain, self.config.min_delay_seconds)
        last = self.last_request_at.get(domain)
        if last is not None:
            wait_seconds = (last + delay) - time.monotonic()
            if wait_seconds > 0:
                time.sleep(wait_seconds)
        self.last_request_at[domain] = time.monotonic()

    def fetch(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"User-Agent": self.config.user_agent})
        try:
            with self.opener.open(request, timeout=self.config.request_timeout) as response:
                status = int(getattr(response, "status", response.getcode()))
                content_type = response.headers.get("Content-Type", "")
                retry_after_seconds = self.parse_retry_after(response.headers.get("Retry-After", ""))
                body = response.read(self.config.max_response_bytes + 1)
                if len(body) > self.config.max_response_bytes:
                    return {"status": status, "content_type": content_type, "html": ""}
                if not any(kind in content_type.lower() for kind in HTML_TYPES) and not looks_like_html(body):
                    return {"status": status, "content_type": content_type, "html": ""}
                encoding = response.headers.get_content_charset() or "utf-8"
                html = body.decode(encoding, errors="replace")
                blocked_reason = self.detect_blocked_reason(status, html)
                return {
                    "status": status,
                    "content_type": content_type,
                    "html": html,
                    "blocked_reason": blocked_reason,
                    "retry_after_seconds": retry_after_seconds,
                }
        except urllib.error.HTTPError as exc:
            body = exc.read(self.config.max_response_bytes)
            encoding = exc.headers.get_content_charset() or "utf-8"
            html = body.decode(encoding, errors="replace") if body else ""
            status = int(exc.code)
            return {
                "status": status,
                "content_type": exc.headers.get("Content-Type", ""),
                "html": html,
                "blocked_reason": self.detect_blocked_reason(status, html),
                "retry_after_seconds": self.parse_retry_after(exc.headers.get("Retry-After", "")),
            }
        except Exception as exc:
            print(f"[warn] fetch failed: {url} ({exc})", file=sys.stderr)
            return {"status": 0, "content_type": "", "html": ""}

    def parse_retry_after(self, value: str) -> float | None:
        value = (value or "").strip()
        if not value:
            return None
        if value.isdigit():
            return float(value)
        return None

    def detect_blocked_reason(self, status: int, html: str) -> str:
        if status == 429:
            return "rate_limited"
        if status == 401:
            return "login_required"
        if status == 403:
            return "forbidden"
        if status in {503, 520, 521, 522, 523, 524}:
            haystack = html[:4000].lower()
            if any(marker in haystack for marker in ["captcha", "cloudflare", "访问验证", "安全验证"]):
                return "challenge"

        haystack = re.sub(r"\s+", " ", html[:8000]).lower()
        markers = [
            ("captcha", "captcha"),
            ("验证码", "captcha"),
            ("安全验证", "challenge"),
            ("访问验证", "challenge"),
            ("人机验证", "challenge"),
            ("too many requests", "rate_limited"),
            ("rate limit", "rate_limited"),
            ("访问过于频繁", "rate_limited"),
            ("请求过于频繁", "rate_limited"),
            ("请稍后再试", "rate_limited"),
            ("登录后查看", "login_required"),
            ("请登录", "login_required"),
            ("用户登录", "login_required"),
        ]
        for marker, reason in markers:
            if marker in haystack:
                return reason
        return ""

    def enqueue(
        self,
        url: str,
        depth: int,
        parent_score: float,
        anchor_text: str,
        ai_analysis: dict[str, Any] | None = None,
    ) -> None:
        if url in self.memory.seen_urls or url in self.enqueued:
            return
        if depth > self.config.max_depth:
            return
        if not self.in_scope(url):
            return

        score = self.url_score(url, anchor_text, parent_score, depth, ai_analysis)
        heapq.heappush(self.frontier, FrontierItem(priority=-score, url=url, depth=depth, parent_score=parent_score))
        self.enqueued.add(url)

    def url_score(
        self,
        url: str,
        anchor_text: str,
        parent_score: float,
        depth: int,
        ai_analysis: dict[str, Any] | None = None,
    ) -> float:
        haystack = f"{url} {anchor_text}".lower()
        signals = self.config.keywords + self.config.opportunity.signals
        keyword_hits = sum(1 for keyword in signals if keyword.lower() in haystack)
        negative_hits = sum(1 for keyword in self.config.opportunity.negative_signals if keyword.lower() in haystack)
        ai_hint_hits = 0
        if ai_analysis:
            hints = ai_analysis.get("link_hints", [])
            if isinstance(hints, list):
                ai_hint_hits = sum(1 for hint in hints if str(hint).lower() in haystack)
        path = urllib.parse.urlparse(url).path
        path_depth = path.strip("/").count("/")
        query_penalty = 0.15 if urllib.parse.urlparse(url).query else 0.0
        score = (
            0.45
            + (0.35 * parent_score)
            + (0.25 * keyword_hits)
            + (0.30 * ai_hint_hits)
            - (0.20 * negative_hits)
            - (0.08 * depth)
            - (0.03 * path_depth)
            - query_penalty
        )
        return round(max(score, 0.01), 4)

    def page_score(
        self,
        title: str,
        description: str,
        text: str,
        reward: float,
        ai_analysis: dict[str, Any] | None = None,
    ) -> float:
        keyword_text = f"{title} {description} {text[:2000]}".lower()
        signals = self.config.keywords + self.config.opportunity.signals
        keyword_hits = sum(keyword_text.count(keyword.lower()) for keyword in signals)
        keyword_boost = math.log1p(keyword_hits) * 0.15 if keyword_hits else 0.0
        rule_score = clamp(reward + keyword_boost, 0.0, 1.0)
        ai_score = self.ai_score(ai_analysis)
        if ai_score is None:
            return round(rule_score, 4)
        return round(clamp((0.35 * rule_score) + (0.65 * ai_score), 0.0, 1.0), 4)

    def ai_score(self, ai_analysis: dict[str, Any] | None) -> float | None:
        if not ai_analysis:
            return None
        raw_score = ai_analysis.get("opportunity_score")
        try:
            return clamp(float(raw_score), 0.0, 1.0)
        except (TypeError, ValueError):
            return None

    def refine_opportunity_analysis(
        self,
        url: str,
        title: str,
        text: str,
        links: list[str],
        analysis: dict[str, Any],
    ) -> dict[str, Any]:
        refined = dict(analysis)
        stage = self.normalize_business_stage(
            url=url,
            title=title,
            text=text,
            raw_stage=str(refined.get("business_stage", "")),
        )
        score = self.ai_score(refined)
        if score is None:
            score = 0.0

        is_navigation = self.is_navigation_or_listing_page(url, title, text, links)
        if is_navigation and stage not in {"policy_info", "credit_info", "not_opportunity"}:
            stage = "market_research"
            score = min(score, 0.58)

        if stage in NON_FINAL_STAGES:
            refined["is_opportunity"] = False
            if stage == "market_research":
                score = min(score, 0.58)
            elif stage in {"policy_info", "credit_info", "not_opportunity"}:
                score = min(score, 0.35)
        else:
            refined["is_opportunity"] = bool(refined.get("is_opportunity", True)) and score >= 0.0

        structured_fields = self.extract_structured_fields(title, text)
        for key, value in structured_fields.items():
            if self.is_missing_field(refined.get(key)):
                refined[key] = value
        refined["structured_fields"] = structured_fields
        for key in [
            "project_id",
            "customer_or_org",
            "agency",
            "supplier_or_winner",
            "budget_or_scale",
            "deadline",
            "publish_date",
            "contact",
            "address",
        ]:
            if key in refined and self.is_missing_field(refined.get(key)):
                refined[key] = None
        refined["budget_normalized"] = self.normalize_money(refined.get("budget_or_scale"))
        refined["deadline_normalized"] = self.normalize_datetime_value(refined.get("deadline"))
        refined["publish_date_normalized"] = self.normalize_datetime_value(refined.get("publish_date"))
        refined["field_completeness"] = self.field_completeness(refined)
        if stage in FINAL_OPPORTUNITY_STAGES and any(
            not self.is_missing_field(refined.get(field))
            for field in ["project_id", "budget_or_scale", "deadline", "customer_or_org"]
        ):
            refined["is_opportunity"] = True
            score = max(score, self.config.opportunity.min_score)
        if refined["field_completeness"]["missing"]:
            refined["needs_detail_followup"] = True
            refined["missing_fields"] = refined["field_completeness"]["missing"]
        else:
            refined["needs_detail_followup"] = False
            refined["missing_fields"] = []

        refined["business_stage"] = stage
        refined["opportunity_score"] = round(clamp(score, 0.0, 1.0), 4)
        refined["is_final_opportunity"] = stage in FINAL_OPPORTUNITY_STAGES
        if is_navigation:
            refined["page_type"] = "navigation_or_listing"
        elif stage in FINAL_OPPORTUNITY_STAGES:
            refined["page_type"] = "project_detail"
        else:
            refined["page_type"] = "information"
        return refined

    def is_missing_field(self, value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, (list, dict)):
            return not bool(value)
        text = str(value).strip()
        if not text:
            return True
        if not re.search(r"[\w\u4e00-\u9fff]", text):
            return True
        lowered = text.lower()
        return any(marker in lowered for marker in MISSING_FIELD_MARKERS)

    def field_completeness(self, analysis: dict[str, Any]) -> dict[str, Any]:
        required = ["customer_or_org", "budget_or_scale", "deadline", "contact", "address"]
        present = [field for field in required if not self.is_missing_field(analysis.get(field))]
        missing = [field for field in required if field not in present]
        return {
            "present": present,
            "missing": missing,
            "ratio": round(len(present) / len(required), 4),
        }

    def extract_structured_fields(self, title: str, text: str) -> dict[str, str]:
        content = re.sub(r"\s+", " ", f"{title} {text}").strip()
        fields = {
            "project_name": self.extract_first_match(
                content,
                [
                    r"(?:项目名称|招标项目名称|采购项目名称)[：:\s]*(.{2,120}?)(?=\s*(?:项目编号|采购方式|招标方式|采购人|招标人|预算金额|最高限价|采购需求|一、|二、|$))",
                    r"本招标项目\s+(.{2,120}?)(?=\s*(?:（招标项目编号|已由|招标公告|采购公告|，|。))",
                    r"^(.{2,120}?)(?=\s*(?:招标公告|采购公告|中标候选人公示|成交候选人公示))",
                ],
            ),
            "project_id": self.extract_first_match(
                content,
                [
                    r"(?:采购项目编号|招标项目编号|项目编号|招标编号|采购编号|交易编号)[：:\s]*([A-Za-z0-9\-_/（）()]+)",
                ],
            ),
            "customer_or_org": self.extract_first_match(
                content,
                [
                    r"(?:采购人信息|招标人信息|建设单位信息)\s*名称[：:\s]*(.{2,80}?)(?=\s*(?:地址|联系方式|联系人|$))",
                    r"(?:采购人|招标人|建设单位|项目单位|采购单位|业主单位)[：:]\s*([^。；;，,\n]{2,80})",
                ],
            ),
            "agency": self.extract_first_match(
                content,
                [
                    r"(?:采购代理机构信息|招标代理机构信息|代理机构信息)\s*名称[：:\s]*(.{2,100}?)(?=\s*(?:地址|联系方式|联系人|$))",
                    r"(?:采购代理机构|招标代理机构|代理机构|招标代理|采购代理)[：:]\s*([^。；;，,\n]{2,80})",
                ],
            ),
            "supplier_or_winner": self.extract_first_match(
                content,
                [
                    r"(?:中标供应商|成交供应商|中标人|成交人|供应商名称)[：:]\s*([^。；;，,\n]{2,100})",
                ],
            ),
            "budget_or_scale": self.extract_first_match(
                content,
                [
                    r"(?:预算金额|采购预算|项目预算|预算|最高限价|最高投标限价|控制价|招标控制价|合同估算价|成交金额|中标金额|中标价|成交价)[：:\s]*([0-9][0-9,，.]*\s*(?:万元|元|亿元|万|亿)(?:人民币)?|[^。；;]{1,80})",
                    r"([0-9][0-9,，.]*\s*(?:万元|元|亿元|万|亿)(?:人民币)?)",
                ],
            ),
            "deadline": self.extract_first_match(
                content,
                [
                    r"(?:提交投标文件截止时间、开标时间和地点|提交响应文件截止时间、开启时间和地点|响应文件提交).*?时间[：:\s]*([0-9]{4}[-年/][0-9]{1,2}[-月/][0-9]{1,2}[日]?(?:\s*[0-9]{1,2}[:：时][0-9]{0,2}分?(?:[0-9]{0,2}秒)?)?)",
                    r"并于\s*([0-9]{4}[-年/][0-9]{1,2}[-月/][0-9]{1,2}[日]?(?:\s*[0-9]{1,2}[:：时][0-9]{0,2}分?(?:[0-9]{0,2}秒)?)?)\s*(?:前|之前).*?(?:递交|提交)",
                    r"(?:投标截止时间|响应文件提交截止时间|提交投标文件截止时间|递交截止时间|开标时间|响应文件开启时间|报名截止时间|截止时间)[：:\s]*([0-9]{4}[-年/][0-9]{1,2}[-月/][0-9]{1,2}[日]?(?:\s*[0-9]{1,2}[:：时][0-9]{0,2}分?)?)",
                ],
            ),
            "publish_date": self.extract_first_match(
                content,
                [
                    r"(?:公告日期|发布日期|发布时间|公示时间)[：:\s]*([0-9]{4}[-年/][0-9]{1,2}[-月/][0-9]{1,2}[日]?)",
                ],
            ),
            "contact": self.extract_first_match(
                content,
                [
                    r"(?:项目联系方式|项目联系人).*?电话[：:\s]*((?:\+?86[-\s]?)?0\d{2,3}[-\s]?\d{7,8}(?:[-转]\d{1,6})?|1[3-9]\d{9})",
                    r"(?:采购代理机构信息|招标代理机构信息|代理机构信息).*?联系方式[：:\s]*((?:\+?86[-\s]?)?0\d{2,3}[-\s]?\d{7,8}(?:[-转]\d{1,6})?|1[3-9]\d{9})",
                    r"(?:采购人信息|招标人信息|建设单位信息).*?联系方式[：:\s]*((?:\+?86[-\s]?)?0\d{2,3}[-\s]?\d{7,8}(?:[-转]\d{1,6})?|1[3-9]\d{9})",
                    r"(?:联系电话|联系方式|电话|联系人及电话|采购人联系方式|代理机构联系方式)[：:\s]*((?:\+?86[-\s]?)?0\d{2,3}[-\s]?\d{7,8}(?:[-转]\d{1,6})?|1[3-9]\d{9})",
                    r"(?<![A-Za-z0-9])((?:\+?86[-\s]?)?0\d{2,3}[-\s]?\d{7,8}(?:[-转]\d{1,6})?)(?![A-Za-z0-9])",
                    r"(?<![A-Za-z0-9])(1[3-9]\d{9})(?![A-Za-z0-9])",
                ],
            ),
            "address": self.extract_first_match(
                content,
                [
                    r"(?:采购人信息|招标人信息|建设单位信息).*?地址[：:\s]*(.{4,120}?)(?=\s*(?:联系方式|联系人|电话|$))",
                    r"(?:采购代理机构信息|招标代理机构信息|代理机构信息).*?地址[：:\s]*(.{4,120}?)(?=\s*(?:联系方式|联系人|电话|$))",
                    r"(?:采购人地址|招标人地址|联系地址|通讯地址|开标地点|投标地点|提交投标文件地点)[：:\s]*([^。；;]{4,120})",
                    r"地址[：:]\s*([^。；;]{4,120})",
                ],
            ),
        }

        normalized: dict[str, str] = {}
        for key, value in fields.items():
            if not value:
                continue
            cleaned = self.clean_extracted_field(key, value)
            if cleaned:
                normalized[key] = cleaned
        return normalized

    def clean_extracted_field(self, key: str, value: str) -> str:
        cleaned = re.sub(r"\s+", " ", value).strip(" ：:，,。；;|")
        cleaned = cleaned.replace("，", ",")
        if not cleaned or self.is_missing_field(cleaned):
            return ""
        if key == "project_name":
            cleaned = re.split(
                r"\s*(?:项目编号|采购方式|招标方式|采购人|招标人|预算金额|最高限价|采购需求)\s*[：:]?",
                cleaned,
                maxsplit=1,
            )[0].strip(" ：:，,。；;|")
        if key == "address" and "北京西城区三里河路58号" in cleaned:
            return ""
        if key == "contact":
            stop_words = ["采购人", "招标人", "代理机构", "项目名称", "预算", "开标", "截止", "地址"]
            positions = [cleaned.find(word) for word in stop_words if cleaned.find(word) > 0]
            if positions:
                cleaned = cleaned[: min(positions)].strip(" ：:，,。；;|")
        return cleaned[:200]

    def summary_value(self, value: Any) -> str:
        if self.is_missing_field(value):
            return ""
        return str(value)

    def normalize_money(self, value: Any) -> dict[str, Any]:
        raw = self.summary_value(value)
        if not raw:
            return {"raw": "", "amount_yuan": None, "unit": "元", "display": ""}

        normalized = raw.replace(",", "").replace("，", "").replace("人民币", "")
        normalized = re.sub(r"(?<=\d)\s+(?=[\d.])", "", normalized)
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(亿元|万元|元|亿|万)?", normalized)
        if not match:
            return {"raw": raw, "amount_yuan": None, "unit": "元", "display": raw}

        try:
            amount = Decimal(match.group(1))
        except InvalidOperation:
            return {"raw": raw, "amount_yuan": None, "unit": "元", "display": raw}

        unit = match.group(2) or "元"
        if unit in {"亿元", "亿"}:
            amount *= Decimal("100000000")
        elif unit in {"万元", "万"}:
            amount *= Decimal("10000")

        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        amount_value: int | float
        if amount == amount.to_integral_value():
            amount_value = int(amount)
            display = f"{amount_value}元"
        else:
            amount_value = float(amount)
            display = f"{amount:.2f}元"
        return {
            "raw": raw,
            "amount_yuan": amount_value,
            "unit": "元",
            "display": display,
        }

    def normalize_datetime_value(self, value: Any) -> dict[str, str]:
        raw = self.summary_value(value)
        if not raw:
            return {"raw": "", "normalized": "", "precision": ""}

        text = raw.strip()
        text = text.replace("T", " ")
        text = re.sub(r"[年月/]", "-", text)
        text = text.replace("日", " ")
        text = text.replace("时", ":").replace("分", ":").replace("秒", "")
        text = re.sub(r"\s+", " ", text).strip()

        match = re.search(
            r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})(?:\s+([0-9]{1,2})(?::([0-9]{1,2}))?(?::([0-9]{1,2}))?)?",
            text,
        )
        if not match:
            return {"raw": raw, "normalized": raw, "precision": "raw"}

        year, month, day, hour, minute, second = match.groups()
        if hour is None:
            return {
                "raw": raw,
                "normalized": f"{int(year):04d}-{int(month):02d}-{int(day):02d}",
                "precision": "date",
            }

        normalized = (
            f"{int(year):04d}-{int(month):02d}-{int(day):02d} "
            f"{int(hour):02d}:{int(minute or 0):02d}:{int(second or 0):02d}"
        )
        return {"raw": raw, "normalized": normalized, "precision": "datetime"}

    def parse_normalized_date(self, value: Any) -> datetime | None:
        normalized = self.normalize_datetime_value(value).get("normalized", "")
        match = re.search(r"([0-9]{4})-([0-9]{2})-([0-9]{2})", normalized)
        if not match:
            return None
        year, month, day = (int(part) for part in match.groups())
        return datetime(year, month, day, tzinfo=timezone.utc)

    def is_within_recent_days(self, value: Any) -> bool:
        publish_date = self.parse_normalized_date(value)
        if publish_date is None:
            return False
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        start = today - timedelta(days=self.config.recent_days - 1)
        return start <= publish_date <= today

    def split_contact_info(self, value: Any) -> tuple[str, str]:
        raw = self.summary_value(value)
        if not raw:
            return "", ""

        phone_pattern = r"(?:\+?86[-\s]?)?(?:0\d{2,3}[-\s]?\d{7,8}(?:[-转]\d{1,6})?|1[3-9]\d{9})"
        phones: list[str] = []
        for match in re.finditer(phone_pattern, raw):
            phone = re.sub(r"\s+", "", match.group(0)).strip("；;，,。")
            if phone and phone not in phones:
                phones.append(phone)

        without_phones = re.sub(phone_pattern, " ", raw)
        without_phones = re.sub(r"[\(\)（）]", " ", without_phones)
        without_phones = re.sub(
            r"(联系方式|联系电话|电\s*话|手机|传真|邮箱|电子邮箱|地址|采购人|招标人|代理机构|项目联系人|联系人|项目负责人|电话)",
            " ",
            without_phones,
        )
        candidates = re.split(r"[、,，;；/|\s]+", without_phones)
        names: list[str] = []
        for candidate in candidates:
            cleaned = candidate.strip(" ：:，,。；;（）()")
            if not cleaned:
                continue
            if re.search(r"[0-9@]", cleaned):
                continue
            if len(cleaned) > 8:
                continue
            if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", cleaned) and cleaned not in names:
                names.append(cleaned)

        return "、".join(names), "；".join(phones)

    def best_project_name(self, row: dict[str, str]) -> str:
        for key in ["project_name", "title", "need"]:
            value = self.summary_value(row.get(key, ""))
            if not value:
                continue
            if value in {"全国公共资源交易平台", "国家公共资源交易平台"}:
                continue
            value = re.sub(r"_交易公开_国家公共资源交易平台$", "", value).strip()
            value = re.sub(r"[-_]?全国公共资源交易平台$", "", value).strip()
            value = re.split(r"[，,。；;]", value, maxsplit=1)[0].strip()
            if value:
                return value
        return self.summary_value(row.get("title", "")) or self.summary_value(row.get("need", ""))

    def build_structured_opportunity_record(self, row: dict[str, str]) -> dict[str, Any]:
        budget = self.normalize_money(row.get("budget_or_scale"))
        deadline = self.normalize_datetime_value(row.get("deadline"))
        publish_date = self.normalize_datetime_value(row.get("publish_date"))
        contact_name, contact_phone = self.split_contact_info(row.get("contact", ""))
        record: dict[str, Any] = {
            "项目名称": self.best_project_name(row),
            "采购单位": row["customer_or_org"],
            "代理机构": row["agency"],
            "金额元": budget["amount_yuan"],
            "截止时间": deadline["normalized"],
            "发布日期": publish_date["normalized"],
            "联系人": contact_name,
            "联系方式": contact_phone,
            "地址": row["address"],
            "项目编号": row["project_id"],
            "来源链接": row["url"],
        }
        supplier_or_winner = self.summary_value(row.get("supplier_or_winner", ""))
        if supplier_or_winner:
            items = list(record.items())
            insert_at = 4
            items.insert(insert_at, ("中标成交方", supplier_or_winner))
            record = dict(items)
        return {key: value for key, value in record.items() if not self.is_missing_field(value)}

    def dedupe_opportunity_rows(self, rows: list[dict[str, str]]) -> list[dict[str, str]]:
        groups: list[dict[str, Any]] = []
        for row in rows:
            keys = self.opportunity_dedupe_keys(row)
            matched = [group for group in groups if group["keys"] & keys]
            if not matched:
                groups.append({"keys": set(keys), "row": row})
                continue

            primary = matched[0]
            primary["keys"].update(keys)
            if self.opportunity_row_rank(row) > self.opportunity_row_rank(primary["row"]):
                primary["row"] = row

            for extra in matched[1:]:
                primary["keys"].update(extra["keys"])
                if self.opportunity_row_rank(extra["row"]) > self.opportunity_row_rank(primary["row"]):
                    primary["row"] = extra["row"]
                groups.remove(extra)

        return [group["row"] for group in groups]

    def opportunity_dedupe_keys(self, row: dict[str, str]) -> set[str]:
        keys: set[str] = set()
        project_id = self.summary_value(row.get("project_id", ""))
        if project_id:
            keys.add(f"id:{project_id}")

        project_name = self.best_project_name(row)
        normalized_name = re.sub(
            r"(招标公告|采购公告|中标候选人公示|成交候选人公示|中标公告|成交公告|结果公告|合同公告)$",
            "",
            project_name,
        )
        normalized_name = re.sub(r"_交易公开_国家公共资源交易平台$", "", normalized_name)
        normalized_name = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized_name)
        if len(normalized_name) >= 12:
            keys.add(f"name:{normalized_name[:32]}")

        if not keys:
            keys.add(f"url:{row.get('url', '')}")
        return keys

    def opportunity_row_rank(self, row: dict[str, str]) -> tuple[int, int, float, int, float]:
        completeness = 0.0
        try:
            completeness = float(row.get("field_completeness") or 0)
        except ValueError:
            pass
        has_budget = 1 if self.normalize_money(row.get("budget_or_scale")).get("amount_yuan") is not None else 0
        is_detail = 1 if "/information/deal/html/b/" in row.get("url", "") else 0
        has_contact = 0 if self.is_missing_field(row.get("contact")) else 1
        score = 0.0
        try:
            score = float(row.get("score") or 0)
        except ValueError:
            pass
        return (has_budget, is_detail, completeness, has_contact, score)

    def structured_record_to_text(self, index: int, record: dict[str, Any]) -> str:
        lines = [
            f"商机 #{index}",
        ]
        for key, value in record.items():
            lines.append(f"{key}: {'' if value is None else value}")
        return "\n".join(lines)

    def normalize_business_stage(self, url: str, title: str, text: str, raw_stage: str) -> str:
        stage = raw_stage.strip().lower()
        content = f"{url}\n{title}\n{text[:2500]}"
        title_content = title or ""
        url_lower = url.lower()
        is_deal_detail = "/information/deal/" in url_lower
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.lower()
        query = parsed.query.lower()

        if path in {"", "/"}:
            return "market_research"
        if any(marker in path for marker in ["deallist", "dealcollect", "search", "list"]):
            return "market_research"
        if "header_deal_type=" in query:
            return "market_research"

        if any(part in url_lower for part in ["/sic/web/details", "creditdetail"]):
            if any(word in content for word in ["交易诚信", "信用信息", "奖励信息", "违法违规", "不良行为"]):
                return "credit_info"
            return "policy_info"
        if any(word in content for word in ["交易诚信", "信用信息", "奖励信息", "违法违规", "不良行为"]):
            return "credit_info"
        if not is_deal_detail and any(word in content for word in ["政策法规", "办事指南", "操作手册", "管理暂行办法", "管理办法", "办法》", "解读", "履行指引", "实施意见"]):
            return "policy_info"

        leading_content = f"{title_content}\n{text[:800]}"
        if "/information/deal/html/b/" in url_lower:
            if any(word in leading_content for word in ["招标公告", "公开招标", "资格预审", "招标文件"]):
                return "tender_notice"
            if any(word in leading_content for word in ["采购公告", "竞争性磋商", "竞争性谈判", "询价公告", "单一来源采购", "采购需求"]):
                return "procurement_notice"

        if any(word in title_content for word in ["中标候选人", "成交候选人", "候选人公示", "候选人公告"]):
            return "award_candidate"
        if any(word in title_content for word in ["招标公告", "公开招标", "资格预审", "招标文件"]):
            return "tender_notice"
        if any(word in title_content for word in ["采购公告", "竞争性磋商", "竞争性谈判", "询价公告", "单一来源采购", "采购需求"]):
            return "procurement_notice"
        if any(word in title_content for word in ["中标结果", "中标公告", "成交结果", "成交公告", "结果公告", "中标（成交）结果公告"]):
            return "award_result"
        if any(word in title_content for word in ["合同公告", "合同公示", "合同签订"]):
            return "contract_notice"

        if any(word in content for word in ["中标候选人", "成交候选人", "候选人公示", "候选人公告"]):
            return "award_candidate"
        if any(word in content for word in ["合同公告", "合同公示", "合同签订"]):
            return "contract_notice"
        if any(word in content for word in ["招标公告", "公开招标", "资格预审", "招标文件"]):
            return "tender_notice"
        if any(word in content for word in ["采购公告", "竞争性磋商", "竞争性谈判", "询价公告", "单一来源采购", "采购需求"]):
            return "procurement_notice"
        if any(word in content for word in ["中标结果", "中标公告", "成交结果", "成交公告", "结果公告"]):
            return "award_result"

        aliases = {
            "tender": "tender_notice",
            "procurement": "procurement_notice",
            "contract": "contract_notice",
            "lead": "market_research",
            "partnership": "market_research",
        }
        if stage in aliases:
            return aliases[stage]
        if stage in ALL_BUSINESS_STAGES:
            return stage
        return "market_research"

    def is_navigation_or_listing_page(self, url: str, title: str, text: str, links: list[str]) -> bool:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.lower()
        query = parsed.query.lower()
        if path in {"", "/"}:
            return True
        if any(marker in path for marker in ["deallist", "dealcollect", "search", "list"]):
            return True
        if "header_deal_type=" in query:
            return True

        content = f"{title}\n{text[:1500]}"
        navigation_words = ["首页", "交易公开", "服务导航", "搜索", "筛选", "更多", "查询"]
        concrete_words = [
            "项目编号",
            "采购人",
            "招标人",
            "预算金额",
            "最高限价",
            "投标截止",
            "开标时间",
            "中标候选人",
            "成交供应商",
        ]
        navigation_hits = sum(1 for word in navigation_words if word in content)
        concrete_hits = sum(1 for word in concrete_words if word in content)
        return len(links) >= 30 and navigation_hits >= 2 and concrete_hits == 0

    def analyze_page_with_ai(
        self,
        url: str,
        title: str,
        description: str,
        text: str,
        links: list[str],
    ) -> dict[str, Any] | None:
        if self.ai_disabled_for_run or not self.ai_client.available:
            return None

        link_sample = links[:80]
        user_payload = {
            "task": self.config.opportunity.goal,
            "recent_days": self.config.recent_days,
            "target_customers": self.config.opportunity.target_customers,
            "positive_signals": self.config.opportunity.signals,
            "negative_signals": self.config.opportunity.negative_signals,
            "url": url,
            "title": title,
            "description": description,
            "text": text[: self.config.ai.max_input_chars],
            "links": link_sample,
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "你是商机挖掘爬虫的页面分析代理。"
                    "你必须只返回 JSON，不要返回 Markdown。"
                    "判断页面是否包含可跟进的招标采购商机，并给出后续爬取建议。"
                    "首页、搜索页、列表页和频道页只能作为 market_research，不能作为最终商机。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请分析下面网页是否符合商机挖掘需求。返回 JSON 字段："
                    "is_opportunity(boolean), opportunity_score(0到1), business_stage("
                    "只能是 tender_notice/procurement_notice/award_candidate/award_result/"
                    "contract_notice/market_research/not_opportunity 之一), "
                    "summary, project_id, customer_or_org, agency, supplier_or_winner, need, "
                    "budget_or_scale, deadline, publish_date, contact, address, "
                    "evidence(array), recommended_action, link_hints(array), "
                    "crawl_decision(follow/deprioritize/stop), reason。"
                    "阶段定义：招标公告=tender_notice；政府采购、竞争性磋商、询价、谈判公告=procurement_notice；"
                    "中标候选人/成交候选人公示=award_candidate；中标结果/成交公告=award_result；"
                    "合同公告=contract_notice；首页/列表/搜索/导航/市场概览=market_research；"
                    "政策法规、办事指南、信用信息、新闻、招聘=not_opportunity。"
                    "只有具体项目详情页才把 is_opportunity 设为 true；列表页即使包含很多商机链接，也应设为 false。"
                    f"只把最近 {self.config.recent_days} 天内发布的招标采购信息作为最终商机；"
                    "发布日期明显超出范围的页面应设为 market_research 或 not_opportunity。"
                    "必须优先抽取预算金额/最高限价/成交金额、投标截止或开标时间、联系电话/联系人、地址/开标地点。"
                    "如果网页正文没有明确字段，字段值返回 null，不要编造，不要写'通常有预算'这类推测。"
                    "\n\n网页数据："
                    + json.dumps(user_payload, ensure_ascii=False)
                ),
            },
        ]
        try:
            result = self.ai_client.chat_json(messages)
            self.ai_failure_count = 0
            return result
        except Exception as exc:
            self.ai_failure_count += 1
            print(f"[warn] AI page analysis failed: {url} ({exc})", file=sys.stderr)
            if self.ai_failure_count >= self.config.ai.max_failures:
                self.ai_disabled_for_run = True
                print(
                    f"[warn] AI disabled for this run after {self.ai_failure_count} consecutive failures. "
                    "Use --check-ai before the next run.",
                    file=sys.stderr,
                )
            return None

    def rule_opportunity_analysis(
        self,
        url: str,
        title: str,
        description: str,
        text: str,
        links: list[str],
    ) -> dict[str, Any]:
        haystack = f"{url}\n{title}\n{description}\n{text}".lower()
        strong_signals = self.config.opportunity.signals
        soft_signals = self.config.keywords
        negative_signals = self.config.opportunity.negative_signals

        strong_hits = [signal for signal in strong_signals if signal.lower() in haystack]
        soft_hits = [keyword for keyword in soft_signals if keyword.lower() in haystack]
        negative_hits = [signal for signal in negative_signals if signal.lower() in haystack]

        title_hits = [
            signal
            for signal in strong_signals + soft_signals
            if signal.lower() in title.lower()
        ]
        url_lower = url.lower()
        is_deal_detail = "/information/deal/" in url_lower
        is_deal_list = "deallist" in url_lower
        is_policy_or_credit_url = any(part in url_lower for part in ["/sic/web/details", "creditdetail"])
        url_boost = 0.16 if is_deal_detail else 0.06 if is_deal_list else 0.0
        title_boost = min(len(title_hits) * 0.08, 0.24)
        strong_score = min(len(strong_hits) * 0.08, 0.48)
        soft_score = min(len(soft_hits) * 0.035, 0.18)
        negative_penalty = min(len(negative_hits) * 0.08, 0.32)
        score = clamp(0.18 + url_boost + title_boost + strong_score + soft_score - negative_penalty, 0.0, 1.0)

        evidence = self.extract_evidence(text, strong_hits + soft_hits)
        structured_fields = self.extract_structured_fields(title, text)
        budget_or_scale = structured_fields.get("budget_or_scale", "")
        deadline = structured_fields.get("deadline", "")
        customer_or_org = structured_fields.get("customer_or_org", "")
        agency = structured_fields.get("agency", "")
        contact = structured_fields.get("contact", "")
        project_id = structured_fields.get("project_id", "")

        stage = self.rule_business_stage(title, text)
        if stage in {"policy_info", "credit_info"} or is_policy_or_credit_url:
            score = min(score, 0.45)
        elif stage == "market_research" and not is_deal_detail:
            score = min(score, 0.58)
        elif is_deal_list:
            score = min(score, 0.58)
        link_hints = sorted(set(strong_hits[:8] + soft_hits[:5] + ["招标公告", "采购公告", "中标结果", "成交公告"]))
        is_opportunity = score >= self.config.opportunity.min_score

        return {
            "analysis_method": "rules",
            "is_opportunity": is_opportunity,
            "opportunity_score": round(score, 4),
            "business_stage": stage,
            "summary": self.rule_summary(title, stage, strong_hits, score),
            "customer_or_org": customer_or_org or "",
            "agency": agency or "",
            "supplier_or_winner": structured_fields.get("supplier_or_winner", ""),
            "need": self.rule_need(title, strong_hits, project_id),
            "budget_or_scale": budget_or_scale or "",
            "deadline": deadline or "",
            "publish_date": structured_fields.get("publish_date", ""),
            "contact": contact or "",
            "address": structured_fields.get("address", ""),
            "project_id": project_id or "",
            "structured_fields": structured_fields,
            "evidence": evidence,
            "recommended_action": self.rule_recommended_action(score, stage, contact),
            "link_hints": link_hints,
            "crawl_decision": "follow" if score >= 0.45 else "deprioritize",
            "reason": f"规则命中强信号 {len(strong_hits)} 个、普通关键词 {len(soft_hits)} 个、负面信号 {len(negative_hits)} 个。",
        }

    def extract_evidence(self, text: str, signals: list[str]) -> list[str]:
        evidence: list[str] = []
        compact = re.sub(r"\s+", " ", text)
        for signal in signals:
            index = compact.lower().find(signal.lower())
            if index == -1:
                continue
            start = max(0, index - 50)
            end = min(len(compact), index + len(signal) + 90)
            snippet = compact[start:end].strip()
            if snippet and snippet not in evidence:
                evidence.append(snippet)
            if len(evidence) >= 5:
                break
        return evidence

    def extract_first_match(self, text: str, patterns: list[str]) -> str | None:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return re.sub(r"\s+", " ", match.group(1)).strip()
        return None

    def rule_business_stage(self, title: str, text: str) -> str:
        content = f"{title}\n{text[:2000]}"
        if any(word in content for word in ["交易诚信", "信用信息", "奖励信息", "违法违规", "不良行为"]):
            return "credit_info"
        if any(word in title for word in ["政策法规", "办事指南", "操作手册", "管理暂行办法", "管理办法", "办法》", "解读", "履行指引", "实施意见"]):
            return "policy_info"
        if any(word in title for word in ["中标候选人", "成交候选人", "候选人公示", "候选人公告"]):
            return "award_candidate"
        if any(word in title for word in ["招标公告", "公开招标", "资格预审"]):
            return "tender_notice"
        if any(word in title for word in ["采购公告", "竞争性磋商", "竞争性谈判", "询价公告"]):
            return "procurement_notice"
        if any(word in title for word in ["中标", "成交", "结果公告"]):
            return "award_result"
        if any(word in title for word in ["合同公告", "合同签订"]):
            return "contract_notice"
        if any(word in content for word in ["中标候选人", "成交候选人", "候选人公示", "候选人公告"]):
            return "award_candidate"
        if any(word in content for word in ["合同公告", "合同签订"]):
            return "contract_notice"
        if any(word in content for word in ["招标公告", "公开招标", "资格预审"]):
            return "tender_notice"
        if any(word in content for word in ["采购公告", "竞争性磋商", "竞争性谈判", "询价公告"]):
            return "procurement_notice"
        if any(word in content for word in ["中标", "成交", "结果公告"]):
            return "award_result"
        return "market_research"

    def rule_summary(self, title: str, stage: str, strong_hits: list[str], score: float) -> str:
        signals = "、".join(strong_hits[:5]) if strong_hits else "招采相关关键词"
        return f"规则判断该页面与{signals}相关，阶段为 {stage}，商机分 {score:.2f}。标题：{title}"

    def rule_need(self, title: str, strong_hits: list[str], project_id: str | None) -> str:
        signals = "、".join(strong_hits[:6]) if strong_hits else "招标采购"
        suffix = f"，项目编号 {project_id}" if project_id else ""
        return f"疑似存在{signals}相关跟进需求{suffix}。标题：{title}"

    def rule_recommended_action(self, score: float, stage: str, contact: str | None) -> str:
        if score >= 0.75 and contact:
            return "高优先级：已发现联系方式，建议人工核验公告详情并跟进采购人、招标人或代理机构。"
        if score >= 0.75:
            return "高优先级：建议继续抓取详情页、附件页和联系方式字段。"
        if stage in FINAL_OPPORTUNITY_STAGES:
            return "中优先级：建议核验项目预算、截止时间、采购人和代理机构。"
        return "低优先级：先作为市场情报保存，后续由更多页面交叉验证。"

    def save_page(self, page: dict[str, Any]) -> None:
        digest = hashlib.sha1(page["url"].encode("utf-8")).hexdigest()[:16]
        path = os.path.join(self.config.output_dir, "pages", f"{digest}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(page, handle, ensure_ascii=False, indent=2)
        print(
            "[save] "
            f"reward={page['reward']:.3f} strategy={page['extraction_strategy']} "
            f"title={page['title'][:80]!r}"
        )

    def save_opportunity(self, page: dict[str, Any]) -> None:
        ai_analysis = page.get("ai_analysis")
        if not isinstance(ai_analysis, dict):
            return
        score = self.ai_score(ai_analysis)
        if score is None or score < self.config.opportunity.min_score:
            return
        stage = str(ai_analysis.get("business_stage", ""))
        if stage not in FINAL_OPPORTUNITY_STAGES:
            return
        if not bool(ai_analysis.get("is_opportunity", False)):
            return
        amount = self.normalize_money(ai_analysis.get("budget_or_scale")).get("amount_yuan")
        try:
            if amount is None or float(amount) <= 0:
                return
        except (TypeError, ValueError):
            return
        if not self.is_within_recent_days(ai_analysis.get("publish_date")):
            return

        digest = hashlib.sha1(page["url"].encode("utf-8")).hexdigest()[:16]
        path = os.path.join(self.config.output_dir, "opportunities", f"{digest}.json")
        payload = {
            "url": page["url"],
            "title": page["title"],
            "fetched_at": page["fetched_at"],
            "page_score": page["page_score"],
            "opportunity_score": score,
            "ai_analysis": ai_analysis,
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        print(f"[opportunity] stage={stage} score={score:.3f} title={page['title'][:80]!r}")

    def opportunity_rows_from_pages(self) -> list[dict[str, str]]:
        pages_dir = os.path.join(self.config.output_dir, "pages")
        rows: list[dict[str, str]] = []
        if not os.path.isdir(pages_dir):
            return rows

        for name in sorted(os.listdir(pages_dir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(pages_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    page = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue

            analysis = page.get("ai_analysis", {})
            if not isinstance(analysis, dict):
                continue

            url = str(page.get("url", ""))
            title = str(page.get("title", ""))
            text = str(page.get("text", ""))
            links = page.get("links", [])
            if not isinstance(links, list):
                links = []

            refined = self.refine_opportunity_analysis(
                url=url,
                title=title,
                text=text,
                links=[str(link) for link in links],
                analysis=analysis,
            )
            stage = str(refined.get("business_stage", ""))
            if stage not in FINAL_OPPORTUNITY_STAGES:
                continue
            if not bool(refined.get("is_opportunity", False)):
                continue
            score = self.ai_score(refined)
            if score is None or score < self.config.opportunity.min_score:
                continue

            structured_fields = refined.get("structured_fields", {})
            if not isinstance(structured_fields, dict):
                structured_fields = {}
            row = {
                "stage": stage,
                "score": self.summary_value(score),
                "title": self.summary_value(title),
                "project_name": self.summary_value(
                    refined.get("project_name", structured_fields.get("project_name", ""))
                ),
                "project_id": self.summary_value(refined.get("project_id", "")),
                "customer_or_org": self.summary_value(refined.get("customer_or_org", "")),
                "agency": self.summary_value(refined.get("agency", "")),
                "supplier_or_winner": self.summary_value(refined.get("supplier_or_winner", "")),
                "need": self.summary_value(refined.get("need", "")),
                "budget_or_scale": self.summary_value(refined.get("budget_or_scale", "")),
                "deadline": self.summary_value(refined.get("deadline", "")),
                "publish_date": self.summary_value(refined.get("publish_date", "")),
                "contact": self.summary_value(refined.get("contact", "")),
                "address": self.summary_value(refined.get("address", "")),
                "field_completeness": str(
                    refined.get("field_completeness", {}).get("ratio", "")
                    if isinstance(refined.get("field_completeness"), dict)
                    else ""
                ),
                "url": self.summary_value(url),
            }
            budget = self.normalize_money(row["budget_or_scale"])
            deadline = self.normalize_datetime_value(row["deadline"])
            publish_date = self.normalize_datetime_value(row["publish_date"])
            row.update(
                {
                    "budget_amount_yuan": str(budget["amount_yuan"] if budget["amount_yuan"] is not None else ""),
                    "deadline_normalized": deadline["normalized"],
                    "publish_date_normalized": publish_date["normalized"],
                }
            )
            rows.append(row)

        return rows

    def write_opportunity_summaries(self) -> None:
        rows = self.opportunity_rows_from_pages()
        filtered_rows: list[dict[str, str]] = []
        for row in rows:
            amount = self.normalize_money(row.get("budget_or_scale")).get("amount_yuan")
            if amount is None:
                continue
            try:
                if float(amount) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            if not self.is_within_recent_days(row.get("publish_date")):
                continue
            filtered_rows.append(row)
        rows = filtered_rows
        rows.sort(key=self.opportunity_row_rank, reverse=True)
        rows = self.dedupe_opportunity_rows(rows)
        rows.sort(key=self.opportunity_row_rank, reverse=True)
        structured_records = [self.build_structured_opportunity_record(row) for row in rows]
        structured_records.sort(
            key=lambda record: (
                1 if record["金额元"] is not None else 0,
                float(record["金额元"] or 0),
            ),
            reverse=True,
        )
        jsonl_path = os.path.join(self.config.output_dir, "opportunities_summary.jsonl")
        with open(jsonl_path, "w", encoding="utf-8") as handle:
            for record in structured_records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

        csv_path = os.path.join(self.config.output_dir, "opportunities_summary.csv")
        fieldnames = [
            "project_name",
            "customer_or_org",
            "agency",
            "supplier_or_winner",
            "budget_amount_yuan",
            "deadline_normalized",
            "publish_date_normalized",
            "contact_name",
            "contact_phone",
            "address",
            "project_id",
            "url",
        ]
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                contact_name, contact_phone = self.split_contact_info(row.get("contact", ""))
                csv_row = {key: row.get(key, "") for key in fieldnames}
                csv_row["contact_name"] = contact_name
                csv_row["contact_phone"] = contact_phone
                writer.writerow(csv_row)

        structured_json_path = os.path.join(self.config.output_dir, "opportunities_structured.json")
        with open(structured_json_path, "w", encoding="utf-8") as handle:
            json.dump(structured_records, handle, ensure_ascii=False, indent=2)

        structured_text_path = os.path.join(self.config.output_dir, "opportunities_structured.txt")
        with open(structured_text_path, "w", encoding="utf-8") as handle:
            for index, record in enumerate(structured_records, start=1):
                if index > 1:
                    handle.write("\n\n---\n\n")
                handle.write(self.structured_record_to_text(index, record))
                handle.write("\n")

        print(
            "[summary] "
            f"opportunities={len(rows)} csv={csv_path} jsonl={jsonl_path} "
            f"structured={structured_json_path}"
        )


def load_config(path: str) -> CrawlerConfig:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return CrawlerConfig.from_dict(data)


def slug_from_url(url: str) -> str:
    host = scope_domain_of(url)
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", host).strip("_").lower()
    return slug or "ai_crawler"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an AI opportunity-mining crawler.")
    parser.add_argument("--config", default="config.example.json", help="Path to crawler JSON config.")
    parser.add_argument("--env-file", default=".env", help="Path to local env file with API credentials.")
    parser.add_argument(
        "--check-ai",
        action="store_true",
        help="Check AI API connectivity using .env and config, then exit.",
    )
    return parser.parse_args(argv)


def check_ai_connection(config: CrawlerConfig) -> bool:
    client = ChatAIClient(config.ai, proxy_url=config.proxy_url)
    print(f"[ai-check] endpoint={client.endpoint}")
    print(f"[ai-check] model={client.model}")
    print(f"[ai-check] key_env={config.ai.api_key_env} key={client.masked_key}")
    print(
        "[ai-check] "
        f"verify_ssl={client.verify_ssl} "
        f"trust_env={client.trust_env} "
        f"proxy={'set' if client.proxy_url else 'none'} "
        f"temperature={client.temperature} "
        f"max_tokens={client.max_tokens if client.max_tokens is not None else 'default'}"
    )

    if not client.available:
        print(f"[ai-check] failed: ${config.ai.api_key_env} is missing or still a placeholder")
        return False

    try:
        content = client.chat_text(
            [
                {"role": "system", "content": "你是连通性测试助手，只回答 OK。"},
                {"role": "user", "content": "请只回答 OK"},
            ]
        )
    except Exception as exc:
        print(f"[ai-check] failed: {exc}")
        return False

    print(f"[ai-check] ok: {content[:200]}")
    return True


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    loaded_env = load_env_file(args.env_file)
    if loaded_env:
        print(f"[env] loaded {len(loaded_env)} values from {args.env_file}")
    if args.check_ai:
        config = load_config(args.config)
        return 0 if check_ai_connection(config) else 1

    config = load_config(args.config)
    crawler = SelfEvolvingCrawler(config)
    crawler.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
