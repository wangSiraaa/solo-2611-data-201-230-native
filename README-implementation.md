# 持久化 MQTT 3.1.1 QoS2 接收器

单客户端、单主题（`telemetry/line`，QoS2），无 TLS、无鉴权、无发布业务、无消费组。
每次完成 QoS2 二次握手的原始负载恰好写入 SQLite 一条业务记录。不实现通用 broker。

## 为什么是受控协议实现，而不是直接用 paho-mqtt

核对过 paho-mqtt 2.2.0（`paho/mqtt/client.py`）的确认时机，它无法满足“先落库再确认”：

- 入站 QoS2 PUBLISH 在 `_handle_publish` 中**立即** `_send_pubrec`（约 4147 行），
  待交付消息只保存在内存字典 `_in_messages`（约 4151 行），PUBLISH 与 PUBREC 之间
  没有任何回调切入点，无法先把负载持久化。
- `on_message` 实际在 `_handle_pubrel`（约 4195 行）才触发。
- `manual_ack=True` 只能延后 PUBCOMP（约 4208-4211 行），**拦不住 PUBREC**；对未知
  PUBREL 它仍会直接回 PUBCOMP，源码注释（约 4202-4207 行）自己承认这可能丢消息，
  且没有磁盘会话。

因此 `receiver/wire.py` 用标准库 socket 实现 MQTT 3.1.1 所需的最小报文子集，
PUBREC / PUBCOMP 都在对应 SQLite 事务提交之后才写出，确认时机完全受控。

## PUBLISH / PUBREC / PUBREL / PUBCOMP 的持久状态

`deliveries.state` 状态机（每行 = 一个会话代内一个包号的一次握手）：

| 协议事件 | 持久动作（先） | 网络动作（后） | 状态转移 |
| --- | --- | --- | --- |
| 收到 PUBLISH | 插入/核对待交付行，保存原始负载与 sha256 | 发 PUBREC | `-> received` |
| 重复 PUBLISH（同包号同负载） | 不新增行、不改负载，仅记事件 | 重发 PUBREC | 保持原状态 |
| 重复 PUBLISH（同包号**不同**负载） | 拒绝覆盖，保留原负载，记冲突事件 | 仍按原交付回 PUBREC | 保持原状态 |
| 收到首个 PUBREL | 一个事务内：改状态 + 插入**唯一**业务记录 | 进入 PUBCOMP 两阶段 | `received -> committed` |
| 重复 PUBREL | 不产生第二条业务记录 | 补发 PUBCOMP | 不变 |
| PUBCOMP 发送前 | 落 `completing` 标记（崩溃恢复点） | —— | `committed -> completing` |
| PUBCOMP 写出后 | 标记完成、释放包号 | PUBCOMP 已写 socket | `completing -> completed` |
| broker 丢失会话（CONNACK SP=false） | 旧代未完成行置 `abandoned`，已完成业务行保留 | 新订阅 | 旧代 `-> abandoned`，新建会话代 |

两阶段 PUBCOMP 是关键：`completing` 在写 socket **之前**提交。若进程在这两步之间
崩溃，重连后只重发 PUBCOMP，不会重处理交付、也不会再插业务记录。

### 为什么不用包号或负载摘要做永久唯一约束

包号在一次交付结束后会复用，相同负载也可能是两次独立交付，所以：

- **没有** `UNIQUE(packet_id)`，也**没有** `UNIQUE(payload_sha256)`；
- 只有一个**部分唯一索引**，约束“同一代内处于未完成状态的包号唯一”，
  这正是 MQTT “每会话每个包号同时只有一个在途交付”的规则；
- 完成后的行永久保留，复用包号会插入新行（新 `delivery_id`），即使负载字节相同。

## 会话代（generation）

- 首次 CONNACK `Session Present=false` → `begin_generation` 建立新代；
- 之后 `Session Present=true` → `resume_generation` 沿用同代与未完成握手；
- broker 丢会话（再次 false）→ 旧代所有未完成行显式置 `abandoned` 并结束旧会话，
  旧代**已完成的业务记录绝不删除**；遗留 PUBREL 在新代查不到在途交付，记为
  `orphan_pubrel_rejected`，不会被套用到复用同包号的新消息上。

## 命令

```bash
# 接收（自动重连；--crash-before-pubcomp 为故障注入；--once 单连接后退出）
python3 -m receiver receive --db out/receiver.db --port 11883
# 查看持久协议状态、业务记录与事件（含 --json）
python3 -m receiver inspect --db out/receiver.db
# 导出已完成业务记录（jsonl / csv）
python3 -m receiver export  --db out/receiver.db --format jsonl
```

固定 Client ID `persistent-qos2-receiver-01`、Clean Session=false、订阅
`telemetry/line` QoS2，均可通过参数覆盖。

## 验证

```bash
python3 -m unittest tests.test_store -v      # 状态机单元测试（含崩溃恢复、代际重置）
python3 run_demo.py                          # 三种端到端模式，全部交叉核对
```

`run_demo.py` 启动真实脚本 broker（`tests/scenario_broker.py`，与
`fixtures/mock_broker.py` 报文逻辑一致，额外支持 `--only/--skip` 以便一个进程
处理一次连接），并只依据 **broker 包日志** 与 **SQLite 内容** 核对：

- `recovery`：两个接收器**进程**共用一个数据库；第一个进程用
  `--crash-before-pubcomp` 在 PUBCOMP 前硬退出，第二个进程靠数据库恢复并补发；
- `inprocess`：同一进程内自动重连完成两次连接；
- `reset`：`Session Present=false` 重建会话代，旧业务记录保留、新交付正常入仓。

核对项包括：两次交付各一条业务记录、重复 PUBLISH/PUBREL 不增行、包号 7 复用后
可再增行、PUBLISH 落库早于 PUBREC、业务记录只在 PUBREL 时产生，以及 broker 日志中
PUBREC/PUBCOMP/PUBLISH/PUBREL 的真实包数量。

## 数据库表

- `sessions`：会话代；
- `deliveries`：握手状态机与原始负载（received/committed/completing/completed/abandoned）；
- `business_records`：每次完成交付恰好一条，`delivery_id` 唯一，天然不重复；
- `protocol_events`：带时间戳的真实协议阶段、方向、状态转移与说明，供审计。

WAL + `synchronous=FULL`，每个协议步骤一个短事务，确认报文一律在事务提交后发出。
