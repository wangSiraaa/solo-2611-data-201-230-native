# 材料说明

启动：python3 fixtures/mock_broker.py --scenario standard --port 11883 --log broker-log.jsonl。服务在监听后打印端口，处理恰好两次客户端连接后退出。客户端应固定Client ID、MQTT3.1.1、Clean Session=false；用户名/口令无需提供。第一连接CONNACK Session Present=false，须SUBSCRIBE telemetry/line QoS2；第二连接true，既有订阅保留，也允许再次订阅。提供CONNECT、SUBSCRIBE、PINGREQ及场景需要的QoS2报文，不是通用broker。第一连接PUBLISH/PUBREC/PUBREL后断开，下一连接重发PUBREL；再复用包号7发新负载，重复其PUBLISH。另起--scenario reset会以false建立新会话。请用真实收到/确认日志核对，不通过读取mock内部变量判断交付。若测试进程退出恢复，请让第二连接由新进程完成。
