# 固定页数据文件的安全缓冲池

一个本地数据文件访问层需要缓存固定页，调用者借出页期间不能被替换，脏页写回失败也不能丢掉内存修改。本实现使用 Python 3.11，`page_io.py` 仍是唯一的磁盘读写后端（128 字节页、8 页输入、可注入写失败）。

## 交付内容

| 文件 | 说明 |
| --- | --- |
| `buffer_pool.py` | `BufferPool`、不透明 `PageHandle`、错误类型、`FlushReport`/`CloseReport` |
| `demo.py` | 运行 `fixtures/scenarios.json` 及内置边界场景，独立核对结果 |
| `example.py` | 逐步可运行的讲解示例 |
| `API.md` | API 说明、错误码、行为保证与非目标 |
| `tests/test_buffer_pool.py` | 单元测试（约 35 项） |

## 快速开始

```bash
python3 demo.py                # 3 个现成场景 + 8 个补充场景，全部检查
python3 demo.py --verbose      # 显示每个操作的记录
python3 demo.py --json         # 机器可读输出（摘要走 stderr）
python3 example.py             # 分步讲解（使用临时副本）
python3 -m unittest discover -s tests -v
```

每次运行会把每个场景的操作记录、池事件、`PageIO` 事件、统计和检查结果写到
`out/demo-reports/<scenario>.json`（`--report-dir` 可改）。

每个场景开始前都会把 `fixtures/pages.bin` 复制到独立临时文件；场景结束后演示器**独立读取结果文件**逐页核对字节，并用池事件与 `PageIO` 事件核对：

- 修改过的字节最终落在正确页；
- 同一页未被驱逐时不会重复读盘；
- 未修改的页不会被写盘；
- 写回失败后页仍驻留、仍 dirty、数据完整，之后重试成功；
- 已失效的旧句柄无法写坏新页。

## 核心语义一览

- `fetch(page_id)` 返回独立句柄；同页再次 fetch 命中同一驻留帧并增加 pin。
- 句柄不暴露可变 `bytearray`；release 后、被驱逐后或跨池使用时读写与重复 release 均报 `invalid_handle`。
- 只驱逐 `pin == 0` 的帧，按最近一次成功 fetch 的全局序号做 LRU；命中也更新序号，read/write 不更新。全部帧被 pin 时立即抛 `all_pinned`，不增容、不替换。
- 驱逐 dirty 帧先成功写回再改变页身份；写失败保留原页、dirty 与全部数据，且新页不加载。
- `flush_all()` 只刷 unpinned dirty 页，报告 `skipped_pinned`，写回成功才清 dirty。
- `close()` 有 pin 时拒绝；全部释放后刷完脏页再关闭。写回失败会保持打开以便重试。
- 单线程串行；不含并发锁、后台刷盘、WAL 或崩溃/掉电恢复保证。

详细的 API、错误码与行为说明见 `API.md`。

## 输入清单（原始材料）

- `fixtures/pages.bin`
- `fixtures/scenarios.json`
- `page_io.py`

运行环境为 Python 3.11。

