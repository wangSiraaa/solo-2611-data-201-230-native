# 设计说明：plan / apply 如何满足各项约束

## 1. 数据布局与事实来源

源数组 `fixtures/source.zarr` 是 Zarr v2：shape `(7, 9)`、chunks `(3, 4)`、
dtype 小端 int32、fill `-9`、C 顺序、Zlib(level=1)。chunk 网格为
`ceil(7/3) × ceil(9/4) = 3 × 3 = 12` 个键，实际只有 `0.0 / 1.1 / 2.2` 三个
chunk 文件存在。

`source.py` 只读取 `.zarray` 与目录列表：

- 强制校验 `zarr_format=2`、二维、正 shape/chunks、小端 int32、C 顺序、
  fill 在 int32 内、compressor/filters 能被 numcodecs 还原；
- `valid_region(ci, cj)` 给出完整 chunk 缓冲内属于数组的切片，例如边缘块
  `(2,2)` 的合法区域是 `[0:1, 0:1]`，其余位置只是格式要求的填充；
- chunk 键按 v2 默认 `.` 分隔符生成（`"ci.cj"`）。

## 2. plan：校验与编译（planning.py）

### 2.1 全有或全无的校验

所有操作先整体校验、整体编译，期间不打开任何输出路径、不读 chunk 字节。
关键规则：

- 整数判定用 `_is_plain_int`：`bool` 是 `int` 子类但被显式排除，索引和值都
  不接受 `True/False`，浮点字符串等也拒绝；
- 索引非负且 `row <= shape-1` / `stop <= shape`（左闭右开），`start > stop`
  拒绝；
- 值统一过 int32 闭区间检查（含 points 逐值、rect 二维列表逐元素）；
- rect 的 `value`（标量）与 `values`（二维列表）必须二选一；`values` 的
  行列数与矩形严格相等——空矩形配非空 `values` 同样按 shape 错误拒绝；
- points 的 coords 必须是 `[row, col]` 对列表且与 values 等长；
- 未知 op、未知字段一律拒绝。

### 2.2 编译产物

每个 cell 写入通过 `_emit_cell` 映射到所属 chunk 的局部坐标
（`divmod(全局索引, chunk 步长)`），写入 `ChunkPlan`：

- `updates`：保留全部局部写入 `(lr, lc, value, op_idx)` 的**有序序列**，
  用于审计和更新计数（同一坐标被多次命中会出现多条）；
- `final_cells`：`dict[(lr,lc)] = value`，依赖 Python dict “后赋值覆盖”
  语义，天然等价于“同操作内后出现的点覆盖前者、后续操作覆盖之前操作”。

因此 plan 阶段就把 N 个操作折叠成“每个受影响 chunk 一份最终坐标覆盖 + 一份
有序局部更新序列”，apply 不需要按操作反复 assignment，也不会反复读取同一
chunk。计划是纯数据，可 `to_json()` 落盘（见 `out/plan.json`）。

空 rect 与空 points 记录在 `noop_operations`，不产生任何 chunk 计划。

## 3. apply：每触块一次解码、一次编码（apply.py）

流程：

1. 重新加载源 `.zarray` 并核对 plan 的 shape/chunks/dtype/fill/order 一致；
   检查输出路径不存在、父目录可写。
2. 在 `输出名 + ".staging"` 临时目录内逐字节复制 `.zarray`（以及可能存在的
   `.zattrs/.zgroup`），保证元数据与 codec 配置原样保留。
3. 未受影响且存在的 chunk：`shutil.copyfile` **原字节复制**，完全不经过
   numcodecs；计数进 `byte_copied_chunks/byte_copy_count`，与解码次数分列。
4. 受影响 chunk（按键排序，仅为报告确定性）：
   - 源存在 → 通过 `CountingCodec.decode` 读一次压缩字节，`frombuffer` 还原
     成完整 chunk 形状；
   - 源缺失 → `np.full(chunk_shape, fill_value)` 构建，**解码次数为 0**；
   - 遍历该 chunk 的 `final_cells`，先断言局部坐标落在 `valid_region` 内再
     赋值（编译只会产生 shape 内坐标，此为防御性检查）；
   - `encode` 一次写出。包装器断言每块 decode ≤ 1、encode == 1。
5. 不存在且未受影响的 chunk 不创建，报告其键名。
6. 全部成功后 `os.rename(staging, output)` 原子发布；任何异常都清理 staging，
   已存在的输出路径绝不会被半写内容污染。

`CountingCodec` 是引擎内计数；`instrument.py` 另外直接给**具体 numcodecs
codec 类**的 `decode/encode` 方法打补丁，从外部独立统计真实入口调用次数，
demo 中要求“真实 decode 次数 == 受影响且源存在的 chunk 数，真实 encode 次数
== 受影响 chunk 总数”。

## 4. 边缘块与填充

v2 中边缘 chunk 仍按完整 chunk 形状存储。引擎始终在完整 chunk 缓冲上工作，
但：

- 编译期只会为全局合法坐标生成局部坐标，shape 外位置永远不会出现在
  `final_cells`；
- apply 期对每条最终覆盖再做一次 `valid_region` 断言；
- 数组 API 只暴露 shape 内区域，填充位置既不读为数据也不被改写。
  `tests` 中专门构造了填充区带哨兵值的边缘块，证明哨兵不会泄漏到结果里。

## 5. 正确性验证

- **Oracle**（`verification.py`）：用 zarr 读出整数组到一个独立 NumPy 数组，
  严格按操作顺序用切片/fancy indexing 重放（points 逐条赋值以保留后写胜），
  与输出 `zarr[:]` 逐元素比较。整数组读取仅存在于 oracle，不属于实现路径。
- **zarr 重开**：核对全部元数据、缺块读回 fill、边缘块解码后仍是完整 chunk
  形状且合法区域等于 oracle。
- **字节级复制**：对每个“未受影响现有 chunk”比较源/输出压缩字节完全相同。
- **codec 入口计数**：真实 numcodecs 类方法计数 + 引擎计数双重核对。
- **非法输入**：fixture 非法批次 + 16 个额外构造场景（越界、负索引、int32
  双向溢出、values shape/锯齿/非二维、长度不匹配、bool 值/索引、未知 op、
  value/values 同现、start>stop、空矩形 values shape 错、非 zarr 源目录、
  输出路径已存在），全部拒绝且无产物、源目录哈希不变。

## 6. 明确不做的事

掉电事务、并发更新、对象存储语义、换轴/重分块、F 顺序、非 int32 dtype、
过滤器链改写——均不在承诺范围内（filters 配置仅原样复制元数据并校验可解析）。
