"""离线端到端自检：不联网、不需要 API Key。

用 Agents SDK 自带的 ScriptedModel 假装模型返回一次 add_records 工具调用，
验证「用户说话 -> 工具 -> 落库 -> SSE 流式输出 -> 记录接口」整条链路。

运行：
    .\\condaenv1\\python.exe tests\\test_ai_flow.py
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 用临时库，绝不碰真实账本
_tmpdir = tempfile.mkdtemp(prefix="qingjizhang-test-")
os.environ["ACCOUNTING_DB"] = os.path.join(_tmpdir, "test.db")

import uvicorn  # noqa: E402
from agents import Agent  # noqa: E402
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call  # noqa: E402

from app import agent as agent_mod  # noqa: E402
from app import db, main  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _scripted_agent() -> Agent:
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        "add_records",
                        {
                            "records": [
                                {
                                    "type": "expense",
                                    "amount": 32,
                                    "main": "生活缴费",
                                    "sub": "地铁",
                                    "note": "打车",
                                }
                            ]
                        },
                        call_id="call_add_1",
                    )
                ]
            ),
            ModelStep(output=[assistant_message("已记账：生活缴费·地铁 ¥32.00 · 今天")]),
        ]
    )
    return agent_mod.build_agent(model=model)


def _post_sse(url: str, payload: dict) -> list[dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    events: list[dict] = []
    with urllib.request.urlopen(req, timeout=30) as resp:
        assert resp.headers.get("content-type", "").startswith("text/event-stream")
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main_check() -> int:
    db.init_db()

    # 1) 真实 Agent 的构造（不联网）与工具清单
    real_agent = agent_mod.build_agent()
    tool_names = {t.name for t in real_agent.tools}
    assert tool_names == {
        "add_records",
        "query_records",
        "stats_by_category",
        "stats_by_day",
        "update_record",
        "delete_record",
    }, tool_names
    print("[1/6] Agent 工具定义 OK:", ", ".join(sorted(tool_names)))

    # 2) 起一个真实 HTTP 服务，但用假模型
    main.build_chat_agent = _scripted_agent  # type: ignore[assignment]
    port = _free_port()
    config = uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            _get_json(base + "/api/health")
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise SystemExit("服务没起来")
    print(f"[2/6] 服务已启动 {base}")

    # 3) 流式对话
    events = _post_sse(base + "/api/chat", {"message": "今天打车 32 块", "history": []})
    kinds = [e["type"] for e in events]
    assert "tool_start" in kinds, kinds
    assert "tool_done" in kinds, kinds
    assert "records_changed" in kinds, kinds
    assert kinds[-1] == "done", kinds
    tool_event = next(e for e in events if e["type"] == "tool_start")
    assert tool_event["tool"] == "add_records", tool_event
    assert "32" in tool_event["args"], tool_event
    reply = events[-1]["reply"]
    assert "¥32.00" in reply, reply
    deltas = "".join(e["delta"] for e in events if e["type"] == "text")
    assert "已记账" in deltas, deltas
    print("[3/6] SSE 流式事件 OK:", " -> ".join(dict.fromkeys(kinds)), "|", reply)

    # 4) 数据真的落库了
    data = _get_json(base + "/api/records")
    assert data["count"] == 1, data
    rec = data["records"][0]
    assert rec["main"] == "生活缴费" and rec["sub"] == "地铁", rec
    assert rec["amount"] == 32.0 and rec["note"] == "打车", rec
    assert rec["date"] == time.strftime("%Y-%m-%d"), rec
    print("[4/6] 落库 OK:", rec["date"], rec["main"], rec["sub"], rec["amount"], rec["source"])

    # 5) 查询/统计工具（直接打数据库层）
    stats = db.stats_by_category(None, None, "expense")
    assert stats["total"] == 32.0 and stats["count"] == 1, stats
    found = db.list_records(keyword="打车")
    assert len(found) == 1, found
    print("[5/6] 统计/查询 OK: 合计", stats["total"], "| 命中关键词", len(found), "条")

    # 6) 前端整表同步 + 分类表同步
    synced = json.loads(
        urllib.request.urlopen(
            urllib.request.Request(
                base + "/api/records",
                data=json.dumps(
                    {
                        "records": [
                            {
                                "id": "from-frontend",
                                "type": "income",
                                "date": "",
                                "main": "红包",
                                "sub": "",
                                "amount": 66,
                                "note": "前端同步",
                            }
                        ]
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="PUT",
            ),
            timeout=15,
        ).read().decode("utf-8")
    )
    assert synced["total"] == 2, synced

    # replace=true 表示「本地列表就是全量」，库里的 AI 记录应被清掉
    replaced = json.loads(
        urllib.request.urlopen(
            urllib.request.Request(
                base + "/api/records",
                data=json.dumps({"records": [], "replace": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="PUT",
            ),
            timeout=15,
        ).read().decode("utf-8")
    )
    assert replaced["total"] == 0, replaced
    schema = _get_json(base + "/api/schema")
    assert schema["taxonomy"]["expense"][0]["main"] == "餐饮", schema
    print("[6/6] 前端同步 OK:", synced)

    server.should_exit = True
    time.sleep(0.3)
    print("\n全部通过 ✅  （临时库：%s）" % os.environ["ACCOUNTING_DB"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main_check())
