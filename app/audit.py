"""配方变更审计：不可变事件、前后完整配料快照与可读差异。

事件写入与配方业务写操作使用同一 sqlite 连接（FastAPI 的 conn_dep 在请求
结束时统一 commit / 出错时 rollback），因此「配方变更 + 审计事件」处于同一个
数据库事务中：任一步失败都会整体回滚，不会出现改了配方却没留下审计记录的情况。

recipe_events 表在数据库层由触发器禁止 UPDATE/DELETE（见 database.py），
应用代码只做 INSERT，保证事件流只追加、不可变。
"""
import json
import sqlite3
from typing import List, Optional

# 允许的时间范围过滤格式（occurred_at 以 UTC ISO8601 存储）
EVENT_TYPES = ("created", "updated", "deleted")
EVENT_LABELS = {"created": "创建", "updated": "修改", "deleted": "删除"}


# ---------------- 快照 ----------------

def recipe_snapshot(conn: sqlite3.Connection, recipe_id: int) -> Optional[dict]:
    """读取配方及其完整配料表，组成快照；配方不存在返回 None。"""
    row = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if row is None:
        return None
    items = conn.execute(
        """SELECT ingredient_code, quantity, unit
             FROM recipe_items WHERE recipe_id = ? ORDER BY id""",
        (recipe_id,),
    ).fetchall()
    return {
        "recipe_id": row["id"],
        "code": row["code"],
        "version": row["version"],
        "name": row["name"],
        "note": row["note"],
        "created_at": row["created_at"],
        "items": [dict(it) for it in items],
    }


# ---------------- 差异计算 ----------------

def _item_map(snapshot: Optional[dict]) -> dict:
    if not snapshot:
        return {}
    return {it["ingredient_code"]: it for it in snapshot.get("items", [])}


def _fmt_qty(q) -> str:
    """格式化用量：None/空串显示为「未填」，整数去掉 .0。"""
    if q is None or q == "":
        return "未填用量"
    if isinstance(q, float) and q.is_integer():
        return str(int(q))
    return str(q)


def _fmt_amount(it) -> str:
    qty = _fmt_qty(it.get("quantity"))
    unit = it.get("unit") or ""
    return f"{qty}{unit}" if qty != "未填用量" else qty


def diff_items(before: Optional[dict], after: Optional[dict]) -> dict:
    """对比两份快照的配料表，返回增 / 删 / 用量单位变化的结构化差异。"""
    before_map, after_map = _item_map(before), _item_map(after)
    added, removed, changed = [], [], []

    for code, it in after_map.items():
        if code not in before_map:
            added.append({
                "ingredient_code": code,
                "quantity": it.get("quantity"),
                "unit": it.get("unit", ""),
            })
    for code, it in before_map.items():
        if code not in after_map:
            removed.append({
                "ingredient_code": code,
                "quantity": it.get("quantity"),
                "unit": it.get("unit", ""),
            })
    for code in before_map.keys() & after_map.keys():
        a, b = before_map[code], after_map[code]
        if a.get("quantity") != b.get("quantity") or a.get("unit") != b.get("unit"):
            changed.append({
                "ingredient_code": code,
                "from": {"quantity": a.get("quantity"), "unit": a.get("unit", "")},
                "to": {"quantity": b.get("quantity"), "unit": b.get("unit", "")},
                "quantity_changed": a.get("quantity") != b.get("quantity"),
                "unit_changed": a.get("unit") != b.get("unit"),
            })

    return {"added": added, "removed": removed, "changed": changed}


def summarize_diff(event_type: str, before: Optional[dict], after: Optional[dict]) -> str:
    """把配料增删与用量/单位变化汇总成一行可读中文摘要。"""
    if event_type == "created":
        n = len(after["items"]) if after else 0
        return f"创建配方 {after['code']} v{after['version']}，含 {n} 项配料"
    if event_type == "deleted":
        n = len(before["items"]) if before else 0
        return f"删除配方 {before['code']} v{before['version']}，原含 {n} 项配料"

    d = diff_items(before, after)
    parts = []
    if d["added"]:
        parts.append("新增配料 " + "、".join(x["ingredient_code"] for x in d["added"]))
    if d["removed"]:
        parts.append("移除配料 " + "、".join(x["ingredient_code"] for x in d["removed"]))
    if d["changed"]:
        detail = []
        for c in d["changed"]:
            bits = []
            if c["quantity_changed"]:
                bits.append(f"用量 {_fmt_qty(c['from']['quantity'])}→{_fmt_qty(c['to']['quantity'])}")
            if c["unit_changed"]:
                fa, fb = c["from"]["unit"] or "（无）", c["to"]["unit"] or "（无）"
                bits.append(f"单位 {fa}→{fb}")
            detail.append(f"{c['ingredient_code']}（{'，'.join(bits)}）")
        parts.append("调整 " + "、".join(detail))
    if not parts:
        return f"修改配方 {after['code']} v{after['version']}（配料无变化）"
    return f"修改配方 {after['code']} v{after['version']}：" + "；".join(parts)


def describe_diff_lines(diff: dict) -> List[str]:
    """供时间线 API 与前端展示的逐行可读差异（按配料增删、用量/单位变化）。"""
    lines = []
    for x in diff["added"]:
        lines.append(f"＋ 新增 {x['ingredient_code']}（{_fmt_amount(x)}）")
    for x in diff["removed"]:
        lines.append(f"－ 移除 {x['ingredient_code']}（原 {_fmt_amount(x)}）")
    for c in diff["changed"]:
        bits = []
        if c["quantity_changed"]:
            bits.append(f"用量 {_fmt_qty(c['from']['quantity'])} → {_fmt_qty(c['to']['quantity'])}")
        if c["unit_changed"]:
            fa = c["from"]["unit"] or "无单位"
            fb = c["to"]["unit"] or "无单位"
            bits.append(f"单位 {fa} → {fb}")
        lines.append(f"～ {c['ingredient_code']}：{'，'.join(bits)}")
    return lines


# ---------------- 事件写入（只追加） ----------------

def record_event(
    conn: sqlite3.Connection,
    event_type: str,
    before: Optional[dict],
    after: Optional[dict],
) -> int:
    """在当前事务中追加一条不可变审计事件，返回事件 id。"""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"未知审计事件类型: {event_type}")
    # 事件归属配方：更新若改了代码，事件仍挂在原代码下，保证同一配方的时间线连续
    anchor = before or after
    recipe_id = anchor["recipe_id"]
    recipe_code = before["code"] if before else after["code"]
    summary = summarize_diff(event_type, before, after)
    cur = conn.execute(
        """INSERT INTO recipe_events
               (event_type, recipe_id, recipe_code, before_snapshot, after_snapshot, summary)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            event_type,
            recipe_id,
            recipe_code,
            json.dumps(before, ensure_ascii=False) if before is not None else None,
            json.dumps(after, ensure_ascii=False) if after is not None else None,
            summary,
        ),
    )
    return cur.lastrowid


# ---------------- 时间线查询 ----------------

def _parse_snapshot(raw):
    return json.loads(raw) if raw else None


def _event_out(row) -> dict:
    before, after = _parse_snapshot(row["before_snapshot"]), _parse_snapshot(row["after_snapshot"])
    diff = diff_items(before, after) if row["event_type"] == "updated" else {
        "added": [{"ingredient_code": it["ingredient_code"],
                   "quantity": it["quantity"], "unit": it["unit"]}
                  for it in (after or {}).get("items", [])],
        "removed": [{"ingredient_code": it["ingredient_code"],
                     "quantity": it["quantity"], "unit": it["unit"]}
                    for it in (before or {}).get("items", [])],
        "changed": [],
    }
    return {
        "id": row["id"],
        "event_type": row["event_type"],
        "event_label": EVENT_LABELS[row["event_type"]],
        "recipe_id": row["recipe_id"],
        "recipe_code": row["recipe_code"],
        "occurred_at": row["occurred_at"],
        "summary": row["summary"],
        "diff": diff,
        "diff_lines": describe_diff_lines(diff),
        "before": before,
        "after": after,
    }


def list_events(
    conn: sqlite3.Connection,
    recipe: Optional[str] = None,
    recipe_id: Optional[int] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: int = 200,
) -> List[dict]:
    """按配方（代码或 id）与时间范围查询事件时间线，按发生时间正序返回。

    recipe 过滤需同时匹配 before/after 快照中的代码（更新可能改代码），
    在 SQL 层用 LIKE 兜底、Python 层精确判定，避免 SQLite JSON1 依赖问题。
    """
    clauses, params = [], []
    if recipe_id is not None:
        clauses.append("recipe_id = ?")
        params.append(recipe_id)
    if recipe is not None:
        # 快照 JSON 形如 {"recipe_id": 1, "code": "X", ...}（冒号后带空格），
        # LIKE 仅做 SQL 层粗筛；Python 层再用解析后的快照代码精确判定，
        # 避免代码互为子串时的误匹配，也不依赖 SQLite JSON1 扩展。
        # recipe_id 子查询保证：配方改名后，用新代码也能查到改名前的全部履历。
        clauses.append(
            "(recipe_id IN (SELECT id FROM recipes WHERE code = ?) "
            "OR recipe_code = ? OR before_snapshot LIKE ? OR after_snapshot LIKE ?)"
        )
        like = f'%"code": "{recipe}"%'
        params.extend([recipe, recipe, like, like])
    if start is not None:
        clauses.append("occurred_at >= ?")
        params.append(start)
    if end is not None:
        clauses.append("occurred_at <= ?")
        params.append(end)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    rows = conn.execute(
        f"""SELECT * FROM recipe_events{where}
            ORDER BY occurred_at ASC, id ASC LIMIT ?""",
        (*params, limit),
    ).fetchall()

    events = [_event_out(r) for r in rows]
    if recipe is not None:
        # SQL 的 LIKE 可能误匹配（代码互为子串）；同时要保留改名履历：
        # 事件命中任一条件即保留——当前仍持有该代码的配方 id、事件归属代码、
        # 或任一侧快照中的代码。
        current_ids = {
            r["id"] for r in conn.execute(
                "SELECT id FROM recipes WHERE code = ?", (recipe,)
            ).fetchall()
        }

        def _codes(ev):
            codes = {ev["recipe_code"]}
            for snap in (ev["before"], ev["after"]):
                if snap:
                    codes.add(snap["code"])
            return codes

        events = [
            ev for ev in events
            if ev["recipe_id"] in current_ids or recipe in _codes(ev)
        ]
    return events
