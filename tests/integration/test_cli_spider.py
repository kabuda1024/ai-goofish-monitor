import asyncio
import importlib
import json
import sys
import types


def _make_fake_scraper_class(async_fn):
    """构造一个假的 Scraper 类,run() 调用被注入的 async_fn(task_config, debug_limit)。"""

    class FakeScraper:
        def __init__(self, task_config, debug_limit=0):
            self.task_config = task_config
            self.debug_limit = debug_limit

        async def run(self):
            return await async_fn(self.task_config, self.debug_limit)

    return FakeScraper


def test_cli_runs_single_task_with_prompt(tmp_path, load_json_fixture, monkeypatch):
    fake_scrapers = types.ModuleType("src.scrapers")

    async def placeholder(task_config, debug_limit):
        return 0

    fake_scrapers.get_scraper_class = lambda name=None: _make_fake_scraper_class(placeholder)
    monkeypatch.setitem(sys.modules, "src.scrapers", fake_scrapers)
    sys.modules.pop("spider_v2", None)

    spider_v2 = importlib.import_module("spider_v2")
    config_data = load_json_fixture("config.sample.json")

    base_prompt = "Base prompt. " + ("x" * 120) + " {{CRITERIA_SECTION}}"
    criteria_prompt = "Criteria text for A7M4."

    base_path = tmp_path / "base_prompt.txt"
    criteria_path = tmp_path / "criteria_prompt.txt"
    base_path.write_text(base_prompt, encoding="utf-8")
    criteria_path.write_text(criteria_prompt, encoding="utf-8")

    config_data[0]["ai_prompt_base_file"] = str(base_path)
    config_data[0]["ai_prompt_criteria_file"] = str(criteria_path)

    config_data[1]["ai_prompt_base_file"] = str(base_path)
    config_data[1]["ai_prompt_criteria_file"] = str(criteria_path)

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data, ensure_ascii=False), encoding="utf-8")

    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(spider_v2, "STATE_FILE", str(state_path))

    called = []

    async def fake_scrape_xianyu(task_config, debug_limit):
        called.append(task_config["task_name"])
        assert "{{CRITERIA_SECTION}}" not in task_config["ai_prompt_text"]
        assert "Criteria text for A7M4." in task_config["ai_prompt_text"]
        return 1

    monkeypatch.setattr(
        spider_v2, "get_scraper_class",
        lambda name=None: _make_fake_scraper_class(fake_scrape_xianyu),
    )
    monkeypatch.setattr(sys, "argv", ["spider_v2.py", "--config", str(config_path), "--task-name", "Sony A7M4"])

    asyncio.run(spider_v2.main())

    assert called == ["Sony A7M4"]


def test_cli_runs_keyword_mode_without_prompt_files(tmp_path, load_json_fixture, monkeypatch):
    fake_scrapers = types.ModuleType("src.scrapers")

    async def placeholder(task_config, debug_limit):
        return 0

    fake_scrapers.get_scraper_class = lambda name=None: _make_fake_scraper_class(placeholder)
    monkeypatch.setitem(sys.modules, "src.scrapers", fake_scrapers)
    sys.modules.pop("spider_v2", None)

    spider_v2 = importlib.import_module("spider_v2")
    config_data = load_json_fixture("config.sample.json")
    config_data[0]["enabled"] = True
    config_data[0]["decision_mode"] = "keyword"
    config_data[0]["keyword_rules"] = ["a7m4", "验货宝"]
    config_data[0]["ai_prompt_base_file"] = "missing_base.txt"
    config_data[0]["ai_prompt_criteria_file"] = "missing_criteria.txt"

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data, ensure_ascii=False), encoding="utf-8")

    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(spider_v2, "STATE_FILE", str(state_path))

    captured = []

    async def fake_scrape_xianyu(task_config, debug_limit):
        captured.append(task_config)
        return 1

    monkeypatch.setattr(
        spider_v2, "get_scraper_class",
        lambda name=None: _make_fake_scraper_class(fake_scrape_xianyu),
    )
    monkeypatch.setattr(sys, "argv", ["spider_v2.py", "--config", str(config_path), "--task-name", "Sony A7M4"])

    asyncio.run(spider_v2.main())

    assert len(captured) == 1
    assert captured[0]["decision_mode"] == "keyword"
    assert captured[0]["ai_prompt_text"] == ""
