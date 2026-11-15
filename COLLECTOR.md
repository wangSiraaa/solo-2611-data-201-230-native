# 单区域 AXFR/IXFR 权威快照采集器

面向本地单 zone、单主服务器的受限采集器。首次同步用 AXFR 建立基线，之后优先
IXFR 应用增量；任何不完整的差量都不会替换已发布快照。不实现递归 DNS，也不实现
域名压缩算法（线格式解码全部依赖 dnspython）。

## 范围

- 仅支持给定 zone、单一主服务器（TCP），记录类型限定
  SOA/NS/A/AAAA/CNAME/MX/TXT，class 固定 IN。
- 不做自动发现、多主合并、DNSSEC 或网络管理平台集成。

## 安装与运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 先按 fixtures/README.md 启动本地权威服务，例如：
.venv/bin/python fixtures/mock_dns.py --stage base --port 11535 --log dns-log.jsonl

# 采集（无快照时自动 AXFR，之后自动 IXFR）
.venv/bin/python -m collector.cli --state-dir state sync

# 显式重新 AXFR：差量缺口后用它整库重建，绝不混合两个版本
.venv/bin/python -m collector.cli --state-dir state sync --full

# 查看快照与最近同步状态（含失败原因历史）
.venv/bin/python -m collector.cli --state-dir state status

# 导出：zone 为冻结文件风格文本，json 含每条 RDATA 的来源标注
.venv/bin/python -m collector.cli --state-dir state export --format zone
.venv/bin/python -m collector.cli --state-dir state export --format json
```

`sync` 成功退出码 0（含无变化 `unchanged`），失败退出码 1；失败只写
`status.json`，不动 `snapshot.json`。

## 正确性要点

### AXFR 基线

- 必须以 zone 顶点 SOA 开始、以 SOA 结束，两个围栏 serial 严格相等，
  内部不得再出现顶点 SOA；围栏不符或连接中断都保留旧快照。
- 首尾两个 SOA 身份相同，按 RDATA 身份自然折叠为一条记录。
- 全部记录必须属于该 zone、class 为 IN、类型在允许集合内。

### IXFR 增量（RFC 1995）

- 请求带当前 SOA（authority 中的 serial）。响应分三种：
  - 单 SOA：serial 相同的无变化响应，不重写任何记录内容；
  - 首个 SOA 后紧跟普通记录：服务端回退的完整 AXFR，识别后整库重建，
    不按删增序列解释；
  - SOA/删除集/SOA/增加集：逐段差量，在工作副本上应用。
- 每段旧 serial 必须严格等于本地版本或上一段的新 serial；接不上即判为
  缺口（`DeltaGapError`），保留原因并提示显式 `sync --full`。
- 删除按规范 owner/type/class/RDATA 身份匹配，基线不存在即失败中止，
  不会忽略删除失败后宣布成功；TTL 不含在身份内，TTL 变化表现为删旧加新。
- 全部差量应用完且收尾 SOA、链尾 serial 核对一致后才原子发布新快照。

### 序列号算术

- 按 RFC 1982 的 32 位序列号空间比较，支持 `4294967295 -> 0` 合法前进。
- 环形差值恰好 `2^31` 时无法比较，显式报错而不是猜测方向。

### 记录身份与折叠

- 域名统一为小写绝对名字，比较大小写不敏感。
- RDATA 用未压缩线格式作为身份的一部分；TXT 的多个字串属于同一条 RDATA，
  不会被展开成多条记录。
- 同一 RRset 内重复 RDATA 折叠为一条，保留最早一次写入的 TTL 与来源标注，
  来源（哪次 AXFR/IXFR 写入）随 JSON 导出可追溯。

## 测试

`tests/run_scenarios.py` 会逐个启停 fixtures 中的模拟服务，覆盖：基线 AXFR、
无变化响应、连续两段差量（含 TTL 变化）、serial 回绕、服务端 AXFR 回退、
坏差量缺口与显式重新 AXFR、半传输截断保护；每阶段用 fixtures 下冻结 `.zone`
文件独立比对 RRset 集合与 TTL。

```bash
PYTHONPATH=. .venv/bin/python tests/run_scenarios.py
```
