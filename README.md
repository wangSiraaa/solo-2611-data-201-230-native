# 已分词长文的定长训练块与监督掩码生产

本目录只包含任务输入和复现实验材料，不含待实现的工具。

语料已完成分词，现在要生产可回查的定长训练块；不能让前一篇文章末尾预测下一篇文章开头，也不能让长文跨块时重复计算监督目标。请用Python 3.11实现pack和audit，读取documents.jsonl与config，输出Parquet数据块和来源报告。输入doc_id唯一，tokens只含3到max_token_id的整数，bool非法；BOS=1、EOS=2、PAD=0为保留符号。每篇逻辑序列是BOS、原tokens、EOS，空文也有BOS→EOS这一条监督边。
严格按输入文档顺序贪心装块，块长L至少3。完整短文可接在前文之后，但分属不同segment；新文开始时若本块只剩一个位置，就用PAD补齐并在下一块开始，不能把孤立BOS塞入末位。填满当前块且该文尚未结束时，下一块首位重复上一块该文的最后一个token作为上下文，再继续未放入的token；重复上下文占位置但不是新的监督目标。末块用PAD补足，不做最优装箱或训练集划分。
每块输出input_ids、loss_mask、segment_ids及逐token来源doc_id和逻辑位置。loss_mask表示当前位置的token是否作为由前一位置预测的目标：BOS、PAD、块首及重复上下文为0；同文其余目标为1，包括EOS。不同文档不能通过拼接产生监督边。segment_ids用来生成同块、同segment且key位置不晚于query的注意力许可关系；PAD不参与注意力，不必存L×L矩阵。
audit读取实际产物和原文档，独立枚举每篇逻辑序列的相邻监督边，核对每条边恰好出现一次、没有跨文边、没有把上下文重复算作目标。token值相同不能当成相同来源，必须按doc_id及逻辑位置判断；保留BOS/EOS的来源位置，PAD来源为NULL。
用现成空文、短文、恰好成块、长文多次跨块和重复token运行，逐项查看块和掩码；改变L后原始监督边集合仍相同，增加非法保留token和重复doc_id应拒绝。说明由这些字段如何构造训练框架常见的shifted labels，但不调用模型、不训练、不实现tokenizer。数据规模保持小样例到万token级，交付可独立读取的Parquet、审计报告、源码和命令。

输入清单：
- `fixtures/documents.jsonl`
- `fixtures/invalid.jsonl`
- `fixtures/config.json`

运行环境为 Python 3.11。
