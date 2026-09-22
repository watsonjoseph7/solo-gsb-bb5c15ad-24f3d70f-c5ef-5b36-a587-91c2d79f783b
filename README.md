# 配方替代过敏原追踪（Allergen Tracker）

面向食品研发的 **配方版本 / 原料 / 替代关系** 管理与过敏原影响追踪 Web 应用。

- 技术栈：Python 3.11 + FastAPI + SQLite + 原生 HTML/CSS/JS（无前端构建、无第三方账号）
- 页面与 API 统一暴露在 **8000** 端口
- 核心能力：以有向图递归计算替代链带来的过敏原影响、检测循环并标记每条受影响路径、**勾选需避开的过敏原后组合整方"安全替代方案"**、**选定一组方案后由后端重新校验并生成可下载（JSON/CSV）的合并采购清单**、两版配方差异对比、**配方变更审计（同事务不可变事件 + 时间线查询 + 可读配料差异）**、原料/配方 JSON 导入导出

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

### 启动说明与自检

- 首次启动时 `init_db()` 会自动建表（含 `recipe_audit_events` 审计表及禁止 UPDATE/DELETE 的触发器），**无需手动迁移**；旧版本已有的 `data/app.db` 也会通过 `CREATE TABLE/TRIGGER IF NOT EXISTS` 平滑升级，已有数据不受影响。
- 审计时间为 UTC（与配方 `created_at` 一致，使用 SQLite `datetime('now')`）。
- 启动后可这样快速自检：

```bash
curl -s http://localhost:8000/api/health                 # {"status":"ok"}
curl -s -X POST http://localhost:8000/api/seed           # 载入示例数据（会产生配方 created 事件）
curl -s "http://localhost:8000/api/audit/events?limit=5" # 查看最近审计事件
```

- 本地开发也可指定临时数据库：`DB_PATH=/tmp/allergen.db uvicorn app.main:app --reload --port 8000`。
- Docker Compose 配置无需调整：镜像只包含 `app/` 与 `static/`，数据库仍持久化在 `allergen-data` volume（容器内 `/data/app.db`），审计事件随该 volume 一并持久化。

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
6. 在「**版本对比**」页下方的「**配方变更审计时间线**」中：
   - 每次创建 / 修改 / 删除配方都会记录一条不可变事件（与配方操作在**同一数据库事务**中提交，操作失败一起回滚）
   - 事件包含操作时间（UTC）、配方 id / 代码 / 版本、前后完整配料快照，以及按配料增删、用量变化、单位变化生成的中文可读差异
   - 可按「全部配方 / 某配方代码的全部版本 / 单个配方 id」与时间范围过滤；展开任一事件可对比前后快照明细表。

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

### 配方变更审计

- 每次创建、修改、删除配方时，`app/crud.py` 在**同一个 SQLite 事务**内向 `recipe_audit_events` 表追加一条事件后才由请求连接统一提交；任一步失败整体回滚（`main.conn_dep` 异常时显式 rollback），因此不存在“配方改了但没留痕”或“留了痕但配方没变”的中间态。
- 事件内容：自增事件 id、配方 id、配方代码与版本、动作（`created` / `updated` / `deleted`）、操作时间 `occurred_at`（UTC，`datetime('now')`）、操作前 / 操作后两份**完整配方 + 配料快照**（JSON，含每个配料的代码、名称、用量、单位）。
- **不可变**：表上建有 `BEFORE UPDATE` / `BEFORE DELETE` 触发器，任何修改或删除审计行的尝试都会被数据库直接拒绝（`RAISE(ABORT)`）；事件只能追加。注意 `import?mode=replace` 清空业务表时会逐个删除配方以逐条留下 `deleted` 事件，审计历史本身不会被清空。
- 内容完全相同的重复保存（快照前后一致）不产生新事件，避免审计噪音。
- 差异在查询时根据两份快照即时计算（`app/audit.py` 的 `diff_snapshots`）：配料**新增 / 删除**、**用量变化**、**单位变化**（或用量与单位同时变化）以及配方名称 / 备注 / 版本号 / 代码等元数据变化，各自输出中文可读行；结构化的 `added` / `removed` / `changed` 字段同时返回，供前端排版。
- 时间线支持按配方 id、配方代码（代码被修改后按新代码查询；历史事件保留事件发生时的代码）和 `start` / `end` 时间范围（ISO 8601，如 `2026-09-01` 或 `2026-09-01T08:00:00`，按 UTC 比较）过滤，按时间倒序、最多返回 `limit`（1~500，默认 100）条。

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
| GET | `/api/audit/events` | 配方变更审计时间线；query：`recipe_id` / `recipe_code` / `start` / `end`（ISO 8601，UTC）/ `limit`（默认 100，1~500）；返回事件及前后快照、结构化与可读差异 |
| GET | `/api/recipes/{id}/audit` | 单个配方的变更审计时间线（参数同上；配方不存在返回 404，已删除配方可用 `/api/audit/events?recipe_code=` 追溯） |
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
│   ├── crud.py        # 原料 / 配方 / 替代关系数据操作（配方写操作同事务追加审计事件）
│   ├── audit.py       # 配方变更审计：快照 / 不可变事件写入 / 时间线查询 / 可读差异
│   ├── schemas.py     # Pydantic 模型与过敏原规范化
│   ├── database.py    # SQLite 连接、建表与审计表不可变触发器
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
