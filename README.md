# 影像谱系 API（Image Lineage API）

遥感制图年度拼图交付前，用于追踪原始瓦片与拼图之间多级固定引用关系、
判定现役拼图可交付状态并列出受阻引用路径的 FastAPI + SQLite 服务。

## 功能与规则

- **分批提交清单**：`POST /api/batches`，每批可含任意数量条目；条目包含
  成果号、修订号、内容摘要，以及固定引用的父成果号 + 父修订号（原始瓦片无父项）。
- **多级引用、父项允许晚到**：拼图可指向另一拼图，父项尚未提交时不拒绝入库，
  查询时标记为「父项缺失」；补传父项后结论自动更新。
- **现役修订**：同一成果号以**最高已提交修订号**为现役；旧修订保留可追溯，
  可通过 `?revision=N` 查询历史修订当时的谱系结论。
- **可交付判定**：只有整条谱系有效才可交付。阻断原因三类，均给出完整引用路径：
  - `missing` 父项缺失（成果或修订从未提交）
  - `superseded` 父项已非现役（同成果号存在更高修订）
  - `cycle` 循环引用（引用链成环，含自引用）
- **整批拒收**：同号同修订但清单内容（摘要/父引用/**足迹**）不同时，整批拒绝、零写入，
  原有结论保持不变，并返回冲突明细；完全相同的重复提交按幂等处理。
- **可选像素足迹**：清单条目支持可选的整数像素足迹（`left/top/right/bottom`，
  右下边界不含，须 `right>left`、`bottom>top`）；旧清单不带足迹仍可提交、追溯。
- **覆盖核验**：`POST /api/coverage/gaps`，输入目标矩形及至多 64 个指定（成果号, 修订号），
  返回目标内**互不重叠的缺口矩形**与缺口总面积，并分类列出问题瓦片：
  - `missing` 成果或修订从未提交
  - `superseded` 该修订已非现役（新修订提交后旧修订不再贡献覆盖）
  - `no_footprint` 现役有效但清单未带足迹
  - 足迹相交/重叠只计一次、超出目标只计目标内实际覆盖（自动裁剪）；
    全程 Python 整数运算，坐标跨度再大也精确，无浮点误差。
- **并发安全**：写事务 `BEGIN IMMEDIATE` 串行化，写入失败整批回滚、不留部分足迹；
  每次查询/核验在单个 SQLite 快照事务内完成全部读取，不会混用某次并发提交前后的状态。
- **审计**：所有批次（含拒收/重复）写入 `batches` 表，可通过 `/api/batches` 追溯。

## 目录结构

```
app/
  main.py       FastAPI 路由与应用工厂
  schemas.py    Pydantic 请求/响应模型（中文 Schema 说明）
  database.py   SQLite 连接、显式事务、整批原子提交、旧库足迹列迁移
  lineage.py    谱系评估（缺失/退役/环检测与引用路径）
  coverage.py   覆盖核验（矩形裁剪/差集、互不重叠缺口、整数精确面积）
tests/
  test_api.py       谱系端到端关键情形自测
  test_coverage.py  覆盖核验（重叠/换版/无足迹/大坐标/写入失败/迁移）自测
Dockerfile
docker-compose.yml
requirements.txt
```

## Docker Compose 启动（推荐）

```bash
docker compose up --build
# 服务: http://localhost:8000
# 可操作文档(Swagger UI): http://localhost:8000/docs
# 备用文档: http://localhost:8000/redoc
```

SQLite 文件位于容器内 `/data/lineage.db`，通过命名卷 `lineage-data` 持久化。
健康检查：`GET /health`。

停止并保留数据：`docker compose down`；连同数据卷一起清除：
`docker compose down -v`。

## 本地直接运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 数据库路径可用环境变量 IMAGE_LINEAGE_DB 覆盖，默认 data/lineage.db
```

## 自测

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/batches` | 分批提交清单（accepted / duplicate / rejected），条目可选带像素足迹 |
| GET | `/api/batches` | 批次审计列表 |
| GET | `/api/batches/{id}` | 单批次记录 |
| GET | `/api/products/{id}/status` | 现役（或 `?revision=N` 指定）修订的可交付状态与阻断路径 |
| GET | `/api/mosaics` | 全部现役拼图，支持 `?deliverable=true/false` 过滤 |
| GET | `/api/products/{id}/revisions` | 成果修订历史（含足迹）与现役标记 |
| POST | `/api/coverage/gaps` | 覆盖核验：目标矩形 + ≤64 个指定瓦片，返回互不重叠缺口、面积与问题瓦片 |

## 快速示例

```bash
# 1) 拼图先到（父瓦片晚到）
curl -X POST localhost:8000/api/batches -H 'Content-Type: application/json' -d '{
  "items":[{"product_id":"M-100","revision":1,"summary":"年度拼图",
            "parent_product_id":"R-1","parent_revision":1}]}'

# 2) 查看受阻路径
curl localhost:8000/api/products/M-100/status

# 3) 补传父瓦片后自动转为可交付
curl -X POST localhost:8000/api/batches -H 'Content-Type: application/json' -d '{
  "items":[{"product_id":"R-1","revision":1,"summary":"原始瓦片"}]}'
curl localhost:8000/api/products/M-100/status
```

阻断路径元素形如 `M-100@1`、`R-1@1`，从被查询成果逐级上行，末位即问题节点。

## 覆盖核验示例

```bash
# 1) 提交带像素足迹的原始瓦片（足迹可省略；旧清单格式继续兼容）
curl -X POST localhost:8000/api/batches -H 'Content-Type: application/json' -d '{
  "items":[
    {"product_id":"R-1","revision":1,"summary":"左半瓦片",
     "footprint":{"left":0,"top":0,"right":40,"bottom":100}},
    {"product_id":"R-2","revision":1,"summary":"右半瓦片（与 R-1 重叠 10px）",
     "footprint":{"left":30,"top":0,"right":100,"bottom":100}}
  ]}'

# 2) 核验目标 100x100 画幅：重叠不重复计数，返回缺口矩形与问题瓦片
curl -X POST localhost:8000/api/coverage/gaps -H 'Content-Type: application/json' -d '{
  "target":{"left":0,"top":0,"right":100,"bottom":100},
  "tiles":[{"product_id":"R-1","revision":1},
           {"product_id":"R-2","revision":1},
           {"product_id":"GHOST","revision":1}]}'
# → covered_area=10000、gap_area=0、gaps=[]；GHOST@1 进入 missing_tiles
```

也可在浏览器打开 `http://localhost:8000/docs`，找到 **覆盖核验** 标签下的
`POST /api/coverage/gaps`，点击 Try it out 直接演示缺口核验。

## 数据模型要点

- `revisions` 表以 `(product_id, revision)` 为主键，父引用为固定的
  `(parent_product_id, parent_revision)`；每条修订不可变（冲突即整批拒收）。
- 足迹以 `footprint_left/top/right/bottom` 四个可空整数列存储；
  无足迹列的旧库启动时自动 `ALTER TABLE` 迁移，旧行足迹为 NULL、行为不变。
- 同号同修订足迹不同视同清单冲突，整批拒收、零写入；
  新修订与全部足迹在同一 `BEGIN IMMEDIATE` 事务内原子提交，写入失败整体回滚。
- `batches` 记录每次提交的状态、条目数、实际写入数与时间。
- 覆盖核验在单个只读快照事务内读取修订状态与足迹，结论不混用并发提交前后状态。
- WAL 模式 + `busy_timeout=30s`，支持读写并发、写事务排队串行化。
