# 定长训练块打包（pack）与可回查审计（audit）

把已分词语料 `documents.jsonl` 按**严格输入顺序贪心**切成定长训练块，每个 token
都带可回查来源；并提供独立审计，核对监督边集合与原文档逐边一致。

- 不调用模型、不训练、不实现 tokenizer。
- 仅依赖 `pyarrow`（见 `requirements.txt`），Python 3.11。

## 1. 概念与规则

保留符号：`PAD=0`、`BOS=1`、`EOS=2`；合法内容 token 为整数 `[3, max_token_id]`，
`bool` 不是整数，保留符号不得出现在输入 `tokens` 中。

每篇文档 `d` 的**逻辑序列**（`n` 为原 token 数）：

```
logical = [BOS] + tokens + [EOS]      # 逻辑位置 lp = 0..n+1
```

监督边为相邻对 `(lp-1 -> lp)`，`lp=1..n+1`。空文也保留唯一一条 `BOS -> EOS`。

装块规则（块长 `L >= 3`）：

1. 严格按文档顺序贪心；不做最优装箱、不做训练/验证划分。
2. 完整短文可接在同块前文之后，但属于**新 segment**，拼接处不产生监督边。
3. 新文开始时若块内只剩 1 个位置：用 `PAD` 补齐本块，新文从下一块开始——
   孤立 `BOS` 不允许进末位。
4. 一块填满而该文未结束：下一块**首位重复上一块该文最后一个 token** 作为上下文，
   再放未放入的 token。重复上下文占位置，但 `loss_mask=0`、来源指向同一逻辑位置，
   **不是新的监督目标**。
5. 末块用 `PAD` 补足到 `L`。

## 2. Parquet 字段（每行一个块，数组长度均为 L）

| 字段 | 类型 | 含义 |
|---|---|---|
| `block_id` | int64 | 块序号，从 0 连续 |
| `input_ids` | list[int64] | 块内 token，含 BOS/EOS/PAD 与重复上下文 |
| `loss_mask` | list[bool] | 该位置 token 是否作为“由前一位置预测”的目标 |
| `segment_ids` | list[int32] | 块内 segment 编号，每块从 0 起，同文连续同号 |
| `source_doc_id` | list[string/int64] | 逐 token 来源文档；PAD 为 NULL |
| `source_logical_position` | list[int32] | 逐 token 在该文逻辑序列中的位置；PAD 为 NULL |

`loss_mask=0` 的位置：所有 `BOS`、所有 `PAD`、每个块首、每个重复上下文。
同文其余目标（含 `EOS`）为 1。**判断来源一律用 `(doc_id, lp)`，token 值相同
不等于来源相同**（如四个值都为 10 的 token 分属 lp=1..4）。

### 注意力许可（不存 L×L 矩阵）

由 `segment_ids` + `source_doc_id` 现算：query 位置 `q` 可注意 key 位置 `k` 当且仅当

- 两者都不是 PAD；
- 同块、同 segment（即同文档段）；
- 因果方向 `k <= q`。

实现见 `core.allowed_attention`。

## 3. 如何构造训练框架常见的 shifted labels

见 `core.build_shifted_labels`，两种约定：

- **HuggingFace 风格**：`labels` 与 `input_ids` 等长，非监督位置填
  `ignore_index=-100`；框架内部对 `logits[..., :-1, :]` 与 `labels[..., 1:]`
  计算交叉熵，`-100` 位置自动忽略。
- **显式 shift 风格（Megatron 等）**：`target = input_ids[1:]`、
  `target_mask = loss_mask[1:]`，由 `input_ids[:-1]` 的隐状态预测，
  `target_mask=0` 不计损失。

因为跨文档边界两侧（前文 EOS 之后是新文 BOS，BOS 的 mask=0）与重复上下文位置
（mask=0）天然被屏蔽，所以既不会出现“前一篇末尾预测下一篇开头”的跨文边，也不会
把上下文重复算成目标。

## 4. 命令

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export PYTHONPATH=src

# pack
.venv/bin/python -m packaudit.pack \
  --documents fixtures/documents.jsonl \
  --config fixtures/config.json \
  --out-dir out/L8

# audit（PASS 退出 0，FAIL 退出 1，输入错误退出 2）
.venv/bin/python -m packaudit.audit \
  --blocks out/L8/blocks.parquet \
  --documents fixtures/documents.jsonl \
  --config fixtures/config.json \
  --report out/L8/audit_report.json

# 一键复现 L=4/8/16 + 非法输入拒绝
bash run.sh

# 端到端演示与回归（逐块视图、变 L 不变性、损坏识别、shifted labels）
PYTHONPATH=src .venv/bin/python tests/demo.py
```

## 5. audit 独立核对内容

audit **不读取 pack 的内存状态，只读 Parquet 产物与原始文档**，独立重建每篇逻辑
序列与全部监督边，然后：

1. 逐位置核对 token 值与 `(doc_id, lp)` 来源一致、长度均为 L、PAD 仅在块尾且
   来源 NULL；
2. 枚举产物中所有 `loss_mask=1` 的边，检查前驱同文、逻辑位置相邻；
3. 与原文档独立枚举的边集合做集合比对：**每条边恰好出现一次**，无缺失、无多余、
   无跨文边、无上下文重复被误算；
4. 检查跨块块首只能是“上一块末位的重复上下文”或“新文 BOS（末位属另一篇）”；
5. 检查 segment 编号每块从 0 连续、孤立 BOS 不入末位、EOS 必为目标。

报告 `audit_report.json` 含 `status / counts / edge_set_invariant /
violations / blocks`（逐位置 kind 标注）。

## 6. 目录

```
src/packaudit/core.py     # 校验、pack、Parquet、audit、shifted labels、注意力
src/packaudit/pack.py     # pack CLI
src/packaudit/audit.py    # audit CLI
tests/demo.py             # 端到端演示与回归
run.sh                    # 一键复现
fixtures/                 # 输入：documents.jsonl / invalid.jsonl / config.json
out/L{4,8,16}/            # 产物：blocks.parquet / pack_manifest.json / audit_report.json
```

## 7. 已验证场景

- 空文（BOS→EOS 一条边）、短文拼接、恰好成块、长文多次跨块、重复 token 值；
- L=3/4/5/7/8/16 下块布局不同但监督边集合恒为原始 36 条；
- 非法保留 token（0/1/2）、超界、bool、重复 doc_id、L<3、保留符号冲突均拒绝；
- 8 类产物损坏（块首/重复上下文误置目标、跨文边、来源污染、删边、重复边等）
  audit 全部判 FAIL；
- 1.2 万 token、605 篇随机语料端到端 PASS。
