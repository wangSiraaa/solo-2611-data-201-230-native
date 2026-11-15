# GTFS服务日到跨午夜站点时刻数据集

这里仅提供输入材料和源端模拟，不包含待实现的组件。题面给出输出契约。使用独立临时输出目录运行，不改写 fixtures 中原始材料。

## 交付组件：`gtfsfact`

纯 Python 3.11 标准库实现（无第三方依赖、无外网、无实时 GTFS、无地图），把
`fixtures/feed` 下的 GTFS CSV 按服务日期闭区间展开为 SQLite 事实表
`trip_instances` 与 `stop_calls`。只做时刻事实展开，不做路线规划；
不推断缺失站时，不预测随机班距。

### 运行

```bash
cd /workspace

# 构建（命令行参数优先，也可用 --config 读取 fixtures/build.json）
python3 -m gtfsfact build --feed fixtures/feed \
    --start 2026-01-01 --end 2026-01-03 --out /tmp/out/fact.sqlite
python3 -m gtfsfact build --config fixtures/build.json --out /tmp/out/fact.sqlite

# 自检已生成的数据库（全过退出码 0，否则 1）
python3 -m gtfsfact verify --db /tmp/out/fact.sqlite

# 按实际时刻半开区间 [from, to) 与 stop_id 查询 stop_calls（JSON 输出）
python3 -m gtfsfact query --db /tmp/out/fact.sqlite --stop s1 \
    --from 2026-01-01T23:00:00+08:00 --to 2026-01-02T02:00:00+08:00

# 独立验收断言（29 例：正向样例 + 无效输入拒绝 + CLI 端到端）
python3 tests/acceptance.py        # 或 python3 -m unittest tests.acceptance
```

### 语义约定

- **服务日**：`calendar` 星期标志 ∩ [start_date, end_date]，再应用
  `calendar_dates` 例外（`exception_type=1` 新增、`=2` 删除，例外优先）；
  允许仅由例外定义、无 `calendar` 行的 service_id。
- **秒数**：`HH:MM:SS` 中 HH 允许 00–47；`*_secs` 列始终相对服务日午夜，
  可 ≥ 86400，跨午夜不另建实例。`*_ts` 为 Asia/Shanghai 的 ISO 8601 真实时刻，
  `*_epoch` 为 UTC epoch 秒。
- **实例身份**：`(trip_id, service_date, start_dep_secs)`（实际首站发车秒），
  表上有唯一约束；`stop_calls.instance_id` 外键回连同一实例。
- **频率**：仅支持 `exact_times=1`，按 `[start_time, end_time)` 以
  `headway_secs` 生成实际首站发车，其余各站按相对首站 `departure_time`
  的偏移平移；模板本身不输出。重叠区间、非正 headway、`exact_times=0`
  均以明确原因拒绝。
- **排序**：按数值 `stop_sequence` 排序（不按站点 ID 或 CSV 行序），允许站点
  重复访问；校验同站到达 ≤ 离开、后站到达 ≥ 前站离开。
- **逻辑行号**：各 CSV 内数据行的 1 基序号（表头与空行不计）。
  `trip_instances.trips_row` / `frequencies_row`、`stop_calls.stop_times_row`。
- **查询**：stop_call 占用实际时段 `[arr, dep]`，与查询半开区间相交即返回
  （零停时视为瞬时点 `t`，包含条件 `from <= t < to`）；无时区的查询时刻按
  Asia/Shanghai 解释。
- **拒绝即整体失败**：矛盾例外、未知外键、同 trip 重复 stop_sequence、
  时序倒挂、HH>47 等任何无效输入都使 build 以退出码 2 失败，
  且不留输出/临时文件（原子写入：先写 `*.tmp` 再 `os.replace`）。

### 表结构

```sql
trip_instances(instance_id PK, trip_id, service_id, service_date,
               start_dep_secs, start_dep_ts, start_dep_epoch,
               source CHECK IN ('schedule','frequency'),
               trips_row, frequencies_row,
               UNIQUE(trip_id, service_date, start_dep_secs))
stop_calls(instance_id FK -> trip_instances, stop_sequence, stop_id,
           arr_secs, dep_secs, arr_ts, dep_ts, arr_epoch, dep_epoch,
           stop_times_row, PRIMARY KEY(instance_id, stop_sequence))
meta(key, value)  -- format_version / agency_timezone / 构建区间等
```

### 代码布局

- `gtfsfact/gtfs.py` — CSV 读取与全部校验，产出内存 Feed
- `gtfsfact/expand.py` — 服务日展开与实例/到发生成
- `gtfsfact/store.py` — SQLite 原子写出、区间查询、`verify_db` 自检
- `gtfsfact/cli.py` — `build` / `query` / `verify` 命令行
- `tests/acceptance.py` — 独立断言（普通服务、周末新增、工作日取消、
  仅例外服务、跨午夜、重复站点、两段频率，及各类拒绝场景）
