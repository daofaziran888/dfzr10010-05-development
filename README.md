# 湖仓一体化治理平台

面向数据架构师的湖仓一体化治理平台。覆盖数据湖表格式管理、Iceberg/Hudi表优化、小文件合并、数据压缩策略、元数据服务、表快照管理、数据分支合并与数据回滚恢复的完整湖仓治理链路。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 湖表格式管理 | `tablefmt` | 表格式配置 | 配置编号、表名称、格式类型 |
| 小文件合并 | `compact` | 合并任务 | 任务编号、目标表名、文件范围 |
| 数据压缩策略 | `compress` | 压缩配置 | 配置编号、目标表名、压缩算法 |
| 元数据服务 | `metadata` | 元数据表 | 表编号、表名称、字段清单 |
| 表快照管理 | `snapshot` | 快照记录 | 快照编号、表名称、快照时间 |
| 数据分支合并 | `branch` | 分支记录 | 分支编号、表名称、分支类型 |
| 数据回滚恢复 | `rollback` | 回滚记录 | 记录编号、表名称、回滚版本 |
| 分区演进管理 | `partition` | 演进计划 | 计划编号、表名称、当前分区 |
| 表优化管理 | `iceberg` | 优化任务 | 任务编号、表名称、优化类型 |
| 表优化执行计划 | `plan` | 执行计划 | 计划编号、资源总预算、优化前后容量 |
| 数据目录同步 | `catalog` | 同步作业 | 作业编号、源路径、目标目录 |
| 数据过期清理 | `cleanup` | 清理策略 | 策略编号、目标表名、过期条件 |
| 湖存储配额 | `quota` | 配额配置 | 配置编号、存储空间、表数量上限 |
| 湖仓血缘追踪 | `lineage` | 血缘记录 | 记录编号、上游表名、下游表名 |
| 治理任务调度 | `schedule` | 治理任务 | 任务编号、任务类型、调度表达式 |
| 表统计信息 | `stats` | 统计报告 | 报告编号、表名称、行总数 |
| 数据迁移管理 | `migrate` | 迁移任务 | 任务编号、源表名、目标表名 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 表优化执行计划（`/plan`）

表优化管理只负责登记优化任务；执行计划把「优化任务 + 容量统计」编排成不超预算的批次。

- 上游读取：`iceberg` 的活动优化任务与 `stats` 的最新容量统计（按表去重、取最新一份）。
- 编排口径：默认按收益比例排序，在资源配置总预算内贪心选表；预算不够就跳过当前候选、
  继续比对剩余候选。同表只允许一个活动优化任务，已保存计划会预留预算，避免重复计算收益。
- 版本：保存、版本冲突处理、重新编排都会推进计划版本；表统计信息与预算核对清单
  都跟随当前版本，同一份计划的核对清单只保留当前版本的行。
- 版本冲突：新到达的高收益任务与已预留预算冲突时，可选择「保留原任务」或「改排新任务」；
  统计信息最终只记录被选计划的实际效果。
- 执行与核对：执行完成后把实际优化前后的文件数、容量与实际收益写回表统计信息，
  并同步到预算核对清单；恢复结论也记在核对清单里。
- 样例：`ods_orders` 当前 1.2 TB、25400 个文件，预算 800 GB，行组上限 512 MB。

接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/plan/config` | 读取资源总预算、行组上限与默认排序 |
| POST | `/api/plan/config` | 调整预算配置（预算必须为正数） |
| GET | `/api/plan/capacity` | 容量统计视图 |
| GET | `/api/plan/candidates?sort_by=` | 候选视图：任务、容量、预算与收益，支持多种排序 |
| POST | `/api/plan/preview` | 试算执行计划（不落库） |
| GET/POST | `/api/plan/plans` | 计划列表 / 生成并保存执行计划 |
| GET | `/api/plan/plans/{id}` | 计划明细：预算、优化前后容量、实际效果、核对清单、版本历史 |
| POST | `/api/plan/plans/{id}/actions` | 执行计划、重新编排、同步核对、恢复、归档 |
| GET | `/api/plan/plans/{id}/reconcile` | 当前版本的预算核对清单 |
| GET | `/api/plan/plans/{id}/versions` | 版本历史 |
| GET | `/api/plan/conflicts` | 版本冲突列表 |
| POST | `/api/plan/conflicts/detect` | 扫描新任务与已预留预算的冲突 |
| POST | `/api/plan/conflicts/{id}/actions` | 保留原任务 / 改排新任务 |

服务级自测：`cd backend && .venv/bin/python -m app.tests_plan`。

领域：数据工程
子方向：数仓与数据湖
