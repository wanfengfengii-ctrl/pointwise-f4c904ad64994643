# QuoteTape

这是完整快照行情的采集与报价小工程，只依赖Python 3.11标准库。
现有FullCapture接收完整订单簿并保留原始输入，BookStore和QuoteReader可独立重开读取。
事件不是撮合指令，产品只做采集、标准化留存和行情可用性查询。
本轮扩展接口及语义见CONTRACT.md；目标模块recovery不在初始实现中。
运行 `python3 -B -m unittest discover -s tests -v` 核对原业务；`python3 -B run_demo.py` 展示已确认行情。
.pointwise-runtime.json采用version=1、kind=cli，setup为argv数组列表，verify/demo为真实命令argv。
