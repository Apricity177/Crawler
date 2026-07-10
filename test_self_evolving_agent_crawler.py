import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from self_evolving_agent_crawler import (
    AIConfig,
    AgentMemory,
    ChatAIClient,
    CrawlerConfig,
    PageParser,
    SelfEvolvingCrawler,
    canonical_url,
    env_bool,
    extract_json_object,
    extract_urls_from_text,
    looks_like_html,
    load_env_file,
    scope_domain_of,
)


class ParserTests(unittest.TestCase):
    def test_parser_extracts_links_and_candidates(self) -> None:
        parser = PageParser()
        parser.feed(
            """
            <html>
              <head><title>Example Page</title><meta name="description" content="Demo"></head>
              <body>
                <main><h1>Example</h1><p>This domain is used for examples.</p></main>
                <a href="/next">Next page</a>
              </body>
            </html>
            """
        )

        self.assertEqual(parser.title, "Example Page")
        self.assertEqual(parser.description, "Demo")
        self.assertIn(("/next", "Next page"), parser.links)
        self.assertIn("This domain", parser.candidates()["semantic"])

    def test_parser_extracts_javascript_onclick_detail_link(self) -> None:
        parser = PageParser()
        parser.feed(
            """
            <a href="javascript:;" onclick="showDetail(this, '0201','/information/deal/html/b/test.html')">
              公告详情
            </a>
            """
        )

        self.assertIn(("/information/deal/html/b/test.html", "公告详情"), parser.links)

    def test_extract_urls_from_script_text(self) -> None:
        urls = extract_urls_from_text("var firstLastUrl = '/information/deal/html/b/test.html';")

        self.assertIn("/information/deal/html/b/test.html", urls)

    def test_extract_urls_from_text_stops_before_chinese_punctuation(self) -> None:
        urls = extract_urls_from_text("登录 http://www.ygcgfw.com）进行注册，注册后搜索项目。")

        self.assertEqual(urls, ["http://www.ygcgfw.com"])


class PolicyTests(unittest.TestCase):
    def test_url_normalization_skips_assets(self) -> None:
        self.assertEqual(canonical_url("/a#part", "https://example.com/root"), "https://example.com/a")
        self.assertIsNone(canonical_url("/image.png", "https://example.com/root"))
        self.assertIsNone(canonical_url("http://www.ygcgfw.com）进行注册，注册后搜索该项目并进行报名"))

    def test_html_sniffing_accepts_plain_text_html_body(self) -> None:
        self.assertTrue(looks_like_html(b"<!doctype html><html><head></head><body>ok</body></html>"))
        self.assertTrue(looks_like_html(b"  <html><body>ok</body></html>"))
        self.assertFalse(looks_like_html(b"{\"ok\": true}"))

    def test_memory_evolves_delay_and_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = CrawlerConfig.from_dict(
                {
                    "seeds": ["https://example.com/"],
                    "state_path": f"{tmpdir}/state.json",
                    "min_delay_seconds": 1,
                    "max_delay_seconds": 10,
                }
            )
            memory = AgentMemory.load(config.state_path, config.min_delay_seconds)
            before_weight = memory.strategy_weights["semantic"]
            memory.evolve_after_page("example.com", 200, "semantic", 0.9, config)

            self.assertLessEqual(memory.domain_delay["example.com"], 1)
            self.assertGreater(memory.strategy_weights["semantic"], before_weight)

    def test_memory_pauses_domain_after_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = CrawlerConfig.from_dict(
                {
                    "seeds": ["https://example.com/"],
                    "state_path": f"{tmpdir}/state.json",
                    "min_delay_seconds": 1,
                    "max_delay_seconds": 60,
                    "block_cooldown_seconds": 10,
                }
            )
            memory = AgentMemory.load(config.state_path, config.min_delay_seconds)
            memory.evolve_after_page(
                "example.com",
                429,
                None,
                0.0,
                config,
                blocked_reason="rate_limited",
                retry_after_seconds=5,
            )

            self.assertEqual(memory.domain_block_count["example.com"], 1)
            self.assertGreater(memory.domain_delay["example.com"], 1)
            self.assertGreater(memory.domain_pause_remaining("example.com"), 0)

    def test_frontier_stays_inside_seed_scope(self) -> None:
        config = CrawlerConfig.from_dict(
            {
                "seeds": ["https://www.example.com/"],
                "max_depth": 1,
                "ai": {"enabled": False},
            }
        )
        crawler = SelfEvolvingCrawler(config)
        crawler.enqueue("https://other.test/", depth=0, parent_score=1, anchor_text="")
        crawler.enqueue("https://docs.example.com/page", depth=0, parent_score=1, anchor_text="")

        self.assertEqual(len(crawler.frontier), 1)
        self.assertEqual(crawler.frontier[0].url, "https://docs.example.com/page")

    def test_frontier_can_be_saved_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = CrawlerConfig.from_dict(
                {
                    "seeds": ["https://www.example.com/"],
                    "output_dir": tmpdir,
                    "state_path": os.path.join(tmpdir, "state.json"),
                    "ai": {"enabled": False},
                }
            )
            crawler = SelfEvolvingCrawler(config)
            crawler.enqueue("https://www.example.com/detail", depth=1, parent_score=0.8, anchor_text="详情")
            crawler.save_pending_frontier()
            crawler.memory.save()

            restored = SelfEvolvingCrawler(config)
            restored.restore_pending_frontier()

            self.assertEqual(len(restored.frontier), 1)
            self.assertEqual(restored.frontier[0].url, "https://www.example.com/detail")

    def test_run_writes_summaries_and_pending_frontier_on_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = CrawlerConfig.from_dict(
                {
                    "seeds": ["https://www.example.com/"],
                    "output_dir": tmpdir,
                    "state_path": os.path.join(tmpdir, "state.json"),
                    "obey_robots": False,
                    "ai": {"enabled": False},
                }
            )
            crawler = SelfEvolvingCrawler(config)
            summary_called = False

            def interrupting_fetch(_url: str) -> dict[str, object]:
                raise KeyboardInterrupt()

            def fake_summary() -> None:
                nonlocal summary_called
                summary_called = True

            crawler.fetch = interrupting_fetch  # type: ignore[method-assign]
            crawler.write_opportunity_summaries = fake_summary  # type: ignore[method-assign]

            crawler.run()

            self.assertTrue(summary_called)
            self.assertEqual(len(crawler.memory.pending_frontier), 1)
            self.assertEqual(crawler.memory.pending_frontier[0]["url"], "https://www.example.com/")

    def test_detect_blocked_reason(self) -> None:
        config = CrawlerConfig.from_dict({"seeds": ["https://example.com/"], "ai": {"enabled": False}})
        crawler = SelfEvolvingCrawler(config)

        self.assertEqual(crawler.detect_blocked_reason(429, ""), "rate_limited")
        self.assertEqual(crawler.detect_blocked_reason(403, ""), "forbidden")
        self.assertEqual(crawler.detect_blocked_reason(200, "请登录后查看详情"), "login_required")
        self.assertEqual(crawler.detect_blocked_reason(200, "访问过于频繁，请稍后再试"), "rate_limited")
        self.assertEqual(crawler.detect_blocked_reason(200, "请输入验证码"), "captcha")

    def test_www_seed_uses_parent_scope(self) -> None:
        self.assertEqual(scope_domain_of("https://www.sustech.edu.cn/"), "sustech.edu.cn")

    def test_config_accepts_ai_opportunity_blocks(self) -> None:
        config = CrawlerConfig.from_dict(
            {
                "seeds": ["https://example.com/"],
                "opportunity": {
                    "goal": "发现教育系统商机",
                    "signals": ["招标", "系统"],
                    "negative_signals": ["招聘"],
                    "min_score": 0.7,
                },
                "ai": {
                    "enabled": True,
                    "base_url": "https://sophon-api.vzoom.com/ai/v1",
                    "model": "qwen-core",
                },
            }
        )

        self.assertEqual(config.opportunity.goal, "发现教育系统商机")
        self.assertEqual(config.opportunity.min_score, 0.7)
        self.assertEqual(config.ai.model, "qwen-core")

    def test_minimal_config_uses_procurement_opportunity_defaults(self) -> None:
        config = CrawlerConfig.from_dict({"seeds": ["https://www.ggzy.gov.cn/"]})

        self.assertTrue(config.ai.enabled)
        self.assertIn("招标", config.keywords)
        self.assertIn("采购公告", config.opportunity.signals)
        self.assertEqual(config.opportunity.min_score, 0.6)
        self.assertEqual(config.recent_days, 7)
        self.assertEqual(config.safety_max_pages, 300)
        self.assertEqual(config.output_dir, "data/ggzy_gov_cn_opportunities")
        self.assertEqual(config.state_path, "data/ggzy_gov_cn_opportunities/crawler_state.json")

    def test_output_dir_sets_default_state_path(self) -> None:
        config = CrawlerConfig.from_dict(
            {
                "seeds": ["https://www.ggzy.gov.cn/"],
                "output_dir": "data/custom_task",
            }
        )

        self.assertEqual(config.output_dir, "data/custom_task")
        self.assertEqual(config.state_path, "data/custom_task/crawler_state.json")

    def test_extract_json_object_from_fenced_ai_response(self) -> None:
        parsed = extract_json_object('```json\n{"opportunity_score": 0.8}\n```')
        self.assertEqual(parsed["opportunity_score"], 0.8)

    def test_load_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, ".env")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("SOPHON_TEST_KEY='abc123'\n")

            old_value = os.environ.pop("SOPHON_TEST_KEY", None)
            try:
                loaded = load_env_file(path)
                self.assertEqual(loaded["SOPHON_TEST_KEY"], "abc123")
                self.assertEqual(os.environ["SOPHON_TEST_KEY"], "abc123")
            finally:
                if old_value is None:
                    os.environ.pop("SOPHON_TEST_KEY", None)
                else:
                    os.environ["SOPHON_TEST_KEY"] = old_value

    def test_env_bool(self) -> None:
        old_value = os.environ.get("SOPHON_TEST_BOOL")
        try:
            os.environ["SOPHON_TEST_BOOL"] = "false"
            self.assertFalse(env_bool("SOPHON_TEST_BOOL", True))
            os.environ["SOPHON_TEST_BOOL"] = "yes"
            self.assertTrue(env_bool("SOPHON_TEST_BOOL", False))
        finally:
            if old_value is None:
                os.environ.pop("SOPHON_TEST_BOOL", None)
            else:
                os.environ["SOPHON_TEST_BOOL"] = old_value

    def test_ai_client_reads_env_overrides(self) -> None:
        old_values = {
            name: os.environ.get(name)
            for name in [
                "SOPHON_API_KEY",
                "SOPHON_API_BASE_URL",
                "SOPHON_MODEL",
                "SOPHON_VERIFY_SSL",
                "SOPHON_MAX_RETRIES",
                "SOPHON_RETRY_DELAY_SECONDS",
                "SOPHON_PROXY_URL",
                "SOPHON_TEMPERATURE",
                "SOPHON_MAX_TOKENS",
                "SOPHON_TRUST_ENV",
            ]
        }
        try:
            os.environ["SOPHON_API_KEY"] = "test-key-123456"
            os.environ["SOPHON_API_BASE_URL"] = "https://ai.example/v1"
            os.environ["SOPHON_MODEL"] = "test-model"
            os.environ["SOPHON_VERIFY_SSL"] = "false"
            os.environ["SOPHON_MAX_RETRIES"] = "4"
            os.environ["SOPHON_RETRY_DELAY_SECONDS"] = "0.25"
            os.environ["SOPHON_PROXY_URL"] = "http://127.0.0.1:7890"
            os.environ["SOPHON_TEMPERATURE"] = "0.75"
            os.environ["SOPHON_MAX_TOKENS"] = "1800"
            os.environ["SOPHON_TRUST_ENV"] = "false"

            client = ChatAIClient(AIConfig())

            self.assertTrue(client.available)
            self.assertEqual(client.endpoint, "https://ai.example/v1/chat/completions")
            self.assertEqual(client.model, "test-model")
            self.assertFalse(client.verify_ssl)
            self.assertEqual(client.max_retries, 4)
            self.assertEqual(client.retry_delay_seconds, 0.25)
            self.assertEqual(client.proxy_url, "http://127.0.0.1:7890")
            self.assertEqual(client.temperature, 0.75)
            self.assertEqual(client.max_tokens, 1800)
            self.assertFalse(client.trust_env)
        finally:
            for name, value in old_values.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_ai_config_reads_env_only_request_options(self) -> None:
        old_values = {
            name: os.environ.get(name)
            for name in [
                "SOPHON_MAX_FAILURES",
                "SOPHON_TEMPERATURE",
                "SOPHON_MAX_TOKENS",
                "SOPHON_TRUST_ENV",
            ]
        }
        try:
            os.environ["SOPHON_MAX_FAILURES"] = "5"
            os.environ["SOPHON_TEMPERATURE"] = "0.75"
            os.environ["SOPHON_MAX_TOKENS"] = "1800"
            os.environ["SOPHON_TRUST_ENV"] = "false"

            config = AIConfig.from_dict({})

            self.assertEqual(config.max_failures, 5)
            self.assertEqual(config.temperature, 0.75)
            self.assertEqual(config.max_tokens, 1800)
            self.assertFalse(config.trust_env)
        finally:
            for name, value in old_values.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_ai_score_changes_page_score(self) -> None:
        config = CrawlerConfig.from_dict(
            {"seeds": ["https://example.com/"], "keywords": ["课程"], "ai": {"enabled": False}}
        )
        crawler = SelfEvolvingCrawler(config)

        score = crawler.page_score(
            title="普通页面",
            description="",
            text="没有明显关键词",
            reward=0.1,
            ai_analysis={"opportunity_score": 0.9},
        )

        self.assertGreater(score, 0.6)

    def test_ai_link_hints_boost_url_score(self) -> None:
        config = CrawlerConfig.from_dict({"seeds": ["https://example.com/"], "ai": {"enabled": False}})
        crawler = SelfEvolvingCrawler(config)

        plain = crawler.url_score("https://example.com/about", "", 0.5, 1)
        hinted = crawler.url_score(
            "https://example.com/procurement",
            "采购公告",
            0.5,
            1,
            {"link_hints": ["procurement", "采购"]},
        )

        self.assertGreater(hinted, plain)

    def test_rule_opportunity_analysis_extracts_tender_fields(self) -> None:
        config = CrawlerConfig.from_dict(
            {
                "seeds": ["https://www.ggzy.gov.cn/"],
                "keywords": ["招标", "采购", "预算"],
                "ai": {"enabled": False},
                "opportunity": {
                    "signals": ["招标公告", "采购公告", "预算金额", "项目编号", "采购人", "联系方式"],
                    "negative_signals": ["招聘"],
                    "min_score": 0.6,
                },
            }
        )
        crawler = SelfEvolvingCrawler(config)
        analysis = crawler.rule_opportunity_analysis(
            url="https://www.ggzy.gov.cn/information/deal/html/a/test.html",
            title="某系统建设项目招标公告",
            description="",
            text=(
                "项目编号：ABC-2026-001。采购人：某市公共资源交易中心。"
                "预算金额：120万元。投标截止时间：2026年8月1日 09:30。"
                "联系方式：010-12345678。本项目发布招标公告。"
            ),
            links=[],
        )

        self.assertEqual(analysis["analysis_method"], "rules")
        self.assertTrue(analysis["is_opportunity"])
        self.assertGreaterEqual(analysis["opportunity_score"], 0.6)
        self.assertEqual(analysis["business_stage"], "tender_notice")
        self.assertIn("ABC-2026-001", analysis["project_id"])
        self.assertIn("120万元", analysis["budget_or_scale"])

    def test_rule_opportunity_analysis_deprioritizes_policy_pages(self) -> None:
        config = CrawlerConfig.from_dict(
            {
                "seeds": ["https://www.ggzy.gov.cn/"],
                "keywords": ["招标", "采购", "项目"],
                "ai": {"enabled": False},
                "opportunity": {
                    "signals": ["招标公告", "采购公告", "工程建设", "招标人"],
                    "negative_signals": ["政策解读", "管理办法", "实施意见"],
                    "min_score": 0.6,
                },
            }
        )
        crawler = SelfEvolvingCrawler(config)
        analysis = crawler.rule_opportunity_analysis(
            url="https://www.ggzy.gov.cn/SIC/web/details.po?id=policy",
            title="关于《工程建设项目招标代理机构管理暂行办法》的解读",
            description="",
            text="这是政策解读，不是具体采购公告，也不包含投标截止时间或采购人联系方式。",
            links=[],
        )

        self.assertEqual(analysis["business_stage"], "policy_info")
        self.assertFalse(analysis["is_opportunity"])
        self.assertLess(analysis["opportunity_score"], 0.6)

    def test_refine_opportunity_analysis_splits_award_candidate(self) -> None:
        config = CrawlerConfig.from_dict({"seeds": ["https://www.ggzy.gov.cn/"], "ai": {"enabled": False}})
        crawler = SelfEvolvingCrawler(config)

        analysis = crawler.refine_opportunity_analysis(
            url="https://www.ggzy.gov.cn/information/deal/html/a/test.html",
            title="金利科创大厦施工总承包工程中标候选人公示",
            text="招标项目编号 ABC，中标候选人公示，工程建设。",
            links=[],
            analysis={
                "is_opportunity": True,
                "opportunity_score": 0.85,
                "business_stage": "tender",
            },
        )

        self.assertEqual(analysis["business_stage"], "award_candidate")
        self.assertTrue(analysis["is_final_opportunity"])

    def test_stage_uses_title_before_ggzy_navigation_tabs(self) -> None:
        config = CrawlerConfig.from_dict({"seeds": ["https://www.ggzy.gov.cn/"], "ai": {"enabled": False}})
        crawler = SelfEvolvingCrawler(config)

        stage = crawler.normalize_business_stage(
            url="https://www.ggzy.gov.cn/information/deal/html/a/test.html",
            title="成都市温江区金马学校教职工体检采购项目招标公告",
            text="采购/资审公告 中标公告 采购合同 更正事项 暂无数据",
            raw_stage="award_result",
        )

        self.assertEqual(stage, "tender_notice")

    def test_deal_detail_policy_words_do_not_demote_tender(self) -> None:
        config = CrawlerConfig.from_dict({"seeds": ["https://www.ggzy.gov.cn/"], "ai": {"enabled": False}})
        crawler = SelfEvolvingCrawler(config)

        stage = crawler.normalize_business_stage(
            url="https://www.ggzy.gov.cn/information/deal/html/b/440000/0201/test.html",
            title="全国公共资源交易平台",
            text="五华县人民医院能源托管项目招标公告 政府采购政策 预算金额：52,916,400.00元",
            raw_stage="policy_info",
        )

        self.assertEqual(stage, "tender_notice")

    def test_refine_opportunity_analysis_demotes_listing_pages(self) -> None:
        config = CrawlerConfig.from_dict({"seeds": ["https://www.ggzy.gov.cn/"], "ai": {"enabled": False}})
        crawler = SelfEvolvingCrawler(config)

        analysis = crawler.refine_opportunity_analysis(
            url="https://www.ggzy.gov.cn/deal/dealList.html?HEADER_DEAL_TYPE=02",
            title="全国公共资源交易平台",
            text="首页 交易公开 搜索 筛选 更多 查询",
            links=[f"https://www.ggzy.gov.cn/information/deal/{index}.html" for index in range(40)],
            analysis={
                "is_opportunity": True,
                "opportunity_score": 0.9,
                "business_stage": "market_research",
            },
        )

        self.assertEqual(analysis["business_stage"], "market_research")
        self.assertFalse(analysis["is_opportunity"])
        self.assertLess(analysis["opportunity_score"], 0.6)

    def test_extract_structured_fields_finds_key_procurement_fields(self) -> None:
        config = CrawlerConfig.from_dict({"seeds": ["https://www.ggzy.gov.cn/"], "ai": {"enabled": False}})
        crawler = SelfEvolvingCrawler(config)

        fields = crawler.extract_structured_fields(
            title="某系统建设项目招标公告",
            text=(
                "项目名称：某系统建设项目。项目编号：ABC-2026-001。采购人：某市公共资源交易中心。"
                "采购代理机构：某招标代理有限公司。预算金额：120万元。"
                "投标截止时间：2026年8月1日 09:30。联系人：张三 010-12345678。"
                "开标地点：北京市海淀区中关村大街1号。"
            ),
        )

        self.assertEqual(fields["project_id"], "ABC-2026-001")
        self.assertEqual(fields["project_name"], "某系统建设项目")
        self.assertEqual(fields["customer_or_org"], "某市公共资源交易中心")
        self.assertEqual(fields["agency"], "某招标代理有限公司")
        self.assertIn("120万元", fields["budget_or_scale"])
        self.assertIn("2026年8月1日 09:30", fields["deadline"])
        self.assertIn("010-12345678", fields["contact"])
        self.assertIn("北京市海淀区中关村大街1号", fields["address"])

    def test_extract_structured_fields_does_not_use_project_id_as_phone(self) -> None:
        config = CrawlerConfig.from_dict({"seeds": ["https://www.ggzy.gov.cn/"], "ai": {"enabled": False}})
        crawler = SelfEvolvingCrawler(config)

        fields = crawler.extract_structured_fields(
            title="成都市温江区金马学校教职工体检采购项目招标公告",
            text="采购项目编号：N5101152026000108 信息来源：四川省 暂无数据",
        )

        self.assertEqual(fields["project_id"], "N5101152026000108")
        self.assertNotIn("contact", fields)

    def test_normalize_money_to_yuan(self) -> None:
        config = CrawlerConfig.from_dict({"seeds": ["https://www.ggzy.gov.cn/"], "ai": {"enabled": False}})
        crawler = SelfEvolvingCrawler(config)

        self.assertEqual(crawler.normalize_money("58.18万元")["amount_yuan"], 581800)
        self.assertEqual(crawler.normalize_money("5 亿")["amount_yuan"], 500000000)
        self.assertEqual(crawler.normalize_money("517,500.00元")["display"], "517500元")
        self.assertEqual(crawler.normalize_money("8235400.0")["display"], "8235400元")

    def test_normalize_datetime_values(self) -> None:
        config = CrawlerConfig.from_dict({"seeds": ["https://www.ggzy.gov.cn/"], "ai": {"enabled": False}})
        crawler = SelfEvolvingCrawler(config)

        self.assertEqual(
            crawler.normalize_datetime_value("2026年07月08日 09时00分")["normalized"],
            "2026-07-08 09:00:00",
        )
        self.assertEqual(
            crawler.normalize_datetime_value("2026-06-30T09:00:00")["normalized"],
            "2026-06-30 09:00:00",
        )
        self.assertEqual(
            crawler.normalize_datetime_value("2026-07-09")["normalized"],
            "2026-07-09",
        )

    def test_recent_days_filter_uses_publish_date(self) -> None:
        config = CrawlerConfig.from_dict(
            {"seeds": ["https://www.ggzy.gov.cn/"], "recent_days": 7, "ai": {"enabled": False}}
        )
        crawler = SelfEvolvingCrawler(config)
        today = datetime.now(timezone.utc).date()
        recent = today - timedelta(days=6)
        old = today - timedelta(days=7)

        self.assertTrue(crawler.is_within_recent_days(recent.isoformat()))
        self.assertFalse(crawler.is_within_recent_days(old.isoformat()))
        self.assertFalse(crawler.is_within_recent_days(""))

    def test_split_contact_info_separates_name_and_phone(self) -> None:
        config = CrawlerConfig.from_dict({"seeds": ["https://www.ggzy.gov.cn/"], "ai": {"enabled": False}})
        crawler = SelfEvolvingCrawler(config)

        name, phone = crawler.split_contact_info("项目联系人：陈旋芳、谢伟健 电话：0753-2292508；采购人 18038598005")

        self.assertEqual(name, "陈旋芳、谢伟健")
        self.assertEqual(phone, "0753-2292508；18038598005")

    def test_build_structured_opportunity_record_is_compact(self) -> None:
        config = CrawlerConfig.from_dict({"seeds": ["https://www.ggzy.gov.cn/"], "ai": {"enabled": False}})
        crawler = SelfEvolvingCrawler(config)

        record = crawler.build_structured_opportunity_record(
            {
                "stage": "tender_notice",
                "score": "0.95",
                "title": "全国公共资源交易平台",
                "project_id": "202606FG845",
                "customer_or_org": "五华县人民医院",
                "agency": "广东意达招标采购有限公司",
                "supplier_or_winner": "",
                "need": "能源托管项目",
                "budget_or_scale": "52,916,400.00元",
                "deadline": "2026年07月30日 09时00分",
                "publish_date": "2026-07-09",
                "contact": "项目联系人：陈旋芳、谢伟健 电话：0753-2292508",
                "address": "梅州市五华县水寨镇华兴北路53号",
                "field_completeness": "1.0",
                "missing_fields": "",
                "recommended_action": "跟进投标。",
                "url": "https://example.com/detail",
            }
        )

        self.assertEqual(record["项目名称"], "能源托管项目")
        self.assertEqual(record["金额元"], 52916400)
        self.assertEqual(record["截止时间"], "2026-07-30 09:00:00")
        self.assertEqual(record["联系人"], "陈旋芳、谢伟健")
        self.assertEqual(record["联系方式"], "0753-2292508")
        self.assertNotIn("budget", record)
        self.assertNotIn("阶段", record)
        self.assertNotIn("missing_fields", record)
        self.assertNotIn("商机分数", record)
        self.assertNotIn("跟进建议", record)

    def test_dedupe_prefers_complete_detail_row_with_budget(self) -> None:
        config = CrawlerConfig.from_dict({"seeds": ["https://www.ggzy.gov.cn/"], "ai": {"enabled": False}})
        crawler = SelfEvolvingCrawler(config)

        rows = crawler.dedupe_opportunity_rows(
            [
                {
                    "project_id": "202606FG845",
                    "project_name": "五华县人民医院能源托管项目招标公告",
                    "title": "五华县人民医院能源托管项目招标公告",
                    "budget_or_scale": "",
                    "field_completeness": "0.2",
                    "score": "0.85",
                    "url": "https://www.ggzy.gov.cn/information/deal/html/a/test.html",
                },
                {
                    "project_id": "202606FG845",
                    "project_name": "能源托管服务",
                    "title": "全国公共资源交易平台",
                    "budget_or_scale": "52,916,400.00元",
                    "field_completeness": "1.0",
                    "score": "0.6",
                    "url": "https://www.ggzy.gov.cn/information/deal/html/b/test.html",
                },
            ]
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["budget_or_scale"], "52,916,400.00元")
        self.assertIn("/html/b/", rows[0]["url"])

    def test_dedupe_prefers_same_project_detail_even_with_different_ids(self) -> None:
        config = CrawlerConfig.from_dict({"seeds": ["https://www.ggzy.gov.cn/"], "ai": {"enabled": False}})
        crawler = SelfEvolvingCrawler(config)

        rows = crawler.dedupe_opportunity_rows(
            [
                {
                    "project_id": "E3505830502173059002",
                    "project_name": "南安市两岸融合海洋经济产业提升项目（一期）施工监理中标候选人公示",
                    "title": "南安市两岸融合海洋经济产业提升项目（一期）施工监理中标候选人公示",
                    "budget_or_scale": "",
                    "field_completeness": "0.2",
                    "score": "0.9",
                    "url": "https://www.ggzy.gov.cn/information/deal/html/a/test.html",
                },
                {
                    "project_id": "南建标[2026]044号",
                    "project_name": "南安市两岸融合海洋经济产业提升项目（一期）施工监理",
                    "title": "全国公共资源交易平台",
                    "budget_or_scale": "15870.62万元",
                    "field_completeness": "1.0",
                    "score": "0.6",
                    "url": "https://www.ggzy.gov.cn/information/deal/html/b/test.html",
                },
            ]
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["budget_or_scale"], "15870.62万元")

    def test_refine_opportunity_analysis_overwrites_missing_ai_fields(self) -> None:
        config = CrawlerConfig.from_dict({"seeds": ["https://www.ggzy.gov.cn/"], "ai": {"enabled": False}})
        crawler = SelfEvolvingCrawler(config)

        analysis = crawler.refine_opportunity_analysis(
            url="https://www.ggzy.gov.cn/information/deal/html/a/test.html",
            title="某系统建设项目招标公告",
            text=(
                "采购项目编号：ABC-2026-001。采购人：某市公共资源交易中心。"
                "预算金额：120万元。开标时间：2026年8月1日 09:30。"
                "联系电话：010-12345678。地址：北京市海淀区中关村大街1号。"
            ),
            links=[],
            analysis={
                "is_opportunity": True,
                "opportunity_score": 0.9,
                "business_stage": "tender_notice",
                "customer_or_org": "暂无数据",
                "budget_or_scale": "暂无数据",
                "deadline": None,
                "contact": "需进一步查看详情",
                "address": "",
            },
        )

        self.assertEqual(analysis["project_id"], "ABC-2026-001")
        self.assertEqual(analysis["customer_or_org"], "某市公共资源交易中心")
        self.assertIn("120万元", analysis["budget_or_scale"])
        self.assertIn("2026年8月1日 09:30", analysis["deadline"])
        self.assertIn("010-12345678", analysis["contact"])
        self.assertIn("北京市海淀区中关村大街1号", analysis["address"])
        self.assertEqual(analysis["budget_normalized"]["amount_yuan"], 1200000)
        self.assertEqual(analysis["deadline_normalized"]["normalized"], "2026-08-01 09:30:00")
        self.assertFalse(analysis["needs_detail_followup"])

    def test_save_opportunity_only_writes_final_project_stages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = CrawlerConfig.from_dict(
                {
                    "seeds": ["https://www.ggzy.gov.cn/"],
                    "output_dir": tmpdir,
                    "state_path": os.path.join(tmpdir, "state.json"),
                    "ai": {"enabled": False},
                    "opportunity": {"min_score": 0.6},
                }
            )
            crawler = SelfEvolvingCrawler(config)
            os.makedirs(os.path.join(tmpdir, "opportunities"), exist_ok=True)
            page = {
                "url": "https://www.ggzy.gov.cn/deal/dealList.html?HEADER_DEAL_TYPE=02",
                "title": "全国公共资源交易平台",
                "fetched_at": "2026-07-08T00:00:00+00:00",
                "page_score": 0.8,
                "ai_analysis": {
                    "is_opportunity": False,
                    "opportunity_score": 0.9,
                    "business_stage": "market_research",
                },
            }

            crawler.save_opportunity(page)

            self.assertEqual(os.listdir(os.path.join(tmpdir, "opportunities")), [])


if __name__ == "__main__":
    unittest.main()
