"""配方变更审计：在同一数据库事务中写入不可变事件并提供时间线与可读差异。

设计要点：
- ``record_event`` 只在调用方的连接上 INSERT，不自行提交；由请求级连接
  （``main.conn_dep``）与配方表修改在同一事务中一起提交或一起回滚。
- ``recipe_audit_events`` 表在 database.py 中通过触发器禁止 UPDATE/DELETE，
  事件只能追加，保证审计链不可变。
- 事件保存操作时的前 / 后完整配料快照（JSON）；结构化差异与可读文案在查询时
  根据快照即时计算，因此审计数据本身不依赖派生字段。
"""
import json
import sqlite3
from datetime import datetime
from typing import List, Optional

ACTIONS = ("created", "updated", "deleted")
ACTION_LABELS = {"created": "创建", "updated": "修改", "deleted": "删除"}


# ---------------- 快照 ----------------

def _snapshot_items(conn: sqlite3.Connection, recipe_id: int):
    return [
        {
            "ingredient_code": r["ingredient_code"],
            "ingredient_name": r["ingredient_name"],
            "quantity": r["quantity"],
            "unit": r["unit"],
        }
        for r in conn.execute(
            """SELECT ri.ingredient_code,
                      COALESCE(i.name, ri.ingredient_code) AS ingredient_name,
                      ri.quantity, ri.unit
                 FROM recipe_items ri
                 LEFT JOIN ingredients i ON i.code = ri.ingredient_code
                WHERE ri.recipe_id = ?
                ORDER BY ri.id""",
            (recipe_id,),
        ).fetchall()
    ]


def snapshot_recipe(conn: sqlite3.Connection, recipe_id: int) -> Optional[dict]:
    """读取配方及其完整配料快照；配方不存在时返回 None。"""
    row = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "code": row["code"],
        "version": row["version"],
        "name": row["name"],
        "note": row["note"],
        "created_at": row["created_at"],
        "items": _snapshot_items(conn, recipe_id),
    }


# ---------------- 事件写入 ----------------

def record_event(
    conn: sqlite3.Connection,
    *,
    recipe_id: int,
    recipe_code: str,
    version: str,
    action: str,
    before: Optional[dict],
    after: Optional[dict],
) -> int:
    """追加一条审计事件（不提交事务）。返回事件 id。"""
    if action not in ACTIONS:
        raise ValueError(f"未知审计动作: {action}")
    cur = conn.execute(
        """INSERT INTO recipe_audit_events
               (recipe_id, recipe_code, version, action, before_snapshot, after_snapshot)
         VALUES (?, ?, ?, ?, ?, ?)""",
        (
            recipe_id,
            recipe_code,
            version,
            action,
            json.dumps(before, ensure_ascii=False) if before is not None else None,
            json.dumps(after, ensure_ascii=False) if after is not None else None,
        ),
    )
    return cur.lastrowid


# ---------------- 差异计算 ----------------

def _fmt_qty(q) -> str:
    if q is None:
        return "未填用量"
    return str(int(q)) if isinstance(q, float) and q.is_integer() else str(q)


def _fmt_measure(q, unit: str) -> str:
    if q is None:
        return "未填用量"
    return f"{_fmt_qty(q)}{unit or ''}"


def _item_brief(item: dict) -> str:
    return f"{item['ingredient_name']}（{item['ingredient_code']}）"


def _meta_changes(before: dict, after: dict):
    changes = []
    for field, label in (("name", "名称"), ("note", "备注"), ("version", "版本号"), ("code", "配方代码")):
        old, new = before.get(field, ""), after.get(field, "")
        if old != new:
            changes.append(f"{label}：{old or '（空）'} → {new or '（空）'}")
    return changes


def diff_snapshots(before: Optional[dict], after: Optional[dict]) -> dict:
    """对比两份完整快照，返回结构化差异及人类可读的中文文案。

    结构：{action, added:[item], removed:[item], changed:[{from,to}],
           meta_changed:[str], lines:[{kind, text}]}
    """
    if before is None and after is not None:
        action = "created"
    elif before is not None and after is None:
        action = "deleted"
    else:
        action = "updated"

    before = before or {}
    after = after or {}
    added, removed, changed = [], [], []

    map_before = {it["ingredient_code"]: it for it in before.get("items", [])}
    map_after = {it["ingredient_code"]: it for it in after.get("items", [])}

    for it in after.get("items", []):
        if it["ingredient_code"] not in map_before:
            added.append(it)
    for it in before.get("items", []):
        if it["ingredient_code"] not in map_after:
            removed.append(it)
    for it in after.get("items", []):
        old = map_before.get(it["ingredient_code"])
        if old is None:
            continue
        if old["quantity"] != it["quantity"] or old["unit"] != it["unit"]:
            changed.append({"code": it["ingredient_code"], "name": it["ingredient_name"],
                            "from": {"quantity": old["quantity"], "unit": old["unit"]},
                            "to": {"quantity": it["quantity"], "unit": it["unit"]}})

    lines: List[dict] = []

    def add_line(kind: str, text: str) -> None:
        lines.append({"kind": kind, "text": text})

    if action == "created":
        add_line("created",
                 f"创建配方 {after.get('code')} v{after.get('version')}（{after.get('name')}），"
                 f"共 {len(after.get('items', []))} 种配料")
        for it in added:
            add_line("added", f"新增配料 {_item_brief(it)}，用量 {_fmt_measure(it['quantity'], it['unit'])}")
    elif action == "deleted":
        add_line("deleted",
                 f"删除配方 {before.get('code')} v{before.get('version')}（{before.get('name')}），"
                 f"原有 {len(before.get('items', []))} 种配料")
        for it in removed:
            add_line("removed", f"移除配料 {_item_brief(it)}，原用量 {_fmt_measure(it['quantity'], it['unit'])}")
    else:
        add_line("updated",
                 f"修改配方 {before.get('code')} v{before.get('version')}（{before.get('name')}）")
        for text in _meta_changes(before, after):
            add_line("meta", text)
        for it in added:
            add_line("added", f"新增配料 {_item_brief(it)}，用量 {_fmt_measure(it['quantity'], it['unit'])}")
        for it in removed:
            add_line("removed", f"移除配料 {_item_brief(it)}，原用量 {_fmt_measure(it['quantity'], it['unit'])}")
        for c in changed:
            old_q, new_q = c["from"]["quantity"], c["to"]["quantity"]
            old_u, new_u = c["from"]["unit"] or "", c["to"]["unit"] or ""
            brief = f"配料 {c['name']}（{c['code']}）"
            if old_q != new_q and old_u != new_u:
                add_line("changed",
                         f"{brief}用量与单位变化：{_fmt_measure(old_q, old_u)} → {_fmt_measure(new_q, new_u)}")
            elif old_q != new_q:
                add_line("changed",
                         f"{brief}用量变化：{_fmt_qty(old_q)} → {_fmt_qty(new_q)}{new_u}")
            else:
                add_line("changed",
                         f"{brief}单位变化：{old_u or '（无单位）'} → {new_u or '（无单位）'}")
        if not added and not removed and not changed and not _meta_changes(before, after):
            add_line("noop", "配料与配方信息均无变化（内容相同的重复保存）")

    return {
        "action": action,
        "added": added,
        "removed": removed,
        "changed": changed,
        "meta_changed": _meta_changes(before, after) if action == "updated" else [],
        "lines": lines,
    }


# ---------------- 查询 ----------------

def parse_dt_boundary(value: Optional[str]) -> Optional[datetime]:
    """解析查询参数中的时间边界；失败抛 ValueError。支持以 Z 结尾的 UTC 时间。"""
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    if "T" not in text and " " in text:
        text = text.replace(" ", "T", 1)
    return datetime.fromisoformat(text)


def list_events(
    conn: sqlite3.Connection,
    *,
    recipe_id: Optional[int] = None,
    recipe_code: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: int = 100,
):
    sql = "SELECT * FROM recipe_audit_events WHERE 1=1"
    params: list = []
    if recipe_id is not None:
        sql += " AND recipe_id = ?"
        params.append(recipe_id)
    if recipe_code:
        sql += " AND recipe_code = ?"
        params.append(recipe_code)
    if start is not None:
        sql += " AND occurred_at >= ?"
        params.append(start.isoformat(sep=" "))
    if end is not None:
        sql += " AND occurred_at <= ?"
        params.append(end.isoformat(sep=" "))
    sql += " ORDER BY occurred_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def _loads(value):
    return json.loads(value) if value else None


def event_out(row) -> dict:
    before = _loads(row["before_snapshot"])
    after = _loads(row["after_snapshot"])
    return {
        "id": row["id"],
        "recipe_id": row["recipe_id"],
        "recipe_code": row["recipe_code"],
        "version": row["version"],
        "action": row["action"],
        "action_label": ACTION_LABELS.get(row["action"], row["action"]),
        "occurred_at": row["occurred_at"],
        "before_snapshot": before,
        "after_snapshot": after,
        "diff": diff_snapshots(before, after),
    }
