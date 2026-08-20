# 群消息同步:水位从「全局单条」改成「每群一条」

> 设计与实施记录。2026-08-11 已按本文落地每群水位;实施评审中发现并修正的两处
> 矛盾见 §3.3 和 §4.1 的“实施修正”。
>
> 评审:Claude + Codex 双方过稿,分歧处在文中标出。所有「代码里确实如此」的断言都
> 带文件与位置;推断一律显式标注。

## 0. 起因

`mars_sync` 里有一段「等图」逻辑(`_ready_count`):企微收到图片消息时**只写 SQLite
的消息行,不下载图片文件** —— 图片要等人真的点开那个会话才落到 `Caches/Images/`。
3 秒的轮询几乎总是跑在下载前面,推出去的永远是 `[图片]` 占位符,而事后没有回补余地
(见 §1.3),所以只能在推之前等,上限 `WECOM_SVR_MARS_IMAGE_WAIT`(默认 60s)。

问题:水位是全局单条的 `Cursor(send_time, lid)`,于是「等 A 群的一张图」会把**其它
所有群**的消息一起推迟最多 60 秒。同源的还有一条:任何一个群推送失败
(`sync_once` 里 `break` 掉整批),同样卡住所有群。

两者的根因是同一个 —— **水位的粒度比阻塞的粒度粗**。

## 1. 约束

### 1.1 群内顺序有意义,跨群顺序没有

`SetTimeline` 从头到尾只在同一个 `GroupNumber` 里找参照物:

```go
// models/qq.go:278-301
db.Get().Where(Message{GroupNumber: msg.GroupNumber}).
    Order("send_time desc").Limit(1).Find(lastMsg)
```

它按 `send_time desc` 取 `lastMsg`,所以**往一个群里插入一条比已有记录更早的消息,
timeline 会切错**。群内保序是硬要求。

跨群则不是。mars 自己就把顺序建模成群内属性 —— `qq/qq.go:167` 的注释:

> 注意:虽然使用 goroutine 异步调用,但通过消息队列保证**同一个群**的消息按时间顺序
> 串行处理

对应实现是 `ticket_sales_mgr.go:594` 的 `getOrCreateMessageQueue(groupID)`,队列按群分。
V2 hook 里其余副作用(`BigCustomerAlert` / `forwardMessage` / `robotLastActiveTime` /
`SyncToSlack` / `NightAlert`)全是裸 `go`,**今天彼此之间就没有顺序保证**
(`qq/qq.go:142-175`)。

**结论:全局单水位买到的「跨群保序」是零收益。**

### 1.2 `forwardMessage` 不命中我们

`forwardMessage` 按 `message.Robot` 分支,只认两个写死的 QQ 号;本机登录企微
VID(拿不到时退回 `wecom-svr`) 落 `default` 分支(`qq/qq.go:314-329`)。

### 1.3 ⚠️ hook **不是**幂等的 —— 这条推翻了一个流传已久的说法

README 和 `mars_sync` 的模块 docstring 都写着「mars 落库前算 checksum,唯一索引 +
FirstOrCreate,重推同一条只会更新,不会重复」,并据此把「宁可重推」当成安全默认。

**这个说法只对 `qq_messages` 的行数成立,对整个 hook 不成立。** 具体:

1. `ConvertMsg()` 里 `SetTimeline()` 跑在算 checksum **之前**(`models/qq.go:415-417`)。
2. `Upsert()` 是 `Where(Message{Checksum}).Assign(*msg).FirstOrCreate(msg)`
   (`models/qq.go:273`)。`Assign` 无论命中与否都会写入 —— 于是**重推一条老消息,会用
   「以该群当前最新消息为参照重算出来的 timeline」覆盖掉它原来的 timeline**。
3. PS 告警、`BigCustomerAlert`、`SyncToSlack`、`NightAlert`、`robotLastActiveTime`、
   `HandleSalesSupportRequest` 都在 Upsert 之外无条件触发,重推会**再执行一遍**
   (`qq/qq.go:142-175`)。
4. checksum = `md5(群号+发送人+秒级时间+正文)`,**不含源 LID**。同一秒、同一人、同样
   正文的两条真实消息会被折叠成一条。
5. `SetTimeline()` 和 `Upsert()` 不在同一个事务里,同群并发请求可能基于同一个旧
   `lastMsg` 算 timeline。

**影响**:任何「反正幂等,多推一点没关系」的设计都不成立。追赶一大段历史不是免费的,
它会重算 timeline 并重放全部副作用。这直接决定了 §3.3 的取舍。

实施时已逐个审计:

- `NightAlert` 对 `ClientType == Wework` 直接返回。
- `BigCustomerAlert` 只认写死的 `R:10943615423553050`,默认 `wework-<rid>` 不命中。
- `HandleSalesSupportRequest` 只认销售支持配置里的精确 `groupID`;没有按 robot 过滤,
  但普通 `wework-<rid>` 默认不在配置里。
- **`SyncToSlack` 会处理所有 Wework 消息**,按 `GroupName` 派生 Slack channel 后发送,
  没有 `wecom-svr` robot 过滤或重复幂等保护。
- `robotLastActiveTime` 仍无条件执行。

所以原先“新 robot 不会撞上任何副作用”的说法不成立;这进一步支持“不默认追赶、迁移
先落盘、保存失败 fail closed”的取舍。

### 1.4 本地取数的既有形状

- `fetch_since` 的 `LIMIT` **作用在过滤之前**,所以 `Batch.cursor`(扫描到的最后一行,
  含被过滤掉的系统消息)必须单独存在 —— 一批全是系统消息时,水位要靠它才能往前走
  (`messages.py:510-594`,有测试盯着)。
- **默认不倒灌历史**。`initial_cursor()` 首跑贴最新一条,只有显式配了
  `mars_sync_backfill` 才往回补。往下游灌几万条历史不可撤销。
- 失败时水位只推进到最后一条**成功**的消息。

## 2. 实施前必须先修的三个既有缺陷

这三条不是本次改动引入的,但每一条都会在每群水位下被放大,**必须先修或先明确**。

### 2.1 `list_groups()` 默认只返回 200 个群

`list_groups(limit=200)` 是默认值(`messages.py:448-473`),而 `resolve_groups()` 和
`target_rids()` 都用默认值调它(`messages.py:481`、`mars_sync.py:274-287`)。

实测:**本机有名字的群会话 275 个,只能看到最近活跃的 200 个 —— 75 个群对白名单解析
和 `sync_all_groups` 不可见。** 一个长期没人说话的白名单群会被报成 `missing`;
`sync_all_groups` 则是静默少同步 75 个群。

docstring 里写明 `limit=None` 表示不截断(是给改名后回库核验用的),说明 200 这个上限
是给挑白名单的界面用的,漏进同步链路属于失误。

**修法**:同步链路一律走 `limit=None`。

### 2.2 `/run` 与后台循环之间没有互斥

`routers/mars.py:69` 的 `/run` 直接 `await mars_sync.sync_once(...)`,后台 `SyncLoop`
同时也在跑 `sync_once`。两者都是 `load_state → 改 → save_state`,是标准的 lost update。
`/watermark`(`routers/mars.py:106-121`)同样是独立的读改写。

全局单水位下损失有限(重叠部分重推,顶多重放一次副作用);**改成 groups dict 之后,
一次覆盖可能整群回退甚至复活一个旧水位**。

**修法**:`sync_once`、`/watermark`、迁移共用一把进程内 `asyncio.Lock`。多进程部署
需要文件锁 —— 但当前是单进程 uvicorn,先按单进程做,并把这个前提写进配置注释。

### 2.3 状态读写的失败姿态是「fail open」

- `load_state()` 遇到文件损坏静默返回空 `State()`(`mars_sync.py:75-80`)。空状态 →
  首跑路径 → 贴 latest → **静默跳过全部积压**。
- `save_state()` 失败只 `log.warning` 就继续,并把本轮当成功返回(`mars_sync.py:83-92`)。
  下轮会重推 —— 而按 §1.3 重推不是免费的。

**修法**:水位是关键状态,应 fail closed —— 损坏就暂停同步并把错误暴露到 `/status`,
不要自动贴 latest;保存失败要作为同步错误上报。

## 3. 状态格式 v2

### 3.1 形状

```json
{
  "version": 2,
  "groups": {
    "10750838085215556": {
      "cursor": {"send_time": 1786416161, "lid": 7672598993047739535},
      "active": true,
      "last_error": "",
      "fail_streak": 0,
      "last_success": 1786416166.2,
      "last_seen": 1786416166.2,
      "gap_from": null,
      "gap_messages": 0
    }
  },
  "cursor": {"send_time": ..., "lid": ...},
  "pushed": 266, "last_run": ..., "last_push": ..., "last_error": ""
}
```

- `groups` 是**真相**,键是 RID(群名可改、可重名,绝不能做键)。
- `version` 决定迁移分支。**不用布尔 `seeded`** —— Codex 的理由成立:布尔开关是隐式
  状态,下次再改格式还得叠一个。(Claude 原稿曾提出用「`groups` 键是否存在」当标记,
  它确实能区分三种情况,但作为长期文档,显式版本号更好维护。)
- 顶层 `cursor` 降级为**影子水位**,取 `min(所有 active 群的 cursor)`。它有两个用途:
  旧二进制回滚时有个不漏消息的落点(代价是重推,见 §7.2),以及 `/status` 上一个
  「整体推到哪了」的粗略读数。

### 3.2 新群的起点

**必须按 RID 查该群自己的最新水位,不能用 `messages.latest_cursor()`。**
后者是全库 `SELECT ... FROM MESSAGE ORDER BY send_time DESC LIMIT 1`,不带 RID 条件
(`messages.py:502-507`)。用它给新群定位,该群后续入库但 `send_time` 较早的消息会被
永久跳过。空群应得到零水位。

需要新增 `messages.latest_cursor(rid=None)` 的按群形态。

### 3.3 群移出白名单后又加回来 —— 与 Codex 的分歧

保留 tombstone(`active: false` + `last_seen`)双方一致:RID 稳定,信息不该丢,275 个
群的状态文件大小不是问题。

分歧在**重新加入时的默认行为**:

| | Codex | 本文采用 |
| --- | --- | --- |
| 默认 | 从旧水位追赶 | **从该群 latest 开始** |
| 理由 | 避免静默数据缺口 | 见下 |

采用「从 latest 开始」是因为 §1.3:追赶不是回放数据,而是**重放副作用** —— 一个移出
白名单一个月的群重新加入,会重算这一个月消息的 timeline,并重新触发全部告警、Slack、
销售工单分支。这个代价比缺口大,而且不可撤销。

但 Codex 的核心关切必须满足:**缺口不能静默**。所以:

- tombstone 保留旧水位,`/status` 显式给出 `gap_from` 和 `gap_messages`(缺口起点与条数)
- 想追赶要显式操作:`POST /watermark {"rid": ..., "resume_from_tombstone": true}`
- 追赶前先用 `/preview?rid=...` 看清条数和时间跨度

即「不默认追赶,但永远知道自己欠了多少」。

**实施修正**:`active: false` 的 tombstone 确实保留旧 `cursor`;重新加入并把活动
`cursor` 跳到 latest 时,必须把旧值另存进 `gap_from`。否则旧值会被覆盖,
`gap_messages` 和 `resume_from_tombstone` 都失去依据。状态格式因此显式增加这两个字段。

## 4. 取数层

### 4.1 SQL:按群限额的 UNION ALL,保留 join

```sql
SELECT m.LID, m.conv_id, m.send_time, ..., c.name AS conv_name
FROM (
  SELECT * FROM (
    SELECT ... FROM MESSAGE WHERE conv_id=? AND <该群 cursor range>
    ORDER BY send_time, LID LIMIT <该群 quota>
  )
  UNION ALL
  SELECT * FROM (
    SELECT ... FROM MESSAGE WHERE conv_id=? AND <该群 cursor range>
    ORDER BY send_time, LID LIMIT <该群 quota>
  )
) m
JOIN CONVERSATION c ON c.RID = m.conv_id AND c.conversationtype = ?
ORDER BY m.send_time, m.LID
```

**实施修正**:原稿的 OR-of-ranges + 全局 LIMIT 虽然性能可接受,却与 §4.3 的公平性
要求矛盾——深积压群仍能吃完全部额度。实现改为每个 UNION 分支各自 LIMIT,总条数
仍不超过 batch;群数大于 batch 时轮转本轮选中的群。这样每个群返回的仍是连续前缀,
同时饥饿问题在取数层被真正消除。

参数个数约 `5N + 1`(275 群 = 1376),远低于本机
`SQLITE_LIMIT_VARIABLE_NUMBER = 32766`。

**这条 SQL 的每一处都是踩出来的,不能随手简化:**

#### 原稿实验:`OR` 和 `JOIN` 放在一起会让 sqlite 放弃 `MULTI-INDEX OR`

真库(568k 行 MESSAGE)实测:

| 形式 | 8 群 | 200 群 | 查询计划 |
| --- | --- | --- | --- |
| 现状 `IN(...)` + join | 18.3ms | 37.8ms | `SCAN c` |
| OR-of-ranges 直接替换(join 留在外层) | **73.8ms** | — | `SCAN c` |
| 过滤 + LIMIT 下推子查询 | 25.5ms | 59.9ms | `MULTI-INDEX OR` |

因此无论使用原稿 OR 还是实施版 UNION,过滤和 LIMIT 都留在子查询,外层只留 join。

#### 陷阱二:去掉 join 会让消息翻倍

`CONVERSATION` 表**有重复 RID**:838 行 / 837 distinct。实例:

```
RID=3395103030  conversationtype=1  name='Notify'
RID=3395103030  conversationtype=0  name=''
```

现有代码靠 `c.conversationtype = 1` 这个谓词**顺带**去了重。下推时如果图省事把 join
去掉(反正 `conv_id` 已经限定了群),那个群的消息会条条翻倍 —— 实测 `LIMIT 200` 返回
218 行。所以 join 必须留着,`conversationtype` 在这里身兼二职:取群名 + 去重。

### 4.2 被否掉的替代:取 `min(所有群水位)` 一次查完,Python 侧按群过滤

看着更简单,但有**饥饿 bug**:`LIMIT` 作用在原始扫描上,落后最多的那个群会让这 200 行
被「其它群早就推过的旧行」占满,于是谁都不前进。OR-of-ranges 返回的 200 行行行有用。

### 4.3 公平性

实现按群分配 `limit // len(rids)` 的基础配额,余额从 round-robin 起点分配。群数大于
limit 时本轮选 limit 个群各取一条,下轮轮转起点,保证所有群最终都能获得扫描机会。

### 4.4 `Batch` 的形状

`Batch.cursor: Cursor` → `scan_end: dict[rid, Cursor]`,每群「扫描到的最后一行」
(含被过滤掉的系统消息)。

**正确性已实证**:在真库上用错开的每群水位跑 OR-of-ranges + `LIMIT 200`,逐群比对
「返回行数」与「该群水位到 scan_end 区间内的实际行数」,6 个群全部吻合 —— 返回的行
确实是各群待推流的**连续前缀**,`scan_end` 按群推进不会漏。

原理:`ORDER BY` 是全局的,`LIMIT` 截断的是全局序列的一个**后缀**,不是子集;所以对
任意群 g,结果里属于 g 的行必然是 g 待推流的前缀。

## 5. 调度

### 5.1 每群三个位置

1. 本轮起点 `cursors[rid]`
2. 最后一条**成功推送**的可见消息
3. 扫描终点 `scan_end[rid]`

提交规则(Codex 提出,原稿表述过宽,已采纳收紧):

| 该群状态 | 水位推进到 |
| --- | --- |
| 有等图或推送失败 | 阻塞点之前最后一条成功的消息。**绝不能用 `scan_end`** |
| 无阻塞,可见消息全部成功 | `scan_end[rid]` |
| 本批只有被过滤的系统消息 | `scan_end[rid]` |
| 因全局 LIMIT 一行都没返回 | 不动 |

一旦某群阻塞,本批中它后面的消息全部跳过;其它群继续。

原稿写的「一轮成功后各群推进到自己的 scan_end」是错的 —— 别的群成功不能让阻塞群越过
自己被扣下的消息,否则永久吞消息。现有测试
`test_a_held_batch_does_not_let_the_watermark_jump_to_the_scan_end` 和
`test_watermark_stays_put_when_a_push_fails_mid_batch` 就是这条不变量的群级版本。

### 5.2 推送并发

**第一阶段仍保持 HTTP 推送整体串行**,只是遇到 A 群阻塞时跳过 A 的后续消息、继续 B/C。
不一开始就每群并发发 HTTP —— 先把状态和正确性做对,再按实测延迟决定要不要引入有限并发。

无论是否并发,**同一 RID 必须严格串行**,且禁止两个 `sync_once` 同时处理同一群(见 §2.2)。

## 6. 接口

- `/status`:群级明细(cursor / active / scanned / pushed / held / last_error /
  fail_streak / last_success / 积压估算)+ 汇总(活动群数、阻塞群数、失败群数、
  最老 lag、`version`)。群数会长到 275,**明细要分页或支持按 `rid` 查**,不能无界返回。
- `/watermark`:加可选 `rid`;不带 `rid` = 重置全部,属高风险操作,响应要回显范围与新旧值。
  另加 `resume_from_tombstone`(见 §3.3)。
- `/preview`:必须和真实首轮**共用同一套起点与取数逻辑**,否则它就不再是「预览第一轮
  会推什么」。加 `rid` 过滤。
- `SyncResult`:加 `group_results: dict[rid, {...}]`;**保留顶层 `error`** 作兼容摘要
  (无错为空、单群错误沿用原文、多群给 "3 groups failed")。部分成功的 `/run` 仍返回
  200 并带 `partial: true`;只有整轮开不起来(配置错、本地库读不了)才 4xx/5xx。
- 错误文本要截断,否则状态文件和 `/status` 会无界增长。

## 7. 迁移与回滚

### 7.1 迁移

1. **先备份**旧 state 文件。
2. `version` 缺失 → 执行 v1→v2:把旧的全局 `cursor` 复制给**迁移时白名单里的每个群**。
3. 全新安装(无 v1 cursor):按 §3.2 逐群查该群 latest;配了 backfill 则按时间算每群起点。
4. **先把迁移结果落盘,再开始推消息** —— 保证迁移可重入。
5. `groups` 即使为空也要落盘,否则每轮都会重新迁移一次。
6. ⚠️ `save_state` 的变更检测目前是
   `(state.cursor, state.pushed, state.last_error) != (before...)`(`mars_sync.py:387-391`),
   加 `groups` 后必须一起比。另外 `replace(state)` 是浅拷贝,**`groups` dict 原地改会
   让变更检测失效**,要深拷贝或改成不可变更新。
7. 迁移必须在**冻结白名单**的前提下做:迁移瞬间某个群因改名、§2.1 的 200 限制、或本地库
   暂时没刷出来而缺席,之后会被误判成新群贴 latest。

### 7.2 回滚

每群水位分叉之后,**不存在一个既不漏又不重推的旧版全局 cursor**。要回滚只能用
`min(所有 active 群水位)`——保证不漏,代价是其它群重推。而按 §1.3 重推会重算 timeline
并重放副作用,所以回滚**必须人工确认**,不是一个可以随手做的操作。

回滚前要停后台循环并挡住 `/run`、`/watermark`,否则状态翻译会被并发覆盖。

## 8. 实施顺序

前四步是前置条件,不做完不要碰取数层。

1. **冻结语义**:新群是否跳过第一条已存在消息;移除再加入的默认(本文选 latest);
   超窗乱序消息怎么办;部分成功的 API 表达。
2. **审计 mars 副作用**:§1.3 待确认的四个函数。测试环境重复提交同一 hook,确认
   消息表 / timeline / 告警 / Slack / 销售任务分别发生什么。
3. **修 §2 的三个既有缺陷**(200 群上限、互斥、fail closed)。
4. **定义并实现 v2 状态协议 + 迁移**,先落盘后推送。
5. **取数层**:按 RID 的 latest 查询、每群 `scan_end`、§4.1 那条 SQL。用真实库跑
   `EXPLAIN QUERY PLAN` 并对 5 / 200 / 275 群分别计时。
6. **群级调度**:同群串行、阻塞群停其余继续、只有无阻塞群才提交 `scan_end`、§4.3 的配额。
7. **API 与观测**:§6。保留旧字段兼容期。
8. **灰度**:先只迁移状态并双读校验、不推送 → 单个低风险群启用 → 扩大白名单,
   观察群级 lag / 重推量 / timeline / 副作用指标。

## 9. 验证清单

- 迁移三种入口:v1 非零 cursor、全新状态、配了 backfill
- 迁移后无消息也能持久化(否则每轮重迁)
- 新群、空群、群的第一条消息、群删除再加入
- **A 群等图,B/C 正常推进**(本次改动的核心目的)
- **A 群推送失败,B/C 正常推进;A 下轮从最后成功处恢复**
- 某群本批全是被过滤的系统消息 → 水位照样前进
- 被过滤的系统消息位于 held/failed 消息**之后** → 不能跳到 scan_end
- 全局 LIMIT 截断下每群仍是连续前缀(§4.4 已在真库验过一次,要固化成测试)
- 深积压群不会饿死活跃群(§4.3)
- 两个 `/run` 并发、`/run` 与 `/watermark` 并发
- 状态损坏、保存失败、进程在 HTTP 成功后落盘前崩溃
- dry-run 不改状态;preview 与紧接着的实际运行结果一致

## 10. 已知缺陷与待确认

### 10.1 乱序入库的消息会被永久漏掉(既有,不由本次引入)

`fetch_since` 的条件是严格的 `(send_time, LID) > cursor`(`messages.py:527-528`)。
如果企微存在延迟落库、修复性回填,或客户端时钟倒退,产生「后入库但 `send_time` 小于
当前水位」的消息,它会被永久跳过。

**待确认**:LID 是否是可靠的入库序列。如果是,乱序检测应该基于 LID 而不是 send_time;
如果不是,需要定义一个乱序窗口和超窗策略。

### 10.2 未来时间戳会拉长等图窗口

`_ready_count` 用 `now - send_time >= wait`。发送方时钟超前时,这条消息会被反复扣下
直到真实时间追上。影响有限(上限就是时钟偏差),但要知道有这回事。

### 10.3 同秒消息在两侧的排序依据不一致

本地用 `(send_time, LID)` 排序,而 mars 的 `qq_messages` 不保存 LID,`SetTimeline` 的
`lastMsg` 查询也只有 `Order("send_time desc")`,没有次级排序。同秒多条消息在 mars 侧的
先后是不确定的 —— 既存问题,每群水位不会让它变好也不会变坏。

### 10.4 是否还有别的入口在写同一个 `wework-<rid>`

如果有,企微单侧的群内保序不足以保证 mars 侧的群内保序。**待确认。**
