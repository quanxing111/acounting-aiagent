"""FastAPI 服务：静态托管前端 + 记录接口 + 流式对话接口（SSE）。

启动：
    .\\condaenv1\\python.exe -m app.main
然后浏览器打开 http://127.0.0.1:8000
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import uvicorn
from agents import RawResponsesStreamEvent, RunItemStreamEvent, Runner
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from openai.types.responses import ResponseTextDeltaEvent
from pydantic import BaseModel, Field

from . import agent as agent_mod
from . import db
from .agent import WRITE_TOOLS

BASE_DIR = db.BASE_DIR

# 启动时读项目根目录的 .env（存在就加载，不存在也不报错）
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:
    pass

# 只允许从这里读前端文件，避免把 condaenv1 / data 暴露出去
STATIC_FILES = {
    "index.html",
    "manifest.json",
    "sw.js",
    "icon-180.png",
    "icon-192.png",
    "icon-512.png",
    "favicon.ico",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="轻记账 AI 后端", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- 请求体

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    history: list[dict[str, Any]] = Field(default_factory=list)


class RecordIn(BaseModel):
    id: str | None = None
    type: str = "expense"
    date: str = ""
    main: str
    sub: str = ""
    amount: float
    note: str = ""
    ts: int | None = None


class RecordsIn(BaseModel):
    records: list[RecordIn]
    replace: bool = False


# ---------------------------------------------------------------- Agent 流式输出

def build_chat_agent():
    """Swappable hook：测试里会替换成 ScriptedModel 版本。"""
    return agent_mod.build_agent()


def _short(text: Any, limit: int = 240) -> str:
    s = "" if text is None else str(text)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _tool_summary(tool: str, raw_output: Any) -> str:
    """把工具返回的 JSON 压成一行给前端展示。"""
    text = "" if raw_output is None else str(raw_output)
    try:
        data = json.loads(text)
    except Exception:
        return _short(text, 120)
    if isinstance(data, dict):
        if "created_count" in data:
            n = data.get("created_count", 0)
            rows = data.get("created") or []
            detail = "；".join(
                f"{r.get('main', '')}{('·' + r['sub']) if r.get('sub') else ''} ¥{float(r.get('amount', 0)):.2f}"
                for r in rows[:3]
            )
            bad = data.get("rejected") or []
            suffix = f"，{len(bad)} 条被拒绝" if bad else ""
            return f"写入 {n} 笔{suffix}" + (f"：{detail}" if detail else "")
        if "categories" in data and "total" in data:
            return f"{len(data.get('categories') or [])} 个大类，合计 ¥{float(data['total']):.2f}，共 {data.get('count', 0)} 笔"
        if "days" in data and "total" in data:
            return f"{len(data.get('days') or [])} 个{('月' if data.get('group') == 'month' else '天')}，合计 ¥{float(data['total']):.2f}"
        if "records" in data and "count" in data:
            return f"查到 {data.get('count', 0)} 条，合计 ¥{float(data.get('total') or 0):.2f}"
        if data.get("ok") is True:
            return "已更新" if tool == "update_record" else "已删除"
        if data.get("ok") is False:
            return f"失败：{_short(data.get('reason'), 80)}"
    return _short(text, 120)


async def stream_agent_events(
    message: str, history: list[dict[str, Any]] | None = None
) -> AsyncIterator[dict[str, Any]]:
    """把 Agents SDK 的事件流翻译成前端好处理的小 JSON。"""
    payload: list[dict[str, Any]] = [*(history or []), {"role": "user", "content": message}]
    pending_tool = ""
    try:
        chat_agent = build_chat_agent()
        run_config = agent_mod.build_run_config(getattr(chat_agent, "model", None))
        result = Runner.run_streamed(chat_agent, input=payload, run_config=run_config)

        async for event in result.stream_events():
            if isinstance(event, RawResponsesStreamEvent):
                if isinstance(event.data, ResponseTextDeltaEvent) and event.data.delta:
                    yield {"type": "text", "delta": event.data.delta}
                continue
            if not isinstance(event, RunItemStreamEvent):
                continue

            if event.name == "tool_called":
                raw = getattr(event.item, "raw_item", None)
                pending_tool = getattr(raw, "name", "") or "tool"
                yield {
                    "type": "tool_start",
                    "tool": pending_tool,
                    "args": _short(getattr(raw, "arguments", ""), 300),
                }
            elif event.name == "tool_output":
                tool = pending_tool
                output = getattr(event.item, "output", None)
                yield {"type": "tool_done", "tool": tool, "summary": _tool_summary(tool, output)}
                if tool in WRITE_TOOLS:
                    yield {"type": "records_changed"}

        reply = result.final_output
        if reply is None:
            reply = "".join(
                getattr(item, "output", "") or ""
                for item in getattr(result, "new_items", [])
                if getattr(item, "type", "") == "message_output_item"
            )
        yield {"type": "done", "reply": str(reply or "").strip()}
    except Exception as exc:  # 网络/鉴权/模型报错都在这里兜住，前端能给出提示
        yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}


def sse(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    async def gen() -> AsyncIterator[str]:
        yield ": connected\n\n"
        async for event in stream_agent_events(req.message, req.history):
            yield sse(event)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------- 记录接口

@app.get("/api/records")
def get_records(
    start: str = "",
    end: str = "",
    type: str = "",
    main: str = "",
    keyword: str = "",
) -> dict[str, Any]:
    rows = db.list_records(
        start_date=start or None,
        end_date=end or None,
        rtype=type or None,
        main=main or None,
        keyword=keyword or None,
    )
    return {"records": rows, "count": len(rows)}


@app.post("/api/records")
def create_record(item: RecordIn) -> dict[str, Any]:
    cat, sub = db.find_category(item.type, item.main, item.sub)
    if cat is None:
        raise HTTPException(status_code=400, detail=f"分类「{item.main}」不在分类表里")
    record = db.add_record(
        rtype=item.type,
        amount=item.amount,
        date=item.date or None,
        main=cat["main"],
        sub=sub,
        note=item.note,
        source="client",
        record_id=item.id,
    )
    return {"record": record}


@app.put("/api/records")
def sync_records(body: RecordsIn) -> dict[str, Any]:
    payload = [r.model_dump() for r in body.records]
    if body.replace:
        count = db.replace_records(payload, source="client")
    else:
        count = db.upsert_records(payload, source="client")
    return {"ok": True, "synced": count, "total": len(db.list_records())}


@app.delete("/api/records/{record_id}")
def remove_record(record_id: str) -> dict[str, Any]:
    if not db.delete_record(record_id):
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"ok": True}


# ---------------------------------------------------------------- 分类与健康检查

@app.get("/api/schema")
def get_schema() -> dict[str, Any]:
    return {"taxonomy": db.get_taxonomy()}


@app.put("/api/schema")
def put_schema(body: dict[str, Any]) -> dict[str, Any]:
    taxonomy = body.get("taxonomy") or body
    try:
        clean = db.set_taxonomy(taxonomy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "taxonomy": clean}


@app.get("/api/health")
def health() -> dict[str, Any]:
    cfg = agent_mod.llm_config()
    return {
        "ok": True,
        "model": cfg["model"],
        "base_url": cfg["base_url"] or "(OpenAI 默认)",
        "has_api_key": bool(cfg["api_key"]),
        "records": len(db.list_records()),
        "db": db.DB_PATH,
    }


# ---------------------------------------------------------------- 前端静态文件

@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(BASE_DIR, "index.html"), media_type="text/html; charset=utf-8")


@app.get("/{filename}")
def static_file(filename: str):
    if filename not in STATIC_FILES:
        raise HTTPException(status_code=404, detail="Not Found")
    path = os.path.join(BASE_DIR, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(path)


def main() -> None:
    host = os.environ.get("ACCOUNTING_HOST", "127.0.0.1")
    port = int(os.environ.get("ACCOUNTING_PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
