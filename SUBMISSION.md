# SUBMISSION

素材检索与结果导出测评交付说明。

- 分支：`feature/search-filter-export`
- 基线：`main` @ `3aa137c`
- 最终 commit SHA：见交付邮件中的 `git rev-parse HEAD`（本文件随最终提交一起入库）
- 技术栈保持不变：FastAPI + SQLite/FTS5 + 原生 JavaScript，无新增运行时依赖

## 1. 交付内容

| 需求 | 实现 |
| --- | --- |
| 组合筛选 | 关键词（FTS5，沿用现有字段/分词）、集合、必须包含全部标签、排除任意标签，全部取交集；只返回 `active`；标签完整名称、忽略大小写、自动去重 |
| 排序与分页 | 创建时间升序/降序（保留名称、大小排序）；关键词生效时仍使用所选排序；上一页/下一页、匹配总数、每页 12/24/48/120 |
| 稳定分页 | 所有排序追加 `a.id` 作为最终排序键；相同创建时间不会跨页重复/遗漏 |
| CSV 导出 | `GET /api/assets/export.csv`，复用列表筛选与排序，不受分页限制；至少包含要求的 7 列；UTF-8 BOM + RFC 4180；0 条结果仍输出表头 |
| 修改后生效 | 复用现有 `PATCH /api/assets/{uuid}`、标签增删入口；写路径重建 FTS，列表和导出立即反映修改 |
| 交互细节 | 生效条件可视化、单条删除、全部清除；筛选/排序变化回到第 1 页；请求序号防竞态；加载失败提示 + 重试；非法分页参数 422 |

### 主要文件

- `keepstack/search.py`：统一筛选条件构造、稳定排序、分页查询、导出全量查询
- `keepstack/export.py`：CSV 序列化（BOM、引号/逗号/换行、标签拼接、公式注入防护）
- `keepstack/app.py`：列表参数扩展与校验、CSV 下载端点、标签删除后同步重建 embedding
- `web/index.html` / `web/app.js` / `web/styles.css`：筛选栏、条件 chips、分页器、导出下载、错误/竞态处理
- `tests/test_search_export.py`：本题验收测试
- `scripts/generate_demo_data.py`：可复现的场景数据

## 2. 运行与验证

### 2.1 启动

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"

export KEEPSTACK_AI_ENABLED=false
export KEEPSTACK_OCR_ENABLED=false

# 可选：上游演示数据（24 素材 / 3 集合）
uv run --no-sync python -m keepstack seed

# 推荐：生成与本题业务场景一致的数据（14 素材 / 2 集合）
uv run --no-sync python scripts/generate_demo_data.py

uv run --no-sync python -m keepstack
```

打开 <http://localhost:8000>，使用 `admin` / `admin` 登录。`scripts/generate_demo_data.py` 会拒绝在同一数据目录重复执行，换目录即可重新生成。

### 2.2 自动化测试

```bash
uv run --no-sync python -m pytest -q
```

实测结果（Python 3.12.10，Windows 10）：

```text
42 passed, 2 skipped in 2.83s
```

2 个 skip 是上游依赖本机 Tesseract / CLIP 模型的测试（`vision.ready()`、`ocr.available()`），与本题无关。新增测试覆盖：

- 关键词 + 集合 + 包含标签 + 排除标签的交集，及被集合/状态排除的素材
- 重复包含标签、同一标签同时包含与排除（空结果）
- 排除多个标签时“任意一个命中即排除”
- 关键词下创建时间升/降序
- 相同 `created_at` 的跨页稳定性、重复请求顺序一致性
- 导出与列表筛选/排序一致、导出行数不受当前页限制
- 中文、逗号、双引号、换行在 CSV 中的往返解析；零结果仅表头
- 媒体类型筛选应用到导出
- 导出鉴权（未登录 401）与重复 query 参数
- 非法分页参数（`limit=0`、`limit=201`、`offset=-1`、非数字）返回 422
- 仅标点/引号/中文/百分号等特殊关键词不报错
- 通过已有 API 修改标题、描述、增删标签后，搜索与导出同步变化
- CSV 公式前缀的中和

### 2.3 手动端到端结果（1280×720）

以 `scripts/generate_demo_data.py` 的数据实测：

1. 关键词 `harbor` + 集合 `Harbor Campaign (quiz)` + 包含 `approved`、`campaign` + 排除 `internal`，创建时间升序 → **6 条**：`Harbor at dawn`、`Harbor at dusk`、`Harbor pilot chart`、`海港 Harbor, "晨光"\n第二行`、`Harbor twin A`、`Harbor twin B`。
2. 三条相同 `created_at` 的素材在升序中按 id 稳定排列；降序严格反向。
3. 无筛选时 13 条 active，每页 12 条显示 `Page 1 of 2`；下一页仅 1 条；修改关键词/集合/标签后自动回到第 1 页。
4. 点击 `Export CSV`：提示 `Exported 6 rows`，响应 `X-Total-Count: 6`，BOM + 表头 + 6 行；中文行字段经 CSV 解析后与原文完全一致。
5. 在详情抽屉把标题改为 `Harbor at dawn uiedit`、描述改为含逗号文本，搜索 `uiedit` → 1 条；导出包含新标题/描述。新增标签 `ui-check` 后，该标签可参与筛选并出现在导出 `tags` 列。
6. 同一标签同时加入“包含”和“排除” → 页面显示冲突提示，结果 0 条，空状态正常。
7. 清空全部筛选 → 搜索框、集合、包含/排除标签全部复位，列表与计数恢复。

## 3. 主要取舍

1. **筛选 SQL 使用 `EXISTS` 而不是多次 JOIN**
   多标签 `JOIN` 需要 `GROUP BY/HAVING COUNT`，且容易在 COUNT 与分页查询之间出现重复行语义差异。`EXISTS` 对每个标签表达“至少一条关联”，与“全部包含/任一排除”语义一一对应，也天然让“同一标签同时包含和排除”得到空集。`asset_tags` 主键 `(asset_id, tag_id)` 与 `tags.name` 唯一索引可支撑查询。

2. **关键词模式放弃 bm25 排序**
   原实现有 `q` 时强制 `ORDER BY bm25(...)`，与“有关键词时也遵守所选排序”冲突。现在统一使用 `created_at + id`，相关性排序不在本题范围内。

3. **`a.id` 作为最终排序键**
   仅按 `created_at` 排序时，相同时间戳的行顺序在 SQLite 中不保证稳定，分页会出现重复/遗漏。`id` 不可变且唯一，是最小、可靠的 tie-breaker；名称/大小排序也加了同样的兜底。

4. **导出与列表共享同一份 WHERE/ORDER 构造**
   `search._build_query()` 同时服务列表与导出，避免两套条件逐渐漂移；导出只去掉 LIMIT/OFFSET。测试直接断言导出 uuid 序列与完整列表一致。

5. **CSV 采用 UTF-8 BOM + `\r\n` + 标准库 `csv`**
   Excel 对无 BOM 的 UTF-8 中文容易乱码；标准库负责逗号、引号、换行的转义。多标签用 `|` 连接，已在本文注明。为防止上传内容在电子表格中作为公式执行，以 `= + - @`、制表符或回车开头的单元格会加一个前导单引号；这是有意的安全取舍。

6. **导出走 `fetch` + Authorization + Blob 下载**
   现有鉴权是 Bearer token，直接 `<a href>` 无法带请求头。先 fetch 再用临时 object URL 下载，401/非 2xx 均以 toast 提示；`X-Total-Count` 用于成功提示。

7. **竞态与分页状态**
   搜索输入 250ms 防抖；所有列表请求带自增序号，只接受最后一次响应，避免慢请求覆盖新条件。筛选/排序/页大小变化统一重置到第 1 页；若数据变化导致当前页超出范围，回退到最后一页并只重载一次。

8. **筛选选项统计不联动**
   侧栏“Media type / Top tags”明确标注为 `all assets` 的全库统计，不冒充当前匹配数（题目允许不联动，但不允许误标）。

## 4. 修复的既有缺陷（影响本题）

- **前端 `api()` 的 JSON 请求未设置 method**：带 body 的 GET 被浏览器拒绝，导致登录、创建集合、分享等所有 `{ json: ... }` 调用失败。真实浏览器验证时发现并修复为默认 POST（显式 method 优先）。
- `q` 仅由标点组成时生成空 FTS 表达式，触发 `MATCH ''` 语法错误；现在视为无关键词。
- 有关键词时 bm25 覆盖所选排序（见取舍 2）。
- `limit` 只限制上界，负数会被 SQLite 当作“无限制”；`offset` 未限制负数。现在 `ge=1/ge=0`，非法值 422。
- 标签删除后未重建 embedding，语义检索会保留旧标签；现已同步重建。
- `PRAGMA`/上游原有功能未改动。

## 5. 限制与未完成项

- **导出是同步、全量内存构造**。当前规模（万级以内）足够；若素材量很大，应改为流式响应或后台任务 + 下载凭证，这不在 4 小时范围内。
- **语义搜索未接入新筛选条件**，且 UI 已按题目允许隐藏语义入口；后端原语义接口保留，仅验收关键词。
- 分页是 offset 模式，不提供跨请求快照。题目只要求“数据不变时”一致，所以未引入快照/游标。
- 筛选状态未写入 URL，刷新或前进/后退不保留条件；不影响题目验收，但可作为后续改进。
- 标签名若本身包含 `|`，CSV 的多标签列会产生歧义；当前标签写入入口未限制该字符。可在后续统一转义或改用 JSON 数组。
- 公式注入防护会给以 `= + - @` 开头的原值加前导 `'`，因此这类值不是逐字节原样导出；普通中英文、逗号、引号、换行不受影响。
- 未实现异步导出、导出模板配置、按用户保存筛选条件等非要求项。

## 6. 可复现性

- 测试数据由 `tests/test_search_export.py` 在临时目录内确定性创建；`tests/conftest.py` 在任何 `keepstack` 导入前设置 `KEEPSTACK_DATA_DIR`，不会写入仓库 `data/`。
- 演示数据由 `scripts/generate_demo_data.py` 生成，随机种子固定，时间戳固定；重复运行会被拒绝而不是产生重复素材。
- `.gitignore` 继续排除 `data/`、数据库、上传文件与虚拟环境；提交中不含本地数据或凭据。
