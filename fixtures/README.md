# 材料说明

准备venv后pip install -r requirements.txt。启动：python3 fixtures/mock_dns.py --stage base --port 11535 --log dns-log.jsonl，只监听127.0.0.1 TCP。区域example.test.。各stage需停止旧服务后重启同端口：base给serial100全量；two含100→101→102连续两段IXFR；wrap-base为4294967295，wrap增至0；fallback对IXFR返回AXFR形态serial103；bad-gap第一差量从99而非100开始；truncated截断最后DNS消息；unchanged对serial100只回SOA。客户端首次AXFR或带SOA authority的IXFR均可。每阶段.zone是权威冻结内容供独立核查，不可作为客户端同步输入。stage定义RR顺序，每TCP消息装最多两个RRset，SOA跨多消息。要求顺序保留解析one_rr_per_rrset，不能把多个同名SOA合并。删增包含TTL变化，TXT由两个字串构成一条RDATA。不涉及公网查询。
