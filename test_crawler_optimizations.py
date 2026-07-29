import unittest

from self_evolving_agent_crawler import (
    AITenderMiner,
    Config,
    canonical_attachment_url,
)


class FakeAI:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat_json(self, messages):
        self.calls.append(messages)
        return self.responses.pop(0)


def test_config(url: str = "https://example.com/") -> Config:
    return Config.from_dict(
        {
            "urls": [url],
            "days": 1,
            "credentials_file": "/tmp/nonexistent-tender-credentials.xlsx",
            "ai": {"enabled": True},
        }
    )


class CrawlerOptimizationTests(unittest.TestCase):
    def test_scan_capable_adapter_does_not_repeat_every_keyword(self):
        miner = AITenderMiner(test_config("http://www.cfcpn.com/plist/caigou"))
        self.assertEqual(miner.site_search_keywords(miner.config.sites[0]), ["AI_SCAN"])

    def test_old_page_is_rejected_before_ai_call(self):
        miner = AITenderMiner(test_config())
        miner.ai = FakeAI([])
        page = {
            "url": "https://example.com/notice/old",
            "title": "大模型平台采购公告",
            "text": "发布时间：2020-01-01。采购需求：建设大模型平台。",
            "publish_date": "2020-01-01",
        }
        self.assertIsNone(miner.analyze(page))
        self.assertEqual(len(miner.ai.calls), 0)
        self.assertEqual(miner.run_stats["pre_ai_outside_date"], 1)

    def test_obvious_vertical_hardware_skips_product_ai(self):
        extraction = {
            "is_opportunity": True,
            "business_stage": "procurement_notice",
            "project_name": "慢阻肺AI筛查系统采购",
            "procurement_ai_related": True,
            "ai_relevance_evidence": "采购慢阻肺AI筛查系统",
            "procurement_target": "慢阻肺AI筛查系统",
            "procurement_scope": "采购慢阻肺AI筛查系统",
        }
        miner = AITenderMiner(test_config())
        miner.ai = FakeAI([extraction])
        page = {
            "url": "https://example.com/notice/medical",
            "title": "慢阻肺AI筛查系统采购公告",
            "text": "采购公告。采购需求：采购慢阻肺AI筛查系统。",
        }
        analysis = miner.analyze(page)
        self.assertIsNotNone(analysis)
        self.assertNotIn("product_analysis", analysis)
        self.assertEqual(len(miner.ai.calls), 1)
        self.assertEqual(miner.run_stats["product_ai_skipped"], 1)

    def test_ai_assistant_still_uses_product_ai(self):
        extraction = {
            "is_opportunity": True,
            "business_stage": "procurement_notice",
            "project_name": "公安AI助手建设",
            "procurement_ai_related": True,
            "ai_relevance_evidence": "建设公安AI助手",
            "procurement_target": "公安AI助手",
            "procurement_scope": "建设公安AI助手",
        }
        matching = {
            "company_relevance": "high",
            "matched_products": ["VZOOM企业级AI智能体"],
            "product_matches": [
                {
                    "product": "VZOOM企业级AI智能体",
                    "score": 85,
                    "evidence": "建设公安AI助手",
                    "rationale": "AI助手需求直接对应",
                }
            ],
            "unmatched_reasons": [],
            "product_match_reason": "AI助手需求与企业级AI智能体直接对应。",
        }
        miner = AITenderMiner(test_config())
        miner.ai = FakeAI([extraction, matching])
        page = {
            "url": "https://example.com/notice/assistant",
            "title": "公安AI助手建设项目采购公告",
            "text": "采购公告。采购需求：建设公安AI助手。",
        }
        analysis = miner.analyze(page)
        self.assertEqual(analysis["product_analysis"], matching)
        self.assertEqual(len(miner.ai.calls), 2)
        self.assertEqual(miner.run_stats["product_ai_calls"], 1)

    def test_navigation_download_is_not_an_attachment(self):
        self.assertIsNone(
            canonical_attachment_url(
                "/cms/categories/帮助中心/资料下载/",
                "https://www.chengezhao.com/cms/post/1/",
                "资料下载",
            )
        )

    def test_chinese_attachment_filename_is_encoded(self):
        url = canonical_attachment_url(
            "/附件/采购需求.pdf",
            "https://example.com/notice/1",
            "附件下载",
        )
        self.assertIn("%E9%87%87%E8%B4%AD%E9%9C%80%E6%B1%82.pdf", url or "")


if __name__ == "__main__":
    unittest.main()
