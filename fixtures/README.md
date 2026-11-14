# 材料说明

capture.json是唯一到达顺序，exporter是接收端附加身份，不在IPFIX报文本体里。messages/为IPFIX v10真实二进制；scope和IE映射限定子集。Template Set=2，Data Set>=256。企业号32473仅用于本地例子，字段1为UTF-8标签；样本含127字节短前缀与255字节扩展前缀。bad-truncated.bin是Message尾截断，bad-variable.bin含完整模板与越界变长字段。正式capture不包含这些坏报文，单独运行坏例。IPFIX Sequence Number按有效Data Record计数；本题只展示它，不据此推断丢包或模板更新顺序。
