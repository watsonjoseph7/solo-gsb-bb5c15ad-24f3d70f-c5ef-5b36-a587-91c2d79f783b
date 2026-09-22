"""原料、配方、替代关系的数据库读写操作。"""
import sqlite3
from typing import List

from .schemas import (
    IngredientIn,
    RecipeIn,
    RecipeItemIn,
    SubstitutionIn,
    normalize_allergens,
)

from .database import get_connection


# ---------------- 原料 ----------------

def list_ingredients(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM ingredients ORDER BY code").fetchall()


def get_ingredient(conn: sqlite3.Connection, code: str):
    return conn.execute("SELECT * FROM ingredients WHERE code = ?", (code,)).fetchone()


def upsert_ingredient(conn: sqlite3.Connection, data: IngredientIn, code: str | None = None):
    target_code = code or data.code
    allergens = ",".join(normalize_allergens(data.allergens))
    existing = get_ingredient(conn, target_code)
    if existing:
        conn.execute(
            "UPDATE ingredients SET code=?, name=?, allergens=?, note=? WHERE id=?",
            (data.code, data.name, allergens, data.note, existing["id"]),
        )
        return existing["id"], "updated"
    cur = conn.execute(
        "INSERT INTO ingredients (code, name, allergens, note) VALUES (?, ?, ?, ?)",
        (data.code, data.name, allergens, data.note),
    )
    return cur.lastrowid, "created"


def delete_ingredient(conn: sqlite3.Connection, code: str) -> bool:
    cur = conn.execute("DELETE FROM ingredients WHERE code = ?", (code,))
    return cur.rowcount > 0


# ---------------- 配方 ----------------

def list_recipes(conn: sqlite3.Connection):
    rows = conn.execute(
        """SELECT r.*, COUNT(ri.id) AS item_count
             FROM recipes r LEFT JOIN recipe_items ri ON ri.recipe_id = r.id
            GROUP BY r.id ORDER BY r.code, r.version"""
    ).fetchall()
    return rows


def get_recipe(conn: sqlite3.Connection, recipe_id: int):
    return conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()


def find_recipe(conn: sqlite3.Connection, code: str, version: str):
    return conn.execute(
        "SELECT * FROM recipes WHERE code = ? AND version = ?", (code, version)
    ).fetchone()


def _replace_items(conn: sqlite3.Connection, recipe_id: int, items: List[RecipeItemIn]) -> None:
    conn.execute("DELETE FROM recipe_items WHERE recipe_id = ?", (recipe_id,))
    for it in items:
        conn.execute(
            "INSERT INTO recipe_items (recipe_id, ingredient_code, quantity, unit) VALUES (?, ?, ?, ?)",
            (recipe_id, it.ingredient_code, it.quantity, it.unit),
        )


def upsert_recipe(conn: sqlite3.Connection, data: RecipeIn, recipe_id: int | None = None):
    """创建或整体替换一个配方版本。"""
    existing = None
    if recipe_id is not None:
        existing = get_recipe(conn, recipe_id)
    if existing is None:
        existing = find_recipe(conn, data.code, data.version)

    if existing:
        conn.execute(
            "UPDATE recipes SET code=?, version=?, name=?, note=? WHERE id=?",
            (data.code, data.version, data.name, data.note, existing["id"]),
        )
        rid = existing["id"]
        status = "updated"
    else:
        cur = conn.execute(
            "INSERT INTO recipes (code, version, name, note) VALUES (?, ?, ?, ?)",
            (data.code, data.version, data.name, data.note),
        )
        rid = cur.lastrowid
        status = "created"

    _replace_items(conn, rid, data.items)
    return rid, status


def delete_recipe(conn: sqlite3.Connection, recipe_id: int) -> bool:
    cur = conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    return cur.rowcount > 0


# ---------------- 替代关系 ----------------

def list_substitutions(conn: sqlite3.Connection):
    return conn.execute(
        """SELECT s.*,
                  si.name AS source_name, ti.name AS target_name,
                  si.allergens AS source_allergens, ti.allergens AS target_allergens
             FROM substitutions s
             LEFT JOIN ingredients si ON si.code = s.source_code
             LEFT JOIN ingredients ti ON ti.code = s.target_code
            ORDER BY s.source_code, s.target_code"""
    ).fetchall()


def get_substitution(conn: sqlite3.Connection, sub_id: int):
    return conn.execute("SELECT * FROM substitutions WHERE id = ?", (sub_id,)).fetchone()


def create_substitution(conn: sqlite3.Connection, data: SubstitutionIn):
    cur = conn.execute(
        "INSERT INTO substitutions (source_code, target_code, note) VALUES (?, ?, ?)",
        (data.source_code, data.target_code, data.note),
    )
    return cur.lastrowid


def update_substitution(conn: sqlite3.Connection, sub_id: int, data: SubstitutionIn) -> bool:
    cur = conn.execute(
        "UPDATE substitutions SET source_code=?, target_code=?, note=? WHERE id=?",
        (data.source_code, data.target_code, data.note, sub_id),
    )
    return cur.rowcount > 0


def delete_substitution(conn: sqlite3.Connection, sub_id: int) -> bool:
    cur = conn.execute("DELETE FROM substitutions WHERE id = ?", (sub_id,))
    return cur.rowcount > 0


def connection():
    """便于脚本层调用。"""
    return get_connection()
