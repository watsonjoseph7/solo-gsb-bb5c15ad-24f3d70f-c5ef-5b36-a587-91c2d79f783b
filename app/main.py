"""FastAPI 主应用：页面 + 全部 REST API，统一 8000 端口。"""
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import crud, graph, safe_plans, seed
from .database import get_connection, init_db
from .schemas import (
    ConfirmPlanIn,
    ExportBundle,
    ImportResult,
    IngredientIn,
    RecipeIn,
    SafePlanIn,
    SubstitutionIn,
    normalize_allergens,
)

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="配方替代过敏原追踪", version="1.0.0", lifespan=lifespan)


# ---------------- 序列化辅助 ----------------

def _ingredient_out(row):
    return {
        "id": row["id"],
        "code": row["code"],
        "name": row["name"],
        "allergens": row["allergens"],
        "note": row["note"],
        "allergen_list": [a.strip() for a in row["allergens"].split(",") if a.strip()],
    }


def _recipe_out(conn, row):
    items = conn.execute(
        """SELECT ri.ingredient_code, ri.quantity, ri.unit,
                  COALESCE(i.name, ri.ingredient_code) AS ingredient_name
             FROM recipe_items ri
             LEFT JOIN ingredients i ON i.code = ri.ingredient_code
            WHERE ri.recipe_id = ? ORDER BY ri.id""",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "code": row["code"],
        "version": row["version"],
        "name": row["name"],
        "note": row["note"],
        "created_at": row["created_at"],
        "items": [dict(it) for it in items],
    }


def _substitution_out(row):
    def _al(v):
        return [a.strip() for a in (v or "").split(",") if a.strip()]

    return {
        "id": row["id"],
        "source_code": row["source_code"],
        "target_code": row["target_code"],
        "note": row["note"],
        "source_name": row["source_name"] or f"(缺失:{row['source_code']})",
        "target_name": row["target_name"] or f"(缺失:{row['target_code']})",
        "source_allergens": _al(row["source_allergens"]),
        "target_allergens": _al(row["target_allergens"]),
    }


def conn_dep():
    c = get_connection()
    try:
        yield c
        c.commit()
    finally:
        c.close()


# ---------------- 健康检查 ----------------

@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---------------- 原料 ----------------

@app.get("/api/ingredients")
def get_ingredients(conn=Depends(conn_dep)):
    return [_ingredient_out(r) for r in crud.list_ingredients(conn)]


@app.post("/api/ingredients", status_code=201)
def create_ingredient(data: IngredientIn, conn=Depends(conn_dep)):
    if crud.get_ingredient(conn, data.code):
        raise HTTPException(409, f"原料代码 {data.code} 已存在，请改用 PUT 更新")
    rid, _ = crud.upsert_ingredient(conn, data)
    return _ingredient_out(crud.get_ingredient(conn, data.code))


@app.put("/api/ingredients/{code}")
def update_ingredient(code: str, data: IngredientIn, conn=Depends(conn_dep)):
    if not crud.get_ingredient(conn, code):
        raise HTTPException(404, f"原料 {code} 不存在")
    crud.upsert_ingredient(conn, data, code=code)
    return _ingredient_out(crud.get_ingredient(conn, data.code))


@app.delete("/api/ingredients/{code}")
def remove_ingredient(code: str, conn=Depends(conn_dep)):
    if not crud.delete_ingredient(conn, code):
        raise HTTPException(404, f"原料 {code} 不存在")
    # 清理悬挂的替代边
    conn.execute("DELETE FROM substitutions WHERE source_code = ? OR target_code = ?", (code, code))
    return {"deleted": code}


# ---------------- 替代关系 ----------------

@app.get("/api/substitutions")
def get_substitutions(conn=Depends(conn_dep)):
    return [_substitution_out(r) for r in crud.list_substitutions(conn)]


@app.post("/api/substitutions", status_code=201)
def create_substitution(data: SubstitutionIn, conn=Depends(conn_dep)):
    if data.source_code == data.target_code:
        raise HTTPException(422, "替代源与目标不能是同一原料")
    if not crud.get_ingredient(conn, data.source_code):
        raise HTTPException(404, f"源原料 {data.source_code} 不存在")
    if not crud.get_ingredient(conn, data.target_code):
        raise HTTPException(404, f"目标原料 {data.target_code} 不存在")
    try:
        sid = crud.create_substitution(conn, data)
    except Exception:
        raise HTTPException(409, "该替代关系已存在")
    row = conn.execute(
        """SELECT s.*, si.name AS source_name, ti.name AS target_name,
                  si.allergens AS source_allergens, ti.allergens AS target_allergens
             FROM substitutions s
             LEFT JOIN ingredients si ON si.code=s.source_code
             LEFT JOIN ingredients ti ON ti.code=s.target_code
            WHERE s.id=?""",
        (sid,),
    ).fetchone()
    return _substitution_out(row)


@app.put("/api/substitutions/{sub_id}")
def update_substitution(sub_id: int, data: SubstitutionIn, conn=Depends(conn_dep)):
    if crud.get_substitution(conn, sub_id) is None:
        raise HTTPException(404, "替代关系不存在")
    if data.source_code == data.target_code:
        raise HTTPException(422, "替代源与目标不能是同一原料")
    if not crud.get_ingredient(conn, data.source_code):
        raise HTTPException(404, f"源原料 {data.source_code} 不存在")
    if not crud.get_ingredient(conn, data.target_code):
        raise HTTPException(404, f"目标原料 {data.target_code} 不存在")
    try:
        crud.update_substitution(conn, sub_id, data)
    except Exception:
        raise HTTPException(409, "已存在相同的替代边")
    row = conn.execute(
        """SELECT s.*, si.name AS source_name, ti.name AS target_name,
                  si.allergens AS source_allergens, ti.allergens AS target_allergens
             FROM substitutions s
             LEFT JOIN ingredients si ON si.code=s.source_code
             LEFT JOIN ingredients ti ON ti.code=s.target_code
            WHERE s.id=?""",
        (sub_id,),
    ).fetchone()
    return _substitution_out(row)


@app.delete("/api/substitutions/{sub_id}")
def remove_substitution(sub_id: int, conn=Depends(conn_dep)):
    if not crud.delete_substitution(conn, sub_id):
        raise HTTPException(404, "替代关系不存在")
    return {"deleted": sub_id}


# ---------------- 配方版本 ----------------

@app.get("/api/recipes")
def get_recipes(conn=Depends(conn_dep)):
    rows = crud.list_recipes(conn)
    return [
        {
            "id": r["id"],
            "code": r["code"],
            "version": r["version"],
            "name": r["name"],
            "note": r["note"],
            "created_at": r["created_at"],
            "item_count": r["item_count"],
        }
        for r in rows
    ]


@app.get("/api/recipes/{recipe_id}")
def get_recipe(recipe_id: int, conn=Depends(conn_dep)):
    row = crud.get_recipe(conn, recipe_id)
    if row is None:
        raise HTTPException(404, "配方不存在")
    return _recipe_out(conn, row)


@app.post("/api/recipes", status_code=201)
def create_recipe(data: RecipeIn, conn=Depends(conn_dep)):
    if crud.find_recipe(conn, data.code, data.version):
        raise HTTPException(409, f"配方 {data.code} 版本 {data.version} 已存在")
    _validate_items(conn, data)
    rid, _ = crud.upsert_recipe(conn, data)
    return _recipe_out(conn, crud.get_recipe(conn, rid))


@app.put("/api/recipes/{recipe_id}")
def update_recipe(recipe_id: int, data: RecipeIn, conn=Depends(conn_dep)):
    if crud.get_recipe(conn, recipe_id) is None:
        raise HTTPException(404, "配方不存在")
    clash = crud.find_recipe(conn, data.code, data.version)
    if clash and clash["id"] != recipe_id:
        raise HTTPException(409, f"已存在同代码同版本的另一条配方 id={clash['id']}")
    _validate_items(conn, data)
    crud.upsert_recipe(conn, data, recipe_id=recipe_id)
    return _recipe_out(conn, crud.get_recipe(conn, recipe_id))


@app.delete("/api/recipes/{recipe_id}")
def remove_recipe(recipe_id: int, conn=Depends(conn_dep)):
    if not crud.delete_recipe(conn, recipe_id):
        raise HTTPException(404, "配方不存在")
    return {"deleted": recipe_id}


def _validate_items(conn, data: RecipeIn):
    codes = {r["code"] for r in crud.list_ingredients(conn)}
    missing = [it.ingredient_code for it in data.items if it.ingredient_code not in codes]
    if missing:
        raise HTTPException(422, f"配方引用了不存在的原料: {', '.join(missing)}")


# ---------------- 过敏原图分析 ----------------

@app.get("/api/recipes/{recipe_id}/analyze")
def analyze_recipe_route(recipe_id: int, conn=Depends(conn_dep)):
    if crud.get_recipe(conn, recipe_id) is None:
        raise HTTPException(404, "配方不存在")
    return graph.analyze_recipe(conn, recipe_id)


# ---------------- 安全替代方案 ----------------

@app.post("/api/recipes/{recipe_id}/safe-plans")
def safe_plans_route(recipe_id: int, data: Optional[SafePlanIn] = None, conn=Depends(conn_dep)):
    if crud.get_recipe(conn, recipe_id) is None:
        raise HTTPException(404, "配方不存在")
    avoid = data.avoid_allergens if data else []
    return safe_plans.build_safe_plans(conn, recipe_id, avoid)


@app.post("/api/recipes/{recipe_id}/confirm-plan")
def confirm_plan_route(recipe_id: int, data: ConfirmPlanIn, conn=Depends(conn_dep)):
    """确认一组安全方案：重新校验避开过敏原约束并生成合并采购清单。

    校验失败统一返回 422，detail 为结构化的逐条原因，前端据此提示并引导重新生成。
    """
    if crud.get_recipe(conn, recipe_id) is None:
        raise HTTPException(404, "配方不存在")
    if not data.selections:
        raise HTTPException(
            422,
            {
                "message": "未收到任何方案选择，请从生成结果中为每种配料选定终点后再确认",
                "failures": [],
            },
        )
    result = safe_plans.confirm_plan(
        conn, recipe_id, data.avoid_allergens, [s.model_dump() for s in data.selections]
    )
    if result is None:
        raise HTTPException(404, "配方不存在")
    if not result["valid"]:
        raise HTTPException(
            422,
            {
                "message": "所选方案已不再满足避开过敏原约束（原料、替代关系或过敏原可能已被修改），请重新生成方案",
                "failures": result["failures"],
            },
        )
    return result


# ---------------- 两版配方差异对比 ----------------

def _resolve_recipe_id(conn, ref) -> int:
    if "id" in ref and ref["id"] is not None:
        row = crud.get_recipe(conn, ref["id"])
        if row:
            return row["id"]
    row = crud.find_recipe(conn, ref["code"], ref["version"])
    if row is None:
        raise HTTPException(404, f"找不到配方 {ref.get('code')} 版本 {ref.get('version')}")
    return row["id"]


def _item_map(analysis):
    return {r["ingredient_code"]: r for r in analysis["roots"]}


@app.post("/api/compare")
def compare_recipes(payload: dict, conn=Depends(conn_dep)):
    try:
        a_id = _resolve_recipe_id(conn, payload["recipe_a"])
        b_id = _resolve_recipe_id(conn, payload["recipe_b"])
    except (KeyError, TypeError):
        raise HTTPException(422, "请求体需包含 recipe_a 与 recipe_b，每个含 code/version 或 id")

    a = graph.analyze_recipe(conn, a_id)
    b = graph.analyze_recipe(conn, b_id)
    ma, mb = _item_map(a), _item_map(b)

    added, removed, changed = [], [], []
    for code, item in mb.items():
        if code not in ma:
            added.append({
                "ingredient_code": code,
                "ingredient_name": item["ingredient_name"],
                "quantity": item["quantity"],
                "unit": item["unit"],
                "direct_allergens": item["direct_allergens"],
                "total_allergens": item["total_allergens"],
            })
    for code, item in ma.items():
        if code not in mb:
            removed.append({
                "ingredient_code": code,
                "ingredient_name": item["ingredient_name"],
                "quantity": item["quantity"],
                "unit": item["unit"],
                "direct_allergens": item["direct_allergens"],
                "total_allergens": item["total_allergens"],
            })
    for code in ma.keys() & mb.keys():
        ia, ib = ma[code], mb[code]
        if ia["quantity"] != ib["quantity"] or ia["unit"] != ib["unit"]:
            changed.append({
                "ingredient_code": code,
                "ingredient_name": ib["ingredient_name"],
                "from": {"quantity": ia["quantity"], "unit": ia["unit"]},
                "to": {"quantity": ib["quantity"], "unit": ib["unit"]},
            })

    def _setdiff(xs, ys):
        sy = set(ys)
        return [x for x in xs if x not in sy]

    return {
        "recipe_a": a["recipe"],
        "recipe_b": b["recipe"],
        "items": {
            "added": added,
            "removed": removed,
            "changed": changed,
        },
        "direct_allergens": {
            "a": a["direct_allergens"],
            "b": b["direct_allergens"],
            "added": _setdiff(b["direct_allergens"], a["direct_allergens"]),
            "removed": _setdiff(a["direct_allergens"], b["direct_allergens"]),
        },
        "total_allergens": {
            "a": a["total_allergens"],
            "b": b["total_allergens"],
            "added": _setdiff(b["total_allergens"], a["total_allergens"]),
            "removed": _setdiff(a["total_allergens"], b["total_allergens"]),
        },
        "introduced_allergens": {
            "a": a["introduced_allergens"],
            "b": b["introduced_allergens"],
        },
        "cycles": {
            "a": a["cycles"],
            "b": b["cycles"],
        },
        "missing_nodes": sorted(set(a["missing_nodes"]) | set(b["missing_nodes"])),
    }


# ---------------- 导入 / 导出 ----------------

@app.get("/api/export")
def export_data(conn=Depends(conn_dep)):
    ingredients = [
        {
            "code": r["code"],
            "name": r["name"],
            "allergens": r["allergens"],
            "note": r["note"],
        }
        for r in crud.list_ingredients(conn)
    ]
    recipes = [
        {
            "code": r["code"],
            "version": r["version"],
            "name": r["name"],
            "note": r["note"],
            "items": [
                {
                    "ingredient_code": it["ingredient_code"],
                    "quantity": it["quantity"],
                    "unit": it["unit"],
                }
                for it in _recipe_out(conn, r)["items"]
            ],
        }
        for r in crud.list_recipes(conn)
    ]
    subs = [
        {"source_code": r["source_code"], "target_code": r["target_code"], "note": r["note"]}
        for r in crud.list_substitutions(conn)
    ]
    return {
        "format": "allergen-tracker/v1",
        "exported_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "ingredients": ingredients,
        "recipes": recipes,
        "substitutions": subs,
    }


@app.post("/api/import")
def import_data(payload: dict, mode: str = "merge", conn=Depends(conn_dep)):
    """mode=merge（默认，按主键 upsert）或 replace（清空后重导）。"""
    result = ImportResult()
    if mode not in ("merge", "replace"):
        raise HTTPException(422, "mode 仅支持 merge 或 replace")

    try:
        bundle = ExportBundle(**payload)
    except ValidationError as exc:
        raise HTTPException(422, f"JSON 结构不合法: {exc.errors()}")

    if mode == "replace":
        conn.executescript(
            "DELETE FROM recipe_items; DELETE FROM recipes; "
            "DELETE FROM substitutions; DELETE FROM ingredients;"
        )

    for ing in bundle.ingredients:
        crud.upsert_ingredient(conn, ing)
        result.ingredients += 1

    for sub in bundle.substitutions:
        if not crud.get_ingredient(conn, sub.source_code) or not crud.get_ingredient(conn, sub.target_code):
            result.errors.append(f"替代关系 {sub.source_code}->{sub.target_code} 引用了缺失原料，已跳过")
            continue
        try:
            crud.create_substitution(conn, sub)
            result.substitutions += 1
        except Exception:
            pass  # 重复边跳过

    for rec in bundle.recipes:
        codes = {r["code"] for r in crud.list_ingredients(conn)}
        missing = [it.ingredient_code for it in rec.items if it.ingredient_code not in codes]
        if missing:
            result.errors.append(f"配方 {rec.code}@{rec.version} 引用缺失原料 {missing}，已跳过")
            continue
        crud.upsert_recipe(conn, rec)
        result.recipes += 1

    return result.model_dump()


# ---------------- 示例数据 ----------------

@app.post("/api/seed", status_code=201)
def seed_demo(conn=Depends(conn_dep)):
    return seed.seed(conn)


# ---------------- 静态页面（根路径） ----------------

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
