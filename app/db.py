"""SQLite 存储层：记录表 + 分类表。

记录字段与前端 index.html 完全对齐：
    id, type('expense'|'income'), date('YYYY-MM-DD'), main, sub, amount, note, ts
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from calendar import monthrange
from contextlib import contextmanager
from typing import Any, Iterable

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("ACCOUNTING_DB") or os.path.join(BASE_DIR, "data", "accounting.db")

# 与 index.html 里的 CATS 保持一致；前端启动时会用 /api/schema 覆盖它，
# 所以你以后在前端加分类，Agent 会自动跟着变。
DEFAULT_TAXONOMY: dict[str, list[dict[str, Any]]] = {
    "expense": [
        {"main": "餐饮", "icon": "🍜", "subs": ["食堂", "小摊", "买菜", "水果", "零食", "奶茶"]},
        {"main": "生活缴费", "icon": "🏠", "subs": ["房租", "水电", "话费", "地铁"]},
        {"main": "出去玩", "icon": "🎒", "subs": ["车费", "住宿", "饮食", "门票"]},
        {"main": "学习", "icon": "📚", "subs": []},
        {"main": "礼金", "icon": "🧧", "subs": []},
        {"main": "日用品及网购", "icon": "🛒", "subs": []},
        {"main": "其他", "icon": "📦", "subs": []},
    ],
    "income": [
        {"main": "工资", "icon": "💼", "subs": []},
        {"main": "补贴", "icon": "💵", "subs": []},
        {"main": "红包", "icon": "🧧", "subs": []},
        {"main": "退款", "icon": "↩️", "subs": []},
        {"main": "其他", "icon": "📦", "subs": []},
    ],
}

_write_lock = threading.RLock()
_schema_cache: tuple[float, dict[str, list[dict[str, Any]]]] | None = None


def _db_dir() -> str:
    folder = os.path.dirname(os.path.abspath(DB_PATH))
    if folder:
        os.makedirs(folder, exist_ok=True)
    return folder


@contextmanager
def connect():
    """每次操作开一个短连接，避免多线程共享游标的坑。"""
    _db_dir()
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _write_lock, connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                id          TEXT PRIMARY KEY,
                type        TEXT NOT NULL,
                date        TEXT NOT NULL,
                main        TEXT NOT NULL,
                sub         TEXT NOT NULL DEFAULT '',
                amount      REAL NOT NULL,
                note        TEXT NOT NULL DEFAULT '',
                ts          INTEGER NOT NULL,
                source      TEXT NOT NULL DEFAULT 'manual',
                created_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_records_date ON records(date);
            CREATE INDEX IF NOT EXISTS idx_records_type_main ON records(type, main);

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        row = conn.execute("SELECT value FROM meta WHERE key = 'taxonomy'").fetchone()
        if row is None:
            import json

            conn.execute(
                "INSERT INTO meta(key, value) VALUES('taxonomy', ?)",
                (json.dumps(DEFAULT_TAXONOMY, ensure_ascii=False),),
            )


# ---------------------------------------------------------------- 分类表

def get_taxonomy() -> dict[str, list[dict[str, Any]]]:
    global _schema_cache
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'taxonomy'").fetchone()
    if row is None:
        return DEFAULT_TAXONOMY
    import json

    try:
        data = json.loads(row["value"])
    except Exception:
        return DEFAULT_TAXONOMY
    if not isinstance(data, dict) or not data.get("expense"):
        return DEFAULT_TAXONOMY
    _schema_cache = (time.time(), data)
    return data


def set_taxonomy(taxonomy: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    import json

    clean: dict[str, list[dict[str, Any]]] = {}
    for rtype in ("expense", "income"):
        items = []
        for cat in taxonomy.get(rtype) or []:
            main = str(cat.get("main", "")).strip()
            if not main:
                continue
            items.append(
                {
                    "main": main,
                    "icon": str(cat.get("icon", "💰")),
                    "subs": [str(s).strip() for s in (cat.get("subs") or []) if str(s).strip()],
                }
            )
        clean[rtype] = items
    if not clean["expense"]:
        raise ValueError("expense 分类不能为空")
    with _write_lock, connect() as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('taxonomy', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(clean, ensure_ascii=False),),
        )
    global _schema_cache
    _schema_cache = (time.time(), clean)
    return clean


def find_category(rtype: str, main: str, sub: str = "") -> tuple[dict[str, Any] | None, str]:
    """返回 (匹配到的大类, 校验后的 sub)。sub 不在表里就降级为空串。"""
    taxonomy = get_taxonomy()
    cats = taxonomy.get("expense" if rtype != "income" else "income", [])
    for cat in cats:
        if cat["main"] == main:
            subs = cat.get("subs") or []
            if sub and sub in subs:
                return cat, sub
            return cat, ""
    return None, ""


# ---------------------------------------------------------------- 日期

def today_str() -> str:
    return time.strftime("%Y-%m-%d")


def norm_date(value: str | None) -> str:
    """把 2026-09 / 2026 / 20260913 / 2026-9-3 之类的写法规整成 YYYY-MM-DD。"""
    if not value:
        return today_str()
    raw = str(value).strip().replace("/", "-").replace(".", "-")
    if raw in ("今天", "today", "now"):
        return today_str()
    parts = [p for p in raw.split("-") if p != ""]
    if len(parts) == 1 and len(parts[0]) == 8 and parts[0].isdigit():
        return f"{parts[0][:4]}-{parts[0][4:6]}-{parts[0][6:]}"
    if len(parts) == 1 and len(parts[0]) == 6 and parts[0].isdigit():
        return f"{parts[0][:4]}-{parts[0][4:6]}-01"
    if len(parts) == 3:
        y, m, d = (int(parts[0]), int(parts[1]), int(parts[2]))
        return f"{y:04d}-{m:02d}-{d:02d}"
    if len(parts) == 2:
        y, m = int(parts[0]), int(parts[1])
        return f"{y:04d}-{m:02d}-01"
    if len(parts) == 1 and parts[0].isdigit() and len(parts[0]) == 4:
        return f"{parts[0]}-01-01"
    raise ValueError(f"无法识别的日期：{value}")


def _range_bounds(start_date: str | None, end_date: str | None) -> tuple[str, str]:
    """把查询区间补齐成闭区间字符串；给 '2026-09' 这类写法自动撑到当月最后一天。"""
    start = norm_date(start_date) if start_date else "0000-01-01"
    if not end_date:
        end = "9999-12-31"
    else:
        raw = str(end_date).strip().replace("/", "-").replace(".", "-")
        parts = [p for p in raw.split("-") if p]
        if len(parts) == 2 and parts[1].isdigit():
            y, m = int(parts[0]), int(parts[1])
            end = f"{y:04d}-{m:02d}-{monthrange(y, m)[1]:02d}"
        elif len(parts) == 1 and parts[0].isdigit() and len(parts[0]) == 4:
            end = f"{parts[0]}-12-31"
        else:
            end = norm_date(end_date)
    return start, end


# ---------------------------------------------------------------- 记录读写

def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "type": row["type"],
        "date": row["date"],
        "main": row["main"],
        "sub": row["sub"],
        "amount": round(float(row["amount"]), 2),
        "note": row["note"],
        "ts": int(row["ts"]),
        "source": row["source"],
    }


def list_records(
    start_date: str | None = None,
    end_date: str | None = None,
    rtype: str | None = None,
    main: str | None = None,
    keyword: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if start_date or end_date:
        start, end = _range_bounds(start_date, end_date)
        where.append("date BETWEEN ? AND ?")
        params += [start, end]
    if rtype in ("expense", "income"):
        where.append("type = ?")
        params.append(rtype)
    if main:
        where.append("main = ?")
        params.append(main)
    if keyword:
        where.append("(note LIKE ? OR main LIKE ? OR sub LIKE ?)")
        like = f"%{keyword}%"
        params += [like, like, like]
    sql = "SELECT * FROM records"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date DESC, ts DESC"
    if limit:
        sql += " LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_record(record_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    return _row_to_dict(row) if row else None


def add_record(
    rtype: str,
    amount: float,
    date: str | None = None,
    main: str = "",
    sub: str = "",
    note: str = "",
    source: str = "ai",
    record_id: str | None = None,
) -> dict[str, Any]:
    rtype = "income" if rtype == "income" else "expense"
    if not main:
        raise ValueError("缺少分类 main")
    date = norm_date(date)
    amount = round(float(amount), 2)
    if amount <= 0:
        raise ValueError("金额必须大于 0")
    ts = int(time.time() * 1000)
    rec = {
        "id": record_id or (uuid.uuid4().hex[:12]),
        "type": rtype,
        "date": date,
        "main": main,
        "sub": sub or "",
        "amount": amount,
        "note": (note or "")[:60],
        "ts": ts,
        "source": source,
    }
    with _write_lock, connect() as conn:
        conn.execute(
            "INSERT INTO records(id, type, date, main, sub, amount, note, ts, source, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                rec["id"],
                rec["type"],
                rec["date"],
                rec["main"],
                rec["sub"],
                rec["amount"],
                rec["note"],
                ts,
                source,
                time.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
    return rec


def update_record(record_id: str, **fields: Any) -> dict[str, Any] | None:
    current = get_record(record_id)
    if current is None:
        return None
    allowed = ("type", "date", "main", "sub", "amount", "note")
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "date" in updates:
        updates["date"] = norm_date(str(updates["date"]))
    if "amount" in updates:
        updates["amount"] = round(float(updates["amount"]), 2)
    if "note" in updates:
        updates["note"] = str(updates["note"])[:60]
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        with _write_lock, connect() as conn:
            conn.execute(f"UPDATE records SET {sets} WHERE id = ?", [*updates.values(), record_id])
    return get_record(record_id)


def delete_record(record_id: str) -> bool:
    with _write_lock, connect() as conn:
        cur = conn.execute("DELETE FROM records WHERE id = ?", (record_id,))
        return cur.rowcount > 0


def _prepare_rows(items: Iterable[dict[str, Any]], source: str) -> list[tuple]:
    rows = []
    for it in items:
        if not it:
            continue
        rtype = "income" if it.get("type") == "income" else "expense"
        main = str(it.get("main") or "").strip()
        try:
            amount = round(float(it.get("amount")), 2)
        except (TypeError, ValueError):
            continue
        if not main or amount <= 0:
            continue
        try:
            date = norm_date(it.get("date"))
        except ValueError:
            continue
        rows.append(
            (
                str(it.get("id") or uuid.uuid4().hex[:12]),
                rtype,
                date,
                main,
                str(it.get("sub") or ""),
                amount,
                str(it.get("note") or "")[:60],
                int(it.get("ts") or time.time() * 1000),
                source,
                time.strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
    return rows


_UPSERT_SQL = (
    "INSERT INTO records(id, type, date, main, sub, amount, note, ts, source, created_at)"
    " VALUES(?,?,?,?,?,?,?,?,?,?)"
    " ON CONFLICT(id) DO UPDATE SET"
    "   type=excluded.type, date=excluded.date, main=excluded.main, sub=excluded.sub,"
    "   amount=excluded.amount, note=excluded.note, ts=excluded.ts"
)


def upsert_records(items: Iterable[dict[str, Any]], source: str = "client") -> int:
    """前端整表同步用：按 id 覆盖写入。"""
    rows = _prepare_rows(items, source)
    if not rows:
        return 0
    with _write_lock, connect() as conn:
        conn.executemany(_UPSERT_SQL, rows)
    return len(rows)


def replace_records(items: Iterable[dict[str, Any]], source: str = "client") -> int:
    """整表替换：按 id 覆盖写入，并删掉不在列表里的记录（首次同步 / 导入备份用）。"""
    rows = _prepare_rows(items, source)
    ids = [r[0] for r in rows]
    with _write_lock, connect() as conn:
        if rows:
            conn.executemany(_UPSERT_SQL, rows)
        if ids:
            placeholders = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM records WHERE id NOT IN ({placeholders})", ids)
        else:
            conn.execute("DELETE FROM records")
    return len(rows)


# ---------------------------------------------------------------- 统计

def _totals(items: list[dict[str, Any]]) -> dict[str, Any]:
    total = round(sum(r["amount"] for r in items), 2)
    return {"total": total, "count": len(items)}


def stats_by_category(
    start_date: str | None = None,
    end_date: str | None = None,
    rtype: str = "expense",
) -> dict[str, Any]:
    items = list_records(start_date, end_date, rtype)
    total = round(sum(r["amount"] for r in items), 2)
    buckets: dict[str, dict[str, Any]] = {}
    for r in items:
        b = buckets.setdefault(
            r["main"], {"main": r["main"], "amount": 0.0, "count": 0, "subs": {}}
        )
        b["amount"] = round(b["amount"] + r["amount"], 2)
        b["count"] += 1
        if r["sub"]:
            b["subs"][r["sub"]] = round(b["subs"].get(r["sub"], 0) + r["amount"], 2)
    rows = sorted(buckets.values(), key=lambda x: x["amount"], reverse=True)
    for b in rows:
        b["percent"] = round(b["amount"] / total * 100, 1) if total else 0.0
        b["avg"] = round(b["amount"] / b["count"], 2) if b["count"] else 0.0
    return {
        "type": rtype,
        "start_date": (start_date or None),
        "end_date": (end_date or None),
        "total": total,
        "count": len(items),
        "categories": rows,
    }


def stats_by_day(
    start_date: str | None = None,
    end_date: str | None = None,
    rtype: str = "expense",
    group: str = "day",
) -> dict[str, Any]:
    items = list_records(start_date, end_date, rtype)
    key_len = 7 if group == "month" else 10
    buckets: dict[str, float] = {}
    counts: dict[str, int] = {}
    for r in items:
        k = r["date"][:key_len]
        buckets[k] = round(buckets.get(k, 0) + r["amount"], 2)
        counts[k] = counts.get(k, 0) + 1
    days = [
        {"period": k, "amount": buckets[k], "count": counts[k]}
        for k in sorted(buckets)
    ]
    total = round(sum(buckets.values()), 2)
    amounts = [d["amount"] for d in days]
    return {
        "type": rtype,
        "group": "month" if group == "month" else "day",
        "total": total,
        "count": len(items),
        "days": days,
        "max": max(amounts) if amounts else 0.0,
        "min": min(amounts) if amounts else 0.0,
        "avg_per_day": round(total / len(days), 2) if days else 0.0,
    }


def summary(
    start_date: str | None = None,
    end_date: str | None = None,
    rtype: str = "expense",
) -> dict[str, Any]:
    items = list_records(start_date, end_date, rtype)
    amount = round(sum(r["amount"] for r in items), 2)
    return {
        "type": rtype,
        "total": amount,
        "count": len(items),
        "avg": round(amount / len(items), 2) if items else 0.0,
        "first_date": items[-1]["date"] if items else None,
        "last_date": items[0]["date"] if items else None,
    }
