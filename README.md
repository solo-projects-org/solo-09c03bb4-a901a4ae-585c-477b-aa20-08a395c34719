# 结算视图影响分析

面向数仓/结算工程师的小工具：导入「基础表字段 + 视图 SELECT 定义」目录，
选择某个基础表字段，立即列出停用该字段后**会失效的结算视图、具体输出列以及
引用路径**。目录导入是整体替换语义；任何结构错误（引用不存在、输出列重名、
循环引用）都会拒绝整份目录并保留原版本。

技术栈：FastAPI · SQLite · sqlglot · 原生 HTML/CSS/JS。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build -d
# 打开 http://localhost:8000
docker compose logs -f
```

- 首次启动且数据库为空时，会自动导入 `samples/catalog.json` 作为示例目录
  （由 `SEED_CATALOG` 控制；不需要可删掉该环境变量）。
- 目录与历史版本保存在命名卷 `settlement-data` 中（`/data/settlement.db`）。
- 端口可通过 `APP_PORT` 覆盖：`APP_PORT=9000 docker compose up -d`。
- 健康检查：`GET /api/health`；停止：`docker compose down`（加 `-v` 会删除数据卷）。

### 本地运行

```bash
python3 -m pip install -r requirements.txt
python3 -m app            # http://localhost:8000
# 可选：SEED_CATALOG=samples/catalog.json python3 -m app
# 可选：SETTLEMENT_DB=/tmp/settlement.db python3 -m app
```

### 测试

```bash
python3 -m pytest -q
```

## 页面操作

1. **导入目录**：选择或拖入 JSON 文件 → 点击「导入并替换目录」。校验失败时
   页面逐条列出错误，顶部版本徽标仍显示原版本。也可以点「载入示例」。
2. **选择字段**：左侧按基础表分组列出字段，支持搜索。点击字段芯片即分析。
3. **查看结果**：右侧显示字段、受影响视图数、输出列数、整视图失效数；
   每个视图卡片列出受影响输出列与引用路径（`表.字段 ──SELECT→ 视图.列 ──JOIN→ …`），
   还可展开原始 SQL；底部列出不受影响的视图。
4. 结果区始终展示**本次分析绑定的目录版本号与 sha256**。若分析前后发生了导入，
   页面会自动同步并明确提示「结果对应旧版本」。

## 目录 JSON 格式

```json
{
  "base_tables": {
    "orders": ["order_id", "customer_id", "amount", "status", "currency"],
    "payments": ["payment_id", "order_id", "paid_amount"]
  },
  "views": [
    {
      "name": "vw_settlement",
      "sql": "SELECT o.order_id AS order_id, o.amount - COALESCE(p.paid_total, 0) AS open_amount FROM orders o LEFT JOIN vw_order_payments p ON o.order_id = p.order_id WHERE o.status = 'paid'"
    }
  ]
}
```

兼容写法：`base_tables` 可为数组 `[{"name": ..., "columns": [...]}]`；
`views` 可为对象 `{"视图名": "SELECT ..."}`；视图定义键也可用 `select`/`query`/`definition`。

## 校验规则（任一失败即整份拒绝，原版保留）

| 规则 | 说明 |
| --- | --- |
| SQL 可解析 | SQLite 方言，单条 `SELECT`；不支持 CTE / 子查询 / UNION，请拆成多个视图 |
| 引用存在 | FROM/JOIN 的表必须在 `base_tables` 或 `views` 中 |
| 字段存在 | 引用的字段必须在目标表/视图的输出列中 |
| 显式投影 | 必须列出输出字段，不支持 `*` / `t.*` |
| 带表别名 | 每个字段引用必须写成 `别名.字段`（GROUP BY / HAVING / ORDER BY 可直接引用投影输出别名） |
| JOIN 方式 | 不支持 `USING` / `NATURAL JOIN`，需改用 `ON` 并带别名 |
| 输出列唯一 | 同一视图输出列名不可重复（大小写不敏感） |
| 无循环 | 视图依赖图不能有环，错误信息会打印环路径 |

## 影响判定规则

- 字段出现在某视图的 **投影表达式**中 → 该视图的对应输出列受影响；影响沿下游
  视图传播（若下游该列被用于 JOIN/WHERE 等谓词，则下游整视图受影响）。
- 字段出现在某视图的 **JOIN / WHERE / GROUP BY / HAVING / ORDER BY** 中 →
  该视图**全部输出**受影响；所有直接或间接引用该视图的下游视图也整视图受影响。
- 路径中 `UPSTREAM` 表示「因上游视图整体失效而受影响」。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 + 当前版本 |
| GET | `/api/catalog` | 当前生效目录与摘要 |
| POST | `/api/catalog/import` | multipart 上传 JSON，整体替换；失败返回 422 与错误列表 |
| GET | `/api/versions` | 版本历史（含 sha256、是否生效） |
| GET | `/api/versions/{version}` | 指定版本摘要 |
| POST | `/api/impact` | `{"table": "...", "column": "...", "version": 可选}`，返回受影响视图/输出列/路径，并带 `catalog` 版本信息 |
| GET | `/api/sample` | 内置示例目录 |

## 一致性设计

- `catalog_versions` 表保存不可变快照（原始 JSON、规范化 JSON、摘要、sha256），
  `state` 表保存唯一生效版本指针。导入在一个 `BEGIN IMMEDIATE` 事务中同时写入
  快照与切换指针，失败整体回滚。
- 校验全部在事务外完成，校验通过的目录才会写入；因此不存在「半份目录」。
- 分析接口在单条 SQL 中同时读取指针与快照（`state JOIN catalog_versions`），
  结果与返回的版本号必然属于同一已提交版本；前端把版本号固定传给后续请求。
- 页面丢弃过期请求响应，避免快速连点时旧结果覆盖新结果。
