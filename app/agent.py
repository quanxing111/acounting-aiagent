"""Agents SDK 层：把「今天打车 32 块」这类自然语言变成工具调用。

模型可通过环境变量切换，默认走 OpenAI 兼容协议（chat/completions），
所以 OpenAI / DeepSeek / 通义千问(DashScope) / 本地 vLLM 都能直接用：

    ACCOUNTING_LLM_API_KEY   API Key
    ACCOUNTING_LLM_BASE_URL  兼容端点，例如 https://dashscope.aliyuncs.com/compatible-mode/v1
    ACCOUNTING_LLM_MODEL     模型名，例如 gpt-4.1-mini / qwen-plus / deepseek-chat
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import Any, Literal, Optional

from agents import (
    Agent,
    OpenAIChatCompletionsModel,
    RunConfig,
    function_tool,
    set_tracing_disabled,
)
from pydantic import BaseModel, Field

from . import db

WEEKDAY_CN = "一二三四五六日"

# 会改动数据的工具：前端收到这些调用后会重新拉一次记录刷新页面
WRITE_TOOLS = {"add_records", "update_record", "delete_record"}


# ---------------------------------------------------------------- 模型配置

def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return ""


def llm_config() -> dict[str, str]:
    return {
        "api_key": _env("ACCOUNTING_LLM_API_KEY", "OPENAI_API_KEY", "DASHSCOPE_API_KEY"),
        "base_url": _env("ACCOUNTING_LLM_BASE_URL", "OPENAI_BASE_URL"),
        "model": _env("ACCOUNTING_LLM_MODEL", "OPENAI_MODEL") or "gpt-4.1-mini",
    }


def build_model():
    """有 base_url 时走 OpenAI 兼容的 chat/completions，否则交给 SDK 默认的 Responses API。"""
    cfg = llm_config()
    if not cfg["base_url"]:
        return cfg["model"]

    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"] or "not-needed")
    return OpenAIChatCompletionsModel(model=cfg["model"], openai_client=client)


def build_run_config(model: Any = None) -> RunConfig:
    cfg = llm_config()
    return RunConfig(
        model=model if model is not None else build_model(),
        workflow_name="轻记账 AI 记账",
        # 第三方/兼容端点没有 OpenAI 的 trace 服务，默认关掉，省得报错
        tracing_disabled=os.environ.get("ACCOUNTING_LLM_TRACING") != "1",
    )


set_tracing_disabled(True)


# ---------------------------------------------------------------- 工具入参

class NewRecord(BaseModel):
    type: Literal["expense", "income"] = Field(default="expense", description="支出填 expense，收入填 income")
    amount: float = Field(description="金额，正数，单位元")
    main: str = Field(description="大类，必须来自分类表")
    sub: str = Field(default="", description="小类，必须是大类下已有的小类；没有合适的就留空")
    date: str = Field(default="", description="日期 YYYY-MM-DD；留空表示今天")
    note: str = Field(default="", description="备注，保留用户原话里的关键信息，最多 60 字")


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


# ---------------------------------------------------------------- 工具实现

@function_tool
def add_records(records: list[NewRecord]) -> str:
    """把一笔或多笔账写入数据库。

    用户一句话里包含多笔消费时（例如「昨天买菜 45，前天打车 12」），一次性传入多条。
    只有确认了金额和分类才调用本工具；金额缺失时先向用户提问。
    """
    created, rejected = [], []
    for item in records:
        cat, sub = db.find_category(item.type, item.main, item.sub or "")
        if cat is None:
            rejected.append({"input": item.model_dump(), "reason": f"分类「{item.main}」不在分类表里"})
            continue
        note = (item.note or "").strip()
        if item.sub and not sub:
            # 小类不在表里就降级：清空小类，把原词保留进备注
            note = (note + " " + item.sub).strip()
        try:
            rec = db.add_record(
                rtype=item.type,
                amount=item.amount,
                date=item.date or None,
                main=cat["main"],
                sub=sub,
                note=note,
                source="ai",
            )
        except ValueError as exc:
            rejected.append({"input": item.model_dump(), "reason": str(exc)})
            continue
        created.append(rec)
    return _dump({"created": created, "created_count": len(created), "rejected": rejected})


@function_tool
def query_records(
    start_date: str = "",
    end_date: str = "",
    type: str = "",
    main: str = "",
    keyword: str = "",
    limit: int = 50,
) -> str:
    """按条件查询明细记录。

    start_date / end_date 形如 2026-09-01，也可只写 2026-09；留空表示不限。
    type 填 expense（支出）或 income（收入）或留空表示全部。
    main 按大类过滤，keyword 在备注/分类里模糊搜索。
    """
    rows = db.list_records(
        start_date=start_date or None,
        end_date=end_date or None,
        rtype=type or None,
        main=main or None,
        keyword=keyword or None,
        limit=max(1, min(int(limit or 50), 200)),
    )
    return _dump(
        {
            "count": len(rows),
            "total": round(sum(r["amount"] for r in rows), 2),
            "records": rows,
        }
    )


@function_tool
def stats_by_category(start_date: str = "", end_date: str = "", type: str = "expense") -> str:
    """按大类（附带小类）统计数据：总额、笔数、占比、平均单笔。

    回答「这个月花了多少」「哪类花得最多」「餐饮花了多少」这类问题用这个工具。
    """
    return _dump(db.stats_by_category(start_date or None, end_date or None, type or "expense"))


@function_tool
def stats_by_day(
    start_date: str = "",
    end_date: str = "",
    type: str = "expense",
    group: Literal["day", "month"] = "day",
) -> str:
    """按天（或按月）统计金额，用于「这周每天花了多少」「哪天花得最多」这类问题。

    时间跨度超过 60 天时建议 group=month。
    """
    return _dump(db.stats_by_day(start_date or None, end_date or None, type or "expense", group))


@function_tool
def update_record(
    record_id: str,
    type: Optional[str] = None,
    amount: Optional[float] = None,
    date: Optional[str] = None,
    main: Optional[str] = None,
    sub: Optional[str] = None,
    note: Optional[str] = None,
) -> str:
    """修改已有记录，只传需要改的字段。record_id 先通过 query_records 查到。

    改分类时同样要保证 main/sub 在分类表里。
    """
    patch: dict[str, Any] = {}
    if type:
        patch["type"] = "income" if type == "income" else "expense"
    if amount is not None:
        patch["amount"] = amount
    if date:
        patch["date"] = date
    if main:
        target_type = patch.get("type") or (db.get_record(record_id) or {}).get("type", "expense")
        cat, sub_ok = db.find_category(target_type, main, sub or "")
        if cat is None:
            return _dump({"ok": False, "reason": f"分类「{main}」不在分类表里"})
        patch["main"] = cat["main"]
        patch["sub"] = sub_ok
    elif sub is not None:
        patch["sub"] = sub
    if note is not None:
        patch["note"] = note
    updated = db.update_record(record_id, **patch)
    if updated is None:
        return _dump({"ok": False, "reason": f"没有找到 id={record_id} 的记录"})
    return _dump({"ok": True, "record": updated})


@function_tool
def delete_record(record_id: str) -> str:
    """删除一笔记录。必须先用 query_records 找到目标记录并与用户确认。"""
    ok = db.delete_record(record_id)
    return _dump({"ok": ok, "id": record_id, "reason": None if ok else "记录不存在"})


TOOLS = [
    add_records,
    query_records,
    stats_by_category,
    stats_by_day,
    update_record,
    delete_record,
]


# ---------------------------------------------------------------- 提示词

def _taxonomy_text() -> str:
    tax = db.get_taxonomy()
    lines = []
    for rtype, label in (("expense", "支出"), ("income", "收入")):
        lines.append(f"【{label}】")
        for cat in tax.get(rtype, []):
            subs = "、".join(cat.get("subs") or []) or "（无小类）"
            lines.append(f"- {cat['main']}（小类：{subs}）")
    return "\n".join(lines)


def build_instructions() -> str:
    today = date.today()
    yesterday = today - timedelta(days=1)
    return f"""改动，，你是手机记账应用「轻记账」里的记账助手，负责把用户的白话变成账本里的一条条记录，也负责回答账目问题。

## 当前时间
今天：{today.isoformat()}（星期{WEEKDAY_CN[today.weekday()]}），昨天：{yesterday.isoformat()}。
所有相对日期都以今天为基准：「今天」= {today.isoformat()}，「昨天」={yesterday.isoformat()}，「前天」= {(today - timedelta(days=2)).isoformat()}。
「本周」从本周一开始，「本月」从当月 1 号开始，「近 7 天」含今天往前数 7 天。

## 分类表（只能用它，不许自创）
{_taxonomy_text()}

## 怎么归类
- 只填分类表里出现过的大类；小类只能填该大类下面列出的词，没有合适的就留空，把原词放进备注。
- 常用映射：食堂/外卖/买菜/水果/奶茶/零食 → 餐饮；房租/水电/话费/地铁（含打车、公交、滴滴等日常通勤） → 生活缴费；
  旅行中的车费/住宿/门票 → 出去玩；教材、课程、文具 → 学习；送礼、份子钱、发出去的红包 → 礼金；网购、日用百货 → 日用品及网购。
- 收入侧：发工资 → 工资；单位/学校补助 → 补贴；别人给的红包、压岁钱 → 红包；退货退款 → 退款。
- 实在判断不出类别，用「其他」，并把用户原话写进备注，不要瞎猜。

## 解析规则
- 金额支持「32」「32.5」「三十二块」「一百二」等写法，统一取正数；「花了/付了/买了」是支出，「发了/收到/报销/退款」是收入。
- 一句话里有好几笔就拆成多条，用一次 add_records 全部写入。
- 只记用户明确说了金额的账。金额缺失、或分不清收入还是支出且影响归类时，先问一句再动手。
- 不要重复记账：用户说「刚才那笔记错了」这类修正，用 query_records 找到记录再 update_record 或 delete_record。

## 工具使用
- 写入必须调用 add_records，不要只在回复里说「已记下」。
- 查询、统计必须调用 query_records / stats_by_category / stats_by_day，用工具结果回答，绝不编造数字。
- 回答金额统计时给出具体日期区间和金额，数字保留两位小数。

## 回复风格
- 中文、简短、像记账 App 的提示语，最多两三句，不要输出 Markdown 表格或代码块。
- 记账成功后按这个格式确认：已记账：餐饮·食堂 ¥32.00 · 2026-09-13（今天）
- 记了多笔就在后面追加一行「共 N 笔，合计 ¥XX.XX」。
- 出错或信息不够时，直接说你缺什么，例如「金额是多少？打车 32 块这样告诉我就行」。
"""


def build_agent(model: Any = None) -> Agent:
    """每次请求重建，保证分类表/日期都是最新的；这一步不联网，开销可忽略。"""
    return Agent(
        name="轻记账助手",
        instructions=build_instructions(),
        tools=TOOLS,
        model=model,
    )
