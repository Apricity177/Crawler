import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from opportunity_dashboard import OpportunityStore, normalize_record


class OpportunityDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = OpportunityStore(Path(self.tempdir.name) / "history.db")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_normalizes_channel_and_china_date(self) -> None:
        item = normalize_record(
            {"项目名称": "智能体项目", "源网址": "https://Example.COM/notices/1"},
            "2026-07-21T18:30:00+00:00",
            "daily",
        )
        self.assertEqual(item["channel"], "example.com")
        self.assertEqual(item["collected_date"], "2026-07-22")
        self.assertEqual(item["source_task"], "daily")

    def test_search_filters_and_deduplicates_by_url(self) -> None:
        first = {"项目名称": "AI 平台一期", "招标单位": "甲单位", "源网址": "https://a.example/1"}
        updated = {"项目名称": "AI 平台一期（更新）", "招标单位": "甲单位", "源网址": "https://a.example/1"}
        other = {"项目名称": "知识库项目", "招标单位": "乙单位", "源网址": "https://b.example/2"}
        self.store.upsert(first, "2026-07-20T01:00:00+00:00", "task-a")
        self.store.upsert(updated, "2026-07-21T01:00:00+00:00", "task-a")
        self.store.upsert(other, "2026-07-22T01:00:00+00:00", "task-b")

        self.assertEqual(self.store.metadata()["total"], 2)
        result = self.store.search({"q": "AI 甲单位", "channel": "a.example"})
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "AI 平台一期（更新）")

    def test_imports_existing_history_files(self) -> None:
        data_dir = Path(self.tempdir.name) / "data"
        target = data_dir / "source-a" / "opportunities" / "one.json"
        target.parent.mkdir(parents=True)
        target.write_text(
            json.dumps(
                {
                    "saved_at": "2026-07-22T01:00:00+00:00",
                    "opportunity": {"项目名称": "大模型采购", "源网址": "https://c.example/3"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        imported, failed = self.store.import_directory(data_dir)
        self.assertEqual((imported, failed), (1, 0))
        result = self.store.search({"task": "source-a"})
        self.assertEqual(result["total"], 1)

    def test_prunes_records_older_than_retention_window(self) -> None:
        old_time = (datetime.now().astimezone() - timedelta(days=31)).isoformat()
        self.store.upsert({"项目名称": "旧项目", "源网址": "https://old.example/1"}, old_time, "old")
        self.store.prune_expired()
        self.assertEqual(self.store.metadata()["total"], 0)


if __name__ == "__main__":
    unittest.main()
