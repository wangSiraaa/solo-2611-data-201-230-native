# Zarr v2 离线分块补丁工具（plan / apply）

在不改动源目录的前提下，把一批**有顺序的**小修订（矩形赋值 + 点赋值）应用到
二维 Zarr v2 数组上，产出一个布局完全相同的新 Zarr 目录。

核心保证：

- **plan 与 apply 分离**：`plan` 只做校验和索引编译，不读任何 chunk 数据；
  同一个 chunk 被多个操作反复命中时，编译成一份局部更新序列 + 最终坐标覆盖，
  而不是按操作反复做数组 assignment、反复读同一 chunk。
- **每触块最多解码一次、编码一次**：apply 以 chunk 为单位处理落到该块的全部
  操作；缺块直接用 fill 值构建（0 次解码），边缘 chunk 按完整 chunk 形状解码、
  只把 shape 内区域当数据，shape 外填充永不暴露也永不参与更新。
- **操作优先级不变**：最终覆盖由有序 dict 赋值构建（后写胜），chunk 的处理
  顺序只影响报告顺序，不影响结果。
- **全有或全无的预检**：任何非法操作（越界、负值、非 int32、bool 当整数、
  values shape 不符等）在写出第一个修改 chunk 之前拒绝整批；输出路径已存在也
  拒绝覆盖。写入先落 `.staging` 临时目录，成功后整体 `rename`。
- **未触块零重编码**：未受影响的现有 chunk 原字节复制（与解码计数分开报告）；
  不存在且未受影响的 chunk 继续不存在。
- 实现本身从不把整个数组读入再整体写回；整数组读取只出现在独立 oracle 校验里。

## 目录结构

```
zarr_patch/
  errors.py        # PatchValidationError
  source.py        # .zarray 读取/校验、chunk 网格、边缘合法区域
  model.py         # ChunkPlan / PatchPlan 及 JSON 序列化
  planning.py      # 全部输入校验 + 编译为按 chunk 的局部更新
  apply.py         # 分块 apply、codec 计数包装、临时目录原子发布、报告
  instrument.py    # 在真实 numcodecs codec 类入口上独立计数（核查用）
  verification.py  # NumPy oracle、zarr 重开校验、字节复制校验
  cli.py           # 命令行入口
scripts/run_demo.py  # 端到端验证脚本（成功路径 + 17 类非法场景）
tests/test_zarr_patch.py  # unittest 套件（10 个测试）
fixtures/          # 任务给定输入（不在工具内修改）
```

## 环境准备

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` 钉的是 `numpy==2.1.3 / zarr==2.18.7 / numcodecs==0.15.1`。

> 本机为 aarch64 Debian 12，且没有 root、系统缺 `Python.h`，PyPI 上
> numcodecs 0.15.1 没有 aarch64 wheel。本仓库实际采用的可复现安装方式是：
> 从 Debian 镜像下载 `libpython3.11-dev` deb 解包到用户目录拿到头文件，再
> `CFLAGS="-I.../include/python3.11 -I.../include/aarch64-linux-gnu/python3.11
> -I.../include" pip wheel numcodecs==0.15.1` 本地构建 wheel 安装；numpy 与
> zarr 均使用钉版官方 wheel。x86_64 / 有官方 wheel 的环境直接
> `pip install -r requirements.txt` 即可，无需此步骤。

## 使用方式

只编译计划（不产生数据输出）：

```bash
python -m zarr_patch plan fixtures/source.zarr fixtures/patches.json out/plan.json
```

编译并应用（计划、报告可选落盘）：

```bash
python -m zarr_patch apply fixtures/source.zarr fixtures/patches.json \
    out/result.zarr --plan-out out/plan.json --report-out out/apply-report.json
# run 是 apply 的别名
python -m zarr_patch run  fixtures/source.zarr fixtures/patches.json out/result.zarr
```

作为库调用：

```python
from zarr_patch import compile_plan, apply_plan

plan = compile_plan("fixtures/source.zarr", patches)   # 非法输入抛 PatchValidationError
report = apply_plan(plan, "out/result.zarr")
print(report.to_dict())
```

## 操作格式

操作按数组顺序执行，仅支持非负索引、步长 1。

```jsonc
// 矩形：rows/cols 为左闭右开 [start, stop)；空矩形是合法 no-op
{"op": "rect", "rows": [1, 5], "cols": [2, 7], "value": 20}
// 或 values 为与矩形完全同 shape 的二维整数列表
{"op": "rect", "rows": [2, 4], "cols": [3, 5],
 "values": [[61, 62], [63, 64]]}
// 点：coords/values 等长；坐标可重复，同一操作内后者覆盖前者
{"op": "points", "coords": [[1, 2], [1, 2]], "values": [30, 31]}
```

校验规则（任一不过即整批拒绝）：索引必须是非负 `int`（`bool` 明确不算整数）、
不超过 shape；rect 起止必须合法；标量与二维 values 二选一且 shape 精确匹配；
points 的 coords/values 等长；所有值必须落在 int32 闭区间；未知 op / 未知字段
拒绝。

## 运行全部验证

```bash
python scripts/run_demo.py            # 端到端：oracle + zarr 重开 + codec 入口计数 + 非法场景
python -m unittest discover -s tests  # 单元/集成测试
```

端到端脚本会校验：

1. 独立 NumPy 数组按原顺序逐项更新得到的 oracle 与输出**逐元素相等**；
2. 用 zarr 库重新打开输出，核对 shape/chunks/dtype/fill/order/codec/filters、
   缺块读回为 fill、边缘块 shape 内区域与 oracle 一致；
3. 未受影响现有 chunk 与源**逐字节相同**（测试套件中的独立场景）；
4. 通过对真实 numcodecs codec 类 `decode/encode` 入口打补丁独立计数，确认每个
   触块解码/编码恰好一次（缺块 0 解码、1 编码）；
5. 17 类非法输入全部被拒且无任何输出/临时目录残留，源目录字节不变。

## 范围说明

只承诺正常运行与输入预检失败下的完整性：不做掉电事务、不支持并发更新、不涉及
对象存储。
