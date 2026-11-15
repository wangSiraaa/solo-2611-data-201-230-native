# IMAP邮箱的稳定身份采集与序号变动核对

这是题目输入材料与受限模拟环境，不包含采集器/转换器实现。所有内容为本地合成数据。协议范围和启动说明见 fixtures/README.md 及 scope.json。

## 采集器实现（本仓库新增）

- `imap_mirror.py` — 只读镜像采集器（Python 3.11，仅用 imaplib）。持久身份为
  （账户、邮箱、UIDVALIDITY、UID)；消息序号只在当前连接内临时解释，绝不入档。
  协议面仅 LOGIN、EXAMINE、UID SEARCH、UID FETCH、NOOP、LOGOUT。
  - `python3 imap_mirror.py sync --save-dir run/mirror` 执行一次同步：
    两次 UID SEARCH 之间按明确 UID 抓取 BODY.PEEK[]，采后复核当前 UID 集合；
    全部确认才发布该代快照，否则保留旧快照、记录错误、暂存已下载原件。
  - `python3 imap_mirror.py status --save-dir run/mirror [--json]` 查看域身份、
    当前 UID、flags 变化、消失记录与未确认项。
  - `python3 imap_mirror.py export --save-dir run/mirror --out run/export/gen-900
    [--generation 900]` 导出某代已发布快照（manifest + 逐字节原件）。
- `run_scenarios.py` — 依次跑 initial / shift / reset / truncated 四个阶段并核对。
- `verify_mirror.py` — 独立核对：只信 fixtures 原始 EML 字节与 mock 服务实际
  发送日志（jsonl），不信采集器自报。

一键复现：`python3 run_scenarios.py`（自动起停各阶段 mock，端口 11430）。
