# Zarr稀疏更新的合并块写计划

本目录只包含任务输入和复现实验材料，不含待实现的工具。

离线数据集要在现有二维Zarr数组上应用一批小修订，同一个chunk可能被多个操作反复命中。请用Python 3.11、Zarr v2和numcodecs实现plan与apply。已有真实source.zarr，shape为7×9、chunks为3×4、dtype为小端int32、fill_value=-9、C顺序和Zlib压缩；部分chunk文件不存在，读取应体现fill。输出保持shape、chunk布局、dtype与codec不变，不做换轴或重分块。
patches是有顺序的操作数组。rect给出左闭右开的行列范围，值为一个标量或与矩形完全同shape的二维整数列表；points给出成对坐标与等长values，坐标可重复，同一操作中后出现者覆盖前者，后续操作再覆盖之前操作。只支持非负索引和步长1，空矩形是合法无操作。所有值须在int32范围内，bool不当整数；任一非法操作应在写出首个修改chunk之前拒绝全批。
plan编译每个受影响chunk的局部更新序列及最终坐标覆盖，不按操作逐次调用数组assignment反复读取相同chunk。apply以chunk为单位处理全部相关操作，每个受影响chunk最多解码一次、编码一次；不存在的chunk从fill构建，边缘chunk须按格式解释合法区域，不能把超出shape的填充当数据。重叠更新要保持原操作优先级，不能为排序chunk而改变结果。
产出新的Zarr目录，原目录保持不变。未受影响的现有chunk允许原字节复制，不解码重编码；不存在且未受影响的chunk继续不存在。报告真实的解码/编码chunk键、来源存在性和更新数，复制压缩字节与解码次数分开记录。只承诺正常运行和输入预检失败的完整性，不要求掉电事务、并发更新或对象存储。
用独立NumPy数组按原操作顺序逐项更新作oracle，核对整个结果；再从Zarr库重新打开输出验证元数据、缺块fill与边缘值。现成数据有跨块矩形、重复点、后续覆盖、缺块和空选择。加入越界、值溢出和shape错误，确认不发布修改产物；在正确运行中依据实际codec入口核查每个触块只解码一次。交付源码、计划和运行说明，不把整个数组读入再写回作为实现。

输入清单：
- `fixtures/source.zarr/`
- `fixtures/patches.json`
- `fixtures/invalid-patches.json`

运行环境为 Python 3.11。
