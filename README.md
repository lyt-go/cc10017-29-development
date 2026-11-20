# 实时数据入湖平台

面向流式数据接入、实时转换、增量合并与湖上物化查询的实时数据入湖治理平台。

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
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 主题注册 | `topic_registry` | 数据主题 | 主题编号、主题名称、消息格式 |
| 流式计算 | `flink_job` | 流式任务 | 任务编号、消费主题、转换逻辑 |
| 消息接入 | `kafka_connector` | 接入配置 | 配置编号、集群地址、消费组名 |
| Schema演进 | `schema_evolve` | 演进规则 | 规则编号、关联主题、新增字段 |
| 增量合并 | `upsert_merge` | 合并策略 | 策略编号、目标表名、合并键列 |
| 湖上分区 | `lake_partition` | 分区定义 | 分区编号、目标表名、分区列名 |
| 流式质量 | `stream_quality` | 质量规则 | 规则编号、监控主题、校验类型 |
| Iceberg管理 | `iceberg_table` | 表元数据 | 表编号、表名称、存储格式 |
| 水位追踪 | `watermark_track` | 水位标记 | 标记编号、数据主题、分区编号 |
| 死信处理 | `dead_letter` | 死信记录 | 记录编号、原始主题、原始分区 |
| 物化查询 | `materialized_view` | 物化视图 | 视图编号、查询SQL、基表名 |
| CDC捕获 | `change_capture` | CDC配置 | 配置编号、源数据库、源表名 |
| 合并清理 | `compact_policy` | 清理策略 | 策略编号、目标表名、触发条件 |
| 流式血缘 | `stream_lineage` | 血缘记录 | 记录编号、上游主题、下游表名 |
| 流式补数 | `backfill_stream` | 补数任务 | 任务编号、目标表名、起始偏移 |
| 补数恢复清单 | `backfill_recovery` | 恢复清单 | 清单编号、已完成偏移、待补区间 |
| 下沉分发 | `sink_dispatch` | 分发规则 | 规则编号、湖表名、目标系统 |
| 数据生命周期 | `ttl_policy` | 生命周期 | 策略编号、目标表名、热数据保留 |
| 实时指标 | `realtime_metrics` | 指标定义 | 指标编号、指标名称、聚合窗口 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 补数恢复清单（流式补数 → 合并清理）

补数任务中断时不再只留一个运行状态，而是把**已完成偏移**与**待补区间**一起写回任务；
补数恢复清单从任务读取这两项成对落账，恢复时按清单的第一条待补项继续，不再从头重放。

- 流式补数页：`GET /api/backfill_stream/orders/tasks` 读取订单表补数任务进度，
  `POST /api/backfill_stream/orders/recovery` 生成恢复清单（同一任务复用清单，不重复开单）。
- 补数恢复清单页 `/backfill_recovery`：按清单编号、任务编号、目标表、来源批次筛选，
  支持关键字检索、状态过滤与分页；排序按记录 id 稳定，列表与详情读取同一读模型。
- 重启点冲突：迟到记录落在「检查点偏移 < 偏移 <= 已完成偏移」时，清单标记「待裁决」，
  必须先选择**续跑**（从已完成偏移继续）或**重放**（从上一检查点重放）之一，
  清单只保留一个恢复起点，并把这次选择记入数据版本历史；未裁决前不允许重试。
- 重试恢复：按清单的已完成偏移跳过重复边界，补齐后续待补项，清单置为已完成。
- 合并清理页：`GET /api/compact_policy/recovery-check?target_table=orders` 核对可合并范围，
  待补区间未完成或冲突未裁决时不放行 `合并文件`；放行后合并产物保留来源批次。

领域：数据工程
子方向：数仓与数据湖
