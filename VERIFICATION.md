# 验证说明

验证脚本：`verify_independent.py`（Python 3.11，仅标准库）。

```bash
python3 verify_independent.py        # 退出码 0 = 全部通过
```

## 验证策略：两层，且不只做主程序往返

1. **原始字节层（独立核对）**：脚本不 import 解析器，直接用独立的 `struct.unpack_from`
   和字节切片读取 fixtures 二进制，手工算出期望值（报文头、Set 头边界、模板指示符、
   企业号、固定字段整数、变长前缀与标签边界、坏报文的非法长度）。
2. **黑盒输出层**：以子进程方式运行 `python -m ipfix_ingestor ingest` 到临时目录，
   再对产出的 JSONL 做断言；CLI 行为（退出码、隔离原因）同样走子进程验证。
3. **合成报文层**：padding 边界无法用现有 fixtures 覆盖，脚本自行构造最小 IPFIX 报文
   （模板 300，单个 uint32 字段）验证合法/非法尾部。

## 覆盖的核对点（共 78 项断言）

### 协议与边界
- 8 个报文全部：版本=10、Message Length 与实际字节**严格相等**、逐 Set 的 Set Length
  自洽且 Set 链恰好耗尽报文（无多字节、无欠字节）。
- `bad-truncated.bin`：声明 44 字节实际 42 字节 → 报文级失败，CLI 退出码 1，
  不产出半截记录。
- `bad-variable.bin`：数据 Set 头合法，但扩展变长声明 512 字节、Set 内仅剩 5 字节 →
  该 Set 以 `variable_length_overrun` 隔离，同报文前面的模板定义仍然生效。

### 两个域的同号模板互不污染
- msg00（domain 1）模板 256 为 4 字段、含企业变长标签；msg01（domain 2）模板 256 为
  3 个全固定字段。独立读取确认 fieldCount=4 vs 3。
- msg03（domain 2）的数据按本域 3 字段模板解析为 port=443 / 192.0.2.3 / packets=6；
  输出断言其字段集合不含 domain 1 的标签字段。
- msg04 来自 **exporter b**、同 domain 1、同号 256：注册表中不存在该身份的模板 →
  `unknown_template`，证明不会借用 exporter a 的模板。

### 企业位与 Enterprise Number
- 独立读取 msg00 第 4 个指示符：`0x8001`（企业位置位、IE id=1），随后 4 字节
  Enterprise Number=32473，长度 `0xffff`。
- domain 2 模板首字段独立读到标准 IE 7；输出中企业标签携带 `enterprise=true`、
  `enterpriseNumber=32473`，与同编号标准 IE 1（octetDeltaCount）并存且不混淆。

### 127 / 255 两种变长长度编码
- msg02：偏移 36 处短前缀 `0x7f`=127，随后 127 个 `x`，标签尾恰好等于 Set 尾。
- msg07：`0xff` 扩展前缀 + 2 字节长度 255，随后 255 个 `y`，末尾对齐 Set 边界
  （44+278=322=报文尾），证明不越界读下一 Set。

### 撤回与重定义
- msg05：`fieldCount=0` 撤回 domain 1 的模板 256（历史事件保留被撤回的 version=1）；
  同报文随后到达的 Data Set 记 `unknown_template` 并保留 rawHex。
- msg06：同号模板重定义为 3 字段，版本升到 **v2**；其数据记录挂 `templateVersion=2`。
- 模板历史中 domain 1 模板 256 的 v1 defined、v1 withdrawn、v2 defined 三个事件全部
  在档；msg02 的旧记录仍标 v1，未被重定义倒灌。

### 未知模板与坏 Set
- 恰好 2 个 unexplained Set（exporter b 隔离域、撤回后到达），均带 reason 与 rawHex；
  正式捕获 message_errors 为 0 行。

### 大整数精度
- msg02 的 64 位计数独立 `struct` 读出为 `9007199254741001`（>2^53）；脚本额外断言
  `int(float(x)) != x` 证明 float 路径必然失真，而 JSONL 输出中该值为 JSON integer 且
  逐字节精确相等。

### 记录尾 padding（合成报文）
- 2 字节零 padding 且剩余短于最短记录（4 字节）→ 合法，1 条记录。
- 非零 2 字节尾 → `nonzero_trailing_bytes` 隔离。
- 5 字节非零尾（够一条记录还多 1 字节，无法确定）→ 隔离报错，不猜空记录。
- 4 个零字节（恰好一条完整记录长度）→ 解析为值 0 的第二条记录，不误判为 padding。

## 当前结果

```
78 项断言全部 PASS
ingest: 4 条流量记录 / 5 个模板历史事件 / 2 个无法解释 Set / 0 个报文错误
```
