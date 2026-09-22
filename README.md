# 配方替代过敏原追踪（Allergen Tracker）

面向食品研发的 **配方版本 / 原料 / 替代关系** 管理与过敏原影响追踪 Web 应用。

- 技术栈：Python 3.11 + FastAPI + SQLite + 原生 HTML/CSS/JS（无前端构建、无第三方账号）
- 页面与 API 统一暴露在 **8000** 端口
- 核心能力：以有向图递归计算替代链带来的过敏原影响、检测循环并标记每条受影响路径、**勾选需避开的过敏原后组合整方"安全替代方案"**、**选定一组方案后由后端重新校验并生成可下载（JSON/CSV）的合并采购清单**、两版配方差异对比、原料/配方 JSON 导入导出

---

## 一键启动（Docker Compose，推荐）

```bash
docker compose up --build
```

启动后访问：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/ | Web 页面（打开后进入「安全替代方案」标签即可使用新能力） |
| http://localhost:8000/docs | FastAPI 交互式 API 文档 |

SQLite 数据保存在 Docker volume `allergen-data`（容器内 `/data/app.db`），容器重建不丢数据。

仅用 Docker：

```bash
docker build -t allergen-tracker .
docker run -p 8000:8000 -v "$PWD/data:/data" -e DB_PATH=/data/app.db allergen-tracker
```

## 本地直接运行（无需 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

默认数据库位于 `./data/app.db`，可用环境变量 `DB_PATH` 覆盖。

---

## 快速体验

1. 打开 http://localhost:8000/
2. 进入「**导入导出**」页 → 点击「🌱 载入示例数据」
   - 12 种原料、8 条替代边、2 个版本的曲奇配方（`COOKIE-001` v1.0 / v2.0）
   - 内含一条 **双向替代循环**：`BUTTER → MARGARINE → BUTTER`
3. 在「**过敏原分析**」页选择配方并分析：
   - 直接过敏原 / 含替代链的全部过敏原 / 替代新引入过敏原
   - 每条替代路径被编号标记（`#1`、`#2` …），循环路径红色高亮并指出回到哪个节点
   - 「过敏原受影响路径索引」表可按过敏原反查受影响路径
4. 在「**安全替代方案**」页选择配方、勾选需要避开的过敏原（如 `乳`、`小麦（麸质）`）后点击「生成安全方案」：
   - 每种配料保留原料本身及沿替代边可达的无环终点，再组合为整方方案；含禁用过敏原、缺失节点、循环路径的候选会被排除
   - 最多返回 50 组，按**替换项数 → 路径总长度**升序排列；点击方案标题可展开查看每种配料的所选原料、替代路径与过敏原
   - 无解时红色区块指出**阻塞配料**，并可展开查看其被排除的候选路径（缺失 / 循环 / 含禁用过敏原）
   - 在某个方案下点击「✓ 确认此方案并生成采购清单」：后端会按**当前数据**重新校验所选终点仍避开过敏原（防止生成后原料/替代边/过敏原被改动），通过后按所选终点合并相同原料的数量与单位生成采购清单；校验失败时红色区块逐行列出失败配料与原因，可一键「重新生成安全方案」
   - 采购清单支持 **下载 JSON / CSV**：同原料同单位的数量自动相加，不同单位不做跨单位换算、分行列出并在「无法合并/备注」中说明，未填写数量的配料单独保留待人工核定；清单同时保留每个配方配料的替代路径、过敏原和用量明细
5. 在「**版本对比**」页选择 v1.0 与 v2.0，查看原料增删、用量变化与过敏原集合差异。

---

## 图模型与计算规则

- **节点**：原料，携带过敏原集合（如黄油 → {乳}）
- **有向边** `source → target`：source 可被 target 替代
- 从配方中的每个原料出发做深度优先（DFS）递归枚举**全部替代路径**：
  - 路径上每个节点的过敏原取并集，即该替代方案下的过敏原集合
  - `直接过敏原` = 配方原料自身过敏原；`替代新引入` = 路径下游新增、根节点没有的过敏原
  - 当目标节点已出现在当前路径上时，判定为**循环**：该路径以红色标记并立即停止展开（防止无限递归），同时在顶部汇总所有循环
  - 图中引用了但原料库缺失的节点标记为「缺失节点」
  - 另设有 60 层深度保护上限（`app/graph.py` 的 `MAX_DEPTH`）
- 每条路径带唯一编号；「过敏原 → 路径」索引让每一条受影响路径都可被标记与追溯。

### 安全替代方案的组合规则

- 对配方中的每种配料，候选 = **原料本身**（长度 0）+ 沿替代边可达的**无环终点**（简单路径，终点无出边）
- 候选是否合规取决于**所选终点（最终入料）自身**的过敏原：原料本身或终点携带需避开的过敏原即排除该候选；中间节点不进入最终配方，不会因其含过敏原而剪枝下游（路径上每个节点的过敏原仍随结果展示供人工复核）。缺失节点、成环（回到路径上已有节点）、超过 60 层深度上限的候选同样排除
- 各配料候选做整方组合时使用最小堆按 `(替换项数, 路径总长度)` 最佳优先枚举（每配料候选按路径长度升序），**在组合搜索过程中提前剪枝**，最多返回 50 组，无需展开完整笛卡尔积
- 某配料无任何安全候选时整方无解（原料缺失，或原料自身与全部可达无环终点都含禁用过敏原），返回阻塞配料及被排除路径诊断（如黄油在避开「乳」时：澄清黄油仍含乳、回替人造黄油成环）

### 确认方案与采购清单

- 用户从生成结果中选定一组方案后，前端把每行配料的 `ingredient_code`、所选终点 `selected_code` 与路径节点序列 `path_codes` 提交到确认接口；后端**不复用旧结果，而是按当前原料库与替代边重新枚举**，逐行校验：
  - 提交行仍属于当前配方、候选路径仍是沿现有替代边的简单路径；
  - 所选终点当前不携带任何需避开的过敏原（原料过敏原被改动、替代边被增删都会导致失效）。
- 任何一行不通过即返回 `422`，`detail.failures` 逐行给出原因（`forbidden` / `endpoint_missing` / `path_changed` / `missing_selection` 等），前端提示重新生成方案。
- 校验通过后按**所选终点**合并采购清单：
  - 同一终点、**同一单位**的数量相加；
  - 同一终点出现**不同单位**时不做跨单位换算，分别成行并在备注与 `unmerged_units` 中说明；
  - 原配料**未填写数量**时无法参与数量合并，单独成行标注待人工核定；
  - 每个采购行的 `sources` 保留贡献它的每条配方配料：原配料、用量、替代路径、路径长度、过敏原、是否替代，便于追溯。
- JSON 直接返回完整结构；CSV 为扁平的「采购行 × 来源配料」表格（含 UTF-8 BOM，Excel 可直接打开）。

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/api/ingredients`，PUT/DELETE `/api/ingredients/{code}` | 原料 CRUD |
| GET/POST | `/api/substitutions`，PUT/DELETE `/api/substitutions/{id}` | 替代关系 CRUD |
| GET/POST | `/api/recipes`，PUT/DELETE `/api/recipes/{id}` | 配方版本 CRUD（含配料表整体替换） |
| GET | `/api/recipes/{id}/analyze` | 替代链过敏原递归分析 + 循环检测 |
| POST | `/api/recipes/{id}/safe-plans` | 安全替代方案，body：`{"avoid_allergens": ["乳", "小麦（麸质）"]}`（可空）；返回按替换项数/路径总长度排序的最多 50 组方案，无解时给出阻塞配料 |
| POST | `/api/recipes/{id}/confirm-plan` | 确认一组方案：后端重新校验所选终点仍避开过敏原，通过后返回按终点合并的采购清单；body：`{"avoid_allergens":[...], "selections":[{"ingredient_code","selected_code","path_codes":[...]}]}`；校验失败返回 422，`detail.failures` 逐行给出原因 |
| POST | `/api/compare` | 两版配方差异对比，body：`{"recipe_a":{"code","version"}, "recipe_b":{"code","version"}}`（也支持 `{"id": n}`） |
| GET | `/api/export` | 导出全部数据 JSON |
| POST | `/api/import?mode=merge\|replace` | 导入 JSON；merge 按主键 upsert，replace 清空后导入 |
| POST | `/api/seed` | 载入内置示例数据（幂等） |

### 导入/导出 JSON 格式

```json
{
  "format": "allergen-tracker/v1",
  "ingredients": [
    {"code": "BUTTER", "name": "黄油", "allergens": "乳", "note": ""}
  ],
  "recipes": [
    {
      "code": "COOKIE-001", "version": "1.0", "name": "经典黄油曲奇", "note": "",
      "items": [
        {"ingredient_code": "BUTTER", "quantity": 120, "unit": "g"}
      ]
    }
  ],
  "substitutions": [
    {"source_code": "BUTTER", "target_code": "MARGARINE", "note": "等质量替换"}
  ]
}
```

`allergens` 支持逗号、顿号分隔，服务端自动去空、去重。导入时引用了缺失原料的配方/替代边会被跳过并在返回的 `errors` 中列出。

## 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由（页面 + REST API）
│   ├── graph.py       # 有向图 DFS：替代链过敏原传播 / 循环检测 / 路径标记
│   ├── safe_plans.py  # 安全替代方案：候选剪枝枚举 + 最佳优先整方组合（≤50 组）
│   ├── crud.py        # 原料 / 配方 / 替代关系数据操作
│   ├── schemas.py     # Pydantic 模型与过敏原规范化
│   ├── database.py    # SQLite 连接与建表
│   └── seed.py        # 内置示例数据
├── static/
│   ├── index.html     # 单页界面（7 个标签页）
│   ├── style.css
│   └── app.js         # 原生 JS 前端逻辑
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 备注

- 应用仅供研发阶段的配方/过敏原信息管理，产品标签与合规声明仍需以供应商规格书和检测结果为准。
