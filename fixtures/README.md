# 材料说明

启动：python3 fixtures/mock_imap.py --stage initial --port 11430 --log initial-log.jsonl。账户与口令均可用demo，仅本地合成认证。以EXAMINE INBOX只读访问。阶段为initial、shift、reset、truncated；停止旧服务后用同端口启动下一阶段，采集器继续使用原保存目录。shift在第一次UID FETCH前发送EXPUNGE并移除UID11，随后UID12/13序号变小。reset将UIDVALIDITY从900变901并重新使用UID11。truncated发送部分literal即断开，后续连接仍截断。所有原件在messages/，stages.json给出服务器数据。无隐藏数据库。mock接受UID FETCH单个/逗号/范围UID，始终返回UID FLAGS BODY[]。
