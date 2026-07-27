"""
任务生成作业执行器
"""
import os

import aiofiles

from src.domain.models.task import TaskCreate, TaskGenerateRequest
from src.prompt_utils import generate_task_metadata
from src.services.scheduler_service import SchedulerService
from src.services.task_generation_service import TaskGenerationService
from src.services.task_service import TaskService

def build_criteria_filename(keyword: str) -> str:
    safe_keyword = "".join(
        char for char in keyword.lower().replace(" ", "_")
        if char.isalnum() or char in "_-"
    ).rstrip()
    return f"prompts/{safe_keyword}_criteria.txt"


PLATFORM_BASE_PROMPT_FILES = {
    "xianyu": "prompts/xianyu/base_prompt.txt",
    "mercari": "prompts/mercari/base_prompt.txt",
}

PLATFORM_REFERENCE_CRITERIA_FILES = {
    "xianyu": "prompts/xianyu/macbook_criteria.txt",
    "mercari": "prompts/mercari/macbook_criteria.txt",
}


def _resolve_base_prompt(platform: str) -> str:
    """按平台派发 base_prompt 路径,回退到通用文件(保持向后兼容)。"""
    normalized = (platform or "xianyu").lower()
    candidate = PLATFORM_BASE_PROMPT_FILES.get(normalized)
    if candidate and os.path.exists(candidate):
        return candidate
    return "prompts/base_prompt.txt"


def _resolve_reference_criteria(platform: str) -> str:
    """选一份 AI 生成 criteria 的参考文件。

    按优先级回退:
      1. 平台专属(如 prompts/mercari/macbook_criteria.txt)
      2. 闲鱼那份(prompts/xianyu/macbook_criteria.txt)
      3. 老版根目录(prompts/macbook_criteria.txt) —— 兼容未升级的部署
    """
    normalized = (platform or "xianyu").lower()
    candidates = [
        PLATFORM_REFERENCE_CRITERIA_FILES.get(normalized),
        PLATFORM_REFERENCE_CRITERIA_FILES.get("xianyu"),
        "prompts/macbook_criteria.txt",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return "prompts/macbook_criteria.txt"


def build_task_create(
    req: TaskGenerateRequest,
    criteria_file: str,
    *,
    search_keywords: list[str] | None = None,
) -> TaskCreate:
    platform = getattr(req, "platform", "xianyu") or "xianyu"
    platform_options = dict(getattr(req, "platform_options", {}) or {})

    # 把 AI 生成的关键词候选存进 platform_options,爬虫运行时会依次搜索
    if search_keywords:
        platform_options["search_keywords"] = [
            kw for kw in search_keywords if kw and kw.strip()
        ]

    return TaskCreate(
        task_name=req.task_name,
        enabled=True,
        keyword=req.keyword,
        description=req.description or "",
        analyze_images=req.analyze_images,
        max_pages=req.max_pages,
        personal_only=req.personal_only,
        min_price=req.min_price,
        max_price=req.max_price,
        cron=req.cron,
        ai_prompt_base_file=_resolve_base_prompt(platform),
        ai_prompt_criteria_file=criteria_file,
        account_state_file=req.account_state_file,
        account_strategy=req.account_strategy,
        free_shipping=req.free_shipping,
        new_publish_option=req.new_publish_option,
        region=req.region,
        decision_mode=req.decision_mode or "ai",
        keyword_rules=req.keyword_rules,
        platform=platform,
        platform_options=platform_options,
    )


async def save_generated_criteria(output_filename: str, generated_criteria: str) -> None:
    if not generated_criteria or not generated_criteria.strip():
        raise RuntimeError("AI 未能生成分析标准，返回内容为空。")

    os.makedirs("prompts", exist_ok=True)
    async with aiofiles.open(output_filename, "w", encoding="utf-8") as file:
        await file.write(generated_criteria)


async def reload_scheduler(
    task_service: TaskService,
    scheduler_service: SchedulerService,
) -> None:
    tasks = await task_service.get_all_tasks()
    await scheduler_service.reload_jobs(tasks)


async def advance_job(
    generation_service: TaskGenerationService,
    job_id: str,
    step_key: str,
    message: str,
) -> None:
    await generation_service.advance(job_id, step_key, message)


async def run_ai_generation_job(
    *,
    job_id: str,
    req: TaskGenerateRequest,
    task_service: TaskService,
    scheduler_service: SchedulerService,
    generation_service: TaskGenerationService,
) -> None:
    output_filename = build_criteria_filename(req.keyword)
    platform = getattr(req, "platform", "xianyu") or "xianyu"
    try:
        await advance_job(
            generation_service,
            job_id,
            "prepare",
            "已接收请求，开始准备分析标准。",
        )

        async def report_progress(step_key: str, message: str) -> None:
            await advance_job(generation_service, job_id, step_key, message)

        reference_file = _resolve_reference_criteria(platform)
        metadata = await generate_task_metadata(
            user_description=req.description or "",
            reference_file_path=reference_file,
            platform=platform,
            progress_callback=report_progress,
        )

        await advance_job(
            generation_service,
            job_id,
            "persist",
            f"正在保存分析标准到 {output_filename}。",
        )
        await save_generated_criteria(output_filename, metadata.criteria)

        # 决定最终关键词列表:优先用户手填,AI 生成的作为补充
        final_keywords: list[str] = []
        user_keyword = (req.keyword or "").strip()
        if user_keyword:
            final_keywords.append(user_keyword)
        for kw in metadata.all_keywords:
            if kw and kw not in final_keywords:
                final_keywords.append(kw)

        # 无论是否有多个关键词都推进这一步,保证状态机进度条完整
        if len(final_keywords) > 1:
            keywords_msg = (
                f"AI 生成了 {len(final_keywords)} 个搜索关键词候选:"
                f" {', '.join(final_keywords[:5])}"
                + (f" 等 {len(final_keywords)} 个" if len(final_keywords) > 5 else "")
            )
        else:
            keywords_msg = "使用单一关键词,无需生成候选。"
        await advance_job(generation_service, job_id, "keywords", keywords_msg)

        await advance_job(
            generation_service,
            job_id,
            "task",
            "分析标准已生成，正在创建任务记录。",
        )
        task = await task_service.create_task(
            build_task_create(
                req,
                output_filename,
                search_keywords=final_keywords,
            )
        )
        await reload_scheduler(task_service, scheduler_service)
        await generation_service.complete(job_id, task, f"任务“{req.task_name}”创建完成。")
    except Exception as exc:
        if os.path.exists(output_filename):
            os.remove(output_filename)
        await generation_service.fail(job_id, f"AI 任务生成失败: {exc}")
