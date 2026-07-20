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
import html
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
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
import xml.etree.ElementTree as ET


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

MISSING_CONFIRMATION = "前往官网确认"
OUTPUT_CSV_FIELDS = [
    "招标单位",
    "招标单位行业分类",
    "项目名称",
    "项目编号",
    "截止日期",
    "采购内容",
    "源网址",
    "我司业务相关度",
    "匹配产品",
    "匹配理由",
]

COMPANY_PRODUCTS = {"VZOOM企业级AI智能体", "VZOOM财税大模型", "VZOOM AI中台"}

INDUSTRY_CATEGORIES = [
    "金融",
    "教育",
    "医疗卫生",
    "政府/政务",
    "能源/制造",
    "交通物流",
    "互联网/科技",
    "建筑地产",
    "农林水利",
    "文旅传媒",
    "其他",
]

ORG_FIELD_CANDIDATES = [
    "customer_or_org",
    "purchaser",
    "purchaseUnit",
    "purchase_unit",
    "buyerName",
    "buyer",
    "userName",
    "cgrName",
    "zbRName",
    "tenderer",
    "tendererName",
    "bidder",
    "companyname",
    "companyName",
    "orgName",
    "organizationName",
    "owner",
    "ownerName",
    "projectOwner",
    "project_owner",
    "projectUnit",
    "project_unit",
    "agencyName",
    "publishOrg",
    "publishOrgName",
    "docSourceName",
]

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

IGNORED_URL_PATTERNS = [
    r"/static/",
    r"/dist/",
    r"/assets/",
    r"/login",
    r"/register",
    r"/user(?:/|$)",
    r"/member(?:/|$)",
    r"/passport",
    r"/auth",
    r"/sso",
    r"ywlyzbcgpt\.jhtml",
    r"/wb_(?:owner|bidder|bideval)/",
]

SITE_ADAPTERS = {
    "auto",
    "ggzy_api",
    "cebpubservice_search",
    "cfcpn_api",
    "china_zbycg_search",
    "qianlima_search",
    "chengezhao_index",
    "chengezhao_search",
    "szygcgpt_public",
    "tpre_cgo_search",
    "ygcgfw_search",
    "guizhou_ggzy_search",
    "cqggzy_search",
    "html_search",
    "json_api",
    "html_index",
    "skip",
}


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


def xlsx_column_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha())
    index = 0
    for char in letters:
        index = index * 26 + ord(char.upper()) - ord("A") + 1
    return max(index - 1, 0)


def read_xlsx_rows(path: str) -> list[list[str]]:
    rows: list[list[str]] = []
    ns = {
        "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    with zipfile.ZipFile(path) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in shared_root.findall("a:si", ns):
                shared_strings.append("".join((text.text or "") for text in item.findall(".//a:t", ns)))

        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relmap = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        for sheet in workbook.findall(".//a:sheet", ns):
            rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            target = relmap.get(rel_id or "")
            if not target:
                continue
            sheet_path = target if target.startswith("xl/") else "xl/" + target.lstrip("/")
            sheet_root = ET.fromstring(archive.read(sheet_path))
            for row in sheet_root.findall(".//a:sheetData/a:row", ns):
                values: list[str] = []
                for cell in row.findall("a:c", ns):
                    index = xlsx_column_index(cell.attrib.get("r", "A1"))
                    while len(values) <= index:
                        values.append("")
                    value_node = cell.find("a:v", ns)
                    if value_node is None or value_node.text is None:
                        continue
                    raw = value_node.text
                    if cell.attrib.get("t") == "s":
                        try:
                            value = shared_strings[int(raw)]
                        except (ValueError, IndexError):
                            value = ""
                    else:
                        value = raw
                    values[index] = value.strip()
                if any(values):
                    rows.append(values)
    return rows


def load_credentials_file(path: str) -> dict[str, SiteCredential]:
    credentials: dict[str, SiteCredential] = {}
    if not path or not os.path.exists(path):
        return credentials
    try:
        rows = read_xlsx_rows(path)
    except Exception as exc:
        print(f"[warn] credentials file could not be read: {path} ({exc})", file=sys.stderr)
        return credentials
    if not rows:
        return credentials

    current_headers: list[str] | None = None
    for row in rows:
        normalized_headers = [cell.strip().lower() for cell in row]
        if any(cell in {"网址", "url", "网站"} for cell in row) and any(cell in {"账号", "用户名", "user", "username"} for cell in row):
            current_headers = row
            continue
        if not current_headers:
            continue
        values = row + [""] * max(0, len(current_headers) - len(row))
        item = {current_headers[index]: values[index].strip() for index in range(len(current_headers))}
        raw_url = first_nonempty(item, ["网址", "URL", "url", "网站"])
        username = first_nonempty(item, ["账号", "用户名", "user", "username", "账户"])
        password = first_nonempty(item, ["密码", "pass", "password", "pwd"])
        if not raw_url or not username or not password:
            continue
        url = canonical_url(raw_url)
        if not url:
            continue
        credential = SiteCredential(
            site_name=first_nonempty(item, ["招标网站名称", "网站名称", "名称"]) or slug_from_url(url),
            url=url,
            username=username,
            password=password,
        )
        if credential.host and credential.host not in credentials:
            credentials[credential.host] = credential
    return credentials


def load_credentials_from_env() -> dict[str, SiteCredential]:
    username = first_env("TENDER_USERNAME", "TENDER_ACCOUNT", "JINCAIWANG_USERNAME", "JINCAI_USERNAME")
    password = first_env("TENDER_PASSWORD", "JINCAIWANG_PASSWORD", "JINCAI_PASSWORD")
    raw_url = first_env("TENDER_CREDENTIAL_URL", "TENDER_SITE_URL", "JINCAIWANG_URL", "JINCAI_URL")
    if not raw_url and (first_env("JINCAIWANG_USERNAME", "JINCAI_USERNAME") or first_env("JINCAIWANG_PASSWORD", "JINCAI_PASSWORD")):
        raw_url = "http://www.cfcpn.com/jcw"
    if not username or not password or not raw_url:
        return {}
    url = canonical_url(raw_url)
    if not url:
        return {}
    credential = SiteCredential(site_name=slug_from_url(url), url=url, username=username, password=password)
    return {credential.host: credential} if credential.host else {}


def first_nonempty(data: dict[str, str], keys: list[str]) -> str:
    lower_map = {key.lower(): value for key, value in data.items()}
    for key in keys:
        value = data.get(key)
        if value:
            return value
        value = lower_map.get(key.lower())
        if value:
            return value
    return ""


def mask_username(username: str) -> str:
    if len(username) <= 2:
        return "*" * len(username)
    return username[:1] + "*" * max(len(username) - 2, 1) + username[-1:]


def strip_input_url(url: str) -> str:
    value = str(url).strip().strip('"').strip("'")
    markdown_match = re.fullmatch(r"\[[^\]]+\]\(([^)]+)\)", value)
    if markdown_match:
        value = markdown_match.group(1).strip()
    return value


def normalize_input_url(url: str) -> str:
    value = strip_input_url(url)
    if value and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        value = "http://" + value
    return value


def canonical_url(url: str, base_url: str | None = None) -> str | None:
    url = strip_input_url(str(url))
    if not url:
        return None
    try:
        if base_url:
            url = urllib.parse.urljoin(base_url, url)
        else:
            url = normalize_input_url(url)
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
    host = host.lower().split("@")[-1].split(":", 1)[0]
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


def decode_response_body(body: bytes, encoding: str | None) -> str:
    candidates = [encoding, "utf-8", "gb18030", "gbk"]
    seen: set[str] = set()
    best = ""
    best_bad_count: int | None = None
    for candidate in candidates:
        if not candidate:
            continue
        normalized = candidate.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            text = body.decode(candidate, errors="replace")
        except LookupError:
            continue
        bad_count = text.count("\ufffd")
        if best_bad_count is None or bad_count < best_bad_count:
            best = text
            best_bad_count = bad_count
        if bad_count == 0:
            return text
    return best or body.decode("utf-8", errors="replace")


def looks_blocked(text: str) -> bool:
    markers = [
        "访问过于频繁",
        "频繁访问",
        "请稍后再试",
        "请求被阻断",
        "您无法继续访问",
        "抱歉，您的请求被阻断了",
        "403 forbidden",
        "access denied",
        "too many requests",
        "you have been blocked",
        "you are unable to access",
        "aliyun_waf",
        "acw_sc__v2",
    ]
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)


def looks_login_required(text: str, url: str = "") -> bool:
    lowered = text.lower()
    if any(marker in lowered for marker in ["login", "register", "signin", "passport"]):
        if any(marker in lowered for marker in ["请输入密码", "用户登录", "会员登录", "供应商登录", "用户注册"]):
            return True
    return any(marker in url.lower() for marker in ["/login", "login.", "#/login", "/register", "registration"])


def looks_verification_required(text: str) -> bool:
    lowered = text.lower()
    markers = [
        "验证码",
        "短信验证码",
        "手机验证码",
        "图形验证码",
        "滑块",
        "拖动滑块",
        "captcha",
        "verifycode",
        "verification code",
    ]
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
    end = datetime.now().date()
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
    record_url_fields: list[str] = field(default_factory=lambda: ["detail_url", "url", "href", "detailUrl", "link"])
    record_title_fields: list[str] = field(default_factory=lambda: ["title", "name", "projectName", "project_name"])
    skip_reason: str = ""

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
            elif host_matches(parsed.netloc, "cebpubservice.com"):
                self.adapter = "cebpubservice_search"
                self.base_url = "https://bulletin.cebpubservice.com/"
                self.allowed_domains = ["bulletin.cebpubservice.com", "ctbpsp.com", "www.cebpubservice.com"]
                self.record_url_fields = ["detail_url", "url", "href"]
                self.record_title_fields = ["title", "noticeTitle", "name"]
            elif host_matches(parsed.netloc, "cfcpn.com"):
                if parsed.netloc.lower().startswith("ec."):
                    self.adapter = "skip"
                    self.skip_reason = "requires_login_or_waf"
                else:
                    self.adapter = "cfcpn_api"
                    self.base_url = "http://www.cfcpn.com/jcw/"
                    self.allowed_domains = ["www.cfcpn.com", "cfcpn.com"]
                    self.record_url_fields = ["detail_url", "url"]
                    self.record_title_fields = ["noticeTitle", "title"]
            elif host_matches(parsed.netloc, "china-zbycg.com"):
                self.adapter = "china_zbycg_search"
                self.search_url_template = "http://www.china-zbycg.com/agent_list/?title={keyword}"
                self.allowed_domains = ["www.china-zbycg.com", "china-zbycg.com"]
                self.include_url_patterns = [r"/agent_\d+\.html"]
                self.record_url_fields = ["url", "href"]
                self.record_title_fields = ["title", "name"]
            elif host_matches(parsed.netloc, "chengezhao.com"):
                self.adapter = "chengezhao_search"
                self.base_url = "https://www.chengezhao.com/cms/"
                self.allowed_domains = ["www.chengezhao.com", "chengezhao.com"]
                self.include_url_patterns = [r"/cms/post/"]
                self.record_url_fields = ["detail_url", "permalink", "url", "href"]
                self.record_title_fields = ["title", "name"]
            elif host_matches(parsed.netloc, "szygcgpt.com"):
                self.adapter = "szygcgpt_public"
                self.base_url = "https://www.szygcgpt.com/"
                self.allowed_domains = ["www.szygcgpt.com", "szygcgpt.com"]
                self.record_url_fields = ["detail_url", "url"]
                self.record_title_fields = ["ggName", "title", "name"]
            elif host_matches(parsed.netloc, "cgo.tpre.cn"):
                self.adapter = "tpre_cgo_search"
                self.base_url = "https://cgo.tpre.cn/"
                self.allowed_domains = ["cgo.tpre.cn"]
                self.record_url_fields = ["detail_url", "url"]
                self.record_title_fields = ["noticeTitle", "title", "name"]
            elif host_matches(parsed.netloc, "ygcgfw.com"):
                self.adapter = "ygcgfw_search"
                self.base_url = "http://www.ygcgfw.com/"
                self.allowed_domains = ["www.ygcgfw.com", "ygcgfw.com"]
                self.include_url_patterns = [r"/gggs/"]
                self.record_url_fields = ["detail_url", "linkurl", "url"]
                self.record_title_fields = ["customtitle", "title"]
            elif host_matches(parsed.netloc, "ggzy.guizhou.gov.cn"):
                self.adapter = "guizhou_ggzy_search"
                self.base_url = "https://ggzy.guizhou.gov.cn/"
                self.allowed_domains = ["ggzy.guizhou.gov.cn"]
                self.record_url_fields = ["detail_url", "apiUrl", "doc_pub_url", "url"]
                self.record_title_fields = ["docTitle", "f_20216323178", "title"]
            elif host_matches(parsed.netloc, "cqggzy.com"):
                self.adapter = "cqggzy_search"
                self.base_url = "https://www.cqggzy.com/"
                self.allowed_domains = ["www.cqggzy.com", "cqggzy.com"]
                self.include_url_patterns = [r"/xxhz/"]
                self.record_url_fields = ["detail_url", "linkurl", "url"]
                self.record_title_fields = ["titlenew", "customtitle", "title"]
            elif host_matches(parsed.netloc, "chinabidding.cn"):
                self.adapter = "skip"
                self.skip_reason = "waf_challenge"
            elif host_matches(parsed.netloc, "prechina.net"):
                self.adapter = "skip"
                self.skip_reason = "site_unavailable_or_bad_entry_url"
            elif any(
                host_matches(parsed.netloc, domain)
                for domain in [
                    "bidizhaobiao.com",
                    "trade.szggzy.com",
                ]
            ) or re.search(r"login|register|registration", self.base_url, re.IGNORECASE):
                self.adapter = "skip"
                self.skip_reason = "requires_login_or_waf"
            elif host_matches(parsed.netloc, "qianlima.com"):
                self.adapter = "qianlima_search"
                self.skip_reason = "requires_login"
                self.base_url = "https://search.qianlima.com/"
                self.allowed_domains = ["qianlima.com", "www.qianlima.com", "search.qianlima.com"]
                self.include_url_patterns = [r"/zb/", r"/zhaobiao/", r"/detail/", r"/notice/"]
                self.exclude_url_patterns = [r"/about/", r"/common/", r"/user/", r"/login", r"/reg"]
                self.record_url_fields = ["detail_url", "url", "href", "linkUrl", "contentUrl"]
                self.record_title_fields = ["title", "showTitle", "progName", "projectName", "name"]
            elif host_matches(parsed.netloc, "365trade.com.cn"):
                self.adapter = "skip"
                self.skip_reason = "requires_login_or_custom_api"
                self.allowed_domains = ["365trade.com.cn", "www.365trade.com.cn", "jy.365trade.com.cn"]
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
    credentials_file: str = ""
    manual_verification: bool = False
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
        if not data.get("credentials_file"):
            default_credentials = "招标网站汇总及账号密码-提供给AI.xlsx"
            data["credentials_file"] = (
                first_env("TENDER_CREDENTIALS_FILE", "AI_TENDER_CREDENTIALS_FILE")
                or (default_credentials if os.path.exists(default_credentials) else "")
            )
        data["ai"] = AIConfig.from_dict(data.get("ai"))
        config = cls(**data)
        config.manual_verification = env_bool_first(
            ("TENDER_MANUAL_VERIFICATION", "AI_TENDER_MANUAL_VERIFICATION"),
            config.manual_verification,
        )
        if config.page_range is not None:
            if len(config.page_range) != 2:
                raise ValueError("page_range must be [start_page, end_page]")
            start_page, end_page = int(config.page_range[0]), int(config.page_range[1])
            if start_page <= 0 or end_page < start_page:
                raise ValueError("page_range must satisfy 1 <= start_page <= end_page")
            config.search_page_start = start_page
            config.search_pages_per_keyword = end_page - start_page + 1
        config.seeds = [url for seed in config.seeds if (url := canonical_url(normalize_input_url(seed)))]
        config.sites = (
            [SiteConfig.from_dict(item) for item in raw_sites]
            if raw_sites
            else [SiteConfig.from_seed(seed) for seed in config.seeds]
        )
        unique_sites: dict[tuple[str, str, str], SiteConfig] = {}
        for site in config.sites:
            key = (site.name, site.base_url, site.adapter)
            if key not in unique_sites:
                unique_sites[key] = site
        config.sites = list(unique_sites.values())
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


class LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, Any]] = []
        self.images: list[dict[str, str]] = []
        self.current_form: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = {key.lower(): value or "" for key, value in attrs}
        if tag == "form":
            self.current_form = {
                "action": attr.get("action", ""),
                "method": attr.get("method", "GET").upper(),
                "inputs": [],
                "images": [],
            }
            self.forms.append(self.current_form)
        elif tag == "input" and self.current_form is not None:
            self.current_form["inputs"].append(
                {
                    "name": attr.get("name", ""),
                    "type": attr.get("type", "text").lower(),
                    "value": attr.get("value", ""),
                    "id": attr.get("id", ""),
                    "placeholder": attr.get("placeholder", ""),
                }
            )
        elif tag == "img":
            image = {
                "src": attr.get("src", ""),
                "id": attr.get("id", ""),
                "class": attr.get("class", ""),
                "alt": attr.get("alt", ""),
                "title": attr.get("title", ""),
            }
            self.images.append(image)
            if self.current_form is not None:
                self.current_form["images"].append(image)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form":
            self.current_form = None


@dataclass
class SiteCredential:
    site_name: str
    url: str
    username: str
    password: str

    @property
    def host(self) -> str:
        return urllib.parse.urlparse(self.url).netloc.lower()


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
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = self._build_opener()
        self.credentials = load_credentials_file(config.credentials_file)
        self.credentials.update(load_credentials_from_env())
        self.warmed_bases: set[str] = set()
        self.blocked_sites: set[str] = set()
        self.skipped_sites: set[str] = set()
        self.site_issues: dict[str, dict[str, Any]] = {}
        self.login_attempted: set[str] = set()
        self.login_status: dict[str, dict[str, Any]] = {}
        self.last_request_at = 0.0
        if config.credentials_file:
            print(f"[auth] loaded credentials for {len(self.credentials)} site(s) from {config.credentials_file}")

    def _build_opener(self) -> urllib.request.OpenerDirector:
        handlers: list[urllib.request.BaseHandler] = [
            urllib.request.HTTPSHandler(context=self._ssl_context()),
            urllib.request.HTTPCookieProcessor(self.cookie_jar),
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
            if not candidates and (self.blocked_sites or self.skipped_sites or self.site_issues):
                sites = ",".join(sorted(self.blocked_sites or self.skipped_sites or set(self.site_issues)))
                issue_statuses = {
                    str(issue.get("status") or "")
                    for issue in [*self.site_issues.values(), *self.login_status.values()]
                }
                if self.blocked_sites or "blocked_or_rate_limited" in issue_statuses:
                    status = "blocked_or_rate_limited"
                elif "verification_required" in issue_statuses:
                    status = "verification_required"
                elif "no_credentials" in issue_statuses:
                    status = "no_credentials"
                elif self.skipped_sites:
                    status = "skipped"
                else:
                    status = "no_candidates"
                self.write_run_status(
                    status,
                    {
                        "blocked_sites": sorted(self.blocked_sites),
                        "skipped_sites": sorted(self.skipped_sites),
                        "site_issues": self.site_issues,
                        "login_status": self.login_status,
                    },
                )
                print(f"[done] opportunities=0 reason={status} sites={sites or 'none'}; existing outputs were not overwritten")
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
                self.write_run_status(
                    "interrupted",
                    {
                        "opportunities": 0,
                        "blocked_sites": sorted(self.blocked_sites),
                        "skipped_sites": sorted(self.skipped_sites),
                        "site_issues": self.site_issues,
                        "login_status": self.login_status,
                    },
                )
                print(f"[done] opportunities=0 reason=interrupted output_dir={self.config.output_dir}; existing outputs were not overwritten")
                return

        self.write_outputs(records)
        self.write_run_status(
            "ok",
            {
                "opportunities": len(records),
                "blocked_sites": sorted(self.blocked_sites),
                "skipped_sites": sorted(self.skipped_sites),
                "site_issues": self.site_issues,
                "login_status": self.login_status,
            },
        )
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

    def credential_for_site(self, site: SiteConfig) -> SiteCredential | None:
        host = urllib.parse.urlparse(site.base_url).netloc.lower()
        candidates = [host, *site.allowed_domains]
        for candidate in candidates:
            candidate = candidate.lower()
            for credential_host, credential in self.credentials.items():
                if host_matches(candidate, credential_host) or host_matches(credential_host, candidate):
                    return credential
        return None

    def ensure_site_login(self, site: SiteConfig) -> bool:
        if site.name in self.login_attempted:
            return self.login_status.get(site.name, {}).get("status") == "success"
        self.login_attempted.add(site.name)
        credential = self.credential_for_site(site)
        if not credential:
            self.login_status[site.name] = {
                "status": "no_credentials",
                "reason": "no username/password found for this site",
            }
            return False
        self.login_status[site.name] = {
            "status": "attempting",
            "account": mask_username(credential.username),
            "credential_site": credential.site_name,
        }
        for login_url in candidate_login_urls(site.base_url, credential.url):
            result = self.try_login_url(site, credential, login_url)
            status = result.get("status")
            if status == "success":
                self.login_status[site.name] = result
                print(f"[auth] site={site.name} login ok account={mask_username(credential.username)}")
                return True
            if status in {"verification_required", "blocked_or_rate_limited"}:
                self.login_status[site.name] = result
                return False
        self.login_status[site.name] = {
            "status": "login_form_not_found",
            "account": mask_username(credential.username),
            "credential_site": credential.site_name,
            "reason": "no ordinary username/password form was found; site may use SPA login or a custom API",
        }
        return False

    def try_login_url(self, site: SiteConfig, credential: SiteCredential, login_url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            login_url,
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "text/html,application/xhtml+xml,*/*",
                "Referer": site.base_url,
            },
        )
        try:
            with self.open_login_page(request, label=f"{site.name} login page") as response:
                body = response.read(self.config.max_response_bytes)
                html = decode_response_body(body, response.headers.get_content_charset())
                page_url = response.geturl()
        except Exception as exc:
            if looks_blocked_error(exc):
                return {
                    "status": "blocked_or_rate_limited",
                    "account": mask_username(credential.username),
                    "login_url": login_url,
                    "reason": str(exc),
                }
            return {"status": "failed", "login_url": login_url, "reason": str(exc)}

        if looks_blocked(html):
            return {
                "status": "blocked_or_rate_limited",
                "account": mask_username(credential.username),
                "login_url": page_url,
                "reason": "login page returned anti-bot or rate-limit page",
            }
        parser = LoginFormParser()
        parser.feed(html)
        form = select_login_form(parser.forms)
        if not form:
            return {"status": "failed", "login_url": page_url, "reason": "no password form found"}
        payload = build_login_payload(form, credential)
        if not payload:
            return {"status": "failed", "login_url": page_url, "reason": "could not identify username/password fields"}
        if form_requires_verification(form, html):
            result = self.complete_manual_verification(site, form, html, page_url, payload)
            if not result.get("ok"):
                return {
                    "status": "verification_required",
                    "account": mask_username(credential.username),
                    "login_url": page_url,
                    "reason": result.get("reason") or "login form appears to require captcha, SMS code, or slider verification",
                }
        action_url = canonical_url(str(form.get("action") or page_url), page_url) or page_url
        method = str(form.get("method") or "POST").upper()
        data = urllib.parse.urlencode(payload).encode("utf-8") if method == "POST" else None
        submit_url = action_url
        if method == "GET":
            separator = "&" if urllib.parse.urlparse(action_url).query else "?"
            submit_url = action_url + separator + urllib.parse.urlencode(payload)
        submit = urllib.request.Request(
            submit_url,
            data=data,
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json,*/*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": page_url,
            },
            method=method,
        )
        try:
            with self.open_with_retries(submit, label=f"{site.name} login submit") as response:
                body = response.read(self.config.max_response_bytes)
                response_text = decode_response_body(body, response.headers.get_content_charset())
                final_url = response.geturl()
        except Exception as exc:
            return {
                "status": "failed",
                "account": mask_username(credential.username),
                "login_url": page_url,
                "reason": str(exc),
            }
        if looks_verification_required(response_text):
            return {
                "status": "verification_required",
                "account": mask_username(credential.username),
                "login_url": page_url,
                "reason": "server requested verification after username/password submit",
            }
        if login_response_success(response_text, final_url, page_url, self.cookie_jar):
            return {
                "status": "success",
                "account": mask_username(credential.username),
                "credential_site": credential.site_name,
                "login_url": page_url,
                "final_url": final_url,
            }
        return {
            "status": "failed",
            "account": mask_username(credential.username),
            "login_url": page_url,
            "reason": "login response still looks like a login page or did not set a session cookie",
        }

    def open_login_page(self, request: urllib.request.Request, label: str) -> Any:
        try:
            self.wait_before_request()
            return self.opener.open(request, timeout=self.config.request_timeout)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{label} unavailable: HTTP {exc.code}") from exc

    def complete_manual_verification(
        self,
        site: SiteConfig,
        form: dict[str, Any],
        html: str,
        page_url: str,
        payload: dict[str, str],
    ) -> dict[str, Any]:
        if looks_non_manual_verification_required(html, form):
            return {"ok": False, "reason": "login requires SMS code, slider, or interactive verification"}
        if not self.config.manual_verification:
            return {
                "ok": False,
                "reason": "login requires image captcha; rerun with --manual-verification to type it manually",
            }
        field_names = verification_field_names(form)
        if not field_names:
            return {"ok": False, "reason": "captcha field was not identified"}
        image_path = self.save_verification_image(site, form, html, page_url)
        if image_path:
            print(f"[auth] site={site.name} captcha image saved={image_path}")
        else:
            print(f"[auth] site={site.name} captcha image was not found; use the website login page as reference", file=sys.stderr)
        if not sys.stdin.isatty():
            return {"ok": False, "reason": "manual captcha input requires an interactive terminal"}
        code = input(f"[auth] site={site.name} enter captcha code: ").strip()
        if not code:
            return {"ok": False, "reason": "captcha input was empty"}
        for name in field_names:
            payload[name] = code
        return {"ok": True}

    def save_verification_image(self, site: SiteConfig, form: dict[str, Any], html: str, page_url: str) -> str:
        image_url = select_verification_image_url(form, html, page_url)
        if not image_url:
            return ""
        request = urllib.request.Request(
            image_url,
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Referer": page_url,
            },
        )
        try:
            with self.open_with_retries(request, label=f"{site.name} captcha image") as response:
                content_type = response.headers.get("Content-Type", "")
                body = response.read(self.config.max_response_bytes)
        except Exception as exc:
            print(f"[warn] captcha image fetch failed: {exc}", file=sys.stderr)
            return ""
        auth_dir = os.path.join(self.config.output_dir, "auth")
        os.makedirs(auth_dir, exist_ok=True)
        ext = extension_from_content_type(content_type) or os.path.splitext(urllib.parse.urlparse(image_url).path)[1] or ".img"
        if len(ext) > 8 or not ext.startswith("."):
            ext = ".img"
        path = os.path.join(auth_dir, f"{site.name}_captcha{ext}")
        with open(path, "wb") as handle:
            handle.write(body)
        return path

    def search_candidates(self) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        seen: set[str] = set()
        for site in self.config.sites:
            site_keywords = self.site_search_keywords(site)
            for keyword in site_keywords:
                if site.name in self.skipped_sites:
                    break
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
                    for record_url, title, snippet, publish_date, customer_or_org in self.urls_from_records(records, site):
                        if record_url in seen:
                            continue
                        if publish_date and not within_recent_days(publish_date, self.config.recent_days):
                            continue
                        candidates.append(
                            {
                                "url": record_url,
                                "title": title,
                                "keyword": keyword,
                                "site": site.name,
                                "snippet": snippet,
                                "publish_date": publish_date,
                                "customer_or_org": customer_or_org,
                            }
                        )
                        seen.add(record_url)
        return candidates

    def site_search_keywords(self, site: SiteConfig) -> list[str]:
        scan_only_adapters = {
            "chengezhao_search",
            "tpre_cgo_search",
            "html_index",
        }
        if site.adapter in scan_only_adapters:
            return ["AI_SCAN"]
        scan_capable_adapters = {
            "cfcpn_api",
            "szygcgpt_public",
            "ygcgfw_search",
            "guizhou_ggzy_search",
            "cqggzy_search",
        }
        if site.adapter in scan_capable_adapters:
            return ["AI_SCAN", *[keyword for keyword in self.config.search_keywords if keyword != "AI_SCAN"]]
        return self.config.search_keywords

    def search_site_records(self, site: SiteConfig, keyword: str, page: int) -> list[dict[str, Any]]:
        if site.adapter == "skip":
            if site.skip_reason == "waf_challenge":
                self.blocked_sites.add(site.name)
                self.mark_site_issue(
                    site,
                    "waf_challenge",
                    "site returned a JavaScript/WAF challenge before normal login or search; automated crawling is not attempted",
                )
                self.login_status[site.name] = {
                    "status": "waf_challenge",
                    "reason": "账号密码还没进入登录流程，站点先返回 WAF/JS 挑战；不建议绕过。",
                }
                return []
            if site.skip_reason == "site_unavailable_or_bad_entry_url":
                self.skipped_sites.add(site.name)
                self.mark_site_issue(
                    site,
                    "site_unavailable_or_bad_entry_url",
                    "configured entry URL is unavailable or is not a public opportunity search/list entry",
                )
                self.login_status[site.name] = {
                    "status": "site_unavailable_or_bad_entry_url",
                    "reason": "当前入口不可用或不是公告搜索入口，需要提供可访问的公告/采购列表页。",
                }
                return []
            if self.credential_for_site(site):
                if self.ensure_site_login(site):
                    self.mark_site_issue(site, "authenticated_generic_index", "login succeeded; using authenticated page link discovery")
                    if page > 1:
                        return []
                    return self.fetch_html_index_records(site, keyword)
                login_status = self.login_status.get(site.name, {})
                self.mark_site_issue(
                    site,
                    str(login_status.get("status") or site.skip_reason or "login_failed"),
                    str(login_status.get("reason") or "login did not complete"),
                )
            else:
                self.mark_site_issue(site, site.skip_reason or "skipped", "site requires login, registration, WAF challenge, or authorized access")
                self.login_status[site.name] = {
                    "status": "no_credentials",
                    "reason": "this site appears to need login or a custom search API, but no username/password was found in the credentials file",
                }
            self.skipped_sites.add(site.name)
            return []
        if site.adapter == "ggzy_api":
            return self.fetch_ggzy_search_records(site, keyword, page)
        if site.adapter == "cebpubservice_search":
            return self.fetch_cebpubservice_records(site, keyword, page)
        if site.adapter == "cfcpn_api":
            return self.fetch_cfcpn_records(site, keyword, page)
        if site.adapter == "china_zbycg_search":
            return self.fetch_html_search_records(site, keyword, page)
        if site.adapter == "qianlima_search":
            return self.fetch_qianlima_records(site, keyword, page)
        if site.adapter == "chengezhao_index":
            return self.fetch_chengezhao_records(site, keyword, page)
        if site.adapter == "chengezhao_search":
            return self.fetch_chengezhao_search_records(site, keyword, page)
        if site.adapter == "szygcgpt_public":
            return self.fetch_szygcgpt_records(site, keyword, page)
        if site.adapter == "tpre_cgo_search":
            return self.fetch_tpre_cgo_records(site, keyword, page)
        if site.adapter == "ygcgfw_search":
            return self.fetch_ygcgfw_records(site, keyword, page)
        if site.adapter == "guizhou_ggzy_search":
            return self.fetch_guizhou_ggzy_records(site, keyword, page)
        if site.adapter == "cqggzy_search":
            return self.fetch_cqggzy_records(site, keyword, page)
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

    def mark_site_issue(self, site: SiteConfig, status: str, reason: str) -> None:
        if site.name not in self.site_issues:
            self.site_issues[site.name] = {
                "base_url": site.base_url,
                "adapter": site.adapter,
                "status": status,
                "reason": reason,
            }

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
                if exc.code in {400, 401, 403, 404, 405, 410, 419, 429}:
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

    def fetch_cebpubservice_records(self, site: SiteConfig, keyword: str, page: int) -> list[dict[str, Any]]:
        categories = [
            ("88", "bulletin.html", "招标公告"),
            ("89", "change.html", "更正公告"),
            ("90", "result.html", "中标结果公示"),
            ("91", "candidate.html", "中标候选人公示"),
            ("92", "qualify.html", "资格预审公告"),
        ]
        dates = date_range_for_recent_days(self.config.recent_days)
        double_keyword = urllib.parse.quote(urllib.parse.quote(keyword))
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for category_id, path, category_name in categories:
            url = (
                f"https://bulletin.cebpubservice.com/xxfbcmses/search/{path}"
                f"?searchDate={dates['start_date']}&dates={self.config.recent_days}"
                f"&categoryId={category_id}&industryName=&area=&status=&publishMedia=&sourceInfo="
                f"&showStatus=1&word={double_keyword}&page={page}"
            )
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.config.user_agent,
                    "Accept": "text/html,application/xhtml+xml,*/*",
                    "Referer": "https://bulletin.cebpubservice.com/",
                },
            )
            try:
                with self.open_with_retries(request, label=f"{site.name} search keyword={keyword} page={page}") as response:
                    body = response.read(self.config.max_response_bytes)
                    html = decode_response_body(body, response.headers.get_content_charset())
            except Exception as exc:
                if looks_blocked_error(exc) or looks_site_unavailable_error(exc):
                    self.blocked_sites.add(site.name)
                    self.mark_site_issue(site, "blocked_or_rate_limited", str(exc))
                    return []
                print(f"[warn] {site.name} search failed after retries: keyword={keyword} page={page} ({exc})", file=sys.stderr)
                continue
            if looks_blocked(html):
                self.blocked_sites.add(site.name)
                self.mark_site_issue(site, "blocked_or_rate_limited", "search page returned anti-bot or rate-limit page")
                return []
            for match in re.finditer(
                r"(<a\b[^>]*href=[\"']javascript:urlOpen\('([^']+)'\)[\"'][^>]*>)(.*?)</a>",
                html,
                re.S,
            ):
                tag_html = match.group(1)
                uuid = match.group(2).strip()
                title_match = re.search(r"title\s*=\s*([\"'])(.*?)\1", tag_html, re.S)
                title = clean_text(title_match.group(2) if title_match else match.group(3))
                if not uuid or uuid in seen:
                    continue
                row_start = html.rfind("<tr", 0, match.start())
                row_end = html.find("</tr>", match.end())
                row_html = html[row_start : row_end if row_end > row_start else match.end() + 500]
                date_match = re.search(r"([0-9]{4}-[0-9]{2}-[0-9]{2})", row_html)
                publish_date = date_match.group(1) if date_match else ""
                if publish_date and not within_recent_days(publish_date, self.config.recent_days):
                    continue
                area_match = re.search(r"【([^】]+)】", row_html)
                detail_url = f"https://ctbpsp.com/#/bulletinDetail?uuid={uuid}&inpvalue=&dataSource=0&tenderAgency="
                records.append(
                    {
                        "detail_url": detail_url,
                        "title": title,
                        "publish_date": publish_date,
                        "area": area_match.group(1) if area_match else "",
                        "category": category_name,
                        "snippet": clean_text(f"{title} {category_name} {publish_date}"),
                    }
                )
                seen.add(uuid)
        return records

    def fetch_cfcpn_records(self, site: SiteConfig, keyword: str, page: int) -> list[dict[str, Any]]:
        if self.config.manual_verification and self.credential_for_site(site) and site.name not in self.login_attempted:
            if not self.ensure_site_login(site):
                login_status = self.login_status.get(site.name, {})
                self.mark_site_issue(
                    site,
                    str(login_status.get("status") or "login_failed"),
                    str(login_status.get("reason") or "optional login did not complete; continuing with public search API"),
                )
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        dates = date_range_for_recent_days(self.config.recent_days)
        search_endpoint = urllib.parse.urljoin(site.base_url, "noticeinfo/noticeInfo/dataNoticeList")
        search_form = {
            "noticeType": "",
            "pageNo": str(page),
            "pageSize": "50",
            "noticeState": "1",
            "isValid": "1",
            "orderBy": "publish_time desc",
            "briefContent": "" if keyword == "AI_SCAN" else keyword,
            "beginPublishTime": dates["start_date"],
            "endPublishTime": dates["end_date"],
        }
        search_request = urllib.request.Request(
            search_endpoint,
            data=urllib.parse.urlencode(search_form).encode("utf-8"),
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "application/json,text/plain,*/*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": urllib.parse.urljoin(site.base_url, "sys/index/goUrl?url=modules/sys/login/list&column=qbgg"),
            },
            method="POST",
        )
        try:
            with self.open_with_retries(search_request, label=f"{site.name} search keyword={keyword} page={page}") as response:
                payload = json.loads(decode_response_body(response.read(self.config.max_response_bytes), response.headers.get_content_charset()))
            for item in self.records_from_json_payload(site, payload):
                self.add_cfcpn_record(records, seen, item, keyword, item.get("noticeType") or item.get("column") or "1")
        except Exception as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 403:
                self.mark_site_issue(
                    site,
                    "keyword_search_forbidden",
                    f"keyword={keyword} page={page} returned HTTP 403; continuing with other keywords",
                )
                return []
            if looks_blocked_error(exc) or looks_site_unavailable_error(exc):
                self.blocked_sites.add(site.name)
                self.mark_site_issue(site, "blocked_or_rate_limited", str(exc))
                return []
            print(f"[warn] {site.name} api keyword search failed: keyword={keyword} page={page} ({exc})", file=sys.stderr)

        if records or page > 1 or keyword == "AI_SCAN":
            return records
        if keyword and keyword != "AI_SCAN":
            return records
        for notice_type in ["1", "2", "3", "4"]:
            endpoint = urllib.parse.urljoin(site.base_url, "noticeinfo/noticeInfo/indexLatestNoticeList")
            request = urllib.request.Request(
                endpoint,
                data=urllib.parse.urlencode({"noticeType": notice_type}).encode("utf-8"),
                headers={
                    "User-Agent": self.config.user_agent,
                    "Accept": "application/json,text/plain,*/*",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Referer": site.base_url,
                },
                method="POST",
            )
            try:
                with self.open_with_retries(request, label=f"{site.name} search keyword={keyword} page={page}") as response:
                    payload = json.loads(decode_response_body(response.read(self.config.max_response_bytes), response.headers.get_content_charset()))
            except Exception as exc:
                if looks_blocked_error(exc) or looks_site_unavailable_error(exc):
                    self.blocked_sites.add(site.name)
                    self.mark_site_issue(site, "blocked_or_rate_limited", str(exc))
                    return []
                print(f"[warn] {site.name} api search failed: keyword={keyword} page={page} ({exc})", file=sys.stderr)
                continue
            if not isinstance(payload, list):
                continue
            for item in payload:
                if not isinstance(item, dict):
                    continue
                self.add_cfcpn_record(records, seen, item, keyword, notice_type)
        return records

    def add_cfcpn_record(
        self,
        records: list[dict[str, Any]],
        seen: set[str],
        item: dict[str, Any],
        keyword: str,
        notice_type: Any,
    ) -> None:
        title = clean_text(
            str(
                item.get("noticeTitle")
                or item.get("purchaseName")
                or item.get("projectName")
                or item.get("title")
                or ""
            )
        )
        if not title_ai_relevant(title, keyword):
            return
        title_key = re.sub(r"\s+", "", title)
        if title_key in seen:
            return
        publish_date = normalize_publish_time(item.get("publishTime") or item.get("publishDate") or item.get("createTime"))
        if publish_date and not within_recent_days(publish_date, self.config.recent_days):
            return
        item_id = str(item.get("id") or item.get("noticeId") or item.get("purchaseId") or item.get("projectId") or "")
        if not item_id or item_id in seen:
            return
        notice_type_value = str(item.get("noticeType") or notice_type or "1")
        detail_url = (
            "http://www.cfcpn.com/jcw/sys/index/goUrl?"
            f"url=modules/sys/login/detail&column={urllib.parse.quote(notice_type_value)}"
            f"&searchVal={urllib.parse.quote(item_id)}"
        )
        row = dict(item)
        row["detail_url"] = detail_url
        row["title"] = title
        row["publish_date"] = publish_date
        row["customer_or_org"] = record_customer_or_org(row, title)
        row["snippet"] = clean_text(f"{title} 招标单位：{row['customer_or_org']} {publish_date}")
        records.append(row)
        seen.add(item_id)
        seen.add(title_key)

    def fetch_chengezhao_records(self, site: SiteConfig, keyword: str, page: int) -> list[dict[str, Any]]:
        categories = [
            "业务公告/项目公告",
            "业务公告/变更公告",
            "业务公告/中标公示",
            "业务公告/结果公告",
            "业务公告/调研公告",
        ]
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for category in categories:
            quoted = "/".join(urllib.parse.quote(part) for part in category.split("/"))
            path = f"categories/{quoted}/" if page <= 1 else f"categories/{quoted}/page/{page}/"
            url = urllib.parse.urljoin(site.base_url, path)
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.config.user_agent,
                    "Accept": "text/html,application/xhtml+xml,*/*",
                    "Referer": site.base_url,
                },
            )
            try:
                with self.open_with_retries(request, label=f"{site.name} category page={page}") as response:
                    html = decode_response_body(response.read(self.config.max_response_bytes), response.headers.get_content_charset())
            except Exception as exc:
                if looks_blocked_error(exc) or looks_site_unavailable_error(exc):
                    self.blocked_sites.add(site.name)
                    self.mark_site_issue(site, "blocked_or_rate_limited", str(exc))
                    return []
                print(f"[warn] {site.name} category failed: page={page} ({exc})", file=sys.stderr)
                continue
            if looks_blocked(html):
                self.blocked_sites.add(site.name)
                self.mark_site_issue(site, "blocked_or_rate_limited", "category page returned anti-bot or rate-limit page")
                return []
            self.add_chengezhao_links(records, seen, html, url, keyword)
        return records

    def fetch_chengezhao_search_records(self, site: SiteConfig, keyword: str, page: int) -> list[dict[str, Any]]:
        if page > 1:
            return []
        url = urllib.parse.urljoin(site.base_url, "search.json")
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "application/json,text/plain,*/*",
                "Referer": urllib.parse.urljoin(site.base_url, f"search?q={urllib.parse.quote(keyword)}"),
            },
        )
        try:
            with self.open_with_retries(request, label=f"{site.name} search index") as response:
                payload = json.loads(
                    decode_response_body(
                        response.read(self.config.max_response_bytes),
                        response.headers.get_content_charset(),
                    )
                )
        except Exception as exc:
            if looks_blocked_error(exc) or looks_site_unavailable_error(exc):
                self.blocked_sites.add(site.name)
                self.mark_site_issue(site, "blocked_or_rate_limited", str(exc))
                return []
            print(f"[warn] {site.name} search index failed: keyword={keyword} ({exc})", file=sys.stderr)
            return self.fetch_chengezhao_records(site, keyword, page)
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in self.records_from_json_payload(site, payload):
            title = clean_text(str(item.get("title") or item.get("name") or ""))
            if not title_ai_relevant(title, keyword):
                continue
            categories = item.get("categories")
            category_text = " ".join(str(value) for value in categories) if isinstance(categories, list) else str(categories or "")
            if category_text and "业务公告" not in category_text and not any(marker in category_text for marker in ["招标", "采购", "中标", "成交", "公示", "公告"]):
                continue
            detail_url = canonical_url(str(item.get("permalink") or item.get("url") or ""), site.base_url)
            if not detail_url or detail_url in seen:
                continue
            if "/cms/post/" not in detail_url:
                continue
            row = dict(item)
            row["detail_url"] = detail_url
            row["title"] = title
            row["snippet"] = clean_text(f"{title} {category_text}")
            records.append(row)
            seen.add(detail_url)
        return records

    def fetch_szygcgpt_records(self, site: SiteConfig, keyword: str, page: int) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        endpoint = urllib.parse.urljoin(site.base_url, "app/home/pageGGList.do")
        notice_types = ["1", "2", "3", "4", "5", "6", "7", "8"]
        purchase_types = ["0", "1"]
        for purchase_type in purchase_types:
            for notice_type in notice_types:
                payload = {
                    "page": page,
                    "rows": 30,
                    "xmLeiXing": "",
                    "caiGouType": purchase_type,
                    "ggLeiXing": notice_type,
                    "isShiShuGuoQi": "",
                    "isZhanLueYingJiWuZi": "",
                    "keyWords": "" if keyword == "AI_SCAN" else keyword,
                }
                request = urllib.request.Request(
                    endpoint,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={
                        "User-Agent": self.config.user_agent,
                        "Accept": "application/json,text/plain,*/*",
                        "Content-Type": "application/json;charset=UTF-8",
                        "Referer": site.base_url,
                        "Origin": site.base_url.rstrip("/"),
                    },
                    method="POST",
                )
                try:
                    with self.open_with_retries(request, label=f"{site.name} list page={page}") as response:
                        response_payload = json.loads(
                            decode_response_body(
                                response.read(self.config.max_response_bytes),
                                response.headers.get_content_charset(),
                            )
                        )
                except Exception as exc:
                    if looks_blocked_error(exc) or looks_site_unavailable_error(exc):
                        self.blocked_sites.add(site.name)
                        self.mark_site_issue(site, "blocked_or_rate_limited", str(exc))
                        return []
                    print(f"[warn] {site.name} public api failed: page={page} ({exc})", file=sys.stderr)
                    continue
                for item in self.records_from_json_payload(site, response_payload):
                    self.add_szygcgpt_record(records, seen, item, notice_type, purchase_type)
        return records

    def add_szygcgpt_record(
        self,
        records: list[dict[str, Any]],
        seen: set[str],
        item: dict[str, Any],
        notice_type: str,
        purchase_type: str,
    ) -> None:
        title = clean_text(str(item.get("ggName") or item.get("title") or item.get("name") or ""))
        org = clean_text(str(item.get("zbRName") or item.get("cgrName") or item.get("tenderer") or ""))
        project_id = clean_text(str(item.get("bdBH") or item.get("xmBH") or item.get("projectNo") or ""))
        publish_date = normalize_publish_time(item.get("faBuTime") or item.get("publishTime") or item.get("createTime"))
        deadline = normalize_publish_time(item.get("wjEndTime") or item.get("endTime") or item.get("kaiBiaoTime"))
        haystack = clean_text(f"{title} {org} {project_id}")
        if not contains_ai_keyword(haystack):
            return
        if publish_date and not within_recent_days(publish_date, self.config.recent_days):
            return
        if not publish_date and deadline and is_before_recent_window(deadline, self.config.recent_days):
            return
        guid = str(item.get("guid") or "")
        gg_guid = str(item.get("ggGuid") or "")
        bd_guid = str(item.get("bdGuid") or "")
        data_source = str(item.get("dataSource") if item.get("dataSource") is not None else "0")
        if not (guid or gg_guid or bd_guid):
            return
        key = "|".join([guid, gg_guid, bd_guid, title])
        if key in seen:
            return
        params = {
            "guid": guid,
            "ggGuid": gg_guid,
            "bdGuid": bd_guid,
            "ggLeiXing": str(item.get("ggXingZhi") or notice_type),
            "dataSource": data_source,
            "caiGouType": purchase_type,
        }
        detail_url = urllib.parse.urljoin("https://www.szygcgpt.com/", "ygcg/detailTop") + "?" + urllib.parse.urlencode(params)
        snippet = clean_text(
            f"项目名称：{title} 招标单位：{org} 项目编号：{project_id} "
            f"发布日期：{publish_date} 截止日期：{deadline}"
        )
        row = dict(item)
        row.update(
            {
                "detail_url": detail_url,
                "title": title,
                "ggName": title,
                "publish_date": publish_date,
                "deadline": deadline,
                "customer_or_org": org,
                "project_id": project_id,
                "snippet": snippet,
            }
        )
        records.append(row)
        seen.add(key)

    def fetch_tpre_cgo_records(self, site: SiteConfig, keyword: str, page: int) -> list[dict[str, Any]]:
        endpoint = urllib.parse.urljoin(site.base_url, "cgo-portal-service/biz/full-site-search/anmuas/purchase/notice/page")
        params = {
            "pageNo": str(page),
            "pageSize": "10",
            "keyword": "" if keyword == "AI_SCAN" else keyword,
        }
        url = endpoint + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "application/json,text/plain,*/*",
                "Referer": urllib.parse.urljoin(site.base_url, "cgo-portal-view/"),
            },
        )
        try:
            with self.open_with_retries(request, label=f"{site.name} search keyword={keyword} page={page}") as response:
                payload_data = json.loads(
                    decode_response_body(response.read(self.config.max_response_bytes), response.headers.get_content_charset())
                )
        except Exception as exc:
            if looks_blocked_error(exc) or looks_site_unavailable_error(exc):
                self.blocked_sites.add(site.name)
                self.mark_site_issue(site, "blocked_or_rate_limited", str(exc))
                return []
            print(f"[warn] {site.name} api search failed: keyword={keyword} page={page} ({exc})", file=sys.stderr)
            return []

        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in self.records_from_json_payload(site, payload_data):
            pk_id = clean_text(str(item.get("pkId") or item.get("id") or item.get("originalId") or ""))
            if not pk_id:
                continue
            row = dict(item)
            row["detail_url"] = (
                urllib.parse.urljoin(site.base_url, "cgo-portal-service/biz/purchase/notice/anmuas/details")
                + "?"
                + urllib.parse.urlencode({"pkId": pk_id})
            )
            self.add_standard_api_record(
                records,
                seen,
                row,
                site,
                keyword,
                base_url=site.base_url,
                title_fields=["noticeTitle", "title"],
                url_fields=["detail_url"],
                date_fields=["publicTime", "createTime"],
                org_fields=ORG_FIELD_CANDIDATES + ["source"],
                project_id_fields=["purchaseCode", "projectCode", "projectNo"],
                content_fields=["source", "purchaseType", "noticeType", "purchaseNoticeType"],
            )
        return records

    def fetch_ygcgfw_records(self, site: SiteConfig, keyword: str, page: int) -> list[dict[str, Any]]:
        dates = date_range_for_recent_days(self.config.recent_days)
        endpoint = urllib.parse.urljoin(site.base_url, "inteligentsearchnew/rest/esinteligentsearch/getFullTextDataNew")
        payload = {
            "token": "",
            "pn": max(page - 1, 0) * 10,
            "rn": 10,
            "sdt": f"{dates['start_date']} 00:00:00",
            "edt": f"{dates['end_date']} 23:59:59",
            "wd": "" if keyword == "AI_SCAN" else urllib.parse.quote(keyword),
            "inc_wd": "",
            "exc_wd": "",
            "fields": "title;content",
            "cnum": "001",
            "sort": "{\"webdate\":0}",
            "ssort": "title",
            "cl": 800,
            "terminal": "",
            "condition": None,
            "time": None,
            "highlights": "title;content",
            "statistics": None,
            "unionCondition": None,
            "accuracy": "",
            "noParticiple": "1",
            "searchRange": None,
        }
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "application/json,text/plain,*/*",
                "Content-Type": "application/json;charset=UTF-8",
                "Referer": urllib.parse.urljoin(site.base_url, f"search/fullsearch.html?wd={'' if keyword == 'AI_SCAN' else urllib.parse.quote(keyword)}"),
            },
            method="POST",
        )
        try:
            with self.open_with_retries(request, label=f"{site.name} search keyword={keyword} page={page}") as response:
                payload_data = json.loads(
                    decode_response_body(response.read(self.config.max_response_bytes), response.headers.get_content_charset())
                )
        except Exception as exc:
            if looks_blocked_error(exc) or looks_site_unavailable_error(exc):
                self.blocked_sites.add(site.name)
                self.mark_site_issue(site, "blocked_or_rate_limited", str(exc))
                return []
            print(f"[warn] {site.name} api search failed: keyword={keyword} page={page} ({exc})", file=sys.stderr)
            return []

        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in self.records_from_json_payload(site, payload_data):
            self.add_standard_api_record(
                records,
                seen,
                item,
                site,
                keyword,
                base_url=site.base_url,
                title_fields=["customtitle", "title"],
                url_fields=["linkurl", "url", "detail_url"],
                date_fields=["webdate", "infodate"],
                org_fields=ORG_FIELD_CANDIDATES,
                project_id_fields=["projectno", "projectCode"],
                content_fields=["content", "highlight.content", "categoryname"],
            )
        return records

    def fetch_guizhou_ggzy_records(self, site: SiteConfig, keyword: str, page: int) -> list[dict[str, Any]]:
        endpoint = urllib.parse.urljoin(site.base_url, "tradeInfo/es/list")
        payload = {
            "pageNum": page,
            "pageSize": 20,
            "docTitle": "" if keyword == "AI_SCAN" else keyword,
        }
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "application/json,text/plain,*/*",
                "Content-Type": "application/json;charset=UTF-8",
                "Referer": urllib.parse.urljoin(site.base_url, f"xxfw/search.html?searchWord={'' if keyword == 'AI_SCAN' else urllib.parse.quote(keyword)}"),
            },
            method="POST",
        )
        try:
            with self.open_with_retries(request, label=f"{site.name} search keyword={keyword} page={page}") as response:
                payload_data = json.loads(
                    decode_response_body(response.read(self.config.max_response_bytes), response.headers.get_content_charset())
                )
        except Exception as exc:
            if looks_blocked_error(exc) or looks_site_unavailable_error(exc):
                self.blocked_sites.add(site.name)
                self.mark_site_issue(site, "blocked_or_rate_limited", str(exc))
                return []
            print(f"[warn] {site.name} api search failed: keyword={keyword} page={page} ({exc})", file=sys.stderr)
            return []

        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in self.records_from_json_payload(site, payload_data):
            self.add_standard_api_record(
                records,
                seen,
                item,
                site,
                keyword,
                base_url=site.base_url,
                title_fields=["docTitle", "title"],
                url_fields=["apiUrl", "doc_pub_url", "url"],
                date_fields=["docRelTime", "publishTime", "save_time"],
                org_fields=ORG_FIELD_CANDIDATES + ["docSourceName"],
                project_id_fields=["tenderProjectCode", "projectCode", "projectNo"],
                content_fields=["businessTypeName", "announcement", "docSourceName"],
            )
        return records

    def fetch_cqggzy_records(self, site: SiteConfig, keyword: str, page: int) -> list[dict[str, Any]]:
        params = {"keyword": "" if keyword == "AI_SCAN" else keyword, "pageNum": str(page)}
        url = urllib.parse.urljoin(site.base_url, "search") + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "text/html,application/xhtml+xml,*/*",
                "Referer": site.base_url,
            },
        )
        try:
            with self.open_with_retries(request, label=f"{site.name} search keyword={keyword} page={page}") as response:
                html_text = decode_response_body(response.read(self.config.max_response_bytes), response.headers.get_content_charset())
        except Exception as exc:
            if looks_blocked_error(exc) or looks_site_unavailable_error(exc):
                self.blocked_sites.add(site.name)
                self.mark_site_issue(site, "blocked_or_rate_limited", str(exc))
                return []
            print(f"[warn] {site.name} search failed: keyword={keyword} page={page} ({exc})", file=sys.stderr)
            return []
        if looks_blocked(html_text):
            self.blocked_sites.add(site.name)
            self.mark_site_issue(site, "blocked_or_rate_limited", "search page returned anti-bot or rate-limit page")
            return []

        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in extract_embedded_json_objects(html_text, ["linkurl", "title"]):
            self.add_standard_api_record(
                records,
                seen,
                item,
                site,
                keyword,
                base_url=site.base_url,
                title_fields=["titlenew", "customtitle", "title"],
                url_fields=["linkurl", "url", "detail_url"],
                date_fields=["pubinwebdate", "infodate", "webdate", "startdate"],
                org_fields=ORG_FIELD_CANDIDATES,
                project_id_fields=["projectno", "projectCode"],
                content_fields=["content", "categorytype", "categorytype2", "infoc"],
            )
        return records

    def add_standard_api_record(
        self,
        records: list[dict[str, Any]],
        seen: set[str],
        item: dict[str, Any],
        site: SiteConfig,
        keyword: str,
        *,
        base_url: str,
        title_fields: list[str],
        url_fields: list[str],
        date_fields: list[str],
        org_fields: list[str],
        project_id_fields: list[str],
        content_fields: list[str],
    ) -> None:
        title = clean_text(html.unescape(str(first_path_value(item, title_fields) or "")))
        title = clean_text(title)
        raw_url = first_path_value(item, url_fields)
        detail_url = canonical_url(str(raw_url or ""), base_url)
        if not detail_url:
            return
        publish_date = normalize_publish_time(first_path_value(item, date_fields))
        if publish_date and not within_recent_days(publish_date, self.config.recent_days):
            return
        org = clean_text(str(first_path_value(item, org_fields) or ""))
        project_id = clean_project_id(first_path_value(item, project_id_fields), title, detail_url)
        content_parts = [str(first_path_value(item, [field]) or "") for field in content_fields]
        snippet = clean_text(html.unescape(" ".join([title, org, project_id, publish_date, *content_parts])))
        if not topic_relevant(title, snippet):
            return
        key = clean_text(str(item.get("id") or item.get("infoid") or item.get("metaDataId") or detail_url))
        if key in seen or detail_url in seen:
            return
        row = dict(item)
        row.update(
            {
                "detail_url": detail_url,
                "title": title,
                "publish_date": publish_date,
                "customer_or_org": org,
                "project_id": project_id,
                "snippet": snippet,
            }
        )
        records.append(row)
        seen.add(key)
        seen.add(detail_url)

    def add_chengezhao_links(
        self,
        records: list[dict[str, Any]],
        seen: set[str],
        html: str,
        base_url: str,
        keyword: str,
    ) -> None:
        blocks = re.split(r"<div[^>]+class=[\"'][^\"']*cez-business-main__news-item(?!-)[^\"']*[\"'][^>]*>", html)
        found_blocks = len(blocks) > 1
        for block in blocks[1:]:
            title_match = re.search(r"<h3>\s*<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", block, re.S)
            if not title_match:
                continue
            detail_url = canonical_url(title_match.group(1), base_url)
            if not detail_url or detail_url in seen:
                continue
            if "/cms/post/" not in detail_url:
                continue
            title = strip_html_text(title_match.group(2))
            date_match = re.search(
                r"cez-business-main__news-item-date[\s\S]*?<span>\s*([0-9]{2})-([0-9]{2})\s*</span>\s*<span>\s*(20[0-9]{2})\s*</span>",
                block,
                re.S,
            )
            publish_date = f"{date_match.group(3)}-{date_match.group(1)}-{date_match.group(2)}" if date_match else ""
            if publish_date and not within_recent_days(publish_date, self.config.recent_days):
                continue
            body_match = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
            body_text = strip_html_text(body_match.group(1)) if body_match else ""
            snippet = clean_text(" ".join([title, publish_date, body_text[:1200]]))
            if keyword == "AI_SCAN":
                if not topic_relevant(title, snippet):
                    continue
            elif not title_ai_relevant(f"{title} {body_text[:1200]}", keyword):
                continue
            records.append({"detail_url": detail_url, "title": title, "publish_date": publish_date, "snippet": snippet})
            seen.add(detail_url)
        if found_blocks:
            return

        parser = PageParser()
        parser.feed(html)
        for href, anchor in parser.links:
            title = clean_text(anchor)
            detail_url = canonical_url(href, base_url)
            if not detail_url or detail_url in seen:
                continue
            if "/cms/post/" not in detail_url:
                continue
            if not title_ai_relevant(title, keyword):
                continue
            records.append({"detail_url": detail_url, "title": title, "snippet": title})
            seen.add(detail_url)

    def fetch_qianlima_records(self, site: SiteConfig, keyword: str, page: int) -> list[dict[str, Any]]:
        credential = self.credential_for_site(site)
        if not credential:
            self.login_status[site.name] = {
                "status": "no_credentials",
                "reason": "qianlima search API requires a logged-in account; no username/password was found in the credentials file",
            }
            self.mark_site_issue(site, "no_credentials", self.login_status[site.name]["reason"])
            self.skipped_sites.add(site.name)
            return []
        if not self.ensure_site_login(site):
            login_status = self.login_status.get(site.name, {})
            self.mark_site_issue(
                site,
                str(login_status.get("status") or "login_failed"),
                str(login_status.get("reason") or "qianlima login did not complete"),
            )
            self.skipped_sites.add(site.name)
            return []

        params = {
            "filtermode": "1",
            "timeType": "101",
            "areas": "",
            "types": "-1",
            "searchMode": "0",
            "keywords": keyword,
            "beginTime": "",
            "endTime": "",
            "isfirst": "true" if page == 1 else "false",
            "currentPage": str(page),
            "numPerPage": "20",
        }
        url = f"https://search.qianlima.com/api/v1/website/search?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            data=b"",
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "application/json,text/plain,*/*",
                "Content-Type": "application/json;charset=UTF-8",
                "Origin": "https://search.qianlima.com",
                "Referer": f"https://search.qianlima.com/?q={urllib.parse.quote(keyword)}",
            },
            method="POST",
        )
        try:
            with self.open_with_retries(request, label=f"{site.name} search keyword={keyword} page={page}") as response:
                payload = json.loads(
                    decode_response_body(response.read(self.config.max_response_bytes), response.headers.get_content_charset())
                )
        except Exception as exc:
            if looks_blocked_error(exc) or looks_site_unavailable_error(exc):
                self.blocked_sites.add(site.name)
                self.mark_site_issue(site, "blocked_or_rate_limited", str(exc))
                return []
            print(f"[warn] {site.name} api search failed: keyword={keyword} page={page} ({exc})", file=sys.stderr)
            return []

        message = str(payload.get("msg") or payload.get("message") or "")
        if "未登录" in message or "登录" in message and payload.get("code") not in {0, 200}:
            self.login_status[site.name] = {
                "status": "login_required",
                "account": mask_username(credential.username),
                "reason": message or "qianlima API requires login",
            }
            self.mark_site_issue(site, "login_required", self.login_status[site.name]["reason"])
            self.skipped_sites.add(site.name)
            return []
        if looks_verification_required(message):
            self.login_status[site.name] = {
                "status": "verification_required",
                "account": mask_username(credential.username),
                "reason": message,
            }
            self.mark_site_issue(site, "verification_required", message)
            self.skipped_sites.add(site.name)
            return []

        records: list[dict[str, Any]] = []
        for item in self.records_from_json_payload(site, payload):
            if isinstance(item, dict):
                self.add_qianlima_record(records, item, keyword)
        return records

    def add_qianlima_record(self, records: list[dict[str, Any]], item: dict[str, Any], keyword: str) -> None:
        title = clean_text(
            str(
                item.get("title")
                or item.get("showTitle")
                or item.get("progName")
                or item.get("projectName")
                or item.get("name")
                or ""
            )
        )
        snippet = clean_text(
            str(
                item.get("content")
                or item.get("summary")
                or item.get("description")
                or item.get("province")
                or item.get("area")
                or ""
            )
        )
        publish_date = normalize_datetime(
            item.get("updateTime")
            or item.get("publishTime")
            or item.get("publishDate")
            or item.get("createTime")
            or item.get("date")
        )
        raw_url = (
            item.get("detail_url")
            or item.get("url")
            or item.get("href")
            or item.get("linkUrl")
            or item.get("contentUrl")
            or ""
        )
        detail_url = canonical_url(str(raw_url), "https://www.qianlima.com/")
        content_id = str(item.get("contentid") or item.get("contentId") or item.get("id") or "").strip()
        if not detail_url and content_id and publish_date:
            date_part = publish_date[:10].replace("-", "")
            detail_url = f"https://www.qianlima.com/zb/detail/{date_part}_{content_id}.html"
        if not detail_url:
            return
        if publish_date and not within_recent_days(publish_date, self.config.recent_days):
            return
        if not title:
            title = clean_text(str(item.get("keywords") or item.get("keyword") or detail_url))
        if keyword.lower() not in f"{title} {snippet} {detail_url}".lower() and not topic_relevant(title, snippet):
            return
        row = dict(item)
        row["detail_url"] = detail_url
        row["title"] = title
        row["publish_date"] = publish_date
        row["snippet"] = clean_text(f"{title} {snippet} {publish_date}")
        records.append(row)

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
        html = decode_response_body(body, encoding)
        if looks_blocked(html):
            self.blocked_sites.add(site.name)
            self.mark_site_issue(site, "blocked_or_rate_limited", "search page returned anti-bot or rate-limit page")
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
        html = decode_response_body(body, encoding)
        if looks_blocked(html):
            self.blocked_sites.add(site.name)
            self.mark_site_issue(site, "blocked_or_rate_limited", "index page returned anti-bot or rate-limit page")
            print(f"[warn] {site.name} index appears blocked/rate-limited; skipping", file=sys.stderr)
            return []
        if looks_login_required(html, response.geturl()):
            self.mark_site_issue(site, "login_required", "index appears to require login or registration for useful results")
        return self.records_from_html_links(site, html, response.geturl(), keyword)

    def fetch_json_search_records(self, site: SiteConfig, keyword: str, page: int) -> list[dict[str, Any]]:
        request = self.build_search_request(site, keyword, page, "application/json,text/plain,*/*")
        try:
            with self.open_with_retries(request, label=f"{site.name} search keyword={keyword} page={page}") as response:
                payload = json.loads(decode_response_body(response.read(self.config.max_response_bytes), response.headers.get_content_charset()))
        except Exception as exc:
            print(f"[warn] {site.name} json search failed after retries: keyword={keyword} page={page} ({exc})", file=sys.stderr)
            return []
        records = self.records_from_json_payload(site, payload)
        return [record for record in records if self.record_matches_site(site, record, keyword)]

    def records_from_json_payload(self, site: SiteConfig, payload: Any) -> list[dict[str, Any]]:
        candidate_paths = [site.json_records_path] if site.json_records_path else []
        candidate_paths.extend([
            "data.records",
            "data.list",
            "data.rows",
            "data.result",
            "data.results",
            "data.items",
            "data.data",
            "data.page.records",
            "data.page.list",
            "result.records",
            "result.list",
            "result.data",
            "records",
            "list",
            "rows",
            "result",
            "results",
            "items",
            "data",
        ])
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
        if any(re.search(pattern, absolute_url, re.IGNORECASE) for pattern in IGNORED_URL_PATTERNS):
            return False
        url_date = date_from_url(absolute_url)
        if url_date and not within_recent_days(url_date, self.config.recent_days):
            return False
        haystack = f"{title} {absolute_url}"
        if title in IGNORED_LINK_TITLES or len(title) < 4:
            if not any(re.search(pattern, absolute_url) for pattern in site.include_url_patterns):
                return False
        if site.include_url_patterns and not any(re.search(pattern, absolute_url) for pattern in site.include_url_patterns):
            return False
        if site.exclude_url_patterns and any(re.search(pattern, absolute_url) for pattern in site.exclude_url_patterns):
            return False
        if keyword == "AI_SCAN":
            return contains_ai_keyword(haystack)
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

    def urls_from_records(self, records: list[dict[str, Any]], site: SiteConfig) -> list[tuple[str, str, str, str, str]]:
        results: list[tuple[str, str, str, str, str]] = []
        seen: set[str] = set()
        for record in records:
            title = clean_text(str(first_path_value(record, site.record_title_fields) or ""))
            publish_date = record_publish_date(record)
            customer_or_org = record_customer_or_org(record, title)
            snippet = clean_text(
                str(
                    record.get("snippet")
                    or record.get("publish_date")
                    or record.get("publishTime")
                    or record.get("category")
                    or ""
                )
            )
            raw = first_path_value(record, site.record_url_fields)
            url = canonical_url(str(raw or ""), site.base_url)
            if url and url not in seen:
                results.append((url, title, snippet, publish_date, customer_or_org))
                seen.add(url)
        return results

    def fetch_detail(self, candidate: dict[str, str]) -> dict[str, Any] | None:
        if candidate.get("site") == "cfcpn_com":
            detail = self.fetch_cfcpn_detail(candidate)
            if detail is not None:
                return detail
        if candidate.get("site") == "szygcgpt_com":
            detail = self.fetch_szygcgpt_detail(candidate)
            if detail is not None:
                return detail
        if candidate.get("site") == "cgo_tpre_cn":
            detail = self.fetch_tpre_cgo_detail(candidate)
            if detail is not None:
                return detail
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
                "title": candidate.get("title") or parser.title,
                "text": clean_text(
                    "\n".join(
                        [
                            candidate.get("title", ""),
                            f"招标单位：{candidate.get('customer_or_org', '')}" if candidate.get("customer_or_org") else "",
                            candidate.get("snippet", ""),
                            parser.text,
                        ]
                    )
                ),
                "links": [u for href, _ in parser.links if (u := canonical_url(href, url))],
                "publish_date": candidate.get("publish_date", ""),
                "customer_or_org": candidate.get("customer_or_org", ""),
            }
            if best is None or detail_score(page) > detail_score(best):
                best = page
        if best is not None:
            return best
        fallback_text = clean_text("\n".join([candidate.get("title", ""), candidate.get("snippet", "")]))
        if fallback_text:
            return {
                "url": candidate["url"],
                "source_url": candidate["url"],
                "title": candidate.get("title", ""),
                "text": clean_text(
                    "\n".join(
                        [
                            f"招标单位：{candidate.get('customer_or_org', '')}" if candidate.get("customer_or_org") else "",
                            fallback_text,
                        ]
                    )
                ),
                "links": [],
                "publish_date": candidate.get("publish_date", ""),
                "customer_or_org": candidate.get("customer_or_org", ""),
            }
        return best

    def fetch_cfcpn_detail(self, candidate: dict[str, str]) -> dict[str, Any] | None:
        parsed = urllib.parse.urlparse(candidate["url"])
        params = urllib.parse.parse_qs(parsed.query)
        notice_id = first_nonempty(
            {key: values[0] for key, values in params.items() if values},
            ["searchVal", "id", "noticeId"],
        )
        if not notice_id:
            return None
        endpoint = "http://www.cfcpn.com/jcw/noticeinfo/noticeInfo/dataNoticeList"
        request = urllib.request.Request(
            endpoint,
            data=urllib.parse.urlencode({"id": notice_id, "isDetail": "1"}).encode("utf-8"),
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "application/json,text/plain,*/*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": candidate["url"],
            },
            method="POST",
        )
        payload: Any = None
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                with self.open_with_retries(request, label=f"fetch {candidate['url']}") as response:
                    payload = json.loads(
                        decode_response_body(
                            response.read(self.config.max_response_bytes),
                            response.headers.get_content_charset(),
                        )
                    )
                break
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code != 403 or attempt == 3:
                    break
                time.sleep(2.5 * attempt)
            except Exception as exc:
                last_error = exc
                break
        if payload is None:
            print(f"[warn] cfcpn detail API failed: {candidate['url']} ({last_error})", file=sys.stderr)
            return cfcpn_candidate_fallback_page(candidate)
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            return cfcpn_candidate_fallback_page(candidate)
        row = rows[0]
        title = clean_text(str(row.get("noticeTitle") or candidate.get("title") or ""))
        content_html = str(row.get("noticeContent") or row.get("briefContent") or "")
        content_text = clean_text(html.unescape(content_html))
        attachments = cfcpn_attachment_names(row.get("file"))
        fields = [
            ("项目名称", title),
            ("项目编号", row.get("bidsNo")),
            ("采购人", row.get("userName")),
            ("采购方式", row.get("purchaseTypeName") or row.get("purchaseTypeLable")),
            ("地区", row.get("area")),
            ("发布时间", row.get("publishTime")),
            ("行业分类", row.get("yxCategoryNames")),
            ("公告标签", row.get("labelAllId")),
            ("附件", "；".join(attachments)),
        ]
        meta_text = "\n".join(f"{label}：{value_or_empty(value)}" for label, value in fields if value_or_empty(value))
        page_text = clean_text("\n".join([candidate.get("snippet", ""), meta_text, content_text]))
        if not page_text:
            return None
        return {
            "url": candidate["url"],
            "source_url": candidate["url"],
            "title": title or candidate.get("title", ""),
            "text": page_text,
            "links": [],
            "publish_date": candidate.get("publish_date") or normalize_publish_time(row.get("publishTime")),
            "customer_or_org": record_customer_or_org(row, title) or candidate.get("customer_or_org", ""),
        }

    def fetch_szygcgpt_detail(self, candidate: dict[str, str]) -> dict[str, Any] | None:
        parsed = urllib.parse.urlparse(candidate["url"])
        query = urllib.parse.parse_qs(parsed.query)
        params = {key: values[0] for key, values in query.items() if values}
        if not params:
            return None
        data_source = str(params.get("dataSource") or "0")
        endpoint_path = "app/etl/detail" if data_source == "1" else "app/home/detail.do"
        endpoint = urllib.parse.urljoin("https://www.szygcgpt.com/", endpoint_path)
        api_params = {
            key: params.get(key, "")
            for key in ["ggGuid", "bdGuid", "ggLeiXing", "guid"]
            if params.get(key)
        }
        if not api_params:
            return None
        api_url = endpoint + "?" + urllib.parse.urlencode(api_params)
        request = urllib.request.Request(
            api_url,
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "application/json,text/plain,*/*",
                "Referer": candidate["url"],
            },
        )
        try:
            with self.open_with_retries(request, label=f"fetch {candidate['url']}") as response:
                payload = json.loads(
                    decode_response_body(
                        response.read(self.config.max_response_bytes),
                        response.headers.get_content_charset(),
                    )
                )
        except Exception as exc:
            print(f"[warn] fetch failed after retries: {candidate['url']} ({exc})", file=sys.stderr)
            payload = None
        if payload is not None:
            text = clean_text(json.dumps(payload, ensure_ascii=False))
            if text:
                return {
                    "url": candidate["url"],
                    "source_url": candidate["url"],
                    "title": candidate.get("title", ""),
                    "text": clean_text("\n".join([candidate.get("snippet", ""), text])),
                    "links": [],
                    "publish_date": candidate.get("publish_date", ""),
                    "customer_or_org": candidate.get("customer_or_org", ""),
                }
        fallback_text = clean_text("\n".join([candidate.get("title", ""), candidate.get("snippet", "")]))
        if fallback_text:
            return {
                "url": candidate["url"],
                "source_url": candidate["url"],
                "title": candidate.get("title", ""),
                "text": fallback_text,
                "links": [],
                "publish_date": candidate.get("publish_date", ""),
                "customer_or_org": candidate.get("customer_or_org", ""),
            }
        return None

    def fetch_tpre_cgo_detail(self, candidate: dict[str, str]) -> dict[str, Any] | None:
        parsed = urllib.parse.urlparse(candidate["url"])
        params = urllib.parse.parse_qs(parsed.query)
        pk_id = first_nonempty({key: values[0] for key, values in params.items() if values}, ["pkId", "id"])
        if not pk_id:
            return None
        endpoint = "https://cgo.tpre.cn/cgo-portal-service/biz/purchase/notice/anmuas/details"
        api_url = endpoint + "?" + urllib.parse.urlencode({"pkId": pk_id})
        request = urllib.request.Request(
            api_url,
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://cgo.tpre.cn/cgo-portal-view/",
            },
        )
        try:
            with self.open_with_retries(request, label=f"fetch {candidate['url']}") as response:
                payload = json.loads(
                    decode_response_body(
                        response.read(self.config.max_response_bytes),
                        response.headers.get_content_charset(),
                    )
                )
        except Exception as exc:
            print(f"[warn] fetch failed after retries: {candidate['url']} ({exc})", file=sys.stderr)
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return None
        parser = PageParser()
        parser.feed(str(data.get("noticeDetail") or ""))
        fields = [
            ("项目名称", data.get("noticeTitle")),
            ("项目编号", data.get("purchaseCode")),
            ("来源渠道", data.get("source")),
            ("发布时间", data.get("publicTime")),
            ("开标时间", data.get("openBidTime")),
            ("截止投标时间", data.get("tenderEndTime") or data.get("bidEndTime")),
            ("公告类型", data.get("noticeType")),
            ("采购类型", data.get("purchaseType")),
        ]
        meta_text = "\n".join(f"{label}：{value_or_empty(value)}" for label, value in fields if value_or_empty(value))
        page_text = clean_text("\n".join([candidate.get("snippet", ""), meta_text, parser.text]))
        if not page_text:
            return None
        return {
            "url": candidate["url"],
            "source_url": candidate["url"],
            "title": clean_text(str(data.get("noticeTitle") or candidate.get("title") or "")),
            "text": page_text,
            "links": [],
            "publish_date": candidate.get("publish_date") or normalize_publish_time(data.get("publicTime")),
            "customer_or_org": record_customer_or_org(data, clean_text(str(data.get("noticeTitle") or candidate.get("title") or ""))) or candidate.get("customer_or_org", ""),
        }

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
        html = decode_response_body(body, encoding)
        if looks_blocked(html):
            print(f"[warn] fetch appears blocked/rate-limited: {url}", file=sys.stderr)
            return None
        return {"html": html}

    def analyze(self, page: dict[str, Any]) -> dict[str, Any] | None:
        if self.config.ai.enabled:
            return self.analyze_with_ai(page)
        return rule_analysis(page)

    def is_outside_recent_window(self, page: dict[str, Any], analysis: dict[str, Any] | None = None) -> bool:
        return is_outside_recent_window(page, analysis, self.config.recent_days)

    def analyze_with_ai(self, page: dict[str, Any]) -> dict[str, Any] | None:
        payload = {
            "url": page["url"],
            "title": page["title"],
            "text": page["text"][:12000],
            "ai_keywords": AI_KEYWORDS,
            "recent_days": self.config.recent_days,
            "industry_categories": INDUSTRY_CATEGORIES,
            "date_range": date_range_for_recent_days(self.config.recent_days),
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "你是招标采购公告抽取助手。只返回 JSON，不要解释。"
                    "任务是抽取AI相关商机，并做简单行业和产品匹配。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "判断网页是否和AI相关。只要项目标题或正文出现AI、人工智能、大模型、智能体、智能问答、智能客服、"
                    "算法、模型训练、机器学习、计算机视觉、语音识别等需求或结果，就视为AI相关商机，is_opportunity=true。"
                    "不要因为公告类型不标准、字段不完整、未匹配我司产品而判 false。"
                    "只有完全不是AI相关，或发布日期明确不在 date_range 内，才 is_opportunity=false。"
                    "返回 JSON 字段：is_opportunity(boolean), business_stage("
                    "tender_notice/procurement_notice/award_candidate/award_result/contract_notice/not_opportunity), "
                    "project_name, project_id, customer_or_org, deadline, procurement_scope, publish_date, reason, "
                    "organization_industry, company_relevance, matched_products, product_match_reason。"
                    "publish_date 是公告发布日期/发布时间，不是投标截止时间。deadline 是投标/响应截止或开标时间。"
                    "project_id 只能来自“项目编号、招标编号、交易编号、采购计划编号、合同编号”等明确标签；"
                    "正文没有明确编号就填 null，绝不能使用 URL、网页文件名、uuid、hash 或链接末尾乱码。"
                    "project_name 必须是完整项目名称，只抽“项目名称/采购项目名称/招标项目名称”后面的值；"
                    "不要包含“进行竞争性磋商采购、公告邀请、潜在供应商、项目基本情况、采购方式”等公告套话。"
                    "customer_or_org 填采购人、招标人、采购单位、招标单位、需求方、采购方、采购主体、项目业主、建设单位、发布单位等最终需求单位；不要填代理机构。"
                    "不要把“采购”“方式：竞争性磋商”“采购方式”当作 customer_or_org。"
                    "如果项目名称开头明显是公司/银行/医院/学校/政府单位名称，也可以作为 customer_or_org。"
                    "procurement_scope 用一句完整短句概括采购内容；不要只返回半句话，不要包含交货地点、联系方式、供应商资格等后续章节。"
                    "organization_industry 只能从以下行业中选一个："
                    + "、".join(INDUSTRY_CATEGORIES)
                    + "。company_relevance 只能填 high/medium/low/none。matched_products 只能从"
                    " VZOOM企业级AI智能体、VZOOM财税大模型、VZOOM AI中台 中选择；无匹配返回空数组。"
                    "智能体/Agent/智能问答/智能客服/流程自动化匹配企业级AI智能体；"
                    "财税、税务、会计、发票、报销、审计、财务核算匹配财税大模型；"
                    "算力、GPU、推理集群、AI平台、中台、模型服务、数据治理匹配AI中台。"
                    "product_match_reason 用一句话说明依据。"
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
            return rule_analysis(page)
        if not topic_relevant(page["title"], page["text"], analysis):
            return None
        if self.is_outside_recent_window(page, analysis):
            return None
        if str(analysis.get("business_stage", "")) not in FINAL_STAGES:
            analysis["business_stage"] = rule_stage(page["title"], page["text"])
        return analysis

    def build_record(self, page: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any] | None:
        if self.is_outside_recent_window(page, analysis):
            return None
        fallback = extract_fields(page["title"], page["text"])
        project_id = clean_project_id(
            analysis.get("project_id") or fallback.get("project_id"),
            page.get("title", ""),
            page.get("url", ""),
        )
        raw_matched_products = normalize_matched_products(analysis.get("matched_products"))
        matched_products = validate_matched_products(
            raw_matched_products,
            f"{page.get('title', '')} {page.get('text', '')}",
        )
        company_relevance = normalize_company_relevance(analysis.get("company_relevance"))
        if not matched_products and company_relevance in {"高", "中"}:
            company_relevance = "低"
        product_match_reason = value_or_empty(analysis.get("product_match_reason"))
        if raw_matched_products and matched_products and raw_matched_products != matched_products:
            product_match_reason = (
                f"产品列已按公告正文关键词校验为：{matched_products}。"
                + (f"原模型理由：{product_match_reason}" if product_match_reason else "")
            )
        project_name = clean_project_name(
            analysis.get("project_name") or fallback.get("project_name") or page["title"],
            page.get("title", ""),
            page.get("text", ""),
        )
        customer_or_org = select_best_org_name(
            analysis.get("customer_or_org"),
            fallback.get("customer_or_org"),
            page.get("customer_or_org"),
            extract_customer_or_org(project_name, page.get("text", "")),
            infer_org_from_title(project_name),
        )
        procurement_scope = clean_procurement_scope(
            analysis.get("procurement_scope") or fallback.get("procurement_scope"),
            project_name,
            page.get("text", ""),
        )
        record = {
            "招标单位": customer_or_org,
            "招标单位行业分类": normalize_industry(
                analysis.get("organization_industry")
                or infer_industry(
                    customer_or_org,
                    project_name,
                    page.get("text", ""),
                )
            ),
            "项目名称": project_name,
            "截止日期": normalize_datetime(analysis.get("deadline") or fallback.get("deadline")),
            "项目编号": project_id,
            "采购内容": procurement_scope,
            "源网址": page["url"],
            "我司业务相关度": company_relevance,
            "匹配产品": matched_products or "无明确匹配产品",
            "匹配理由": product_match_reason,
        }
        if not record.get("项目名称") or not record.get("源网址"):
            return None
        return record

    def write_opportunity_file(self, record: dict[str, Any]) -> str:
        source = str(record.get("源网址") or record.get("项目编号") or record.get("项目名称"))
        digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]
        path = os.path.join(self.config.output_dir, "opportunities", f"{digest}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"saved_at": utc_now(), "opportunity": record}, handle, ensure_ascii=False, indent=2)
        print(f"[opportunity] saved={path}")
        return path

    def write_outputs(self, records: list[dict[str, Any]], quiet: bool = False) -> None:
        os.makedirs(self.config.output_dir, exist_ok=True)
        json_path = os.path.join(self.config.output_dir, "opportunities_structured.json")
        csv_path = os.path.join(self.config.output_dir, "opportunities_summary.csv")

        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(records, handle, ensure_ascii=False, indent=2)
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_CSV_FIELDS)
            writer.writeheader()
            for record in records:
                writer.writerow(format_csv_record(record))
        if not quiet:
            print(f"[summary] json={json_path} csv={csv_path}")

    def write_run_status(self, status: str, details: dict[str, Any]) -> None:
        os.makedirs(self.config.output_dir, exist_ok=True)
        path = os.path.join(self.config.output_dir, "run_status.json")
        payload = {
            "saved_at": utc_now(),
            "status": status,
            "date_range": date_range_for_recent_days(self.config.recent_days),
            **details,
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        print(f"[status] saved={path}")

    def dedupe_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: dict[str, dict[str, Any]] = {}
        aliases: dict[str, str] = {}
        for record in records:
            keys = record_dedupe_keys(record)
            primary = next((aliases[key] for key in keys if key in aliases), keys[0])
            if primary not in deduped or record_quality(record) > record_quality(deduped[primary]):
                deduped[primary] = record
            for key in keys:
                aliases[key] = primary
        return sorted(deduped.values(), key=lambda r: str(r.get("截止日期", "")), reverse=True)


def detail_url_variants(url: str) -> list[str]:
    variants = [url]
    if "/information/deal/html/a/" in url:
        variants.insert(0, url.replace("/information/deal/html/a/", "/information/deal/html/b/", 1))
    elif "/information/deal/html/b/" in url:
        variants.append(url.replace("/information/deal/html/b/", "/information/deal/html/a/", 1))
    return list(dict.fromkeys(variants))


def candidate_login_urls(base_url: str, credential_url: str) -> list[str]:
    urls = [credential_url, base_url]
    parsed = urllib.parse.urlparse(base_url)
    root = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))
    if host_matches(parsed.netloc, "365trade.com.cn"):
        urls.extend(
            [
                "https://jy.365trade.com.cn/wb_bidder/static/dist/index",
                "https://jy.365trade.com.cn/wb_owner/static/dist/index",
                "https://jy.365trade.com.cn/zzlh/",
            ]
        )
    if host_matches(parsed.netloc, "cfcpn.com"):
        urls.extend(
            [
                "http://www.cfcpn.com/jcw/sys/index/goUrl?url=modules/sys/login/login",
                "http://www.cfcpn.com/jcw/sys/index/goUrl?url=modules/sys/login/index",
                "http://www.cfcpn.com/jcw/modules/sys/login/login",
            ]
        )
    paths = [
        "/login",
        "/login.html",
        "/user/login",
        "/user/login.html",
        "/member/login",
        "/member/login.html",
        "/passport/login",
        "/auth/login",
        "/sso/login",
    ]
    urls.extend(urllib.parse.urljoin(root, path) for path in paths)
    return list(dict.fromkeys(url for url in urls if canonical_url(url)))


def select_login_form(forms: list[dict[str, Any]]) -> dict[str, Any] | None:
    password_forms = [form for form in forms if any(item.get("type") == "password" for item in form.get("inputs", []))]
    if not password_forms:
        return None
    return max(password_forms, key=lambda form: len(form.get("inputs", [])))


def form_requires_verification(form: dict[str, Any], html: str) -> bool:
    if looks_verification_required(html):
        return True
    for item in form.get("inputs", []):
        haystack = " ".join(str(item.get(key, "")) for key in ["name", "id", "placeholder"]).lower()
        if any(marker in haystack for marker in ["captcha", "verify", "code", "sms", "validate"]):
            return True
        if any(marker in haystack for marker in ["验证码", "短信", "校验码"]):
            return True
    return False


def looks_non_manual_verification_required(html: str, form: dict[str, Any]) -> bool:
    haystack = html.lower()
    for item in form.get("inputs", []):
        haystack += " " + " ".join(str(item.get(key, "")) for key in ["name", "id", "placeholder"]).lower()
    markers = ["短信验证码", "手机验证码", "滑块", "拖动滑块", "sms", "slider", "slide", "geetest", "aliyun_waf"]
    return any(marker.lower() in haystack for marker in markers)


def verification_field_names(form: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in form.get("inputs", []):
        name = str(item.get("name") or "")
        if not name:
            continue
        input_type = str(item.get("type") or "text").lower()
        if input_type in {"hidden", "submit", "button", "password"}:
            continue
        haystack = " ".join(str(item.get(key, "")) for key in ["name", "id", "placeholder"]).lower()
        if any(marker in haystack for marker in ["captcha", "verify", "verifycode", "validcode", "checkcode", "code"]):
            names.append(name)
        elif any(marker in haystack for marker in ["验证码", "校验码"]):
            names.append(name)
    return list(dict.fromkeys(names))


def select_verification_image_url(form: dict[str, Any], html: str, page_url: str) -> str:
    images = list(form.get("images", []))
    candidates: list[str] = []
    for image in images:
        haystack = " ".join(str(image.get(key, "")) for key in ["src", "id", "class", "alt", "title"]).lower()
        if any(marker in haystack for marker in ["captcha", "verify", "valid", "check", "code", "验证码"]):
            candidates.append(str(image.get("src") or ""))
    if not candidates:
        for match in re.finditer(r"<img\b[^>]*\bsrc\s*=\s*([\"'])(.*?)\1[^>]*>", html, flags=re.IGNORECASE | re.S):
            tag = match.group(0).lower()
            src = match.group(2)
            if any(marker in tag for marker in ["captcha", "verify", "valid", "check", "code", "验证码"]):
                candidates.append(src)
    for src in candidates:
        url = canonical_url(src, page_url)
        if url:
            return url
    return ""


def extension_from_content_type(content_type: str) -> str:
    lowered = content_type.lower().split(";", 1)[0].strip()
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
        "image/bmp": ".bmp",
    }.get(lowered, "")


def build_login_payload(form: dict[str, Any], credential: SiteCredential) -> dict[str, str] | None:
    payload: dict[str, str] = {}
    inputs = [item for item in form.get("inputs", []) if item.get("name")]
    for item in inputs:
        input_type = str(item.get("type") or "text").lower()
        if input_type in {"submit", "button", "image", "file", "reset"}:
            continue
        payload[str(item["name"])] = str(item.get("value") or "")

    password_input = next((item for item in inputs if item.get("type") == "password"), None)
    username_input = select_username_input(inputs)
    if not password_input or not username_input:
        return None
    payload[str(username_input["name"])] = credential.username
    payload[str(password_input["name"])] = credential.password
    return payload


def select_username_input(inputs: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [
        item
        for item in inputs
        if item.get("name") and str(item.get("type") or "text").lower() in {"", "text", "email", "tel", "number", "hidden"}
    ]
    visible_candidates = [item for item in candidates if str(item.get("type") or "text").lower() != "hidden"]
    scored = [(username_field_score(item), item) for item in (visible_candidates or candidates)]
    scored = [pair for pair in scored if pair[0] > 0]
    if scored:
        return max(scored, key=lambda pair: pair[0])[1]
    return visible_candidates[0] if visible_candidates else None


def username_field_score(item: dict[str, Any]) -> int:
    haystack = " ".join(str(item.get(key, "")) for key in ["name", "id", "placeholder"]).lower()
    score = 0
    for marker in ["username", "user_name", "loginname", "login_name", "account", "userid", "user", "mobile", "phone", "email"]:
        if marker in haystack:
            score += 10
    for marker in ["账号", "用户名", "手机号", "手机", "邮箱", "账户"]:
        if marker in haystack:
            score += 10
    if "name" in haystack:
        score += 2
    return score


def login_response_success(
    response_text: str,
    final_url: str,
    login_url: str,
    cookie_jar: http.cookiejar.CookieJar,
) -> bool:
    lowered = response_text.lower()
    success_markers = ["退出", "注销", "个人中心", "用户中心", "我的", "logout", "sign out", "dashboard"]
    failure_markers = ["密码错误", "账号错误", "用户名或密码", "登录失败", "invalid password", "invalid username", "login failed"]
    if any(marker in lowered for marker in failure_markers):
        return False
    if any(marker.lower() in lowered for marker in success_markers):
        return True
    if urllib.parse.urlparse(final_url).path != urllib.parse.urlparse(login_url).path and not looks_login_required(response_text, final_url):
        return True
    return any(cookie.name.lower() in {"sid", "session", "sessionid", "jsessionid", "token", "auth_token"} for cookie in cookie_jar)


def detail_score(page: dict[str, Any]) -> int:
    text = str(page.get("text", ""))
    markers = ["预算", "最高限价", "金额", "采购人", "招标人", "联系人", "联系方式", "电话", "地址", "开标", "截止"]
    return len(text) + sum(1000 for marker in markers if marker in text)


def clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_html_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return clean_text(text)


def cfcpn_attachment_names(value: Any) -> list[str]:
    raw = value_or_empty(value)
    if not raw or raw in {"]", "[]"}:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    names: list[str] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = value_or_empty(item.get("fileName") or item.get("name"))
        if name:
            names.append(name)
    return names


def cfcpn_candidate_fallback_page(candidate: dict[str, str]) -> dict[str, Any] | None:
    fallback_text = clean_text("\n".join([candidate.get("title", ""), candidate.get("snippet", "")]))
    if not fallback_text:
        return None
    return {
        "url": candidate["url"],
        "source_url": candidate["url"],
        "title": candidate.get("title", ""),
        "text": fallback_text,
        "links": [],
    }


def extract_embedded_json_objects(text: str, required_keys: list[str]) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    seen: set[str] = set()
    key_pattern = "|".join(re.escape(key) for key in required_keys)
    variants = [text]
    if '\\"' in text:
        variants.append(text.replace('\\"', '"').replace("\\/", "/"))
    for variant in variants:
        for match in re.finditer(rf'"(?:{key_pattern})"\s*:', variant):
            cursor = match.start()
            for _ in range(40):
                start = variant.rfind("{", 0, cursor)
                if start < 0:
                    break
                cursor = start
                raw = balanced_json_object(variant, start)
                if not raw or raw in seen:
                    continue
                try:
                    value = json.loads(raw)
                except Exception:
                    continue
                if isinstance(value, dict) and all(key in value for key in required_keys):
                    objects.append(value)
                    seen.add(raw)
                    break
    return objects


def balanced_json_object(text: str, start: int) -> str:
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


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
    return not text or text.lower() in {"null", "none", "暂无", "未明确", "未提供", "无"} or text == MISSING_CONFIRMATION


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
    return contains_ai_keyword(haystack)


def title_ai_relevant(title: str, keyword: str = "") -> bool:
    if keyword and keyword != "AI_SCAN" and contains_keyword(title, keyword):
        return True
    return contains_ai_keyword(title)


def contains_ai_keyword(text: str) -> bool:
    return any(contains_keyword(text, keyword) for keyword in AI_KEYWORDS)


def contains_keyword(text: str, keyword: str) -> bool:
    if not keyword:
        return False
    if keyword.lower() == "ai":
        lowered = text.lower()
        return bool(re.search(r"(?<![a-z0-9])ai(?![a-z0-9])", lowered)) or "openai" in lowered
    return keyword.lower() in text.lower()


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


def normalize_publish_time(value: Any) -> str:
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        try:
            number = int(float(str(value).strip()))
        except ValueError:
            number = 0
        if number > 10_000_000_000:
            number //= 1000
        if number > 0:
            return datetime.fromtimestamp(number).strftime("%Y-%m-%d %H:%M:%S")
    return normalize_datetime(value)


def parse_date(value: Any) -> datetime | None:
    normalized = normalize_datetime(value)
    match = re.search(r"([0-9]{4})-([0-9]{2})-([0-9]{2})", normalized)
    if not match:
        return None
    return datetime(*(int(part) for part in match.groups()))


def recent_window(days: int) -> tuple[datetime, datetime]:
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=max(days, 1) - 1)
    start = datetime.combine(start_date, datetime.min.time())
    end = datetime.combine(end_date, datetime.max.time())
    return start, end


def date_from_url(url: str) -> str:
    match = re.search(r"/(20[0-9]{2})([01][0-9])([0-3][0-9])(?:[_/.-]|$)", url)
    if not match:
        return ""
    year, month, day = match.groups()
    return f"{year}-{month}-{day}"


def within_recent_days(value: Any, days: int) -> bool:
    date = parse_date(value)
    if date is None:
        return False
    start, end = recent_window(days)
    return start <= date <= end


def is_before_recent_window(value: Any, days: int) -> bool:
    date = parse_date(value)
    if date is None:
        return False
    start, _ = recent_window(days)
    return date < start


def record_publish_date(record: dict[str, Any]) -> str:
    fields = [
        "publish_date",
        "publishDate",
        "publishTime",
        "faBuTime",
        "createTime",
        "publicTime",
        "docRelTime",
        "webdate",
        "infodate",
        "pubinwebdate",
        "startdate",
        "date",
    ]
    value = first_path_value(record, fields)
    date = normalize_publish_time(value)
    if date:
        return date
    text = clean_text(
        " ".join(
            str(record.get(key) or "")
            for key in ["snippet", "title", "noticeTitle", "ggName", "customtitle", "docTitle"]
        )
    )
    return extract_publish_date_from_text(text)


def record_customer_or_org(record: dict[str, Any], title: str = "") -> str:
    value = first_path_value(record, ORG_FIELD_CANDIDATES)
    org = clean_org_name(value)
    if org:
        return org
    text = clean_text(
        " ".join(
            str(record.get(key) or "")
            for key in [
                "snippet",
                "content",
                "announcement",
                "noticeContent",
                "briefContent",
                "title",
                "noticeTitle",
                "ggName",
                "customtitle",
                "docTitle",
            ]
        )
    )
    return extract_customer_or_org(title or str(record.get("title") or ""), text)


def extract_publish_date_from_text(text: str) -> str:
    content = clean_text(text)
    patterns = [
        r"(?:公告日期|发布日期|发布时间|发布日|公示日期|公示时间|发出日期|发布于)[：:\s]*([0-9]{4}[-年/.][0-9]{1,2}[-月/.][0-9]{1,2}[日]?(?:\s*[0-9]{1,2}[:：时][0-9]{0,2}分?)?)",
        r"(?:datePublished|publishTime|publishDate)[\"'：:\s=]+([0-9]{4}[-/][0-9]{1,2}[-/][0-9]{1,2}(?:[ T][0-9]{1,2}:[0-9]{1,2}(?::[0-9]{1,2})?)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, content, flags=re.IGNORECASE)
        if match:
            date = normalize_publish_time(match.group(1))
            if date:
                return date
    return ""


def page_publish_date_candidates(page: dict[str, Any], analysis: dict[str, Any] | None = None) -> list[str]:
    values = [
        page.get("publish_date"),
        date_from_url(str(page.get("url") or "")),
        date_from_url(str(page.get("source_url") or "")),
    ]
    if analysis:
        values.append(analysis.get("publish_date"))
    fallback = extract_fields(str(page.get("title") or ""), str(page.get("text") or ""))
    values.extend([fallback.get("publish_date"), extract_publish_date_from_text(str(page.get("text") or ""))])
    dates: list[str] = []
    seen: set[str] = set()
    for value in values:
        date = normalize_publish_time(value)
        key = date[:10]
        if date and key not in seen:
            dates.append(date)
            seen.add(key)
    return dates


def is_outside_recent_window(page: dict[str, Any], analysis: dict[str, Any] | None, days: int) -> bool:
    publish_dates = page_publish_date_candidates(page, analysis)
    if publish_dates:
        return not any(within_recent_days(date, days) for date in publish_dates)
    return False


def extract_customer_or_org(title: str, text: str) -> str:
    content = re.sub(r"\s+", " ", f"{title} {text}").strip()
    labels = [
        "采购人名称",
        "采购人",
        "招标人名称",
        "招标人",
        "采购单位名称",
        "采购单位",
        "招标单位名称",
        "招标单位",
        "需求单位",
        "需求方",
        "采购方",
        "采购主体",
        "项目业主",
        "项目单位",
        "建设单位",
        "业主单位",
        "委托单位",
        "征集人",
        "比选人",
        "询价人",
        "发布单位",
        "发包人",
        "实施单位",
        "用户单位",
    ]
    stop_labels = [
        "采购代理机构",
        "代理机构",
        "项目名称",
        "项目编号",
        "地址",
        "联系方式",
        "联系人",
        "电话",
        "预算",
        "最高限价",
        "开标",
        "截止",
        "邮箱",
        "网址",
    ]
    label_pattern = "|".join(re.escape(label) for label in labels)
    stop_pattern = "|".join(re.escape(label) for label in stop_labels)
    patterns = [
        rf"(?:{label_pattern})[：:\s]*([^。；;，,\n]{{2,100}}?)(?=\s*(?:{stop_pattern})[：:\s]*|$)",
        rf"(?:{label_pattern})[：:\s]*([\u4e00-\u9fffA-Za-z0-9（）()·\-]{{2,80}})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, content, flags=re.IGNORECASE):
            org = clean_org_name(match.group(1))
            if org:
                return org
    return infer_org_from_title(title)


def infer_org_from_title(title: Any) -> str:
    text = clean_text(value_or_empty(title))
    if not text:
        return ""
    text = re.sub(r"^(?:关于|中华人民共和国)", "", text).strip()
    suffixes = (
        "有限责任公司",
        "股份有限公司",
        "集团有限公司",
        "有限公司",
        "集团",
        "分公司",
        "总公司",
        "银行",
        "证券",
        "保险",
        "医院",
        "大学",
        "学院",
        "学校",
        "研究院",
        "研究所",
        "委员会",
        "管理局",
        "财政局",
        "公安局",
        "中心",
        "公司",
    )
    suffix_pattern = "|".join(re.escape(suffix) for suffix in suffixes)
    patterns = [
        rf"^([\u4e00-\u9fffA-Za-z0-9（）()·\-]{{2,45}}(?:{suffix_pattern}))(?=的|关于|采购|招标|项目|AI|人工智能|大模型|智能|系统|平台|服务|设备|软件|硬件|生产|机房|续租|办公|OA|[-—_（(])",
        r"^([\u4e00-\u9fff]{3,18})(?=AI|人工智能|大模型|智能客服|智能问答|智能体|算力|模型)",
        rf"^([\u4e00-\u9fffA-Za-z0-9（）()·]{{3,35}}?)(?:-|—|_).*(?:采购|招标|项目|服务|设备|系统|平台)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            org = clean_org_name(match.group(1))
            if org:
                return org
    return ""


def clean_org_name(value: Any) -> str:
    org = clean_text(value_or_empty(value))
    if not org:
        return ""
    org = html.unescape(org)
    org = re.sub(r"<[^>]+>", "", org)
    org = re.split(r"(?:采购代理机构|代理机构|联系人|联系方式|联系电话|电话|地址|邮箱|项目名称|项目编号|预算金额|最高限价)[：:\s]", org)[0]
    org = org.strip(" ：:，,。；;、（）()[]【】\"'")
    org = re.sub(r"^(?:名称|单位|为|是|：|:)\s*", "", org)
    org = re.sub(r"\s+", "", org)
    if not org or len(org) < 2 or len(org) > 80:
        return ""
    generic_values = {
        "采购",
        "招标",
        "采购人",
        "招标人",
        "采购单位",
        "招标单位",
        "采购方",
        "需求方",
        "项目业主",
        "方式",
        "竞争性磋商",
        "竞争性谈判",
        "公开招标",
        "询价",
    }
    if org in generic_values or org.startswith(("式：", "方式：", "采购方式", "招标方式")):
        return ""
    bad_markers = ["详见", "见附件", "未知", "前往官网", "http", "www.", "@", "竞争性磋商", "竞争性谈判", "委托", "以下简称", "招标代理"]
    if any(marker.lower() in org.lower() for marker in bad_markers):
        return ""
    if re.fullmatch(r"[0-9A-Za-z_\-./]+", org):
        return ""
    if org.endswith("项目") and not re.search(r"(?:有限公司|公司|银行|医院|学校|学院|大学|中心)$", org):
        return ""
    if len(re.findall(r"\d", org)) > 2 and not re.search(r"(?:有限公司|公司)$", org):
        return ""
    return org


def select_best_org_name(*values: Any) -> str:
    candidates: list[str] = []
    for value in values:
        org = clean_org_name(value)
        if org and org not in candidates:
            candidates.append(org)
    if not candidates:
        return ""

    def score(org: str) -> tuple[int, int]:
        suffix_score = 0
        if re.search(r"(?:有限责任公司|股份有限公司|集团有限公司|有限公司|分公司|总公司)$", org):
            suffix_score = 5
        elif re.search(r"(?:银行|证券|保险|医院|大学|学院|学校|委员会|管理局|中心)$", org):
            suffix_score = 3
        return (suffix_score, len(org))

    best = max(candidates, key=score)
    for org in candidates:
        if org != best and org in best:
            continue
        if best != org and best in org and score(org) >= score(best):
            best = org
    return best


def extract_project_name(title: str, text: str) -> str:
    content = re.sub(r"\s+", " ", f"{title} {text}").strip()
    labels = ["采购项目名称", "招标项目名称", "项目名称", "项目名称及编号", "采购标的", "标的名称"]
    stop_labels = [
        "项目编号",
        "采购项目编号",
        "招标编号",
        "采购人",
        "招标人",
        "采购单位",
        "采购方式",
        "预算金额",
        "最高限价",
        "合同履行期限",
        "采购需求",
        "采购内容",
        "获取采购文件",
        "响应文件提交",
        "投标截止",
        "开标时间",
    ]
    label_pattern = "|".join(re.escape(label) for label in labels)
    stop_pattern = "|".join(re.escape(label) for label in stop_labels)
    patterns = [
        rf"(?:{label_pattern})[：:\s]*([\s\S]{{2,180}}?)(?=\s*(?:\d+[、.．]|[一二三四五六七八九十]+[、.．]|{stop_pattern})[：:\s]*|$)",
        r"^(.{4,120}?)(?:招标公告|采购公告|竞争性磋商公告|竞争性谈判公告|询价公告|中标公告|成交公告|结果公告)",
    ]
    for pattern in patterns:
        match = re.search(pattern, content, flags=re.IGNORECASE)
        if match:
            name = clean_project_name(match.group(1), "", "")
            if name:
                return name
    return ""


def clean_project_name(value: Any, page_title: str = "", text: str = "") -> str:
    raw = clean_text(value_or_empty(value))
    labeled = ""
    if text or page_title:
        labeled = extract_project_name(page_title, text) if value_or_empty(value) != "__from_extract__" else ""
    candidates = [labeled, raw, clean_text(value_or_empty(page_title))]
    for candidate in candidates:
        name = normalize_project_name(candidate)
        if is_good_project_name(name):
            return name
    return normalize_project_name(raw or page_title)


def normalize_project_name(value: Any) -> str:
    name = clean_text(value_or_empty(value))
    if not name:
        return ""
    if "项目名称" in name:
        parts = re.split(r"(?:采购项目名称|招标项目名称|项目名称)[：:\s]*", name)
        name = parts[-1] if parts else name
    name = re.split(
        r"\s*(?:\d+[、.．]|[一二三四五六七八九十]+[、.．])\s*(?:采购项目编号|项目编号|采购人|招标人|采购方式|预算金额|最高限价|采购需求|采购内容|交货地点|合同履行期限)[：:\s]*",
        name,
    )[0]
    name = re.split(
        r"\s+(?:采购项目编号|项目编号|采购人|招标人|采购单位|招标单位|采购方式|预算金额|最高限价|采购需求|采购内容|交货地点|合同履行期限)[：:\s]*",
        name,
    )[0]
    name = re.sub(r"^(?:一、|二、|三、|四、|采购项目基本概况|项目基本情况|基本概况)\s*", "", name)
    name = re.sub(r"^(?:现采用\s*公告邀请\s*方式.*?采购活动。)\s*", "", name)
    name = re.sub(r"(?:\s*[一二三四五六七八九十]+[、.．]\s*|\s*\d+[、.．]\s*)$", "", name)
    name = re.sub(r"\s+[0-9]+[.．]\s*Project\s+No[.]?\s*/?$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+[0-9]+[.．]\s*(?:Project|项目)?$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+[0-9]+(?:[.．][0-9]+)+\s*(?:招标代理|采购代理|代理机构).*$", "", name, flags=re.IGNORECASE)
    name = name.strip(" ：:，,。；;、（）()[]【】\"'")
    return name[:140].rstrip(" ，,；;。")


def is_good_project_name(value: Any) -> bool:
    name = value_or_empty(value)
    if len(name) < 4 or len(name) > 140:
        return False
    dirty_markers = [
        "竞争性磋商采购活动",
        "公告邀请",
        "潜在供应商",
        "获取采购文件",
        "提交响应文件",
        "采购项目基本概况",
        "项目基本情况",
        "采购方式：",
        "采购方式:",
    ]
    if any(marker in name for marker in dirty_markers):
        return False
    if re.fullmatch(r"[0-9A-Za-z_\-+]+", name):
        return False
    return True


def clean_procurement_scope(value: Any, project_name: str = "", text: str = "") -> str:
    raw = clean_text(value_or_empty(value))
    if not raw:
        raw = extract_labeled_section(
            clean_text(text),
            ["采购内容", "采购需求", "项目内容", "招标范围", "采购范围", "服务内容", "建设内容", "项目概况", "简要规格描述"],
            max_length=220,
        )
    scope = normalize_procurement_scope(raw)
    if scope:
        return scope
    name = normalize_project_name(project_name)
    if is_good_project_name(name):
        return name
    return ""


def normalize_procurement_scope(value: Any) -> str:
    scope = clean_text(value_or_empty(value))
    if not scope:
        return ""
    scope = re.sub(r"\s+[0-9]+[.．]\s*Project\s+No[.]?\s*/?$", "", scope, flags=re.IGNORECASE)
    scope = re.sub(r"\s+[0-9]+[.．]\s*(?:Project|项目)?$", "", scope, flags=re.IGNORECASE)
    scope = re.split(
        r"\s*(?:\d+[、.．]|[一二三四五六七八九十]+[、.．])\s*(?:交货地点|服务期限|合同履行期限|供应商资格|申请人资格|获取采购文件|响应文件提交|开标时间|联系方式)[：:\s]*",
        scope,
    )[0]
    scope = re.sub(r"^(?:详见|见|具体详见)\s*", "", scope)
    scope = scope.strip(" ：:，,。；;、（）()[]【】\"'")
    if len(scope) < 6:
        return ""
    if re.fullmatch(r"(?:与)?(?:采购内容|采购需求|招标范围|服务内容|项目概况)(?:\s*[0-9.．]+)?", scope):
        return ""
    dirty_markers = ["潜在供应商应在", "获取采购文件", "提交响应文件", "北京时间", "项目基本情况"]
    if any(marker in scope for marker in dirty_markers) and len(scope) > 80:
        return ""
    return scope[:220].rstrip(" ，,；;。")


def extract_fields(title: str, text: str) -> dict[str, str]:
    content = re.sub(r"\s+", " ", f"{title} {text}").strip()

    def first(patterns: list[str]) -> str:
        for pattern in patterns:
            match = re.search(pattern, content, flags=re.IGNORECASE)
            if match:
                return re.sub(r"\s+", " ", match.group(1)).strip(" ：:，,。；;")
        return ""

    return {
        "project_name": extract_project_name(title, text),
        "project_id": extract_project_id(content),
        "customer_or_org": extract_customer_or_org(title, text),
        "deadline": first([r"(?:投标截止时间|响应文件提交截止时间|开标时间|截止时间|截止日期)[：:\s]*([0-9]{4}[-年/][0-9]{1,2}[-月/][0-9]{1,2}[日]?(?:\s*[0-9]{1,2}[:：时][0-9]{0,2}分?)?)"]),
        "publish_date": first([r"(?:公告日期|发布日期|发布时间|公示时间)[：:\s]*([0-9]{4}[-年/][0-9]{1,2}[-月/][0-9]{1,2}[日]?)"]),
        "procurement_scope": extract_labeled_section(
            content,
            ["采购内容", "采购需求", "项目内容", "招标范围", "采购范围", "服务内容", "建设内容", "项目概况"],
            max_length=260,
        ),
    }


def extract_project_id(content: str) -> str:
    labels = [
        "采购项目编号",
        "项目编号",
        "招标项目编号",
        "招标编号",
        "交易编号",
        "采购计划编号",
        "政府采购计划编号",
        "计划编号",
        "招标公告编号",
        "合同编号",
        "标段编号",
        "标段（包）编号",
        "标段(包)编号",
        "包号",
    ]
    label_pattern = "|".join(re.escape(label) for label in labels)
    patterns = [
        rf"(?:{label_pattern})[：:\s]*([A-Za-z0-9][A-Za-z0-9\-_/（）().\[\]【】]+)",
        rf"(?:{label_pattern})[：:\s]*([^\s。；;，,、<>《》]{{2,80}})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, content, flags=re.IGNORECASE):
            cleaned = clean_project_id(match.group(1))
            if cleaned:
                return cleaned
    return ""


def clean_project_id(value: Any, title: Any = "", source_url: str = "") -> str:
    raw = value_or_empty(value)
    if not raw:
        return ""
    candidate = clean_text(raw)
    candidate = re.sub(r"^(?:项目编号|采购项目编号|招标项目编号|招标编号|交易编号|采购计划编号|政府采购计划编号|计划编号|招标公告编号|合同编号|标段编号|标段（包）编号|标段\(包\)编号|包号)[：:\s]*", "", candidate)
    candidate = candidate.strip(" ：:，,。；;、（）()[]【】")
    candidate = candidate.split()[0].strip(" ：:，,。；;、（）()[]【】")
    candidate = re.sub(r"(?:\.html?|\.shtml?)$", "", candidate, flags=re.IGNORECASE)
    if not candidate or is_bad_project_id(candidate, source_url):
        return ""
    if title and candidate == value_or_empty(title):
        return ""
    return candidate


def is_bad_project_id(candidate: str, source_url: str = "") -> bool:
    lowered = candidate.lower()
    if lowered in {"null", "none", "无", "暂无", "详见公告", "详见附件", "不详"}:
        return True
    if "http://" in lowered or "https://" in lowered:
        return True
    if candidate.count("/") >= 3 or candidate.startswith(("/", "\\")):
        return True
    if len(candidate) > 80:
        return True
    compact = re.sub(r"[^0-9A-Za-z]", "", candidate)
    if len(compact) >= 24 and re.fullmatch(r"[0-9a-fA-F]+", compact):
        return True
    if len(compact) >= 30 and re.fullmatch(r"[0-9A-Za-z]+", compact):
        return True
    if source_url:
        basename = os.path.splitext(os.path.basename(urllib.parse.urlparse(source_url).path))[0]
        if basename and candidate.lower() == basename.lower():
            return True
    return False


def extract_labeled_section(content: str, labels: list[str], max_length: int = 240) -> str:
    stop_labels = [
        "项目编号",
        "项目名称",
        "采购人",
        "招标人",
        "采购单位",
        "代理机构",
        "预算金额",
        "最高限价",
        "合同履行期限",
        "提交投标文件截止时间",
        "投标截止时间",
        "开标时间",
        "获取招标文件",
        "联系方式",
        "联系人",
        "地址",
    ]
    label_pattern = "|".join(re.escape(label) for label in labels)
    stop_pattern = "|".join(re.escape(label) for label in stop_labels)
    pattern = rf"(?:{label_pattern})[：:\s]*([\s\S]{{4,{max_length * 2}}}?)(?=\s*(?:{stop_pattern})[：:\s]*|$)"
    match = re.search(pattern, content, flags=re.IGNORECASE)
    if not match:
        return ""
    value = clean_text(match.group(1))
    value = re.sub(r"^(详见|见|具体详见)\s*", "", value)
    return value[:max_length].rstrip(" ，,；;。")


def format_csv_record(record: dict[str, Any]) -> dict[str, str]:
    aliases = {
        "招标单位": ["招标单位", "采购单位名称", "采购单位", "customer_or_org"],
        "招标单位行业分类": ["招标单位行业分类", "organization_industry", "行业分类"],
        "截止日期": ["截止日期", "截止时间", "deadline"],
        "采购内容": ["采购内容", "采购内容/范围", "采购范围", "procurement_scope"],
        "源网址": ["源网址", "来源链接", "url"],
        "我司业务相关度": ["我司业务相关度", "company_relevance"],
        "匹配产品": ["匹配产品", "matched_products"],
        "匹配理由": ["匹配理由", "product_match_reason"],
    }
    row: dict[str, str] = {}
    for field in OUTPUT_CSV_FIELDS:
        value = record.get(field)
        for alias in aliases.get(field, []):
            if not is_missing(value):
                break
            value = record.get(alias)
        if field == "匹配产品" and is_missing(value):
            row[field] = "无明确匹配产品"
        else:
            row[field] = csv_cell(value)
    return row


def csv_cell(value: Any) -> str:
    text = value_or_empty(value)
    return text if text else MISSING_CONFIRMATION


def normalize_industry(value: Any) -> str:
    text = value_or_empty(value)
    if not text:
        return ""
    normalized = text.replace("行业", "").strip(" ：:，,。；;")
    alias_map = {
        "医疗": "医疗卫生",
        "卫生": "医疗卫生",
        "医药": "医疗卫生",
        "政府": "政府/政务",
        "政务": "政府/政务",
        "公共服务": "政府/政务",
        "制造": "能源/制造",
        "能源": "能源/制造",
        "工业": "能源/制造",
        "交通": "交通物流",
        "物流": "交通物流",
        "科技": "互联网/科技",
        "互联网": "互联网/科技",
        "软件": "互联网/科技",
        "地产": "建筑地产",
        "建筑": "建筑地产",
        "水利": "农林水利",
        "农业": "农林水利",
        "文旅": "文旅传媒",
        "传媒": "文旅传媒",
        "文化旅游": "文旅传媒",
    }
    if normalized in INDUSTRY_CATEGORIES:
        return normalized
    for key, category in alias_map.items():
        if key in normalized:
            return category
    return "其他"


def infer_industry(org: Any, project_name: Any = "", text: Any = "") -> str:
    content = clean_text(" ".join([value_or_empty(org), value_or_empty(project_name), value_or_empty(text)[:1500]]))
    rules = [
        ("金融", ["银行", "证券", "保险", "信托", "基金", "金融", "农商行", "农信", "交易所", "银联"]),
        ("医疗卫生", ["医院", "卫生院", "卫健", "医保", "疾控", "医学", "医疗", "诊疗", "中医", "病理"]),
        ("教育", ["学校", "学院", "大学", "教育局", "教体局", "职业技术", "中学", "小学", "幼儿园", "课程", "实训"]),
        ("政府/政务", ["人民政府", "政务", "公安", "法院", "检察", "司法", "财政局", "住建局", "自然资源", "管理局", "委员会", "事业单位"]),
        ("能源/制造", ["电力", "能源", "煤", "石油", "燃气", "制造", "工业", "工厂", "矿", "电网"]),
        ("交通物流", ["交通", "铁路", "机场", "航空", "港口", "物流", "公交", "高速", "轨道"]),
        ("互联网/科技", ["科技", "软件", "信息技术", "数据", "网络", "通信", "电信", "互联网", "云"]),
        ("建筑地产", ["地产", "房地产", "建筑", "建工", "工程局", "置业", "城投"]),
        ("农林水利", ["农业", "农村", "林业", "水利", "水务", "渔", "畜牧"]),
        ("文旅传媒", ["文旅", "文化", "旅游", "传媒", "广电", "博物馆", "图书馆", "融媒体"]),
    ]
    for category, markers in rules:
        if any(marker in content for marker in markers):
            return category
    return "其他"


def normalize_company_relevance(value: Any) -> str:
    text = value_or_empty(value).lower()
    mapping = {
        "high": "高",
        "medium": "中",
        "mid": "中",
        "low": "低",
        "none": "无",
        "高": "高",
        "中": "中",
        "低": "低",
        "无": "无",
        "不相关": "无",
    }
    return mapping.get(text, value_or_empty(value))


def normalize_matched_products(value: Any) -> str:
    allowed = set(COMPANY_PRODUCTS)
    if isinstance(value, list):
        names = [str(item).strip() for item in value]
    else:
        names = re.split(r"[,，、;/；\s]+", value_or_empty(value))
    matched: list[str] = []
    for name in names:
        if not name:
            continue
        normalized = normalize_product_name(name)
        if normalized in allowed and normalized not in matched:
            matched.append(normalized)
    return "、".join(matched)


def normalize_product_name(value: str) -> str:
    text = value.strip()
    lowered = text.lower()
    if text in COMPANY_PRODUCTS:
        return text
    if "财税" in text or "tax" in lowered:
        return "VZOOM财税大模型"
    if "中台" in text or "算力" in text or "平台" in text or "gpu" in lowered:
        return "VZOOM AI中台"
    if "智能体" in text or "agent" in lowered:
        return "VZOOM企业级AI智能体"
    return text


def validate_matched_products(products: str, context: str) -> str:
    if not products:
        return ""
    text = context.lower()
    validators = {
        "VZOOM企业级AI智能体": [
            "智能体",
            "agent",
            "助手",
            "智能问答",
            "智能客服",
            "工作流",
            "信贷",
            "授信",
            "尽调",
            "贷后",
            "合规",
            "编码助手",
            "智能办公",
            "办公",
        ],
        "VZOOM财税大模型": [
            "财税",
            "税务",
            "会计",
            "发票",
            "报销",
            "审计",
            "财务核算",
            "税收",
            "纳税",
            "供应链关联",
            "财税知识",
        ],
        "VZOOM AI中台": [
            "算力",
            "gpu",
            "推理",
            "集群",
            "中台",
            "平台",
            "模型服务",
            "数据治理",
            "高并发",
            "公有云",
            "私有云",
            "大模型项目",
            "基础能力",
        ],
    }
    kept: list[str] = []
    for product in products.split("、"):
        product = product.strip()
        markers = validators.get(product)
        if markers and any(marker in text for marker in markers):
            kept.append(product)
    return "、".join(dict.fromkeys(kept))


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
        stage = "procurement_notice"
    fields = extract_fields(page["title"], page["text"])
    return {
        "is_opportunity": True,
        "business_stage": stage,
        **fields,
        "organization_industry": infer_industry(
            fields.get("customer_or_org"),
            fields.get("project_name") or page["title"],
            page["text"],
        ),
        **rule_company_product_match(page["title"], page["text"]),
    }


def rule_company_product_match(title: str, text: str) -> dict[str, Any]:
    content = f"{title} {text[:3000]}".lower()
    products: list[str] = []
    if any(marker in content for marker in ["财税", "税务", "会计", "发票", "供应链关联", "财务大模型"]):
        products.append("VZOOM财税大模型")
    if any(marker in content for marker in ["算力", "gpu", "推理", "集群", "中台", "ai平台", "大模型平台", "数据治理", "服务器"]):
        products.append("VZOOM AI中台")
    if any(marker in content for marker in ["智能体", "agent", "智能问答", "智能客服", "编码助手", "办公助手", "工作流", "信贷", "投资", "合规"]):
        products.append("VZOOM企业级AI智能体")
    products = list(dict.fromkeys(products))
    if not products:
        return {
            "company_relevance": "low",
            "matched_products": [],
            "product_match_reason": "规则判断仅识别到AI相关商机，未命中明确产品关键词，需销售进一步确认。",
        }
    relevance = "high" if len(products) == 1 else "medium"
    return {
        "company_relevance": relevance,
        "matched_products": products,
        "product_match_reason": f"规则关键词匹配到：{'、'.join(products)}。",
    }


def record_quality(record: dict[str, Any]) -> int:
    score = sum(1 for value in record.values() if not is_missing(value))
    if clean_project_id(record.get("项目编号"), record.get("项目名称"), record.get("源网址")):
        score += 4
    if not is_missing(record.get("截止日期")):
        score += 2
    if not is_missing(record.get("采购内容")):
        score += 2
    source_url = value_or_empty(record.get("源网址"))
    if "goUrl" not in source_url and "login" not in source_url.lower():
        score += 1
    return score


def record_dedupe_keys(record: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    project_id = clean_project_id(record.get("项目编号"), record.get("项目名称"), record.get("源网址"))
    if project_id:
        keys.append(f"id:{project_id.lower()}")
    title_key = normalize_dedupe_title(record.get("项目名称"))
    if title_key:
        keys.append(f"title:{title_key}")
    source_url = value_or_empty(record.get("源网址"))
    if source_url:
        keys.append(f"url:{source_url}")
    return keys or [f"row:{hashlib.sha1(json.dumps(record, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()}"]


def normalize_dedupe_title(value: Any) -> str:
    title = clean_text(value_or_empty(value)).lower()
    if not title:
        return ""
    title = re.sub(r"[【】\\[\\]（）()\\s]+", "", title)
    title = re.sub(
        r"(?:招标公告|采购公告|竞争性磋商公告|竞争性谈判公告|询价公告|中标结果公示|中标结果公告|"
        r"成交结果公告|成交公告|结果公告|中标候选人公示|成交候选人公示|更正公告|变更公告|澄清公告)$",
        "",
        title,
    )
    return title


def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as handle:
        return Config.from_dict(json.load(handle))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mine AI-related tender/procurement opportunities.")
    parser.add_argument("--config", default="config.example.json")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--credentials-file", default=None)
    parser.add_argument("--manual-verification", action="store_true", help="Prompt for ordinary image captcha codes in the terminal.")
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
    if args.credentials_file is not None:
        config.credentials_file = args.credentials_file
    if args.manual_verification:
        config.manual_verification = True
    if args.check_ai:
        return 0 if check_ai_connection(config) else 1
    AITenderMiner(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
