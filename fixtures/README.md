# 材料说明

sequence.json明确接收顺序。messages内文件均UTF-8、CR分段，没有MLLP封装。MSH字段编号按HL7规则：MSH1为字段分隔符，MSH2为四个编码字符；不能直接把split数组索引当全部字段编号。04-alternate使用!字段、:组件、%重复、?转义、@子组件。scope定义身份和状态；P不替换当前最终结果，C/D无目标保留未应用历史。duplicate与01逐字节相同；conflict沿用M1但值不同，必须报告冲突。数据均合成，不含真实患者资料。
