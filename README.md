# QuoteTape

这是完整快照与增量行情的采集与报价小工程，只依赖Python 3.11标准库。
现有FullCapture接收完整订单簿并保留原始输入，BookStore和QuoteReader可独立重开读取。
quotetape.recovery.Receiver按来源代次和全局包序衔接增量，接收分片快照并在缺包时标记
恢复中，同时保持原始留存、完整簿发布和独立报价读取。事件与读取协议见CONTRACT.md，
交接不变量见HANDOFF.md。
事件不是撮合指令，产品只做采集、标准化留存和行情可用性查询。
运行 `python3 -B -m unittest discover -s tests -v` 核对业务；`python3 -B run_demo.py`
展示已确认行情与恢复过程。
.pointwise-runtime.json采用version=1、kind=cli，setup为argv数组列表，verify/demo为真实命令argv。
