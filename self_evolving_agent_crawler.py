#!/usr/bin/env python3
"""AI tender opportunity miner.

Focused on one task:
search AI-related tender/procurement notices, analyze detail pages with an
OpenAI-compatible chat API, and write structured opportunities incrementally.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import http.cookiejar
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from typing import Any


AI_KEYWORDS = [
    "AI",
    "人工智能",
    "大模型",
    "大语言模型",
    "生成式AI",
    "AIGC",
    "智能体",
    "智能问答",
    "智能客服",
    "知识图谱",
    "机器学习",
    "深度学习",
    "算法模型",
    "模型训练",
    "自然语言处理",
    "NLP",
    "计算机视觉",
    "图像识别",
    "语音识别",
    "智能分析",
]

FINAL_STAGES = {
    "tender_notice",
    "procurement_notice",
    "award_candidate",
    "award_result",
    "contract_notice",
}

SKIP_EXTENSIONS = {
    ".css",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".svg",
    ".webp",
    ".xls",
    ".xlsx",
    ".zip",
}

DEFAULT_RESULT_LINK_KEYWORDS = [
    "招标",
    "采购",
    "中标",
    "成交",
    "公告",
    "公示",
    "合同",
    "磋商",
    "谈判",
    "询价",
    "预算",
    "项目",
    "notice",
    "bid",
    "tender",
    "procurement",
    "bulletin",
    "contract",
]

IGNORED_LINK_TITLES = {
    "首页",
    "上一页",
    "下一页",
    "尾页",
    "末页",
    "更多",
    "详情",
    "查看",
    "登录",
    "注册",
    "返回",
    "关闭",
}

SITE_ADAPTERS = {"auto", "ggzy_api", "html_search", "json_api", "html_index"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            return value
    return None


def env_bool_first(names: tuple[str, ...], default: bool) -> bool:
    raw = first_env(*names)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_optional_url(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip()
    if not value or value.lower() in {"0", "false", "no", "none", "off", "direct"}:
        return None
    return value


def env_optional_url_first(*names: str) -> str | None:
    for name in names:
        value = env_optional_url(name)
        if value:
            return value
    return None


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
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if re.search(r"[\s\u4e00-\u9fff，。；（）()]", parsed.netloc):
        return None
    path = parsed.path or "/"
    if os.path.splitext(path.lower())[1] in SKIP_EXTENSIONS:
        return None
    return urllib.parse.urlunparse(parsed._replace(fragment="", path=path))


def host_matches(host: str, scope: str) -> bool:
    host = host.lower()
    scope = scope.lower()
    return host == scope or host.endswith("." + scope)


def slug_from_url(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return re.sub(r"[^a-z0-9]+", "_", host).strip("_") or "site"


def looks_like_html(body: bytes) -> bool:
    prefix = body[:1000].lstrip().lower()
    return prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html") or b"<html" in prefix[:300]


def looks_blocked(text: str) -> bool:
    markers = [
        "访问过于频繁",
        "频繁访问",
        "请稍后再试",
        "请求被阻断",
        "您无法继续访问",
        "抱歉，您的请求被阻断了",
        "验证码",
        "安全验证",
        "人机验证",
        "403 forbidden",
        "access denied",
        "too many requests",
        "you have been blocked",
        "you are unable to access",
    ]
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)


def looks_blocked_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in ["403", "forbidden", "blocked", "too many requests", "访问过于频繁", "请求被阻断"])


def looks_site_unavailable_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = [
        "nodename nor servname provided",
        "name or service not known",
        "temporary failure in name resolution",
        "no address associated with hostname",
        "network is unreachable",
    ]
    return any(marker in text for marker in markers)


def date_range_for_recent_days(days: int) -> dict[str, str]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=max(days, 1) - 1)
    return {"start_date": start.isoformat(), "end_date": end.isoformat()}


def template_context(keyword: str, page: int, recent_days: int) -> dict[str, str]:
    dates = date_range_for_recent_days(recent_days)
    start_colon = dates["start_date"].replace("-", ":")
    end_colon = dates["end_date"].replace("-", ":")
    return {
        "keyword": urllib.parse.quote(keyword),
        "keyword_raw": keyword,
        "keyword_plus": urllib.parse.quote_plus(keyword),
        "page": str(page),
        "start_date_colon": start_colon,
        "end_date_colon": end_colon,
        **dates,
    }


def render_template(value: str, keyword: str, page: int, recent_days: int) -> str:
    return value.format(**template_context(keyword, page, recent_days))


def path_value(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            return None
    return current


def first_path_value(data: Any, paths: list[str]) -> Any:
    for path in paths:
        value = path_value(data, path)
        if not is_missing(value):
            return value
    return None


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
    if start < 0 or end <= start:
        raise ValueError("AI response did not contain a JSON object")
    parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("AI response JSON must be an object")
    return parsed


@dataclass
class SiteConfig:
    name: str = ""
    base_url: str = ""
    adapter: str = "auto"
    search_url_template: str = ""
    search_method: str = "GET"
    search_form: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    allowed_domains: list[str] = field(default_factory=list)
    include_url_patterns: list[str] = field(default_factory=list)
    exclude_url_patterns: list[str] = field(default_factory=list)
    result_link_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_RESULT_LINK_KEYWORDS))
    max_pages_per_keyword: int | None = None
    json_records_path: str = ""
    record_url_fields: list[str] = field(default_factory=lambda: ["url", "href", "detailUrl", "detail_url", "link"])
    record_title_fields: list[str] = field(default_factory=lambda: ["title", "name", "projectName", "project_name"])

    @classmethod
    def from_seed(cls, seed: str) -> "SiteConfig":
        base_url = canonical_url(seed)
        if not base_url:
            raise ValueError(f"invalid site seed URL: {seed}")
        site = cls(base_url=base_url, name=slug_from_url(base_url))
        site.apply_defaults()
        return site

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SiteConfig":
        data = dict(data)
        if "url" in data and "base_url" not in data:
            data["base_url"] = data.pop("url")
        if "type" in data and "adapter" not in data:
            data["adapter"] = data.pop("type")
        if "json_url_field" in data and "record_url_fields" not in data:
            data["record_url_fields"] = [data.pop("json_url_field")]
        if "json_title_field" in data and "record_title_fields" not in data:
            data["record_title_fields"] = [data.pop("json_title_field")]
        site = cls(**data)
        site.apply_defaults()
        return site

    def apply_defaults(self) -> None:
        normalized = canonical_url(self.base_url)
        if not normalized:
            raise ValueError(f"invalid site base_url: {self.base_url}")
        self.base_url = normalized
        parsed = urllib.parse.urlparse(self.base_url)
        if not self.name:
            self.name = slug_from_url(self.base_url)
        self.adapter = self.adapter.lower().strip() or "auto"
        if self.adapter not in SITE_ADAPTERS:
            raise ValueError(f"unsupported site adapter {self.adapter!r} for {self.name}")
        if host_matches(parsed.netloc, "ggzy.gov.cn") and parsed.scheme != "https":
            parsed = parsed._replace(scheme="https")
            self.base_url = urllib.parse.urlunparse(parsed)
        if self.adapter == "auto":
            if host_matches(parsed.netloc, "ggzy.gov.cn"):
                self.adapter = "ggzy_api"
            elif host_matches(parsed.netloc, "ccgp.gov.cn"):
                self.adapter = "html_search"
                self.search_url_template = (
                    "http://search.ccgp.gov.cn/bxsearch?"
                    "searchtype=1&page_index={page}&bidSort=0&buyerName=&projectId=&pinMu=0&"
                    "bidType=0&dbselect=bidx&kw={keyword}&start_time={start_date_colon}&"
                    "end_time={end_date_colon}&timeType=6&displayZone=&zoneId=&pppStatus=0&agentName="
                )
                self.allowed_domains = ["ccgp.gov.cn", "www.ccgp.gov.cn", "search.ccgp.gov.cn"]
                self.headers = {
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                    "Referer": "https://www.ccgp.gov.cn/",
                }
                self.include_url_patterns = [
                    r"/cggg/(?:zygg|dfgg|dfgggz)/(?:gkzb|xjgg|jzxtp|dyly|zgys|zbgg|cjgg|qtgg)/\d{6}/t\d+_\d+\.htm",
                    r"/cggg/.+?/t\d+_\d+\.htm",
                ]
                self.exclude_url_patterns = [
                    r"/xxgg/?$",
                    r"/news/",
                    r"/zcfg/",
                    r"/sjgk/",
                    r"/jdcx/",
                    r"/gpa/",
                    r"/ppp/",
                    r"/zcdt/",
                    r"/zxly/",
                    r"/contact",
                ]
            elif self.search_url_template:
                self.adapter = "json_api" if self.json_records_path else "html_search"
            else:
                self.adapter = "html_index"
        if not self.allowed_domains:
            self.allowed_domains = [parsed.netloc.lower()]
        self.search_method = self.search_method.upper()
        if self.search_method not in {"GET", "POST"}:
            raise ValueError(f"unsupported search_method {self.search_method!r} for {self.name}")
        if self.max_pages_per_keyword is not None and self.max_pages_per_keyword <= 0:
            raise ValueError(f"max_pages_per_keyword must be positive for {self.name}")


@dataclass
class AIConfig:
    enabled: bool = True
    base_url: str = "https://sophon-api.vzoom.com/ai/v1"
    model: str = "qwen-core"
    api_key_env: str = "SOPHON_API_KEY"
    timeout: float = 30.0
    temperature: float = 0.1
    max_tokens: int | None = None
    max_retries: int = 2
    retry_delay_seconds: float = 1.5
    verify_ssl: bool = True
    trust_env: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AIConfig":
        config = cls(**data) if data else cls()
        if value := first_env("AI_TEMPERATURE", "SOPHON_TEMPERATURE"):
            config.temperature = float(value)
        if value := first_env("AI_MAX_TOKENS", "SOPHON_MAX_TOKENS"):
            config.max_tokens = int(value)
        if value := first_env("AI_MAX_RETRIES", "SOPHON_MAX_RETRIES"):
            config.max_retries = int(value)
        if value := first_env("AI_RETRY_DELAY_SECONDS", "SOPHON_RETRY_DELAY_SECONDS"):
            config.retry_delay_seconds = float(value)
        config.verify_ssl = env_bool_first(("AI_VERIFY_SSL", "SOPHON_VERIFY_SSL"), config.verify_ssl)
        config.trust_env = env_bool_first(("AI_TRUST_ENV", "SOPHON_TRUST_ENV"), config.trust_env)
        return config


@dataclass
class Config:
    seeds: list[str] = field(default_factory=list)
    recent_days: int = 7
    page_range: list[int] | None = None
    search_page_start: int = 1
    search_pages_per_keyword: int = 3
    safety_max_pages: int = 100
    request_timeout: float = 30.0
    request_retries: int = 3
    request_retry_delay_seconds: float = 2.0
    request_backoff_multiplier: float = 1.5
    request_interval_seconds: float = 0.2
    verify_ssl: bool = True
    max_response_bytes: int = 3_000_000
    output_dir: str = "data/ai_tenders"
    search_keywords: list[str] = field(default_factory=lambda: list(AI_KEYWORDS))
    user_agent: str = "AIOpportunityMiner/0.2"
    proxy_url: str | None = None
    sites: list[SiteConfig] = field(default_factory=list)
    ai: AIConfig = field(default_factory=AIConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        data = dict(data)
        raw_urls = data.pop("urls", None)
        if raw_urls is not None and not data.get("seeds"):
            data["seeds"] = raw_urls
        raw_days = data.pop("days", None)
        if raw_days is not None and "recent_days" not in data:
            data["recent_days"] = raw_days
        raw_pages = data.pop("pages", None)
        if raw_pages is not None and "page_range" not in data:
            data["page_range"] = raw_pages
        raw_sites = data.pop("sites", None)
        raw_seeds = data.get("seeds") or []
        if not raw_sites and not raw_seeds:
            raise ValueError("config.urls must contain at least one URL")
        data["ai"] = AIConfig.from_dict(data.get("ai"))
        config = cls(**data)
        if config.page_range is not None:
            if len(config.page_range) != 2:
                raise ValueError("page_range must be [start_page, end_page]")
            start_page, end_page = int(config.page_range[0]), int(config.page_range[1])
            if start_page <= 0 or end_page < start_page:
                raise ValueError("page_range must satisfy 1 <= start_page <= end_page")
            config.search_page_start = start_page
            config.search_pages_per_keyword = end_page - start_page + 1
        config.seeds = [url for seed in config.seeds if (url := canonical_url(seed))]
        config.sites = (
            [SiteConfig.from_dict(item) for item in raw_sites]
            if raw_sites
            else [SiteConfig.from_seed(seed) for seed in config.seeds]
        )
        if not config.sites:
            raise ValueError("config.urls must contain at least one valid URL")
        if not config.seeds:
            config.seeds = [site.base_url for site in config.sites]
        if not config.search_keywords:
            config.search_keywords = list(AI_KEYWORDS)
        if config.recent_days <= 0:
            raise ValueError("recent_days must be positive")
        if config.search_pages_per_keyword <= 0:
            raise ValueError("page_range must include at least one page")
        if config.safety_max_pages <= 0:
            raise ValueError("safety_max_pages must be positive")
        if config.request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if config.request_retries < 0:
            raise ValueError("request_retries must be >= 0")
        if config.request_retry_delay_seconds < 0:
            raise ValueError("request_retry_delay_seconds must be >= 0")
        if config.request_backoff_multiplier < 1:
            raise ValueError("request_backoff_multiplier must be >= 1")
        if config.request_interval_seconds < 0:
            raise ValueError("request_interval_seconds must be >= 0")
        if config.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        return config


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.stack: list[str] = []
        self.skip_depth = 0
        self.current_href: str | None = None
        self.anchor_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = {key.lower(): value for key, value in attrs if value is not None}
        if tag in {"script", "style", "noscript"}:
            self.skip_depth += 1
        if tag == "a" and attr.get("href"):
            self.current_href = attr["href"]
            self.anchor_parts = []
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self.current_href:
            self.links.append((self.current_href, " ".join(self.anchor_parts).strip()))
            self.current_href = None
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
        if self.current_href is not None:
            self.anchor_parts.append(text)
        if self.stack and self.stack[-1] == "title":
            self.title_parts.append(text)
        if "body" in self.stack:
            self.text_parts.append(text)

    @property
    def title(self) -> str:
        return " ".join(self.title_parts).strip()

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.text_parts)).strip()


class ChatAIClient:
    def __init__(self, config: AIConfig, proxy_url: str | None = None) -> None:
        self.config = config
        self.api_key = first_env("AI_API_KEY", config.api_key_env) or ""
        if self.api_key.strip() in {"", "replace_with_your_key", "your_key", "你的 key"}:
            self.api_key = ""
        self.base_url = first_env("AI_API_BASE_URL", "SOPHON_API_BASE_URL") or config.base_url
        self.model = first_env("AI_MODEL", "SOPHON_MODEL") or config.model
        self.verify_ssl = config.verify_ssl
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens
        self.max_retries = config.max_retries
        self.retry_delay_seconds = config.retry_delay_seconds
        self.trust_env = config.trust_env
        self.proxy_url = env_optional_url_first("AI_PROXY_URL", "SOPHON_PROXY_URL") or proxy_url
        self.opener = self._build_opener()

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
        return "***" if len(self.api_key) <= 10 else f"{self.api_key[:6]}...{self.api_key[-4:]}"

    def _build_opener(self) -> urllib.request.OpenerDirector:
        handlers: list[urllib.request.BaseHandler] = [urllib.request.HTTPSHandler(context=self._ssl_context())]
        if self.proxy_url:
            handlers.append(urllib.request.ProxyHandler({"http": self.proxy_url, "https": self.proxy_url}))
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

    def chat_text(self, messages: list[dict[str, str]]) -> str:
        if not self.available:
            raise RuntimeError("AI is enabled but neither $AI_API_KEY nor the configured API key env is set")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        data = json.loads(self.post_json(payload).decode("utf-8"))
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"unexpected AI response format: {data}") from exc

    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        return extract_json_object(self.chat_text(messages))

    def post_json(self, payload: dict[str, Any]) -> bytes:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(1, max(1, self.max_retries + 1) + 1):
            request = urllib.request.Request(
                self.endpoint,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "AIOpportunityMiner/0.2",
                },
                method="POST",
            )
            try:
                with self.opener.open(request, timeout=self.config.timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                body_text = exc.read().decode("utf-8", errors="replace")[:1000]
                raise RuntimeError(f"AI HTTP {exc.code} from {self.endpoint}: {body_text}") from exc
            except Exception as exc:
                last_error = exc
                if attempt <= self.max_retries:
                    print(f"[warn] AI request failed attempt={attempt}: {exc}; retrying...", file=sys.stderr)
                    time.sleep(self.retry_delay_seconds)
        raise RuntimeError(f"AI request failed after retries: {last_error}")


class AITenderMiner:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.ai = ChatAIClient(config.ai, proxy_url=config.proxy_url)
        self.opener = self._build_opener()
        self.warmed_bases: set[str] = set()
        self.blocked_sites: set[str] = set()
        self.last_request_at = 0.0

    def _build_opener(self) -> urllib.request.OpenerDirector:
        handlers: list[urllib.request.BaseHandler] = [
            urllib.request.HTTPSHandler(context=self._ssl_context()),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        ]
        if self.config.proxy_url:
            handlers.append(urllib.request.ProxyHandler({"http": self.config.proxy_url, "https": self.config.proxy_url}))
        return urllib.request.build_opener(*handlers)

    def _ssl_context(self) -> ssl.SSLContext:
        if not self.config.verify_ssl:
            return ssl._create_unverified_context()
        try:
            import certifi  # type: ignore

            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            return ssl.create_default_context()

    def run(self) -> None:
        os.makedirs(os.path.join(self.config.output_dir, "opportunities"), exist_ok=True)
        records: list[dict[str, Any]] = []

        if self.config.ai.enabled and not self.preflight_ai():
            print("[done] opportunities=0 reason=ai_unavailable")
            return

        try:
            candidates = self.search_candidates()
            print(f"[search] candidates={len(candidates)}")
            if not candidates and self.blocked_sites:
                sites = ",".join(sorted(self.blocked_sites))
                self.write_run_status("site_unavailable", {"blocked_sites": sorted(self.blocked_sites)})
                print(f"[done] opportunities=0 reason=site_unavailable sites={sites}; existing outputs were not overwritten")
                return
            seen: set[str] = set()

            for index, candidate in enumerate(candidates[: self.config.safety_max_pages], start=1):
                if candidate["url"] in seen:
                    continue
                seen.add(candidate["url"])
                print(f"[fetch] {index}/{len(candidates)} {candidate['url']}")
                page = self.fetch_detail(candidate)
                if not page:
                    continue
                analysis = self.analyze(page)
                if not analysis:
                    continue
                record = self.build_record(page, analysis)
                if not record:
                    continue
                records.append(record)
                records = self.dedupe_records(records)
                self.write_opportunity_file(record)
                self.write_outputs(records, quiet=True)
                print(f"[hit] {record.get('项目名称', '')[:80]}")
        except KeyboardInterrupt:
            print("\n[stop] interrupted by user; writing current outputs...", file=sys.stderr)
            if not records:
                self.write_run_status("interrupted", {"opportunities": 0, "blocked_sites": sorted(self.blocked_sites)})
                print(f"[done] opportunities=0 reason=interrupted output_dir={self.config.output_dir}; existing outputs were not overwritten")
                return

        self.write_outputs(records)
        self.write_run_status("ok", {"opportunities": len(records), "blocked_sites": sorted(self.blocked_sites)})
        print(f"[done] opportunities={len(records)} output_dir={self.config.output_dir}")

    def preflight_ai(self) -> bool:
        if not self.ai.available:
            print(f"[error] AI unavailable: $AI_API_KEY or ${self.config.ai.api_key_env} is missing", file=sys.stderr)
            return False
        try:
            content = self.ai.chat_text(
                [
                    {"role": "system", "content": "你是连通性测试助手，只回答 OK。"},
                    {"role": "user", "content": "请只回答 OK"},
                ]
            )
        except Exception as exc:
            print(f"[error] AI preflight failed: {exc}", file=sys.stderr)
            print(
                "[error] check AI_API_BASE_URL/SOPHON_API_BASE_URL, proxy settings and DNS. "
                f"current proxy={'set' if self.ai.proxy_url else 'none'} trust_env={self.ai.trust_env}",
                file=sys.stderr,
            )
            return False
        print(f"[ai] preflight ok: {content[:80]}")
        return True

    def search_candidates(self) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        seen: set[str] = set()
        for site in self.config.sites:
            for keyword in self.config.search_keywords:
                if site.name in self.blocked_sites:
                    print(f"[warn] site={site.name} appears blocked/rate-limited; stop searching this site for this run", file=sys.stderr)
                    break
                page_limit = site.max_pages_per_keyword or self.config.search_pages_per_keyword
                for page in range(self.config.search_page_start, self.config.search_page_start + page_limit):
                    if site.name in self.blocked_sites:
                        break
                    records = self.search_site_records(site, keyword, page)
                    print(f"[search] site={site.name} keyword={keyword} page={page} records={len(records)}")
                    if not records:
                        break
                    for record_url, title in self.urls_from_records(records, site):
                        if record_url in seen:
                            continue
                        candidates.append({"url": record_url, "title": title, "keyword": keyword, "site": site.name})
                        seen.add(record_url)
        return candidates

    def search_site_records(self, site: SiteConfig, keyword: str, page: int) -> list[dict[str, Any]]:
        if site.adapter == "ggzy_api":
            return self.fetch_ggzy_search_records(site, keyword, page)
        if site.adapter == "html_search":
            return self.fetch_html_search_records(site, keyword, page)
        if site.adapter == "json_api":
            return self.fetch_json_search_records(site, keyword, page)
        if site.adapter == "html_index":
            if page > 1:
                return []
            return self.fetch_html_index_records(site, keyword)
        print(f"[warn] unsupported adapter={site.adapter} site={site.name}", file=sys.stderr)
        return []

    def warm_ggzy(self, base: str) -> None:
        base = base.rstrip("/")
        if base in self.warmed_bases:
            return
        request = urllib.request.Request(f"{base}/deal/dealList.html", headers={"User-Agent": self.config.user_agent})
        try:
            self.open_with_retries(request, label="ggzy search warmup").read(200_000)
        except Exception as exc:
            print(f"[warn] ggzy search warmup failed after retries: {exc}", file=sys.stderr)
        self.warmed_bases.add(base)

    def open_with_retries(self, request: urllib.request.Request, label: str) -> Any:
        last_error: Exception | None = None
        total_attempts = self.config.request_retries + 1
        for attempt in range(1, total_attempts + 1):
            try:
                self.wait_before_request()
                return self.opener.open(request, timeout=self.config.request_timeout)
            except urllib.error.HTTPError as exc:
                if exc.code in {400, 401, 403, 404, 405, 410, 429}:
                    raise
                last_error = exc
                if attempt < total_attempts:
                    delay = self.config.request_retry_delay_seconds * (
                        self.config.request_backoff_multiplier ** (attempt - 1)
                    )
                    print(
                        f"[warn] {label} failed attempt={attempt}/{total_attempts}: HTTP {exc.code}; "
                        f"retrying in {delay:.1f}s...",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
            except Exception as exc:
                if looks_site_unavailable_error(exc):
                    raise
                last_error = exc
                if attempt < total_attempts:
                    delay = self.config.request_retry_delay_seconds * (
                        self.config.request_backoff_multiplier ** (attempt - 1)
                    )
                    print(
                        f"[warn] {label} failed attempt={attempt}/{total_attempts}: {exc}; "
                        f"retrying in {delay:.1f}s...",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
        raise RuntimeError(last_error)

    def wait_before_request(self) -> None:
        if self.config.request_interval_seconds <= 0:
            return
        elapsed = time.monotonic() - self.last_request_at
        if elapsed < self.config.request_interval_seconds:
            time.sleep(self.config.request_interval_seconds - elapsed)
        self.last_request_at = time.monotonic()

    def fetch_ggzy_search_records(self, site: SiteConfig, keyword: str, page: int) -> list[dict[str, Any]]:
        base = site.base_url.rstrip("/")
        self.warm_ggzy(base)
        endpoint = f"{base}/information/pubTradingInfo/getTradList"
        form = {
            "SOURCE_TYPE": "1",
            "DEAL_TIME": self.ggzy_deal_time(),
            "FINDTXT": keyword,
            "PAGENUMBER": str(page),
        }
        request = urllib.request.Request(
            endpoint,
            data=urllib.parse.urlencode(form).encode("utf-8"),
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": f"{base.rstrip('/')}/deal/dealList.html",
                "Origin": base.rstrip("/"),
                "X-Requested-With": "XMLHttpRequest",
            },
            method="POST",
        )
        try:
            with self.open_with_retries(request, label=f"{site.name} search keyword={keyword} page={page}") as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            print(f"[warn] {site.name} search failed after retries: keyword={keyword} page={page} ({exc})", file=sys.stderr)
            return []
        if payload.get("code") != 200:
            return []
        data = payload.get("data", {})
        records = data.get("records", []) if isinstance(data, dict) else []
        return [item for item in records if isinstance(item, dict)]

    def build_search_request(self, site: SiteConfig, keyword: str, page: int, accept: str) -> urllib.request.Request:
        if site.search_url_template:
            url = render_template(site.search_url_template, keyword, page, self.config.recent_days)
            url = urllib.parse.urljoin(site.base_url, url)
        else:
            url = site.base_url

        data = None
        if site.search_method == "POST":
            form = {
                key: render_template(str(value), keyword, page, self.config.recent_days)
                for key, value in site.search_form.items()
            }
            data = urllib.parse.urlencode(form).encode("utf-8")

        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": accept,
            "Referer": site.base_url,
        }
        headers.update(site.headers)
        if data is not None and "Content-Type" not in headers:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        return urllib.request.Request(url, data=data, headers=headers, method=site.search_method)

    def fetch_html_search_records(self, site: SiteConfig, keyword: str, page: int) -> list[dict[str, Any]]:
        request = self.build_search_request(site, keyword, page, "text/html,application/xhtml+xml,*/*")
        try:
            with self.open_with_retries(request, label=f"{site.name} search keyword={keyword} page={page}") as response:
                content_type = response.headers.get("Content-Type", "")
                body = response.read(self.config.max_response_bytes)
                encoding = response.headers.get_content_charset() or "utf-8"
        except Exception as exc:
            if looks_blocked_error(exc) or looks_site_unavailable_error(exc):
                self.blocked_sites.add(site.name)
                print(f"[warn] {site.name} search is unavailable or blocked; stop this site for now ({exc})", file=sys.stderr)
                return []
            print(f"[warn] {site.name} search failed after retries: keyword={keyword} page={page} ({exc})", file=sys.stderr)
            return []
        if "html" not in content_type.lower() and not looks_like_html(body):
            return []
        html = body.decode(encoding, errors="replace")
        if looks_blocked(html):
            self.blocked_sites.add(site.name)
            print(f"[warn] {site.name} search appears blocked/rate-limited; skipping page={page}", file=sys.stderr)
            return []
        return self.records_from_html_links(site, html, response.geturl(), keyword)

    def fetch_html_index_records(self, site: SiteConfig, keyword: str) -> list[dict[str, Any]]:
        request = urllib.request.Request(
            site.base_url,
            headers={"User-Agent": self.config.user_agent, "Accept": "text/html,application/xhtml+xml,*/*"},
        )
        try:
            with self.open_with_retries(request, label=f"{site.name} index") as response:
                content_type = response.headers.get("Content-Type", "")
                body = response.read(self.config.max_response_bytes)
                encoding = response.headers.get_content_charset() or "utf-8"
        except Exception as exc:
            if looks_blocked_error(exc) or looks_site_unavailable_error(exc):
                self.blocked_sites.add(site.name)
                print(f"[warn] {site.name} index is unavailable or blocked; stop this site for now ({exc})", file=sys.stderr)
                return []
            print(f"[warn] {site.name} index failed after retries: {exc}", file=sys.stderr)
            return []
        if "html" not in content_type.lower() and not looks_like_html(body):
            return []
        html = body.decode(encoding, errors="replace")
        if looks_blocked(html):
            self.blocked_sites.add(site.name)
            print(f"[warn] {site.name} index appears blocked/rate-limited; skipping", file=sys.stderr)
            return []
        return self.records_from_html_links(site, html, response.geturl(), keyword)

    def fetch_json_search_records(self, site: SiteConfig, keyword: str, page: int) -> list[dict[str, Any]]:
        request = self.build_search_request(site, keyword, page, "application/json,text/plain,*/*")
        try:
            with self.open_with_retries(request, label=f"{site.name} search keyword={keyword} page={page}") as response:
                payload = json.loads(response.read(self.config.max_response_bytes).decode("utf-8", errors="replace"))
        except Exception as exc:
            print(f"[warn] {site.name} json search failed after retries: keyword={keyword} page={page} ({exc})", file=sys.stderr)
            return []
        records = self.records_from_json_payload(site, payload)
        return [record for record in records if self.record_matches_site(site, record, keyword)]

    def records_from_json_payload(self, site: SiteConfig, payload: Any) -> list[dict[str, Any]]:
        candidate_paths = [site.json_records_path] if site.json_records_path else []
        candidate_paths.extend(["data.records", "data.list", "data.rows", "records", "list", "rows", "data"])
        for path in candidate_paths:
            value = path_value(payload, path) if path else payload
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    def records_from_html_links(self, site: SiteConfig, html: str, base_url: str, keyword: str) -> list[dict[str, Any]]:
        parser = PageParser()
        parser.feed(html)
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for href, anchor in parser.links:
            title = clean_text(anchor)
            url = canonical_url(href, base_url)
            if not url or url in seen:
                continue
            record = {"url": url, "title": title}
            if not self.record_matches_site(site, record, keyword):
                continue
            records.append(record)
            seen.add(url)
        return records

    def record_matches_site(self, site: SiteConfig, record: dict[str, Any], keyword: str) -> bool:
        url = first_path_value(record, site.record_url_fields)
        title = clean_text(str(first_path_value(record, site.record_title_fields) or ""))
        absolute_url = canonical_url(str(url or ""), site.base_url)
        if not absolute_url or not self.url_allowed_for_site(site, absolute_url):
            return False
        haystack = f"{title} {absolute_url}"
        if title in IGNORED_LINK_TITLES or len(title) < 4:
            if not any(re.search(pattern, absolute_url) for pattern in site.include_url_patterns):
                return False
        if site.include_url_patterns and not any(re.search(pattern, absolute_url) for pattern in site.include_url_patterns):
            return False
        if site.exclude_url_patterns and any(re.search(pattern, absolute_url) for pattern in site.exclude_url_patterns):
            return False
        if keyword and keyword.lower() in haystack.lower():
            return True
        if any(marker.lower() in haystack.lower() for marker in site.result_link_keywords):
            return True
        return any(marker.lower() in haystack.lower() for marker in AI_KEYWORDS)

    def url_allowed_for_site(self, site: SiteConfig, url: str) -> bool:
        host = urllib.parse.urlparse(url).netloc.lower()
        return any(host_matches(host, domain) for domain in site.allowed_domains)

    def ggzy_deal_time(self) -> str:
        if self.config.recent_days <= 1:
            return "01"
        if self.config.recent_days <= 3:
            return "02"
        if self.config.recent_days <= 10:
            return "03"
        if self.config.recent_days <= 31:
            return "04"
        if self.config.recent_days <= 93:
            return "05"
        return "06"

    def urls_from_records(self, records: list[dict[str, Any]], site: SiteConfig) -> list[tuple[str, str]]:
        results: list[tuple[str, str]] = []
        seen: set[str] = set()
        for record in records:
            title = clean_text(str(first_path_value(record, site.record_title_fields) or ""))
            raw = first_path_value(record, site.record_url_fields)
            url = canonical_url(str(raw or ""), site.base_url)
            if url and url not in seen:
                results.append((url, title))
                seen.add(url)
        return results

    def fetch_detail(self, candidate: dict[str, str]) -> dict[str, Any] | None:
        best: dict[str, Any] | None = None
        for url in detail_url_variants(candidate["url"]):
            result = self.fetch_html(url)
            if not result:
                continue
            parser = PageParser()
            parser.feed(result["html"])
            page = {
                "url": url,
                "source_url": candidate["url"],
                "title": parser.title or candidate["title"],
                "text": parser.text,
                "links": [u for href, _ in parser.links if (u := canonical_url(href, url))],
            }
            if best is None or detail_score(page) > detail_score(best):
                best = page
        return best

    def fetch_html(self, url: str) -> dict[str, Any] | None:
        request = urllib.request.Request(url, headers={"User-Agent": self.config.user_agent})
        try:
            with self.open_with_retries(request, label=f"fetch {url}") as response:
                content_type = response.headers.get("Content-Type", "")
                body = response.read(self.config.max_response_bytes)
        except Exception as exc:
            print(f"[warn] fetch failed after retries: {url} ({exc})", file=sys.stderr)
            return None
        if "html" not in content_type.lower() and not looks_like_html(body):
            return None
        encoding = response.headers.get_content_charset() or "utf-8"
        html = body.decode(encoding, errors="replace")
        if looks_blocked(html):
            print(f"[warn] fetch appears blocked/rate-limited: {url}", file=sys.stderr)
            return None
        return {"html": html}

    def analyze(self, page: dict[str, Any]) -> dict[str, Any] | None:
        if self.config.ai.enabled:
            return self.analyze_with_ai(page)
        return rule_analysis(page)

    def analyze_with_ai(self, page: dict[str, Any]) -> dict[str, Any] | None:
        payload = {
            "url": page["url"],
            "title": page["title"],
            "text": page["text"][:12000],
            "ai_keywords": AI_KEYWORDS,
            "recent_days": self.config.recent_days,
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "你是招标采购商机抽取助手。只返回 JSON。"
                    "AI相关包括AI、人工智能、大模型、大语言模型、AIGC、智能体、机器学习、深度学习、"
                    "知识图谱、自然语言处理、计算机视觉、语音识别、智能问答、智能客服、算法模型等。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "判断网页是否为AI相关招标、采购、中标、成交或合同公告。"
                    "返回 JSON 字段：is_opportunity(boolean), business_stage("
                    "tender_notice/procurement_notice/award_candidate/award_result/contract_notice/not_opportunity), "
                    "project_name, project_id, customer_or_org, agency, supplier_or_winner, "
                    "budget_or_scale, deadline, publish_date, contact_name, contact_phone, address, reason。"
                    "金额必须保留正文原始单位，例如 193.65万元、1200000元、1.2亿元；不要只返回裸数字。"
                    "联系人、电话、地址必须从正文抽取；没有则填 null，不要编造。"
                    "\n\n网页："
                    + json.dumps(payload, ensure_ascii=False)
                ),
            },
        ]
        try:
            analysis = self.ai.chat_json(messages)
        except Exception as exc:
            print(f"[warn] AI page analysis failed: {page['url']} ({exc})", file=sys.stderr)
            return None
        if not analysis.get("is_opportunity"):
            return None
        if str(analysis.get("business_stage", "")) not in FINAL_STAGES:
            return None
        if not topic_relevant(page["title"], page["text"], analysis):
            return None
        publish_date = analysis.get("publish_date")
        if publish_date and not within_recent_days(publish_date, self.config.recent_days):
            return None
        return analysis

    def build_record(self, page: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any] | None:
        fallback = extract_fields(page["title"], page["text"])
        contact = " ".join(
            part
            for part in [
                value_or_empty(analysis.get("contact_name")),
                value_or_empty(analysis.get("contact_phone")),
                value_or_empty(fallback.get("contact")),
            ]
            if part
        )
        contact_name, contact_phone = split_contact(contact)
        money = normalize_money_from_sources(
            analysis.get("budget_or_scale"),
            fallback.get("budget_or_scale"),
            page["text"],
        )
        record = {
            "项目名称": value_or_empty(analysis.get("project_name") or fallback.get("project_name") or page["title"]),
            "采购单位": value_or_empty(analysis.get("customer_or_org") or fallback.get("customer_or_org")),
            "代理机构": value_or_empty(analysis.get("agency") or fallback.get("agency")),
            "中标成交方": value_or_empty(analysis.get("supplier_or_winner") or fallback.get("supplier_or_winner")),
            "金额元": money["amount_yuan"],
            "截止时间": normalize_datetime(analysis.get("deadline") or fallback.get("deadline")),
            "发布日期": normalize_datetime(analysis.get("publish_date") or fallback.get("publish_date")),
            "联系人": value_or_empty(analysis.get("contact_name")) or contact_name,
            "联系方式": value_or_empty(analysis.get("contact_phone")) or contact_phone,
            "地址": value_or_empty(analysis.get("address") or fallback.get("address")),
            "项目编号": value_or_empty(analysis.get("project_id") or fallback.get("project_id")),
            "来源链接": page["url"],
        }
        record = {key: value for key, value in record.items() if not is_missing(value)}
        if not record.get("项目名称") or not record.get("来源链接"):
            return None
        return record

    def write_opportunity_file(self, record: dict[str, Any]) -> str:
        source = str(record.get("来源链接") or record.get("项目编号") or record.get("项目名称"))
        digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]
        path = os.path.join(self.config.output_dir, "opportunities", f"{digest}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"saved_at": utc_now(), "opportunity": record}, handle, ensure_ascii=False, indent=2)
        print(f"[opportunity] saved={path}")
        return path

    def write_outputs(self, records: list[dict[str, Any]], quiet: bool = False) -> None:
        os.makedirs(self.config.output_dir, exist_ok=True)
        json_path = os.path.join(self.config.output_dir, "opportunities_structured.json")
        txt_path = os.path.join(self.config.output_dir, "opportunities_structured.txt")
        csv_path = os.path.join(self.config.output_dir, "opportunities_summary.csv")

        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(records, handle, ensure_ascii=False, indent=2)
        with open(txt_path, "w", encoding="utf-8") as handle:
            for index, record in enumerate(records, start=1):
                if index > 1:
                    handle.write("\n\n---\n\n")
                handle.write(record_to_text(index, record) + "\n")
        fields = ["项目名称", "采购单位", "代理机构", "中标成交方", "金额元", "截止时间", "发布日期", "联系人", "联系方式", "地址", "项目编号", "来源链接"]
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for record in records:
                writer.writerow({field: record.get(field, "") for field in fields})
        if not quiet:
            print(f"[summary] json={json_path} txt={txt_path} csv={csv_path}")

    def write_run_status(self, status: str, details: dict[str, Any]) -> None:
        os.makedirs(self.config.output_dir, exist_ok=True)
        path = os.path.join(self.config.output_dir, "run_status.json")
        payload = {
            "saved_at": utc_now(),
            "status": status,
            **details,
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        print(f"[status] saved={path}")

    def dedupe_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: dict[str, dict[str, Any]] = {}
        for record in records:
            key = str(record.get("项目编号") or record.get("来源链接") or record.get("项目名称"))
            if key not in deduped or record_quality(record) > record_quality(deduped[key]):
                deduped[key] = record
        return sorted(deduped.values(), key=lambda r: (str(r.get("发布日期", "")), float(r.get("金额元") or 0)), reverse=True)


def detail_url_variants(url: str) -> list[str]:
    variants = [url]
    if "/information/deal/html/a/" in url:
        variants.insert(0, url.replace("/information/deal/html/a/", "/information/deal/html/b/", 1))
    elif "/information/deal/html/b/" in url:
        variants.append(url.replace("/information/deal/html/b/", "/information/deal/html/a/", 1))
    return list(dict.fromkeys(variants))


def detail_score(page: dict[str, Any]) -> int:
    text = str(page.get("text", ""))
    markers = ["预算", "最高限价", "金额", "采购人", "招标人", "联系人", "联系方式", "电话", "地址", "开标", "截止"]
    return len(text) + sum(1000 for marker in markers if marker in text)


def clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def value_or_empty(value: Any) -> str:
    if is_missing(value):
        return ""
    return str(value).strip()


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, dict)):
        return not bool(value)
    text = str(value).strip()
    return not text or text.lower() in {"null", "none", "暂无", "未明确", "未提供", "无"}


def topic_relevant(title: str, text: str, analysis: dict[str, Any] | None = None) -> bool:
    analysis = analysis or {}
    haystack = "\n".join(
        [
            title,
            text[:8000],
            str(analysis.get("project_name", "")),
            str(analysis.get("reason", "")),
        ]
    ).lower()
    return any(keyword.lower() in haystack for keyword in AI_KEYWORDS)


def normalize_money(value: Any, context: str = "") -> dict[str, Any]:
    raw = value_or_empty(value)
    if not raw:
        return {"raw": "", "amount_yuan": None}
    text = raw.replace(",", "").replace("，", "").replace("人民币", "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(亿元|万元|元|亿|万)?", text)
    if not match:
        return {"raw": raw, "amount_yuan": None}
    number_text = match.group(1)
    unit = match.group(2)
    if unit is None and context:
        unit = infer_money_unit_from_context(number_text, context)
    try:
        amount = Decimal(number_text)
    except InvalidOperation:
        return {"raw": raw, "amount_yuan": None}
    unit = unit or "元"
    if unit in {"亿元", "亿"}:
        amount *= Decimal("100000000")
    elif unit in {"万元", "万"}:
        amount *= Decimal("10000")
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"raw": raw, "amount_yuan": int(amount) if amount == amount.to_integral_value() else float(amount)}


def normalize_money_from_sources(ai_value: Any, fallback_value: Any, context: str) -> dict[str, Any]:
    ai_raw = value_or_empty(ai_value)
    fallback_raw = value_or_empty(fallback_value)
    ai_money = normalize_money(ai_raw, context)
    fallback_money = normalize_money(fallback_raw, context)

    if money_has_unit(ai_raw):
        return ai_money
    if fallback_money["amount_yuan"] is not None and money_has_unit(fallback_raw):
        return fallback_money
    if ai_money["amount_yuan"] is not None:
        return ai_money
    return fallback_money


def money_has_unit(value: Any) -> bool:
    return bool(re.search(r"(亿元|万元|元|亿|万)", value_or_empty(value)))


def infer_money_unit_from_context(number_text: str, context: str) -> str | None:
    if not number_text:
        return None
    normalized_context = context.replace(",", "").replace("，", "")
    escaped = re.escape(number_text)
    for match in re.finditer(rf"{escaped}\s*(亿元|万元|元|亿|万)", normalized_context):
        start = max(0, match.start() - 30)
        end = min(len(normalized_context), match.end() + 20)
        window = normalized_context[start:end]
        if is_bad_money_context(window):
            continue
        return match.group(1)
    return None


def normalize_datetime(value: Any) -> str:
    raw = value_or_empty(value)
    if not raw:
        return ""
    text = raw.replace("T", " ")
    text = re.sub(r"[年月/]", "-", text).replace("日", " ")
    text = text.replace("时", ":").replace("分", ":").replace("秒", "")
    match = re.search(r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})(?:\s+([0-9]{1,2})(?::([0-9]{1,2}))?(?::([0-9]{1,2}))?)?", text)
    if not match:
        return raw
    year, month, day, hour, minute, second = match.groups()
    if hour is None:
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d} {int(hour):02d}:{int(minute or 0):02d}:{int(second or 0):02d}"


def parse_date(value: Any) -> datetime | None:
    normalized = normalize_datetime(value)
    match = re.search(r"([0-9]{4})-([0-9]{2})-([0-9]{2})", normalized)
    if not match:
        return None
    return datetime(*(int(part) for part in match.groups()), tzinfo=timezone.utc)


def within_recent_days(value: Any, days: int) -> bool:
    date = parse_date(value)
    if date is None:
        return False
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=days - 1) <= date <= today


def split_contact(value: Any) -> tuple[str, str]:
    raw = value_or_empty(value)
    phone_pattern = r"(?:\+?86[-\s]?)?(?:0\d{2,3}[-\s]?\d{7,8}(?:[-转]\d{1,6})?|1[3-9]\d{9})"
    phones = [re.sub(r"\s+", "", match.group(0)).strip("；;，,。") for match in re.finditer(phone_pattern, raw)]
    text = re.sub(phone_pattern, " ", raw)
    text = re.sub(r"(联系方式|联系电话|电话|手机|联系人|项目联系人)", " ", text)
    names = []
    for part in re.split(r"[、,，;；/|\s]+", text):
        cleaned = part.strip(" ：:，,。；;（）()")
        if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", cleaned) and cleaned not in names:
            names.append(cleaned)
    return "、".join(names), "；".join(dict.fromkeys(phones))


def extract_fields(title: str, text: str) -> dict[str, str]:
    content = re.sub(r"\s+", " ", f"{title} {text}").strip()

    def first(patterns: list[str]) -> str:
        for pattern in patterns:
            match = re.search(pattern, content, flags=re.IGNORECASE)
            if match:
                return re.sub(r"\s+", " ", match.group(1)).strip(" ：:，,。；;")
        return ""

    return {
        "project_name": first([r"(?:项目名称|采购项目名称|招标项目名称)[：:\s]*(.{2,120}?)(?=\s*(?:项目编号|采购人|招标人|预算|最高限价|$))", r"^(.{2,120}?)(?:招标公告|采购公告|中标公告|成交公告)"]),
        "project_id": first([r"(?:项目编号|采购项目编号|招标编号|交易编号)[：:\s]*([A-Za-z0-9\-_/（）()]+)"]),
        "customer_or_org": first([r"(?:采购人|招标人|建设单位|采购单位)[：:\s]*([^。；;，,\n]{2,80})"]),
        "agency": first([r"(?:采购代理机构|招标代理机构|代理机构)[：:\s]*([^。；;，,\n]{2,80})"]),
        "supplier_or_winner": first([r"(?:中标供应商|成交供应商|中标人|成交人)[：:\s]*([^。；;，,\n]{2,100})"]),
        "budget_or_scale": extract_money_field(content),
        "deadline": first([r"(?:投标截止时间|响应文件提交截止时间|开标时间|截止时间)[：:\s]*([0-9]{4}[-年/][0-9]{1,2}[-月/][0-9]{1,2}[日]?(?:\s*[0-9]{1,2}[:：时][0-9]{0,2}分?)?)"]),
        "publish_date": first([r"(?:公告日期|发布日期|发布时间|公示时间)[：:\s]*([0-9]{4}[-年/][0-9]{1,2}[-月/][0-9]{1,2}[日]?)"]),
        "contact": first([r"(?:联系人|项目联系人|联系方式|联系电话|电话)[：:\s]*([^。；;]{2,80})"]),
        "address": first([r"(?:地址|地点|开标地点)[：:\s]*([^。；;]{3,120})"]),
    }


def extract_money_field(content: str) -> str:
    amount = r"([0-9][0-9,，]*(?:\.[0-9]+)?\s*(?:亿元|万元|元|亿|万))"
    strong_labels = [
        "预算金额",
        "采购预算",
        "项目预算",
        "最高限价",
        "最高投标限价",
        "中标金额",
        "成交金额",
        "中标价",
        "成交价",
        "合同金额",
        "投标报价",
    ]
    for label in strong_labels:
        pattern = rf"{label}[：:\s]*{amount}"
        match = re.search(pattern, content, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()

    for match in re.finditer(amount, content):
        window = content[max(0, match.start() - 40) : match.end() + 30]
        if is_bad_money_context(window):
            continue
        if any(label in window for label in strong_labels):
            return match.group(1).strip()
    return ""


def is_bad_money_context(text: str) -> bool:
    bad_words = [
        "采购文件售价",
        "招标文件售价",
        "文件售价",
        "售价",
        "标书费",
        "资料费",
        "保证金",
        "代理服务费",
        "服务费收费",
        "收费标准",
    ]
    return any(word in text for word in bad_words)


def rule_stage(title: str, text: str) -> str:
    content = f"{title} {text[:2000]}"
    if any(word in content for word in ["中标候选人", "成交候选人"]):
        return "award_candidate"
    if any(word in content for word in ["招标公告", "公开招标", "资格预审"]):
        return "tender_notice"
    if any(word in content for word in ["采购公告", "竞争性磋商", "竞争性谈判", "询价公告"]):
        return "procurement_notice"
    if any(word in content for word in ["中标", "成交", "结果公告"]):
        return "award_result"
    if "合同" in content:
        return "contract_notice"
    return "not_opportunity"


def rule_analysis(page: dict[str, Any]) -> dict[str, Any] | None:
    if not topic_relevant(page["title"], page["text"]):
        return None
    stage = rule_stage(page["title"], page["text"])
    if stage not in FINAL_STAGES:
        return None
    fields = extract_fields(page["title"], page["text"])
    return {"is_opportunity": True, "business_stage": stage, **fields}


def record_to_text(index: int, record: dict[str, Any]) -> str:
    lines = [f"商机 #{index}"]
    lines.extend(f"{key}: {'' if value is None else value}" for key, value in record.items())
    return "\n".join(lines)


def record_quality(record: dict[str, Any]) -> int:
    return sum(1 for value in record.values() if not is_missing(value))


def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as handle:
        return Config.from_dict(json.load(handle))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mine AI-related tender/procurement opportunities.")
    parser.add_argument("--config", default="config.example.json")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--check-ai", action="store_true")
    return parser.parse_args(argv)


def check_ai_connection(config: Config) -> bool:
    client = ChatAIClient(config.ai, proxy_url=config.proxy_url)
    print(f"[ai-check] endpoint={client.endpoint}")
    print(f"[ai-check] model={client.model}")
    print(f"[ai-check] key_env=AI_API_KEY or {config.ai.api_key_env} key={client.masked_key}")
    print(f"[ai-check] verify_ssl={client.verify_ssl} trust_env={client.trust_env} proxy={'set' if client.proxy_url else 'none'}")
    if not client.available:
        print(f"[ai-check] failed: $AI_API_KEY or ${config.ai.api_key_env} is missing or placeholder")
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
    loaded = load_env_file(args.env_file)
    if loaded:
        print(f"[env] loaded {len(loaded)} values from {args.env_file}")
    config = load_config(args.config)
    if args.check_ai:
        return 0 if check_ai_connection(config) else 1
    AITenderMiner(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
