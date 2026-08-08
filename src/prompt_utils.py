import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional

import aiofiles

from src.infrastructure.external.ai_client import AIClient

# The meta-prompt to instruct the AI
META_PROMPT_TEMPLATE = """
你是一位世界级的AI提示词工程大师。你的任务是根据用户提供的【购买需求】，模仿一个【参考范例】，为闲鱼监控机器人的AI分析模块（代号 EagleEye）生成一份全新的【分析标准】文本。

你的输出必须严格遵循【参考范例】的结构、语气和核心原则，但内容要完全针对用户的【购买需求】进行定制。最终生成的文本将作为AI分析模块的思考指南。

---
这是【参考范例】（`macbook_criteria.txt`）：
```text
{reference_text}
```
---

这是用户的【购买需求】：
```text
{user_description}
```
---

请现在开始生成全新的【分析标准】文本。请注意：
1.  **只输出新生成的文本内容**，不要包含任何额外的解释、标题或代码块标记。
2.  保留范例中的 `[V6.3 核心升级]`、`[V6.4 逻辑修正]` 等版本标记，这有助于保持格式一致性。
3.  将范例中所有与 "MacBook" 相关的内容，替换为与用户需求商品相关的内容。
4.  思考并生成针对新商品类型的“一票否决硬性原则”和“危险信号清单”。
"""


KEYWORDS_META_PROMPT_TEMPLATE = """
你是一位电商搜索关键词专家。根据用户的【购买需求】,为 {platform_label} 生成一组用于\
搜索的关键词候选。

**平台特点:**
{platform_hint}

**用户需求:**
```text
{user_description}
```

**任务:**
生成 3~6 个多样化的搜索关键词。它们应该:
- 覆盖用户意图的不同表达(如型号变体、品牌加型号、常用别名)
- 每个关键词单独作为 keyword 参数搜索时都能匹配相关商品
- 单个关键词不要过长(2~4 个词最理想);词太多会因为 AND 逻辑导致 0 命中
- 优先使用 {platform_language},避免拼音/罗马字与本地文字混用
- 主关键词(primary)是最"标准"的写法,其他是候选

**输出格式**(严格 JSON,不要 markdown 代码块):
{{
  "primary": "主关键词",
  "alternatives": ["候选1", "候选2", "候选3"]
}}
"""


PLATFORM_KEYWORD_HINTS = {
    "xianyu": {
        "label": "闲鱼(中国二手交易平台)",
        "hint": (
            "- 中国用户用中文搜索,可以用型号编号、品牌+品类、俗称\n"
            "- 例:MacBook Air M1、iPhone 15 Pro、索尼 A7M4、任天堂 Switch"
        ),
        "language": "中文",
    },
    "mercari": {
        "label": "Mercari(日本二手交易平台)",
        "hint": (
            "- 日本用户主要用日文搜索,但英文品牌名/型号也常见\n"
            "- Mercari 搜索是 AND 逻辑,词太多会 0 命中\n"
            "- 例:iMac M1、MacBook Air M2 16GB、iPhone 15 Pro、"
            "α7 IV、Leica Q3、ニンテンドースイッチ"
        ),
        "language": "日语(或英日混合)",
    },
    "hoyoyo": {
        "label": "Hoyoyo(日购聚合站,覆盖Yahoo拍卖/Mercari/雅虎购物)",
        "hint": (
            "- 聚合多个日本站点,搜索逻辑与各站点类似,优先用日文/型号关键词\n"
            "- 词太多会因 AND 逻辑导致 0 命中,建议 2~4 个词\n"
            "- 例:iMac M1、MacBook Air M2、α7 IV、ニンテンドースイッチ"
        ),
        "language": "日语(或英日混合)",
    },
}


ProgressCallback = Callable[[str, str], Awaitable[None]]


@dataclass
class TaskMetadata:
    """AI 一次生成的任务元数据:筛选标准 + 搜索关键词候选。"""
    criteria: str
    primary_keyword: str = ""
    alternative_keywords: List[str] = field(default_factory=list)

    @property
    def all_keywords(self) -> List[str]:
        seen = set()
        result = []
        for kw in [self.primary_keyword, *self.alternative_keywords]:
            key = kw.strip().lower()
            if key and key not in seen:
                seen.add(key)
                result.append(kw.strip())
        return result


async def _report_progress(
    progress_callback: Optional[ProgressCallback],
    step_key: str,
    message: str,
) -> None:
    if progress_callback:
        await progress_callback(step_key, message)


def _read_reference_text(reference_file_path: str) -> str:
    try:
        with open(reference_file_path, "r", encoding="utf-8") as file:
            return file.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"参考文件未找到: {reference_file_path}")
    except IOError as exc:
        raise IOError(f"读取参考文件失败: {exc}")


async def _request_generated_text(ai_client: AIClient, prompt: str) -> str:
    print("正在调用AI生成新的分析标准，请稍候...")
    try:
        generated_text = await ai_client._call_ai(
            [{"role": "user", "content": prompt}],
            temperature=0.5,
            max_output_tokens=800,
            enable_json_output=False,
        )
    except Exception as exc:
        print(f"调用 OpenAI API 时出错: {exc}")
        raise

    print("AI已成功生成内容。")
    return generated_text.strip()


async def _request_keywords_json(
    ai_client: AIClient,
    platform: str,
    user_description: str,
) -> dict:
    """让 AI 输出关键词 JSON。返回 {"primary": "...", "alternatives": [...]}"""
    hint = PLATFORM_KEYWORD_HINTS.get(platform, PLATFORM_KEYWORD_HINTS["xianyu"])
    prompt = KEYWORDS_META_PROMPT_TEMPLATE.format(
        platform_label=hint["label"],
        platform_hint=hint["hint"],
        platform_language=hint["language"],
        user_description=user_description or "(未提供,请基于常识生成通用关键词)",
    )
    print(f"正在调用 AI 生成 {platform} 关键词候选...")
    try:
        raw = await ai_client._call_ai(
            [{"role": "user", "content": prompt}],
            temperature=0.3,
            max_output_tokens=300,
            enable_json_output=True,
        )
    except Exception as exc:
        print(f"生成关键词时出错: {exc}")
        raise

    return _parse_keywords_response(raw)


def _parse_keywords_response(raw_text: str) -> dict:
    """解析 AI 返回的关键词 JSON,健壮地兜底。"""
    if not raw_text:
        return {"primary": "", "alternatives": []}

    text = raw_text.strip()

    # 剥离可能存在的 markdown 代码块
    if text.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        print(f"警告:AI 关键词响应不是合法 JSON,原文: {text[:300]}")
        return {"primary": "", "alternatives": []}

    if not isinstance(data, dict):
        return {"primary": "", "alternatives": []}

    primary = str(data.get("primary") or "").strip()
    alternatives_raw = data.get("alternatives") or []
    if not isinstance(alternatives_raw, list):
        alternatives_raw = []
    alternatives = [
        str(item).strip() for item in alternatives_raw if str(item).strip()
    ]
    return {"primary": primary, "alternatives": alternatives}


async def _close_ai_client(
    ai_client: AIClient,
    active_error: BaseException | None,
) -> None:
    try:
        await ai_client.close()
    except Exception as close_error:
        print(f"关闭 AI 客户端时出错: {close_error}")
        if active_error is None:
            raise


async def generate_task_metadata(
    user_description: str,
    reference_file_path: str,
    platform: str = "xianyu",
    progress_callback: Optional[ProgressCallback] = None,
) -> TaskMetadata:
    """
    一次 AI 生成两个产物:
      1. criteria 文本(analysis 标准)
      2. keywords 候选(primary + alternatives)
    """
    ai_client = AIClient()
    active_error: BaseException | None = None
    try:
        if not ai_client.is_available():
            ai_client.refresh()
        if not ai_client.is_available():
            raise RuntimeError("AI客户端未初始化，无法生成分析标准。请检查.env配置。")

        await _report_progress(progress_callback, "reference", "正在读取参考文件。")
        print(f"正在读取参考文件: {reference_file_path}")
        reference_text = _read_reference_text(reference_file_path)

        await _report_progress(progress_callback, "prompt", "正在构建发送给 AI 的指令。")
        print("正在构建发送给AI的指令...")
        criteria_prompt = META_PROMPT_TEMPLATE.format(
            reference_text=reference_text,
            user_description=user_description,
        )

        await _report_progress(progress_callback, "llm", "正在调用 AI 生成分析标准。")
        criteria_text = await _request_generated_text(ai_client, criteria_prompt)

        await _report_progress(
            progress_callback, "keywords", "正在生成搜索关键词候选。"
        )
        try:
            keywords_data = await _request_keywords_json(
                ai_client, platform, user_description
            )
        except Exception as kw_exc:
            # 关键词生成失败不阻塞任务:退回空列表,让用户手填 keyword
            print(f"警告:关键词生成失败,任务将只使用手填 keyword: {kw_exc}")
            keywords_data = {"primary": "", "alternatives": []}

        return TaskMetadata(
            criteria=criteria_text,
            primary_keyword=keywords_data.get("primary", ""),
            alternative_keywords=keywords_data.get("alternatives", []),
        )
    except Exception as exc:
        active_error = exc
        raise
    finally:
        await _close_ai_client(ai_client, active_error)


async def generate_criteria(
    user_description: str,
    reference_file_path: str,
    progress_callback: Optional[ProgressCallback] = None,
) -> str:
    """向后兼容:只生成 criteria 文本。新代码请用 generate_task_metadata。"""
    metadata = await generate_task_metadata(
        user_description=user_description,
        reference_file_path=reference_file_path,
        platform="xianyu",  # 老调用点固定为闲鱼
        progress_callback=progress_callback,
    )
    return metadata.criteria


async def update_config_with_new_task(new_task: dict, config_file: str = "config.json"):
    """
    将一个新任务添加到指定的JSON配置文件中。
    """
    print(f"正在更新配置文件: {config_file}")
    try:
        # 读取现有配置
        config_data = []
        if os.path.exists(config_file):
            async with aiofiles.open(config_file, 'r', encoding='utf-8') as f:
                content = await f.read()
                # 处理空文件的情况
                if content.strip():
                    try:
                        config_data = json.loads(content)
                        print(f"成功读取现有配置，当前任务数量: {len(config_data)}")
                    except json.JSONDecodeError as e:
                        print(f"解析配置文件失败，将创建新配置: {e}")
                        config_data = []
        else:
            print(f"配置文件不存在，将创建新文件: {config_file}")

        # 追加新任务
        config_data.append(new_task)

        # 写回配置文件
        async with aiofiles.open(config_file, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(config_data, ensure_ascii=False, indent=2))
            print(f"配置文件写入完成")

        print(f"成功！新任务 '{new_task.get('task_name')}' 已添加到 {config_file} 并已启用。")
        return True
    except json.JSONDecodeError as e:
        error_msg = f"错误: 配置文件 {config_file} 格式错误，无法解析: {e}"
        sys.stderr.write(error_msg + "\n")
        print(error_msg)
        return False
    except IOError as e:
        error_msg = f"错误: 读写配置文件失败: {e}"
        sys.stderr.write(error_msg + "\n")
        print(error_msg)
        return False
    except Exception as e:
        error_msg = f"错误: 更新配置文件时发生未知错误: {e}"
        sys.stderr.write(error_msg + "\n")
        print(error_msg)
        import traceback
        print(traceback.format_exc())
        return False
