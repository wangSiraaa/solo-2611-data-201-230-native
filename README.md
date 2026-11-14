# 跨批次有序分类字典的统一编码

本目录只包含任务输入和复现实验材料，不含待实现的工具。

三个采集批次把相同分类列保存为Arrow dictionary，但相同整数码在不同批次可能指向不同标签，ordered字典还表达类别顺序。请用Python 3.11和PyArrow实现unify命令，读取清单指定的真实IPC stream文件，产出统一编码的IPC文件、可直接读取的Parquet及字典映射报告。固定字段row_id、ordered的tier和unordered的region，不做任意schema演进。
每个输入字典的完整标签列表都属于声明，包括当前没有被行引用的类别；不允许NULL标签或同一字典内重复标签。tier的相邻类别声明先后约束，所有文件合起来应构成一个全局有序字典。若约束成环，拒绝该次产物并给出一条闭环及每条边来源文件/位置，不能按文件顺序任意丢掉约束。无环时用标签UTF-8字节序打破拓扑排序的并列，输入清单重排不得改变全局字典。
region没有顺序约束，以全部标签的UTF-8字节序统一。每个文件按实际局部字典将codes映到全局codes，不能把本地整数直接拼接。NULL code仍是NULL，空字符串是合法标签；读取chunk或slice时须尊重offset，不能假定底层缓冲从零对应逻辑首行。输出数据按row_id排序，重复row_id整次失败。
映射报告列出每个输入字段的原标签/原code/新code及tier顺序依据。IPC产物必须真实携带统一dictionary类型与ordered属性；Parquet按解码逻辑值校验，不能假定所有Parquet读取器保留相同物理codes。源文件不改写，失败不留下可误认为成功的版本。
用good.json验证不同本地码、NULL、相同标签的相同身份及未用类别；cycle.json应提供可回查的顺序冲突。独立按标签读取输入与两个产物逐行核对，另对已读取数组做非零offset切片再交给你的转换核心，证明不是只适配整文件。增加空串、未使用类别和重复标签的测试；不做自动同义词合并或类别业务含义推断。交付源码、字典报告与运行说明。

输入清单：
- `fixtures/a.arrow`
- `fixtures/b.arrow`
- `fixtures/c.arrow`
- `fixtures/cycle.arrow`
- `fixtures/good.json`
- `fixtures/cycle.json`

运行环境为 Python 3.11。
