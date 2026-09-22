"""安全替代方案：在避开指定过敏原的前提下，为整方枚举替代组合。

候选规则（每种配料）
--------------------
- 保留「原料本身」（长度 0 的路径，无论它是否有出边）；
- 以及沿现有替代边可达的「无环终点」：路径为简单路径（不重复经过节点），
  终点出度为 0。
- 候选是否合规取决于**所选终点（最终入料）自身**是否携带禁用过敏原：
  原料本身或终点命中即排除该候选。替代的语义是用终点替换原配料，中间
  节点不会进入最终配方，因此不因中间节点含禁用过敏原而剪枝
  （路径上各节点过敏原仍随结果展示，供人工复核）。
- 命中缺失节点、回到已访问节点（成环）、超过深度上限的路径同样排除。

整方组合
--------
各配料的安全候选做笛卡尔积，使用最小堆按
(替换项数, 路径总长度) 最佳优先枚举（候选按路径长度升序，保证入堆键
单调不减），最多产出 MAX_PLANS 组——无需展开完整笛卡尔积即可保证
返回的就是排序最靠前的前 N 组，即组合搜索过程中的提前剪枝。

当某配料没有任何安全候选时整方无解（例如原料缺失，或原料自身与全部
可达无环终点都含禁用过敏原），此时返回阻塞配料及被排除候选路径的诊断。

确认方案（confirm_plan）
------------------------
用户从生成结果中选定一组方案后，服务端**按当前数据重新校验**所选终点
仍然满足避开过敏原约束（防止结果生成后原料/替代边/过敏原被改动而失效），
随后按所选终点合并相同原料的数量与单位生成采购清单；无法合并的单位
（同一终点对应多种不同单位或未填写数量）单独保留并给出说明。
"""
import heapq
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from . import crud, graph
from .schemas import normalize_allergens

MAX_PLANS = 50
MAX_SAMPLE_PATHS = 10


# ---------------- 单配料候选枚举 ----------------

def _node_dict(snap: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "code": snap["code"],
        "name": snap["name"],
        "allergens": list(snap["allergens"]),
        "exists": snap["exists"],
    }


def _edge_dict(edges: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return [
        {"source_code": e["source_code"], "target_code": e["target_code"], "note": e.get("note", "")}
        for e in edges
    ]


def _excluded(reason: str, path, edges, trigger_node, trigger_allergens) -> Dict[str, Any]:
    return {
        "reason": reason,
        "trigger_node": trigger_node,
        "trigger_allergens": list(trigger_allergens),
        "path": [_node_dict(n) for n in path],
        "edges": _edge_dict(edges),
    }


def _make_option(path, edges) -> Dict[str, Any]:
    endpoint = path[-1]
    return {
        "selected_code": endpoint["code"],
        "selected_name": endpoint["name"],
        "path_length": len(edges),
        "path": [_node_dict(n) for n in path],
        "edges": _edge_dict(edges),
        "allergens": list(endpoint["allergens"]),
    }


def _enumerate_options(
    root_code: str,
    nodes: Dict[str, Dict[str, Any]],
    adjacency: Dict[str, List[Dict[str, str]]],
    avoid_set: Set[str],
):
    """枚举单个配料的安全候选。

    返回 (options, excluded, root_meta)：
    - options: 通过全部约束的候选（已按偏好排序）；
    - excluded: 被排除候选/分支的诊断（含禁用过敏原、缺失、循环、超深）；
    - root_meta: 原料节点是否存在、原料自身命中的禁用过敏原。
    """
    options: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    root = graph._node_snapshot(nodes, root_code)

    if not root["exists"]:
        excluded.append(_excluded("missing", [root], [], root_code, []))
        return options, excluded, {"exists": False, "self_forbidden": []}

    root_hits = sorted(set(root["allergens"]) & avoid_set)
    if root_hits:
        excluded.append(_excluded("forbidden", [root], [], root_code, root_hits))
    else:
        # 原料本身始终保留为候选
        options.append(_make_option([root], []))

    # 栈元素：(路径节点, 已走边)；简单路径由"目标已在路径上"判定为环
    stack: List[Tuple[List[Dict[str, Any]], List[Dict[str, str]]]] = [([root], [])]

    while stack:
        path, edges = stack.pop()
        current = path[-1]
        out = adjacency.get(current["code"], [])

        if not out:
            # 出度为 0 的无环终点（长度 0 的自身已单独收录）
            if len(path) > 1:
                hits = sorted(set(current["allergens"]) & avoid_set)
                if hits:
                    excluded.append(_excluded("forbidden", path, edges, current["code"], hits))
                else:
                    options.append(_make_option(path, edges))
            continue

        if len(edges) >= graph.MAX_DEPTH:
            excluded.append(_excluded("truncated", path, edges, None, []))
            continue

        for edge in reversed(out):  # 反转入栈以保持录入顺序优先
            target = edge["target_code"]
            new_edges = edges + [
                {"source_code": current["code"], "target_code": target, "note": edge["note"]}
            ]

            # 成环：目标已在当前路径上
            if any(n["code"] == target for n in path):
                excluded.append(
                    _excluded("cyclic", path + [graph._node_snapshot(nodes, target)], new_edges, target, [])
                )
                continue

            child = graph._node_snapshot(nodes, target)
            if not child["exists"]:
                excluded.append(_excluded("missing", path + [child], new_edges, target, []))
                continue

            # 中间节点含禁用过敏原不剪枝：更下游的无环终点仍可能安全
            stack.append((path + [child], new_edges))

    options.sort(
        key=lambda o: (
            o["path_length"],
            o["selected_code"],
            tuple(n["code"] for n in o["path"]),
        )
    )
    return options, excluded, {"exists": True, "self_forbidden": root_hits}


# ---------------- 整方最佳优先组合 ----------------

def _combine(
    options_per_item: List[List[Dict[str, Any]]],
    limit: int = MAX_PLANS,
) -> Tuple[List[Tuple[int, ...]], bool]:
    """以 (替换项数, 路径总长度) 升序，用最小堆枚举候选索引组合。

    每个配料的候选已按路径长度升序排列，因此沿"某一配料换下一个候选"
    生成后继时键值单调不减；配合 visited 去重，弹出序列即为全局有序，
    且最多只生成 limit × 配料数 数量级的状态。
    返回 (索引组合列表, 是否还有更多未产出的方案)。
    """
    n = len(options_per_item)
    if n == 0:
        return [()], False

    def key_of(idx: Tuple[int, ...]):
        sub = total = 0
        for k, j in enumerate(idx):
            length = options_per_item[k][j]["path_length"]
            total += length
            if length > 0:
                sub += 1
        return sub, total

    start = (0,) * n
    heap: List[Tuple[Tuple[int, int], Tuple[int, ...]]] = [(key_of(start), start)]
    visited = {start}
    picked: List[Tuple[int, ...]] = []

    while heap and len(picked) < limit:
        _, idx = heapq.heappop(heap)
        picked.append(idx)
        for k in range(n):
            if idx[k] + 1 < len(options_per_item[k]):
                nxt = idx[:k] + (idx[k] + 1,) + idx[k + 1:]
                if nxt not in visited:
                    visited.add(nxt)
                    heapq.heappush(heap, (key_of(nxt), nxt))

    return picked, bool(heap)


# ---------------- 响应组装 ----------------

def _blocked_entry(item, root_meta: Dict[str, Any], excluded: List[Dict[str, Any]]) -> Dict[str, Any]:
    missing = not root_meta["exists"]
    reason = "missing" if missing else "forbidden"
    counts = {"forbidden": 0, "missing": 0, "cyclic": 0, "truncated": 0}
    samples: List[Dict[str, Any]] = []
    for exc in excluded:
        if exc["reason"] in counts:
            counts[exc["reason"]] += 1
        if len(samples) < MAX_SAMPLE_PATHS:
            samples.append(exc)

    if missing:
        text = "原料节点缺失：该配料引用的原料不在原料库中，无法参与替代"
    else:
        text = (
            f"原料本身含禁用过敏原（{'、'.join(root_meta['self_forbidden'])}），"
            "且沿替代边不存在合规的无环终点"
        )
        detail = []
        if counts["forbidden"]:
            detail.append(f"{counts['forbidden']} 个无环终点含禁用过敏原")
        if counts["cyclic"]:
            detail.append(f"{counts['cyclic']} 条替代路径成环")
        if counts["missing"]:
            detail.append(f"{counts['missing']} 条替代路径到达缺失节点")
        if detail:
            text += "（" + "、".join(detail) + "）"

    return {
        "ingredient_code": item["ingredient_code"],
        "ingredient_name": item["ingredient_name"] or f"(缺失原料:{item['ingredient_code']})",
        "quantity": item["quantity"],
        "unit": item["unit"],
        "reason": reason,
        "reason_text": text,
        "self_forbidden": root_meta["self_forbidden"],
        "excluded_counts": counts,
        "samples": samples,
    }


def build_safe_plans(conn, recipe_id: int, avoid_raw: Optional[List[str]]) -> Optional[Dict[str, Any]]:
    recipe = crud.get_recipe(conn, recipe_id)
    if recipe is None:
        return None

    nodes, adjacency = graph._build_graph(conn)
    items = conn.execute(
        """SELECT ri.ingredient_code, ri.quantity, ri.unit,
                  COALESCE(i.name, '') AS ingredient_name
             FROM recipe_items ri
             LEFT JOIN ingredients i ON i.code = ri.ingredient_code
            WHERE ri.recipe_id = ?
            ORDER BY ri.id""",
        (recipe_id,),
    ).fetchall()

    avoid: List[str] = []
    for raw in avoid_raw or []:
        for allergen in normalize_allergens(raw):
            if allergen not in avoid:
                avoid.append(allergen)
    avoid_set = set(avoid)

    options_per_item: List[List[Dict[str, Any]]] = []
    blocked: List[Dict[str, Any]] = []

    for item in items:
        options, excluded, root_meta = _enumerate_options(
            item["ingredient_code"], nodes, adjacency, avoid_set
        )
        options_per_item.append(options)
        if not options:
            blocked.append(_blocked_entry(item, root_meta, excluded))

    plans: List[Dict[str, Any]] = []
    has_more = False
    if not blocked:
        chosen, has_more = _combine(options_per_item)
        for rank, idx in enumerate(chosen, start=1):
            plan_items = []
            allergen_lists: List[List[str]] = []
            sub_count = 0
            total_length = 0
            for k, item in enumerate(items):
                opt = options_per_item[k][idx[k]]
                if opt["path_length"] > 0:
                    sub_count += 1
                total_length += opt["path_length"]
                allergen_lists.append(opt["allergens"])
                plan_items.append(
                    {
                        "ingredient_code": item["ingredient_code"],
                        "ingredient_name": item["ingredient_name"]
                        or f"(缺失原料:{item['ingredient_code']})",
                        "quantity": item["quantity"],
                        "unit": item["unit"],
                        "substituted": opt["path_length"] > 0,
                        "selected_code": opt["selected_code"],
                        "selected_name": opt["selected_name"],
                        "path_length": opt["path_length"],
                        "path": opt["path"],
                        "edges": opt["edges"],
                        "allergens": opt["allergens"],
                    }
                )
            plans.append(
                {
                    "rank": rank,
                    "substitution_count": sub_count,
                    "total_path_length": total_length,
                    "allergens": graph._union(allergen_lists),
                    "items": plan_items,
                }
            )

    return {
        "recipe": {
            "id": recipe["id"],
            "code": recipe["code"],
            "version": recipe["version"],
            "name": recipe["name"],
            "note": recipe["note"],
        },
        "avoid_allergens": avoid,
        "solvable": not blocked,
        "plan_count": len(plans),
        "limit": MAX_PLANS,
        "has_more": has_more,
        "plans": plans,
        "blocked": blocked,
    }


# ---------------- 确认方案：重新校验 + 采购清单合并 ----------------

def _option_matches(opt: Dict[str, Any], selected_code: str, path_codes: List[str]) -> bool:
    """用户所选是否与当前枚举出来的某个候选一致。

    优先按完整路径节点代码序列精确匹配；前端未携带路径时退化为仅按终点匹配。
    """
    if opt["selected_code"] != selected_code:
        return False
    if not path_codes:
        return True
    return [n["code"] for n in opt["path"]] == list(path_codes)


def _selection_failure(
    sel: Dict[str, Any],
    item: Optional[Any],
    options: List[Dict[str, Any]],
    nodes: Dict[str, Dict[str, Any]],
    adjacency: Dict[str, List[Dict[str, str]]],
    avoid_set: Set[str],
) -> Tuple[str, str]:
    """说明某一行配料的所选候选为什么当前不再合规。返回 (reason, reason_text)。"""
    code = sel["ingredient_code"]
    selected_code = sel["selected_code"]
    path_codes = sel.get("path_codes") or []
    unit_text = f"（单位：{item['unit'] or '无'}）" if item is not None else ""

    root = graph._node_snapshot(nodes, code)
    endpoint = graph._node_snapshot(nodes, selected_code)

    if not root["exists"]:
        return "missing", f"配料 {code} 已不在原料库中（缺失原料），无法确认{unit_text}"

    # 终点原料是否还存在
    if not endpoint["exists"]:
        return "endpoint_missing", (
            f"配料 {code} 所选的替代终点 {selected_code} 已不在原料库中，"
            "替代关系可能已被删除或改动，请重新生成方案"
        )

    # 终点当前是否携带禁用过敏原
    hits = sorted(set(endpoint["allergens"]) & avoid_set)
    if hits:
        return "forbidden", (
            f"配料 {code} 所选的 {selected_code} 目前携带需避开的过敏原"
            f"（{'、'.join(hits)}），请重新生成方案"
        )

    # 声称的路径是否仍构成一条简单路径且沿现有替代边可达
    if path_codes:
        chain_ok = path_codes[0] == code and path_codes[-1] == selected_code
        if chain_ok:
            for a, b in zip(path_codes, path_codes[1:]):
                targets = {e["target_code"] for e in adjacency.get(a, [])}
                if b not in targets:
                    chain_ok = False
                    break
        if not chain_ok:
            return "path_changed", (
                f"配料 {code} 原选替代路径 {' → '.join(path_codes)} 已失效"
                "（替代关系已被增删或改动），请重新生成方案"
            )
        if len(set(path_codes)) != len(path_codes):
            return "path_changed", (
                f"配料 {code} 原选替代路径出现重复节点（成环），请重新生成方案"
            )
        # 路径仍在且终点安全，只是没出现在枚举里——理论上不应发生
        if not any(_option_matches(o, selected_code, path_codes) for o in options):
            return "path_changed", (
                f"配料 {code} 所选路径不再是合规的无环终点候选，请重新生成方案"
            )
        return "invalid_option", "所选候选当前不可用，请重新生成方案"

    # 未携带路径：只要当前枚举里仍存在该终点候选即合规
    if any(o["selected_code"] == selected_code for o in options):
        return "invalid_option", "所选候选当前不可用，请重新生成方案"

    # 终点安全但当前不可达（替代边被删）
    return "unreachable", (
        f"配料 {code} 已不存在通往 {selected_code} 的合规替代路径，"
        "替代关系可能已被删除，请重新生成方案"
    )

def _qnum(v) -> Optional[float]:
    """合并后的数量规整：去掉浮点误差，整数返回 int。"""
    if v is None:
        return None
    r = round(float(v) + 0.0, 6)
    return int(r) if r == int(r) else r


def _merge_purchase(confirmed: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按所选终点合并相同原料：同单位数量相加，不同单位保留为无法合并项。

    返回 (purchase_rows, unmerged_units)。行的首次出现顺序即终点在配料表中
    的出现顺序。
    """
    order: List[str] = []
    grouped: Dict[str, Dict[str, Any]] = {}

    for c in confirmed:
        code = c["selected_code"]
        unit = c["unit"] or ""
        if code not in grouped:
            order.append(code)
            grouped[code] = {
                "selected_code": code,
                "selected_name": c["selected_name"],
                "allergens": list(c["allergens"]),
                "by_unit": {},       # unit -> {"qty": float|None累加, "sources": [...]}
                "unquantified": [],  # quantity 为空的来源
            }
        g = grouped[code]

        source = {
            "ingredient_code": c["ingredient_code"],
            "ingredient_name": c["ingredient_name"],
            "quantity": c["quantity"],
            "unit": unit,
            "substituted": c["substituted"],
            "path_length": c["path_length"],
            "path": c["path"],
            "edges": c["edges"],
        }

        if c["quantity"] is None:
            g["unquantified"].append(source)
        else:
            bu = g["by_unit"].setdefault(unit, {"qty": 0.0, "sources": []})
            bu["qty"] += float(c["quantity"])
            bu["sources"].append(source)

    rows: List[Dict[str, Any]] = []
    unmerged: List[Dict[str, Any]] = []

    for code in order:
        g = grouped[code]
        units = sorted(g["by_unit"].keys())

        # 有数值的单位各自成行（同单位才合并）
        for unit in units:
            bu = g["by_unit"][unit]
            note = ""
            if len(units) > 1:
                others = "、".join(u or "（无单位）" for u in units if u != unit)
                note = f"同一原料存在不同单位（{others}），未做跨单位换算，已分行列出"
            rows.append(
                {
                    "selected_code": code,
                    "selected_name": g["selected_name"],
                    "quantity": _qnum(bu["qty"]),
                    "unit": unit,
                    "allergens": list(g["allergens"]),
                    "merged": len(bu["sources"]) > 1,
                    "source_count": len(bu["sources"]),
                    "sources": bu["sources"],
                    "note": note,
                }
            )

        # 未填写数量的配料无法参与数量合并，单独成行
        for src in g["unquantified"]:
            rows.append(
                {
                    "selected_code": code,
                    "selected_name": g["selected_name"],
                    "quantity": None,
                    "unit": src["unit"],
                    "allergens": list(g["allergens"]),
                    "merged": False,
                    "source_count": 1,
                    "sources": [src],
                    "note": "原配料未填写数量，无法参与数量合并，请按配方人工核定",
                }
            )
            unmerged.append(
                {
                    "selected_code": code,
                    "selected_name": g["selected_name"],
                    "unit": src["unit"],
                    "reason": "原配料未填写数量，无法参与数量合并",
                    "sources": [
                        {"ingredient_code": src["ingredient_code"], "ingredient_name": src["ingredient_name"]}
                    ],
                }
            )

        # 多单位并存时，记录一条无法合并说明（同单位组合已各自成行）
        if len(units) > 1:
            unmerged.append(
                {
                    "selected_code": code,
                    "selected_name": g["selected_name"],
                    "unit": None,
                    "reason": "同一终点原料对应多种不同单位，系统不做跨单位换算，已分别成行",
                    "units": units,
                    "sources": [
                        {"ingredient_code": s["ingredient_code"], "ingredient_name": s["ingredient_name"],
                         "quantity": s["quantity"], "unit": s["unit"]}
                        for u in units for s in g["by_unit"][u]["sources"]
                    ],
                }
            )

    return rows, unmerged


def confirm_plan(
    conn,
    recipe_id: int,
    avoid_raw: Optional[List[str]],
    selections: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """校验用户选定的方案仍满足避开过敏原约束，确认后生成合并采购清单。

    返回 None 表示配方不存在；返回 {"valid": False, ...} 表示校验失败；
    返回 {"valid": True, ...} 时含可下载的采购清单。
    """
    recipe = crud.get_recipe(conn, recipe_id)
    if recipe is None:
        return None

    nodes, adjacency = graph._build_graph(conn)
    items = conn.execute(
        """SELECT ri.ingredient_code, ri.quantity, ri.unit,
                  COALESCE(i.name, '') AS ingredient_name
             FROM recipe_items ri
             LEFT JOIN ingredients i ON i.code = ri.ingredient_code
            WHERE ri.recipe_id = ?
            ORDER BY ri.id""",
        (recipe_id,),
    ).fetchall()

    avoid: List[str] = []
    for raw in avoid_raw or []:
        for allergen in normalize_allergens(raw):
            if allergen not in avoid:
                avoid.append(allergen)
    avoid_set = set(avoid)

    # 重新枚举当前数据下每个配料行的安全候选（与生成接口完全一致的规则）。
    # 注意：配方允许同一原料代码出现多行，因此一切按"配方行（位置）"对齐，
    # 不按 ingredient_code 去重。
    options_per_row: List[List[Dict[str, Any]]] = []
    for item in items:
        options, _, _ = _enumerate_options(item["ingredient_code"], nodes, adjacency, avoid_set)
        options_per_row.append(options)

    failures: List[Dict[str, Any]] = []
    matched_per_row: List[Optional[Dict[str, Any]]] = []

    for idx in range(len(items)):
        item = items[idx]
        options = options_per_row[idx]
        code = item["ingredient_code"]

        if idx >= len(selections):
            matched_per_row.append(None)
            failures.append(
                {
                    "ingredient_code": code,
                    "ingredient_name": item["ingredient_name"] or f"(缺失原料:{code})",
                    "selected_code": None,
                    "quantity": item["quantity"],
                    "unit": item["unit"],
                    "reason": "missing_selection",
                    "reason_text": (
                        f"第 {idx + 1} 行配料 {code} 未选择最终原料，"
                        "请为配方中的每行配料各选择一个终点后再确认"
                    ),
                }
            )
            continue

        sel = selections[idx]
        selected_code = sel.get("selected_code")
        path_codes = sel.get("path_codes") or []

        # 提交行与配方行必须指向同一个配料
        if sel.get("ingredient_code") != code:
            matched_per_row.append(None)
            failures.append(
                {
                    "ingredient_code": code,
                    "ingredient_name": item["ingredient_name"] or f"(缺失原料:{code})",
                    "selected_code": selected_code,
                    "quantity": item["quantity"],
                    "unit": item["unit"],
                    "reason": "row_mismatch",
                    "reason_text": (
                        f"第 {idx + 1} 行提交的是配料 {sel.get('ingredient_code')}，"
                        f"但当前配方该行是 {code}（配方可能已被修改），请重新生成方案"
                    ),
                }
            )
            continue

        matched = next(
            (o for o in options if _option_matches(o, selected_code, path_codes)),
            None,
        )
        if matched is None:
            reason, text = _selection_failure(
                {"ingredient_code": code, "selected_code": selected_code, "path_codes": path_codes},
                item, options, nodes, adjacency, avoid_set,
            )
            failures.append(
                {
                    "ingredient_code": code,
                    "ingredient_name": item["ingredient_name"] or f"(缺失原料:{code})",
                    "selected_code": selected_code,
                    "quantity": item["quantity"],
                    "unit": item["unit"],
                    "reason": reason,
                    "reason_text": text,
                }
            )
        matched_per_row.append(matched)

    if len(selections) > len(items):
        for extra in selections[len(items):]:
            failures.append(
                {
                    "ingredient_code": extra.get("ingredient_code"),
                    "selected_code": extra.get("selected_code"),
                    "reason": "extra_selection",
                    "reason_text": (
                        f"配料 {extra.get('ingredient_code')} 的选择超出当前配方行数"
                        "（配方可能已被修改），请重新生成方案"
                    ),
                }
            )

    recipe_out = {
        "id": recipe["id"],
        "code": recipe["code"],
        "version": recipe["version"],
        "name": recipe["name"],
        "note": recipe["note"],
    }

    if failures:
        return {
            "recipe": recipe_out,
            "avoid_allergens": avoid,
            "valid": False,
            "failures": failures,
        }

    # 校验通过：以当前枚举到的候选为准组装确认明细（按配方行）
    confirmed: List[Dict[str, Any]] = []
    for idx, item in enumerate(items):
        opt = matched_per_row[idx]
        confirmed.append(
            {
                "ingredient_code": item["ingredient_code"],
                "ingredient_name": item["ingredient_name"]
                or f"(缺失原料:{item['ingredient_code']})",
                "quantity": item["quantity"],
                "unit": item["unit"],
                "substituted": opt["path_length"] > 0,
                "selected_code": opt["selected_code"],
                "selected_name": opt["selected_name"],
                "path_length": opt["path_length"],
                "path": opt["path"],
                "edges": opt["edges"],
                "allergens": opt["allergens"],
            }
        )

    purchase_rows, unmerged = _merge_purchase(confirmed)
    return {
        "recipe": recipe_out,
        "avoid_allergens": avoid,
        "valid": True,
        "confirmed_at": datetime.now().isoformat(timespec="seconds"),
        "substitution_count": sum(1 for c in confirmed if c["substituted"]),
        "allergens": graph._union([c["allergens"] for c in confirmed]),
        "confirmed_items": confirmed,
        "purchase_list": purchase_rows,
        "unmerged_units": unmerged,
    }
