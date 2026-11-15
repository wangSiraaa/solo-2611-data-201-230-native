# Elasticsearch 批量写入的项级结果与不确定提交核对

本地 bulk 数据 sink，面向可能部分成功、可能在服务端已写入后断连的下游
Elasticsearch 兼容接口。只实现固定 `index` bulk 操作与 `/_mget` 核对，
不做全文检索、集群管理、鉴权或并发服务。

## 文件

- `bulk_sink.py` — sink 本体与 `send` / `status` / `verify` 三个子命令（纯标准库）
- `run_demo.py` — 拉起 `mock_es.py`，分别以 `batch_size=2` 和 `4` 跑完整场景并交叉核对
- `test_bulk_sink.py` — 内存 fake 后端的单元测试（预算耗尽、mget 不可用、响应错位等分支）
- `mock_es.py`、`fixtures/` — 题目提供的模拟服务与输入，不修改

## 语义要点

- 指纹：document 规范 JSON（UTF-8、`ensure_ascii=False`、键排序、
  `separators=(',', ':')`、禁止 NaN）的 SHA256，写入
  `_source.ingest_fingerprint`；bulk 元数据固定 `version_type=external`。
- document 内的错误类型原样提交（例如 `amount_cents` 为字符串），由下游返回
  真实映射错误，本地不修数；只有无法序列化（NaN/Inf）或非正整数 version 在本地拒绝。
- 按输入顺序组批；同一 `(index,id)` 的多条记录严格串行，上一条未决时不派发
  其后版本，不同 ID 可同批。
- 完整响应逐项按请求位置与 `_index/_id` 核对：200/201 接受、429 可重试、
  400 永久映射错误；顶层 HTTP 200 不代表成功。回执绑定 `source_id`。
- 409 或连接中断导致结果不确定时用 `/_mget` 分类：
  - 目标版本与指纹都相同 → `confirmed`
  - 目标版本更高 → `superseded`（单独报告，不声称本端内容已写入）
  - 版本相同、指纹不同 → `conflict`
  - 目标不存在或版本更低 → 才可重发
- 每条记录最多 3 次写入尝试；已确认/永久错误项绝不随整批重发；预算耗尽保留
  `unresolved`，不用异常吞掉。服务端处理次数通过 `attempts` 历史核对。

## 运行

```bash
# 端到端：自动管理 mock 进程，两个 batch size 对照，退出码 0 表示全部通过
python3 run_demo.py

# 单元测试
python3 -m unittest test_bulk_sink -v

# 单独使用 CLI（先按 fixtures/protocol.md 启动 mock_es，取 ready 中的 base_url）
python3 bulk_sink.py send   --base-url "$BASE_URL" --input fixtures/records.jsonl \
    --work-dir <dir> --batch-size 2 --retry-interval 0.2
python3 bulk_sink.py status --work-dir <dir>
python3 bulk_sink.py verify --base-url "$BASE_URL" --work-dir <dir>
```

## 工作目录产物

```
config.json                 # 运行配置
status.json                 # 最终逐项状态汇总
bulk/round-N/request.ndjson # 实际发送的 NDJSON
bulk/round-N/request.json   # 位置 -> source_id/ID/version 对照
bulk/round-N/response.json  # 完整 bulk 响应（或 transport_error.txt）
mget/round-N.json           # 每次 /_mget 的请求、重试与完整回读依据
receipts/<source_id>.json   # 逐项回执：状态、原因、尝试次数、完整历史
```

`verify` 读取 mock 的只读 `/__inspect`（`docs`/`attempts`/`writes`）与本地
回执交叉核对：每条 confirmed 的成功响应或回读依据、superseded/conflict 的
mget 依据、终态后无误重试、尝试次数与服务端一致、同 key 串行、以及当前索引
只须符合各 ID 的最终有效版本（旧 confirmed 可被更高版本覆盖，不要求仍是当前内容）。
