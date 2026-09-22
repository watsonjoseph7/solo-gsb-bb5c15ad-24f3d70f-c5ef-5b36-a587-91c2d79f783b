"""有向图递归分析：替代链传播过敏原、检测循环、标记受影响路径。

图模型
------
- 节点：原料（携带过敏原集合）
- 有向边：source -> target 表示 "source 可被 target 替代"

从配方中的每个原料出发递归 DFS，枚举出所有替代路径（含路径上每个节点），
对每条路径聚合过敏原；当目标节点已经出现在当前路径上时，该路径构成循环并被标记。
"""
from typing import Any, Dict, List, Optional, Set, Tuple

import sqlite3

MAX_DEPTH = 60  # 保护性深度上限（循环已由 on_path 集合提前截断）


def _build_graph(conn: sqlite3.Connection) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[Dict[str, str]]]]:
    nodes: Dict[str, Dict[str, Any]] = {}
    for row in conn.execute("SELECT code, name, allergens FROM ingredients"):
        nodes[row["code"]] = {
            "code": row["code"],
            "name": row["name"],
            "allergens": [a.strip() for a in row["allergens"].split(",") if a.strip()],
            "exists": True,
        }

    adjacency: Dict[str, List[Dict[str, str]]] = {}
    for row in conn.execute("SELECT source_code, target_code, note FROM substitutions ORDER BY id"):
        adjacency.setdefault(row["source_code"], []).append(
            {"target_code": row["target_code"], "note": row["note"] or ""}
        )
    return nodes, adjacency


def _node_snapshot(nodes: Dict[str, Dict[str, Any]], code: str) -> Dict[str, Any]:
    if code in nodes:
        n = nodes[code]
        return {"code": code, "name": n["name"], "allergens": list(n["allergens"]), "exists": True}
    # 图中被引用但原料库里不存在的节点
    return {"code": code, "name": f"(缺失原料:{code})", "allergens": [], "exists": False}


def _union(values: List[List[str]]) -> List[str]:
    seen: Set[str] = set()
    result: List[str] = []
    for group in values:
        for v in group:
            if v not in seen:
                seen.add(v)
                result.append(v)
    return result


def _dfs_paths(
    root_code: str,
    nodes: Dict[str, Dict[str, Any]],
    adjacency: Dict[str, Dict[str, str]],
    missing: Set[str],
    next_id,
) -> List[Dict[str, Any]]:
    """从一个根节点出发枚举全部替代路径。

    每条路径结构：
      nodes: 路径上的节点快照（长度 = edges + 1，循环时首尾代码可重复）
      edges: 走过的替代边
      cyclic / cycle_node / missing / terminal / truncated 标记位
      allergens: 路径上全部过敏原并集；new_allergens: 非根节点引入的过敏原
    """
    paths: List[Dict[str, Any]] = []

    root_snap = _node_snapshot(nodes, root_code)
    if not root_snap["exists"]:
        missing.add(root_code)

    # 元素为 (路径节点, 路径边)；循环由 "目标已在当前路径上" 判定
    stack: List[Tuple[List[Dict[str, Any]], List[Dict[str, str]]]] = [
        ([root_snap], [])
    ]

    while stack:
        path_nodes, path_edges = stack.pop()
        current_code = path_nodes[-1]["code"]
        last_exists = path_nodes[-1]["exists"]

        edges_out = adjacency.get(current_code, []) if last_exists else []
        prior_codes = {n["code"] for n in path_nodes[:-1]}
        cyclic = current_code in prior_codes
        truncated = len(path_edges) >= MAX_DEPTH

        path_allergens = _union([n["allergens"] for n in path_nodes if n["exists"]])
        root_allergens = set(path_nodes[0]["allergens"]) if path_nodes[0]["exists"] else set()
        new_allergens = [a for a in path_allergens if a not in root_allergens]

        emitted: Dict[str, Any] = {
            "id": next_id(),
            "nodes": path_nodes,
            "edges": path_edges,
            "cyclic": cyclic,
            "cycle_node": current_code if cyclic else None,
            "missing": not last_exists,
            "truncated": truncated,
            "terminal": (not edges_out) and not cyclic and not truncated and last_exists,
            "allergens": path_allergens,
            "new_allergens": new_allergens,
        }
        paths.append(emitted)

        if cyclic or not last_exists or truncated:
            continue  # 循环/缺失/超深路径不再展开

        for edge in reversed(edges_out):  # 反转入栈以保持录入顺序优先
            target = edge["target_code"]
            edge_snap = {"source_code": current_code, "target_code": target, "note": edge["note"]}
            child_snap = _node_snapshot(nodes, target)
            if not child_snap["exists"]:
                missing.add(target)

            new_nodes = path_nodes + [child_snap]
            new_edges = path_edges + [edge_snap]
            stack.append((new_nodes, new_edges))

    return paths


def analyze_recipe(conn: sqlite3.Connection, recipe_id: int) -> Optional[Dict[str, Any]]:
    recipe = conn.execute(
        "SELECT id, code, version, name, note FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()
    if recipe is None:
        return None

    nodes, adjacency = _build_graph(conn)
    items = conn.execute(
        """SELECT ri.ingredient_code, ri.quantity, ri.unit,
                  COALESCE(i.name, '') AS ingredient_name
             FROM recipe_items ri
             LEFT JOIN ingredients i ON i.code = ri.ingredient_code
            WHERE ri.recipe_id = ?
            ORDER BY ri.id""",
        (recipe_id,),
    ).fetchall()

    pid = 0

    def next_id() -> int:
        nonlocal pid
        pid += 1
        return pid

    missing: Set[str] = set()
    roots: List[Dict[str, Any]] = []
    all_direct: List[List[str]] = []
    all_total: List[List[str]] = []
    cycles: List[Dict[str, Any]] = []

    for item in items:
        code = item["ingredient_code"]
        paths = _dfs_paths(code, nodes, adjacency, missing, next_id)

        direct = _union([paths[0]["nodes"][0]["allergens"]]) if paths and paths[0]["nodes"][0]["exists"] else []
        total = _union([p["allergens"] for p in paths])
        introduced = [a for a in total if a not in set(direct)]

        for p in paths:
            if p["cyclic"]:
                cycles.append(
                    {
                        "path_id": p["id"],
                        "root": code,
                        "cycle_node": p["cycle_node"],
                        "nodes": [n["code"] for n in p["nodes"]],
                        "edges": [{"source": e["source_code"], "target": e["target_code"]} for e in p["edges"]],
                    }
                )

        # 过敏原 -> 受影响路径索引（用于"标记每条受影响路径"）
        allergen_index: Dict[str, List[Dict[str, Any]]] = {}
        for p in paths:
            root_allergen_set = set(p["nodes"][0]["allergens"]) if p["nodes"][0]["exists"] else set()
            for allergen in p["allergens"]:
                carriers = [n["code"] for n in p["nodes"] if allergen in n["allergens"]]
                allergen_index.setdefault(allergen, []).append(
                    {
                        "path_id": p["id"],
                        "chain": [n["code"] for n in p["nodes"]],
                        "introduced_by": carriers[-1] if carriers else None,
                        "via_substitution": allergen not in root_allergen_set,
                    }
                )

        all_direct.append(direct)
        all_total.append(total)
        roots.append(
            {
                "ingredient_code": code,
                "ingredient_name": item["ingredient_name"] or f"(缺失原料:{code})",
                "quantity": item["quantity"],
                "unit": item["unit"],
                "exists": code in nodes,
                "direct_allergens": direct,
                "total_allergens": total,
                "introduced_allergens": introduced,
                "path_count": len(paths),
                "has_cycle": any(p["cyclic"] for p in paths),
                "allergen_index": allergen_index,
                "paths": paths,
            }
        )

    direct_all = _union(all_direct)
    total_all = _union(all_total)

    return {
        "recipe": {
            "id": recipe["id"],
            "code": recipe["code"],
            "version": recipe["version"],
            "name": recipe["name"],
            "note": recipe["note"],
        },
        "direct_allergens": direct_all,
        "total_allergens": total_all,
        "introduced_allergens": [a for a in total_all if a not in set(direct_all)],
        "has_cycles": bool(cycles),
        "cycles": cycles,
        "missing_nodes": sorted(missing),
        "roots": roots,
    }
