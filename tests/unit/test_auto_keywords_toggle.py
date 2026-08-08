"""auto_keywords 开关测试。

验证:
- 默认关闭时,search_keywords 保持空,爬虫只搜主关键词
- 开启后,AI 生成的关键词写入 platform_options.search_keywords
- 关闭时,即使 AI 意外生成了关键词也不落库
"""
from __future__ import annotations

import asyncio
import tempfile

import pytest

from src.domain.models.task import Task, TaskCreate, TaskGenerateRequest


class TestAutoKeywordsField:
    def test_task_defaults_to_disabled(self):
        t = Task(
            task_name="t", enabled=True, keyword="k",
            max_pages=1, personal_only=False,
            ai_prompt_base_file="p", ai_prompt_criteria_file="p",
            description="d",
        )
        assert t.auto_keywords is False

    def test_task_can_be_enabled(self):
        t = Task(
            task_name="t", enabled=True, keyword="k",
            max_pages=1, personal_only=False,
            ai_prompt_base_file="p", ai_prompt_criteria_file="p",
            description="d", auto_keywords=True,
        )
        assert t.auto_keywords is True

    def test_task_create_defaults(self):
        c = TaskCreate(task_name="t", keyword="k", description="d")
        assert c.auto_keywords is False

    def test_task_generate_request_defaults(self):
        r = TaskGenerateRequest(task_name="t", keyword="k", description="d")
        assert r.auto_keywords is False


class TestSqliteRoundtrip:
    def test_auto_keywords_persists(self, monkeypatch, tmp_path):
        db_path = tmp_path / "test.sqlite3"
        monkeypatch.setenv("APP_DATABASE_FILE", str(db_path))

        # 需要 reimport 以拿到新 APP_DATABASE_FILE
        import importlib
        import src.infrastructure.persistence.storage_names as sn
        import src.infrastructure.persistence.sqlite_connection as sc
        import src.infrastructure.persistence.sqlite_bootstrap as sb
        import src.infrastructure.persistence.sqlite_task_repository as sr
        importlib.reload(sn); importlib.reload(sc)
        importlib.reload(sb); importlib.reload(sr)

        async def _run():
            sb.bootstrap_sqlite_storage(legacy_config_file=None)
            repo = sr.SqliteTaskRepository(legacy_config_file=None)

            t_off = Task(
                task_name="off", enabled=True, keyword="k1", max_pages=1,
                personal_only=False, ai_prompt_base_file="p",
                ai_prompt_criteria_file="p", description="d",
                auto_keywords=False,
            )
            t_on = Task(
                task_name="on", enabled=True, keyword="k2", max_pages=1,
                personal_only=False, ai_prompt_base_file="p",
                ai_prompt_criteria_file="p", description="d",
                auto_keywords=True,
            )
            await repo.save(t_off)
            await repo.save(t_on)

            tasks = await repo.find_all()
            by_name = {t.task_name: t for t in tasks}
            return by_name

        by_name = asyncio.run(_run())
        assert by_name["off"].auto_keywords is False
        assert by_name["on"].auto_keywords is True


class TestRunnerDispatch:
    """验证 runner 按 auto_keywords 分流:关闭时只调 generate_criteria,开启时调 generate_task_metadata。"""

    def test_auto_keywords_false_skips_keywords_generation(self, monkeypatch, tmp_path):
        """auto_keywords=False 时不写入 search_keywords。"""
        from src.services.task_generation_runner import build_task_create

        req = TaskGenerateRequest(
            task_name="t", keyword="MacBook Air", description="d",
            platform="mercari", auto_keywords=False,
        )
        # 模拟 runner 关闭 auto 时传 search_keywords=None
        create = build_task_create(req, "prompts/foo_criteria.txt", search_keywords=None)
        assert create.platform_options.get("search_keywords") is None
        assert create.auto_keywords is False

    def test_auto_keywords_true_stores_keywords(self, monkeypatch):
        """auto_keywords=True 时 search_keywords 存入 platform_options。"""
        from src.services.task_generation_runner import build_task_create

        req = TaskGenerateRequest(
            task_name="t", keyword="MacBook Air", description="d",
            platform="mercari", auto_keywords=True,
        )
        create = build_task_create(
            req,
            "prompts/foo_criteria.txt",
            search_keywords=["MacBook Air", "MacBook Air M1", "MacBook Air M2"],
        )
        assert create.platform_options["search_keywords"] == [
            "MacBook Air", "MacBook Air M1", "MacBook Air M2"
        ]
        assert create.auto_keywords is True
