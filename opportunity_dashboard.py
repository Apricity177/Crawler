#!/usr/bin/env python3
"""Lightweight web dashboard and SQLite history for mined opportunities."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import mimetypes
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = ROOT / "data"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "opportunities.db"
STATIC_DIR = ROOT / "dashboard_static"
CHINA_TZ = timezone(timedelta(hours=8))


def _text(record: dict[str, Any], *names: str) -> str:
    for name in names:
        value = record.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _local_date(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(CHINA_TZ).date().isoformat()
    except (TypeError, ValueError):
        return value[:10] if len(value) >= 10 else datetime.now(CHINA_TZ).date().isoformat()


def normalize_record(
    record: dict[str, Any], saved_at: str = "", source_task: str = ""
) -> dict[str, str]:
    source_url = _text(record, "源网址", "来源链接", "url")
    host = (urlparse(source_url).hostname or "未知渠道").lower()
    collected_at = saved_at or datetime.now(timezone.utc).isoformat()
    identity = source_url or "|".join(
        [_text(record, "项目编号"), _text(record, "项目名称"), _text(record, "招标单位", "采购单位")]
    )
    return {
        "id": hashlib.sha1(identity.encode("utf-8")).hexdigest(),
        "title": _text(record, "项目名称", "project_name"),
        "organization": _text(record, "招标单位", "采购单位名称", "采购单位", "customer_or_org"),
        "industry": _text(record, "招标单位行业分类", "organization_industry", "行业分类"),
        "project_id": _text(record, "项目编号", "project_id"),
        "deadline": _text(record, "截止日期", "截止时间", "deadline"),
        "content": _text(record, "采购内容", "采购内容/范围", "采购范围", "procurement_scope"),
        "source_url": source_url,
        "channel": host,
        "source_task": source_task,
        "relevance": _text(record, "与公司业务匹配度", "我司业务相关度", "company_relevance"),
        "products": _text(record, "匹配产品", "matched_products"),
        "reason": _text(record, "匹配理由", "product_match_reason"),
        "collected_at": collected_at,
        "collected_date": _local_date(collected_at),
        "raw_json": json.dumps(record, ensure_ascii=False, sort_keys=True),
    }


class OpportunityStore:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB_PATH, retention_days: int = 30) -> None:
        self.db_path = Path(db_path)
        self.retention_days = max(1, retention_days)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()
        self.prune_expired()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=20)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS opportunities (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    organization TEXT NOT NULL DEFAULT '',
                    industry TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    deadline TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    channel TEXT NOT NULL DEFAULT '',
                    source_task TEXT NOT NULL DEFAULT '',
                    relevance TEXT NOT NULL DEFAULT '',
                    products TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    collected_at TEXT NOT NULL,
                    collected_date TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_opportunities_date
                    ON opportunities(collected_date DESC, collected_at DESC);
                CREATE INDEX IF NOT EXISTS idx_opportunities_channel
                    ON opportunities(channel);
                CREATE INDEX IF NOT EXISTS idx_opportunities_relevance
                    ON opportunities(relevance);
                """
            )

    def upsert(self, record: dict[str, Any], saved_at: str = "", source_task: str = "") -> None:
        item = normalize_record(record, saved_at=saved_at, source_task=source_task)
        columns = list(item)
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{name}=excluded.{name}" for name in columns if name != "id")
        with self.connect() as connection:
            connection.execute(
                f"INSERT INTO opportunities ({', '.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates} "
                "WHERE excluded.collected_at >= opportunities.collected_at",
                [item[name] for name in columns],
            )

    def prune_expired(self) -> int:
        """Keep the dashboard focused on the most recent collection window."""
        cutoff = (datetime.now(CHINA_TZ).date() - timedelta(days=self.retention_days - 1)).isoformat()
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM opportunities WHERE collected_date < ?", (cutoff,))
        return cursor.rowcount

    def prune_directory(self, data_dir: str | os.PathLike[str]) -> int:
        """Remove raw opportunity files outside the same retention window."""
        cutoff = (datetime.now(CHINA_TZ).date() - timedelta(days=self.retention_days - 1)).isoformat()
        removed = 0
        for path in Path(data_dir).glob("*/opportunities/*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                saved_at = str(payload.get("saved_at") or "")
                if saved_at and _local_date(saved_at) < cutoff:
                    path.unlink()
                    removed += 1
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return removed

    def import_directory(self, data_dir: str | os.PathLike[str]) -> tuple[int, int]:
        imported = failed = 0
        for path in sorted(Path(data_dir).glob("*/opportunities/*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                record = payload.get("opportunity", payload)
                if not isinstance(record, dict):
                    raise ValueError("opportunity must be an object")
                self.upsert(record, str(payload.get("saved_at") or ""), path.parent.parent.name)
                imported += 1
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                failed += 1
                print(f"[dashboard] skip invalid file={path}: {exc}", file=sys.stderr)
        self.prune_expired()
        return imported, failed

    @staticmethod
    def _filters(params: dict[str, str]) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        query = params.get("q", "").strip()
        if query:
            terms = [term for term in query.split() if term]
            for term in terms:
                clauses.append(
                    "(title LIKE ? OR organization LIKE ? OR project_id LIKE ? OR content LIKE ? OR products LIKE ?)"
                )
                values.extend([f"%{term}%"] * 5)
        for key, column in (("channel", "channel"), ("relevance", "relevance"), ("industry", "industry"), ("task", "source_task")):
            if params.get(key):
                clauses.append(f"{column} = ?")
                values.append(params[key])
        if params.get("date_from"):
            clauses.append("collected_date >= ?")
            values.append(params["date_from"])
        if params.get("date_to"):
            clauses.append("collected_date <= ?")
            values.append(params["date_to"])
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", values

    def search(self, params: dict[str, str], paginate: bool = True) -> dict[str, Any]:
        where, values = self._filters(params)
        page = max(1, _integer(params.get("page"), 1))
        page_size = min(100, max(1, _integer(params.get("page_size"), 12)))
        order = " ORDER BY collected_date DESC, collected_at DESC, title ASC"
        with self.connect() as connection:
            total = int(connection.execute(f"SELECT COUNT(*) FROM opportunities{where}", values).fetchone()[0])
            sql = f"SELECT * FROM opportunities{where}{order}"
            query_values = list(values)
            if paginate:
                sql += " LIMIT ? OFFSET ?"
                query_values.extend([page_size, (page - 1) * page_size])
            rows = [dict(row) for row in connection.execute(sql, query_values).fetchall()]
        for row in rows:
            row.pop("raw_json", None)
        return {"items": rows, "total": total, "page": page, "page_size": page_size}

    def metadata(self, configured_sources: list[str] | None = None) -> dict[str, Any]:
        today = datetime.now(CHINA_TZ).date().isoformat()
        with self.connect() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0])
            today_count = int(
                connection.execute("SELECT COUNT(*) FROM opportunities WHERE collected_date = ?", (today,)).fetchone()[0]
            )
            channels = [
                {"name": row[0], "count": row[1]}
                for row in connection.execute(
                    "SELECT channel, COUNT(*) FROM opportunities GROUP BY channel ORDER BY COUNT(*) DESC, channel"
                )
            ]
            tasks = [
                {"name": row[0], "count": row[1]}
                for row in connection.execute(
                    "SELECT source_task, COUNT(*) FROM opportunities WHERE source_task != '' "
                    "GROUP BY source_task ORDER BY COUNT(*) DESC, source_task"
                )
            ]
            high = int(connection.execute("SELECT COUNT(*) FROM opportunities WHERE relevance = '高'").fetchone()[0])
            industries = [
                {"name": row[0], "count": row[1]}
                for row in connection.execute(
                    "SELECT industry, COUNT(*) FROM opportunities WHERE industry != '' GROUP BY industry ORDER BY COUNT(*) DESC, industry"
                )
            ]
        counts = {item["name"]: item["count"] for item in channels}
        sources = [{"url": url, "channel": (urlparse(url).hostname or "").lower(), "count": counts.get((urlparse(url).hostname or "").lower(), 0)} for url in (configured_sources or [])]
        return {"total": total, "today": today_count, "high_relevance": high, "channels": channels, "configured_sources": sources, "industries": industries, "tasks": tasks}


def persist_opportunity(
    record: dict[str, Any], output_dir: str = "", saved_at: str = "", db_path: str | os.PathLike[str] = DEFAULT_DB_PATH
) -> None:
    """Persist one crawler hit. Kept as a small public integration hook."""
    OpportunityStore(db_path).upsert(record, saved_at=saved_at, source_task=Path(output_dir).name)


def _integer(value: str | None, default: int) -> int:
    try:
        return int(value or default)
    except ValueError:
        return default


class DashboardHandler(BaseHTTPRequestHandler):
    store: OpportunityStore
    configured_sources: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        try:
            if parsed.path == "/api/opportunities":
                self.send_json(self.store.search(params))
            elif parsed.path == "/api/metadata":
                self.send_json(self.store.metadata(self.configured_sources))
            elif parsed.path == "/api/export.csv":
                self.send_csv(self.store.search(params, paginate=False)["items"])
            elif parsed.path in {"/", "/index.html"}:
                self.send_static(STATIC_DIR / "index.html")
            elif parsed.path.startswith("/static/"):
                target = (STATIC_DIR / parsed.path.removeprefix("/static/")).resolve()
                if STATIC_DIR.resolve() not in target.parents:
                    self.send_error(HTTPStatus.FORBIDDEN)
                else:
                    self.send_static(target)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_csv(self, rows: list[dict[str, Any]]) -> None:
        output = io.StringIO()
        fields = ["采集日期", "渠道", "招标单位", "项目名称", "项目编号", "截止日期", "采购内容", "与公司业务匹配度", "匹配产品", "源网址"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "采集日期": row["collected_date"], "渠道": row["channel"], "招标单位": row["organization"],
                    "项目名称": row["title"], "项目编号": row["project_id"], "截止日期": row["deadline"],
                    "采购内容": row["content"], "与公司业务匹配度": row["relevance"], "匹配产品": row["products"],
                    "源网址": row["source_url"],
                }
            )
        body = ("\ufeff" + output.getvalue()).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="opportunities.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[dashboard] {self.address_string()} {fmt % args}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Browse and search historical business opportunities.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--retention-days", type=int, default=30, help="Only retain this many recent collection days (default: 30).")
    parser.add_argument("--no-import", action="store_true", help="Do not import existing JSON files before serving.")
    parser.add_argument("--import-only", action="store_true", help="Import existing JSON files and exit.")
    parser.add_argument("--daily-at", help="Run the crawler once daily at HH:MM (China time), for example 08:30.")
    parser.add_argument("--crawler-config", default="config.example.json", help="Crawler configuration used with --daily-at.")
    parser.add_argument("--env-file", default=".env", help="Crawler environment file used with --daily-at.")
    return parser.parse_args(argv)


def parse_daily_at(daily_at: str) -> tuple[int, int]:
    try:
        hour, minute = (int(part) for part in daily_at.split(":", 1))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        raise ValueError("--daily-at must use 24-hour HH:MM format, for example 08:30") from None
    return hour, minute


def daily_crawler_loop(daily_at: str, crawler_config: str, env_file: str, store: OpportunityStore, data_dir: str) -> None:
    hour, minute = parse_daily_at(daily_at)
    command = [sys.executable, str(ROOT / "self_evolving_agent_crawler.py"), "--config", crawler_config, "--env-file", env_file]
    last_run_date = ""
    print(f"[scheduler] crawler will run every day at {hour:02d}:{minute:02d} China time")
    while True:
        now = datetime.now(CHINA_TZ)
        today = now.date().isoformat()
        if (now.hour, now.minute) == (hour, minute) and today != last_run_date:
            last_run_date = today
            print(f"[scheduler] starting daily crawler at {now.isoformat()}")
            try:
                subprocess.run(command, cwd=ROOT, check=False)
                store.prune_expired()
                removed = store.prune_directory(data_dir)
                if removed:
                    print(f"[scheduler] removed {removed} raw opportunity file(s) older than retention window")
            except OSError as exc:
                print(f"[scheduler] failed to start crawler: {exc}", file=sys.stderr)
        time.sleep(15)


def load_configured_sources(config_path: str) -> list[str]:
    """Return every configured site URL, including sites that have no hits yet."""
    try:
        payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[dashboard] cannot read crawler config={config_path}: {exc}", file=sys.stderr)
        return []
    values: list[Any] = list(payload.get("urls") or payload.get("seeds") or [])
    values.extend(site.get("base_url") for site in payload.get("sites", []) if isinstance(site, dict))
    sources: list[str] = []
    seen: set[str] = set()
    for value in values:
        url = str(value or "").strip()
        if url and url not in seen:
            sources.append(url)
            seen.add(url)
    return sources


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.retention_days <= 0:
        raise ValueError("--retention-days must be positive")
    store = OpportunityStore(args.db, retention_days=args.retention_days)
    removed = store.prune_directory(args.data_dir)
    if not args.no_import:
        imported, failed = store.import_directory(args.data_dir)
        print(f"[dashboard] history scanned={imported} invalid={failed} removed={removed} database={store.db_path}")
    if args.import_only:
        return 0
    if args.daily_at:
        parse_daily_at(args.daily_at)
        scheduler = threading.Thread(
            target=daily_crawler_loop,
            args=(args.daily_at, args.crawler_config, args.env_file, store, args.data_dir),
            daemon=True,
        )
        scheduler.start()
    handler = type("ConfiguredDashboardHandler", (DashboardHandler,), {"store": store, "configured_sources": load_configured_sources(args.crawler_config)})
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"[dashboard] open http://{args.host}:{args.port} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
