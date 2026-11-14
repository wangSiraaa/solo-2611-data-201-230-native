# 材料说明

catalog.html和catalog-variant.html的目标table=catalog；仅nested表文字不同、属性顺序不同。scope.json精确定义支持的逻辑网格、显式headers、隐式scope及文本/类型规则，优先按该受限规范实现，不追求完整HTML算法。三个bad文件分别产生表头环、跨行组rowspan、跨表ID引用。表头ID是语义身份，长表位置用行组ID、组内行号、列号表示；编号均从0开始。rowspan/colspan保留共享来源关系。需要HTML库可自行在venv安装lxml或beautifulsoup4。
