# 材料说明

启动：python3 fixtures/mock_modbus.py --mode normal --port 11502 --log requests.jsonl。只有Unit ID=1/function03。points地址是0起点，forbidden_ranges为左闭右开[6,8)，不得跨越；max_registers=4。宽度u16/i16为1、u32/i32/float32为2。byte_order作用于每个16位寄存器两字节，word_order作用于32位的两个寄存器，先按word_order排列寄存器，再按byte_order排列每寄存器字节形成解码字节串。版本寄存器100只单独读取。--mode change-once在第一次数据读取发送的内容确定后修改寄存器与版本；changing每次数据读都变化；exception在覆盖8时返回02；wrong-tid首次数据响应改transaction id；nonfinite提供NaN。可重复启动每个场景得到同样初态。所有观测来自本地模拟器，无设备控制命令。
