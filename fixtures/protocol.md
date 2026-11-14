# 模拟服务范围
启动：python3 mock_es.py --port 0 --scenario fixtures/scenario.json --ready /tmp/es-ready.json。ready中返回base_url；进程持续到终止。
POST /_bulk接收NDJSON，两行为一项：{"index":{"_index":"orders","_id":"x","version":2,"version_type":"external"}}，随后_source对象。_source包括sku、amount_cents、ingest_fingerprint。index本身相同也需保留项级ID。external仅当新version严格大于现值才接受；相同version一律409。
POST /_mget接收{"docs":[{"_index":"orders","_id":"x"}]}，响应docs原序返回，存在项有found=true、_version、_source，不存在found=false。GET /__inspect只读返回docs、attempts、实际成功写入历史writes以及drop_done；用于验证，不用于sink做正常决策。
429是确定未写；400是确定映射失败；连接被关闭时，已处理前缀可能持久在服务端内存，余项尚未处理。服务端不自动重试。单次进程数据保持，无需重启恢复。
