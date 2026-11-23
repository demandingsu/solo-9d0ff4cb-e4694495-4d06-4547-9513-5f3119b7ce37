# 展签送印 · 主展登记与巡展分站差异

馆员在展签送印前查询某展品的每张图版可配哪些语种，并按批上传
「图版号 + 语种」CSV 进行去重追加登记。巡展分站沿用主展展签，
只保存本站差异（增补当地语种 / 排除个别搭配）。Flask + SQLite，
Docker Compose 启动。

## 功能与规则

### 主展（展品登记）

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

### 巡展分站（差异登记）

- 馆员在 `/stations` 页为分站选一个**已登记的主展**并登记分站号；
  主展不存在时拒绝创建。
- 分站差异 CSV 每行三列 `图版号,语种,操作`（可带表头，列名含
  `plate/图版`、`language/语种`、`action/操作`）：
  - 操作 `add`（别名：增补/加印/新增/添加…）= 分站增补；
  - 操作 `exclude`（别名：排除/取消/删除/剔除…）= 提前排除。
- **新文件整份替换**该站旧差异（单事务先删后插，无中间空窗）；
  **空清单**（空文件或只有表头）清空差异，分站恢复直接沿用主展。
- 同一搭配在同一文件内写了相反操作（既 add 又 exclude）时**整单拒收**，
  在差异日志留痕，**原差异继续生效**。
- 生效搭配在查询时由「**同一时点**的主展已登记数据 + 本站差异」即时合成：
  - 主展补录或切换 dense/sparse 存法后，分站**自动跟上**，无需迁移；
  - **提前排除的搭配后来补到主展，本站仍不印**（排除持续生效）；
  - 同一搭配主展与分站增补都有时，来源算**分站增补**且**只计一次**；
  - 分站页按图版列出可印语种并标注来源（主展 / 分站增补）。
- **各站只存自己的差异**（`station_diffs` 按分站号隔离），不复制主展数据。
- **dense 主展算总数不展开笛卡尔积**：用清单基数做集合算术
  `主展数 − 落在主展内的排除数 + 主展外增补数`，万级搭配同样只扫清单与差异；
  明细与总数在同一连接同一时点读取，口径一致（含断言自检）。

## 目录结构

```
app/
  db.py        # 连接/WAL/建表/BEGIN IMMEDIATE 重试封装
  storage.py   # 主展：合并、存法选择与互换、查询
  stations.py  # 分站：注册、差异整份替换、生效视图合成（dense 基数算术）
  csvio.py     # CSV 解析（主展两列 / 分站三列差异、表头/编码/拒收）
  web.py       # Flask 路由（网页 + JSON API）
  wsgi.py      # gunicorn 入口
  templates/   # 主展列表/详情、分站列表/详情
tests/         # 存储层 11+17 项 + HTTP 端到端 8 项（标准库 unittest）
docker/entrypoint.sh  # 启动前幂等建表
Dockerfile, docker-compose.yml, requirements.txt
examples/      # 主展完整/缺项示例 + 分站差异示例 CSV
```

## Docker Compose 启动

```bash
docker compose up --build
# 打开 http://localhost:8000
# 分站入口：http://localhost:8000/stations
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
| GET | `/api/exhibits` | 主展展品列表及存法 |
| GET | `/api/exhibits/<展品号>` | 按图版列出可印语种、存法、缺项、批次历史 |
| POST | `/api/exhibits/<展品号>/batches` | multipart 字段 `csv_file` 上传主展一批 |
| GET | `/api/stations` | 分站列表（含各站增补/排除条数） |
| POST | `/api/stations` | 表单/JSON 字段 `station_no`、`parent_exhibit_no` 登记分站 |
| GET | `/api/stations/<分站号>` | 生效视图：按图版的可印语种与来源、总数、差异、日志 |
| PUT | `/api/stations/<分站号>/diffs` | multipart 字段 `csv_file` 整份替换差异（空清单清空） |
| GET | `/healthz` | 健康检查 |

## 自测

```bash
# 存储层：主展存法选择/互换/并发 + 分站差异替换/提前排除/来源标注/dense 基数
python -m unittest discover -s tests -v

# HTTP 端到端（真实 gunicorn 2×4，含分站提前排除→主展补录→空清单清空）
GUNICORN_BIN=$(which gunicorn) python -m unittest tests.test_http -v
```

## 数据库表

- `storage_state`：每个主展展品当前存法（dense/sparse）与更新时间；
- `dense_plates` / `dense_languages`：清单存法的两份去重清单；
- `sparse_pairs`：配对存法的实际 `(图版,语种)` 配对；
- `batch_log`：主展每批提交/拒收留痕；
- `stations`：分站号及其沿用的主展展品号；
- `station_diffs`：各站自己的差异 `(图版,语种,action)`，action 为 add/exclude；
- `station_diff_log`：分站差异文件提交/拒收留痕（含增补、排除条数）。
