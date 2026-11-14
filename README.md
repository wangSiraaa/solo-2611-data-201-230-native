# 固定页数据文件的安全缓冲池

本目录只包含任务输入和复现实验材料，不含待实现的工具。

一个本地数据文件访问层需要缓存固定页，调用者借出页期间不能被替换，脏页写回失败也不能丢掉内存修改。请用Python 3.11实现BufferPool及运行fixtures/scenarios.json的演示入口。page_io.py只提供128字节页的磁盘读写和失败注入；输入pages.bin有八页。核心缓存和句柄管理由你完成，不做B+树、SQL、WAL或崩溃恢复。
BufferPool容量以页帧数给定。fetch(page_id)返回独立handle，同一页再次fetch应命中同一驻留帧并增加pin数。read/write只接受仍有效的handle和页内范围，write后自动标记dirty；release只释放该handle一次，后续对它的读写或重复release都报错。不能把可被调用者长期保存的裸可变bytearray当handle返回，从而绕过pin和失效检查。
只允许驱逐pin=0的帧，以最近一次成功fetch的全局序号选择最久未使用者；命中也更新序号，read/write不更新。全部帧被pin时fetch立即返回明确的all_pinned，不偷偷增容或替换正在借用的页。不同handle指向同一页时应立即看到彼此已完成的修改。
驱逐dirty帧必须先成功写回再改变它的页身份；注入写失败时保留原驻留页、dirty标记和全部数据，fetch新页失败，之后仍能重新fetch旧页并重试。成功替换后任何已失效旧handle也不能误写新页。flush_all仅刷pin=0的dirty页，报告仍被pin的跳过页；写回成功才清dirty。关闭时存在pin则拒绝，全部释放后刷完脏页再关闭。
在每个现成场景开始前复制输入文件，记录页读写、缓存命中、驱逐、pin和错误结果。独立核对修改字节最终落在正确页，重复fetch不重复读盘，未修改页不写盘，失败写回后数据仍在且重试成功。补充页号越界、页内越界、重复release和关闭有pin的案例。单线程串行操作即可，不引入并发锁、后台刷盘或文件掉电持久化承诺；交付源码、API说明和逐步可运行示例。

输入清单：
- `fixtures/pages.bin`
- `fixtures/scenarios.json`
- `page_io.py`

运行环境为 Python 3.11。
