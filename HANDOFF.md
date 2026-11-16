# 交接说明：增量与分片快照接收

## 新增接口

* `quotetape.recovery.Receiver(db, symbols)`：`feed(event)` 同步接纳 `epoch` / `delta` /
  `snapshot` 三类事件，`close()` 释放连接；以同一 `db`、`symbols` 重开即续接。
  `work()` 返回 `{"delta_entries_applied": 累计值}`。
* `BookStore.read` / `QuoteReader.quote` 对被 Receiver 注册的证券追加
  `status`（`live`/`recovering`）与 `source_epoch`；未注册证券保持原返回结构。
  `epoch`/`seq` 始终描述最后确认簿，恢复中不回退、不改写成实时。

原有 `Tape`、`BookStore.publish`、`FullCapture`、`QuoteReader` 的调用方式、返回值与
原测试字节全部保留；`.pointwise-runtime.json` 未改动。

## 交接不变量

1. **来源代次**：`epoch` 事件严格递增，旧代次消息一律不改变状态；新代次把全部证券置为
   `recovering`，旧确认簿原样保留（`epoch`/`seq` 仍指向旧代次）供说明来源。
2. **全局包序**：每代内 `seq` 从 1 起；重复包按身份幂等，空 `changes` 也推进位点。
   `H` 为该代见过的最大 `seq`（无 delta 为 0），`F` 为证券确认位点；`F<H` 必须
   `recovering`，`F>=H` 且有本代完整图像才是 `live`。
3. **只衔接连续后缀**：证券只沿 `F+1, F+2, ...` 连续接纳的包前进，缺号即停；
   没有本证券修改的包也推进其位点，但绝不跳过缺口。`<=F` 的包不再回放。
4. **分片快照**：图像元数据与分片按 `(epoch,symbol,image)` 留存；收齐 `T` 片前
   不替换当前簿、不改变位点。完整图像按 `cut` 选取；`cut <= F` 的图像整体忽略，
   因此旧图像与旧代次都不能回退状态。高 `cut` 图像跨越既有缺口后，只重放其
   `cut` 之后当前能衔接的后缀。
5. **单包原子性**：一包的 `changes` 在同一 SQLite 事务内应用到全部涉及证券，
   独立进程的读取者只能看到提交前或提交后的整包结果。
6. **持久与幂等**：原始事件（含被忽略的旧代次与重复事件）逐条追加到原有
   `raw_events`；位点、缺口、未完成图像、工作计数均在 SQLite 中。重开不依赖
   已关闭接收端的内存；重投历史不重复应用，`work()` 不倒退。
7. **资源**：常驻内存只随当前订单、未消解缺口与未完成图像增长；已确认历史折叠进
   `books`，`recovery_seen`/`recovery_pending` 低于全证券最小确认位点即清理。
8. **计量**：`work()` 只在真正把 `changes` 条目执行到确认簿（含图像切点之后的
   后缀重放）时累加，装图、存包、查索引、求最优价都不计。

## 存储

沿用并新建以下 SQLite 表：`raw_events`、`books`（原表）、`recovery_state`
（当前代次、`H`、工作计数）、`recovery_symbols`（每证券确认位点与图像代次）、
`recovery_seen`（本代已见包号）、`recovery_pending`（待衔接的包内容）、
`recovery_images` / `recovery_image_parts`（图像元数据与分片）。

## 验证

* `python3 -B -m unittest discover -s tests -v`：原 5 项加新增 21 项，覆盖交错
  （高 cut 分片先到、低 cut 完整图先采用）、双证券原子包、逐证券恢复、代次切换、
  重开续接、重复重投、独立进程读取、包间崩溃、常驻状态不随历史增长。
* `python3 -B run_demo.py`：既有完整快照演示后，用同一运行清单演示缺口标记、
  A 单独恢复、B 独立追赶、分片完整前不替换、重开与换代保留旧簿；输出为真实读取结果。
