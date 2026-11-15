# 离线 IPFIX v10 入仓器 — 使用说明

纯 Python 3.11 标准库实现，无第三方依赖；只读本地文件，不接真实网络、不做 TCP 重组。

## 目录结构

```
ipfix_ingestor/
  parser.py     # 协议解析核心：消息/Set/模板生命周期/字段解码
  cli.py        # ingest / inspect 两个子命令
  __main__.py   # python -m ipfix_ingestor 入口
verify_independent.py  # 独立验证（自带 struct 手工核对 + 子进程黑盒断言）
fixtures/                # 题目输入：capture.json、messages/、IE 注册表、坏报文
output/                  # ingest 产物（JSONL，每行一条）
```

## 1. 入仓：ingest

按 `capture.json` 的到达顺序逐条读取二进制报文，输出 5 个 JSONL 文件。

```bash
python3 -m ipfix_ingestor ingest \
  --capture fixtures/capture.json \
  --ie fixtures/information-elements.json \
  --outdir output
```

| 产物 | 内容 |
| --- | --- |
| `flow_records.jsonl` | 规范流量记录，每行含 exporter、domain、templateId/templateVersion、messageIndex、setOffset、recordOffset 与逐字段精确值 |
| `template_history.jsonl` | 模板定义/撤回/重定义的完整历史（含字段指示符、企业号、最短记录长度） |
| `unexplained_sets.jsonl` | 无法解释的 Set：unknown_template 或各类边界错误，保留 reason 与 rawHex |
| `message_errors.jsonl` | 报文级失败（截断、版本/长度不符）；正式捕获该文件为空（0 行） |
| `messages.jsonl` | 每报文摘要（domain、序列号、长度、各类计数） |

正式捕获退出码 0；存在报文级错误时退出码 1（坏报文需单独运行，见下）。

## 2. 检视：inspect

人类可读的逐条检视，不写文件。

```bash
# 整条捕获（各自维护模板状态，按 capture 顺序推进）
python3 -m ipfix_ingestor inspect --capture fixtures/capture.json

# 单个报文（exporter 由接收端身份手工指定，不在报文本体内）
python3 -m ipfix_ingestor inspect --file fixtures/bad-variable.bin --exporter a
```

## 关键语义

- **模板身份** = `(exporter, observationDomainId, templateId)` 三元组，跨域、跨 exporter
  的同号模板互不可见。模板按到达顺序生效，重定义产生新版本且只影响后续 Set；
  已产出的记录永久保留当时的 `templateVersion` 与原始字节偏移，绝不倒灌重解释。
- **撤回**：Template Set 中 `fieldCount=0` 撤回该身份的单个模板（id≥256）；撤回后到达
  的 Data Set 记 `unknown_template`，rawHex 保留原始 Set。
- **企业位**：指示符高位为 1 时多读 4 字节 Enterprise Number，按 `(企业号, IE id)` 查
  enterprise 注册表；企业 IE 1 与标准 IE 1 是两个字段，注册表外字段直接判错不猜义。
- **变长字段**：长度前缀 1 字节（0–254）或 `0xff` + 2 字节扩展长度；声明长度超出 Set
  边界立即隔离整个 Set（丢弃该 Set 的半截记录），不会越界读到下一 Set。
- **尾部规则**：仅当剩余字节短于模板最短合法记录时才允许 0–3 个**零**字节 padding；
  非零尾、超长尾一律报错。恰好等于一条记录长度的零字节按值为 0 的完整记录处理
  （scope 允许零数据记录），不会被误当 padding 或空记录。
- **数值精度**：uint（1/2/4/8 字节，网络序无符号）全程用 Python `int`，JSON 序列化为
  整数；样例 `9007199254741001 > 2^53`，任何 float 路径都会损失精度，本实现不经过 float。
- **报文截断**：Message Length 与实际字节数严格相等校验，Set 链不得超出报文；不满足时
  整个报文失败，已读到的一半不产生任何记录，模板注册表保持失败前状态。

## 范围外（按 scope 明确不做）

Options Template（Set id=3，遇到即隔离并报 `unsupported_set_id`）、完整 IE 注册表、
实时流量分析、TCP 重组、基于 Sequence Number 的丢包推断。
