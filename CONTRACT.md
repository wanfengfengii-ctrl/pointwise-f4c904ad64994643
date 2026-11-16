# 行情采集契约

Python 3.11 标准库。现有 quotetape.raw.Tape 留存已接收的 JSON 对象；
quotetape.books.BookStore 发布完整订单簿；quotetape.quote.QuoteReader 是可在另一进程
独立打开的报价读取者。run_demo.py 与原测试展示现有完整快照能力。
当前没有增量同步、缺包追赶或来源换代能力，本轮扩展不能删改原接口或原断言。

## 已有完整快照接口

BookStore(db).publish(symbol, epoch, cut, orders) 发布完整订单簿。
orders 是 {order_id: [side, price, quantity]}，side 为 B 或 S，价格及数量是正整数；
订单号是非空字符串，各证券分别命名。read(symbol) 返回
{symbol, epoch, seq, orders} 或 None，返回值不能成为后续修改数据库的可变引用。
QuoteReader(db).quote(symbol) 返回 None 或
{symbol, epoch, seq, bid, ask}；bid 为最高买价，ask 为最低卖价，某侧为空则该侧 None。
原完整快照调用不新增恢复字段，旧接口与值保持兼容。

## 新增接收端

新增 quotetape.recovery.Receiver(db,symbols)，symbols 是固定、非空、无重复证券名列表。
feed(event) 同步接纳一个已解码字典，close() 释放连接；关闭后以同一 db、symbols 重开。
无并发写者，允许独立报价进程在任一成功 feed 返回后读取。不要求修复任意字节写坏的数据库。
SQLite 的正常事务/崩溃恢复可用。feed 返回的成功接纳必须持久；进程在任意两个 feed
之间退出，重开与继续投递后结果等同未退出。新类不能依赖已关闭接收端的内存。

事件格式：

* {"kind":"epoch","epoch":E}：E 是严格递增的正整数，开启新的全局包序空间。
  相同 E 可重投，较旧 E 的任意消息均忽略；较新数据只在其 epoch 事件接纳后到达。
  开始新代次后所有证券进入恢复中，旧确认簿保留供说明来源，直到本代有合格完整簿。
* {"kind":"delta","epoch":E,"seq":N,"changes":[[symbol,op,id,value],...]}：
  N 是本代从1起的全局整数包序。包可乱序、重复且长期缺失；同身份重复内容必相同。
  op 为 put 或 del；put 的 value 是 [side,price,quantity]，替换该订单全部字段；
  del 的 value 为 null，删除不存在的订单是空操作。列表按顺序执行，整个包的修改一同可见。
  每个包可能只改变部分证券，空 changes 也推进包序。
* {"kind":"snapshot","epoch":E,"symbol":S,"image":I,"cut":K,
   "part":P,"parts":T,"orders":[[id,side,price,quantity],...]}：
  同代同证券的 I 为不复用的字符串，K>=0；该图像准确包含全局包 K 已处理后的完整簿。
  P 从0到T-1，T>=1；分片可乱序、重复并与其他图像、证券、增量交错；同图像元数据一致，
  同分片重复相同，图像内订单号唯一。接收全部分片才是完整图像。
  无须重新收到<=K的增量。多个可用图像按 cut 最大的选择；相同 cut 的图像内容等价。
  比本证券已确认位点旧的图像不得回退。未来可用更高 cut 的图像跨过此前缺口。

输入均满足上述格式及一致性保证；未知证券不会出现。不要求网络客户端、FIX字节解析、
撮合交易或真实行情服务。源可无限持续，未到的包、分片及图像完成没有有限时限。

## 完整性与读取

每个证券在一个完整图像 K 上，只能应用 K+1 起连续接纳的全局包到当前位点，
其中没有本证券修改的包仍可推进它。不能因为某证券没有出现在更高包中就跨缺口。
接收端最近见到本代 seq 的最大值为 H（没有 delta 时为0）；证券已确认位点 F<H
时必须标记 recovering；F>=H 且具有本代完整图像时为 live。
高切点完整快照自身可使 F>H，此时 live；之后见到<=F的包不回放到该簿。
尚无完整图像时为 recovering。部分图像不能覆盖旧簿，也不能改变确认位点。
一包涉及两证券时，不能让读取者见到只应用其中部分的已成功 feed 结果。

对被 Receiver 管理的证券，BookStore.read 和 QuoteReader.quote 都返回
{symbol, epoch, seq, orders/bid/ask, status, source_epoch}：其中 epoch、seq 仍属于
最后确认簿，source_epoch 是当前来源代次；status 为 live 或 recovering。
还没有任何确认簿时 read 返回
{symbol,epoch:null,seq:null,orders:{},status:"recovering",source_epoch:E}，
quote 的 bid/ask 为 null。恢复中保留最后确认簿的值，禁止将它标成 live。
BookStore.read 的 orders 与 QuoteReader.quote 的 bid/ask 必须指向同一确认状态。
旧完整快照路径中没有被 Receiver 注册的证券仍沿用旧返回结构。

例：epoch=1，A/B都有cut=0快照；seq=2先到且只改A，此时两者恢复中且位点0；
只给A送齐cut=2快照，A为live/2，B仍恢复中/0；seq=1随后到，B连续追赶到2后live，
A不得再次应用seq=1或2。若先收到A/cut=5图像的一部分，再收到完整cut=3，
应采用3，不能因更高不完整图像而无限等待；5完整后再合法切换。

## 持续采集资源与核对

不计调用者本次输入和返回簿，接收端常驻内存只随证券当前订单、尚未消解的序号缺口及
未完成图像增长，不能随已确认的完整历史增长；可以使用现有SQLite和磁盘暂存。
提供 Receiver.work() 返回 {"delta_entries_applied":整数}，计量自本代开始向证券
候选或确认簿实际执行的 changes 条目次数，包含未发布及重开恢复时的重算，不包含只查索引、
存原始包、装载完整图像和求最佳价。计数持久且不倒退，真实执行处累加，不得估算或写常数。
没有更新图像而连续接纳各包一次的N包流，每个changes条目只需应用一次；
一次重复包或空缺等待不应重新应用已经确认的整个前缀。切换新图像后只重放其cut之后
当前能衔接的后缀。此可观察要求服务于持续行情采集，避免随着历史增长每包都重扫重算；
不限数据库查询计划、存储结构或指定算法。

保留原测试全部字节与原运行清单格式。扩展 verify/demo 入口可以调用新增验证，必须真实
检查接收与独立读取结果；有限原测试不代表新功能验收已覆盖。
交付实现、必要回归及简短交接说明，不要求UI、外部账号、服务部署或额外分析产品。
