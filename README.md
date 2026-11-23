# 展签送印 · 图版-语种搭配登记

馆员在展签送印前查询某展品的每张图版可配哪些语种，并按批上传
「图版号 + 语种」CSV 进行去重追加登记。Flask + SQLite，Docker Compose 启动。

## 功能与规则

- 网页填写**展品号**并上传 CSV，每行一种可印搭配 `图版号,语种`（可带表头，
  列名识别 `plate/图版`、`language/语种/语言`；支持 UTF-8(BOM) 与 GB18030）。
- 数据**去重追加**到 SQLite：文件内重复行先折叠，再与库内已登记配对取并集。
- **每批按合并后的全量数据选择存法**：
  - **清单存法（dense）**：图版集合 × 语种集合构成完整笛卡尔积（任意图版可配
    任意语种）时，只存去重后的图版清单、语种清单两张表；
  - **配对存法（sparse）**：存在缺项时，只存实际的 `(图版, 语种)` 配对。
- 存法可随时**互换**：合并后重新判定，单事务清空旧存法数据并重写新存法数据，
  已登记的可印搭配集合不丢失。
- **核对搭配是否齐全只用已登记数据**：详情页与 API 基于库内数据给出
  `is_complete` 与缺项明细 `missing_pairs`。
- 网页按**展品 → 图版**列出可印语种，并标注当前存法、批次历史与缺项。
- **整理（补录）期间查询仍返回已提交的完整资料**：写事务只锁写，查询不被阻塞
  （WAL + `BEGIN IMMEDIATE` 短事务），其他展品可照常补录。
- **同一展品的并发补录全部纳入结果**：每个请求独立连接，写事务
  `BEGIN IMMEDIATE` 串行化读-改-写，配合 `busy_timeout` 与应用层重试，无丢更新。
- **批次失败保留已提交资料**：单批次异常整事务回滚；CSV 校验失败整单拒收并在
  批次日志留痕，不触碰既有数据。

## 目录结构

```
app/
  db.py        # 连接/WAL/建表/BEGIN IMMEDIATE 重试封装
  storage.py   # 合并、存法选择与互换、查询
  csvio.py     # CSV 解析（表头/编码/去重/拒收）
  web.py       # Flask 路由（网页 + JSON API）
  wsgi.py      # gunicorn 入口
  templates/   # 列表页、详情页
tests/         # 存储层 11 项 + HTTP 端到端 6 项（标准库 unittest）
docker/entrypoint.sh  # 启动前幂等建表
Dockerfile, docker-compose.yml, requirements.txt
examples/      # 完整/缺项两份示例 CSV
```

## Docker Compose 启动

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 数据持久化在宿主机 `./data/labels.db`（首次自动建表）。
容器内以 gunicorn `2 worker × 4 thread` 运行，健康检查 `GET /healthz`。

## 本机直接运行（无 Docker 时）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export LABEL_DB_PATH=./data/labels.db
python -m app.db                       # 可选：显式建表（首请求也会幂等建表）
gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 4 app.wsgi:app
```

## JSON API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/exhibits` | 展品列表及存法 |
| GET | `/api/exhibits/<展品号>` | 按图版列出可印语种、存法、缺项、批次历史 |
| POST | `/api/exhibits/<展品号>/batches` | multipart 字段 `csv_file` 上传一批 |
| GET | `/healthz` | 健康检查 |

## 自测

```bash
# 存储层：存法选择、互换、交错补录、并发不丢更新、失败回滚
python -m unittest discover -s tests -v

# HTTP 端到端（真实 gunicorn 2×4，含并发补录与交错展品）
GUNICORN_BIN=$(which gunicorn) python -m unittest tests.test_http -v
```

## 数据库表

- `storage_state`：每个展品当前存法（dense/sparse）与更新时间；
- `dense_plates` / `dense_languages`：清单存法的两份去重清单；
- `sparse_pairs`：配对存法的实际 `(图版,语种)` 配对；
- `batch_log`：每批提交/拒收留痕（原始行数、去重行数、新增数、累计数、存法切换）。
