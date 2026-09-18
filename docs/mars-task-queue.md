# mars → 这台 Mac:反向调用的任务队列

> 现状是单向的:Mac 把群消息推给 mars(`mars_sync.py`)。这份文档设计反过来的那条 ——
> mars 想让这台 Mac 发一条企微消息时怎么办。涉及两个仓库:
> `wecom-svr`(本仓库,Mac 侧)与 `qbox/mars`(Go,生产,分支 `feat/wecom-task-queue`)。

## 0. 约束

决定形状的是这四条,不是偏好:

1. **Mac 在 NAT 后面,IP 会变,还会合盖休眠。** mars 拨不进来。
2. **Mac 拨出去是通的。** `mars_sync` 每 3s 就在往 `https://ts.qiniu.io` 推东西。
3. **mars 是 4 节点在 LB 后面**(xs4793 / yzh897 / yzh895 / xs4796)。任何"连接落在
   某个节点上"的方案,都要额外解决"另外 3 个节点怎么把命令送到那条连接上"。
4. **发消息不可撤销。** 重发一条 = 对方多收一条。这跟群消息同步方向相反:那边
   mars 有 `md5(群号+发送人+时间+正文)` 唯一索引兜底,所以默认"宁可重推";
   这边没有任何下游幂等,所以默认**宁可漏发**。

第 4 条不是一句口号 —— 它决定了下面一半的设计(见 §4.2 的 `executing` 状态)。

## 1. 被否掉的方案

记下来,免得半年后有人重新提这些问题。

### 1.1 WebSocket / SSE

| | WebSocket / SSE | 任务队列 + 长轮询 |
| --- | --- | --- |
| mars 侧改动 | hub + 心跳 + 重连 + 背压,还要改 xs4797/yzh1410 的 openresty 放 Upgrade、调长超时 | 一张表 + 4 个接口 |
| 4 节点 | 连接只在一个节点上,其余节点要 Redis pubsub 才能推过去 | 共享 MySQL,天然可用 |
| Mac 休眠/重启 | 断连期间的命令直接丢 | 任务留在表里,醒来接着做 |
| 排障 | 要看连接状态 | `select * from wecom_tasks` |
| 实时性 | 亚秒 | 长轮询下**同样是亚秒**(有活立刻返回) |

WebSocket 唯一真正的优势是"mars 主动说话"的延迟,而长轮询在有活时是立刻返回的,
这点优势并不存在。它换来的是一堆有状态的东西。

### 1.2 内网隧道

frp / tailscale,让 mars 直接 HTTP 调 Mac:mars 侧几乎零改动,是最干净的,但这台
Mac 进内网只有跳板机一条路,而跳板机**禁止端口转发**。除非愿意在某个生产节点上
常驻 frps 并开端口,否则走不通。

### 1.3 换个存储

这张表放在 mars 的 MySQL 里,跟 `wecomtask.Enqueue` 要服务的业务代码同库。这不是
默认值,是条件:**入队要能跟业务写在同一个事务里提交**(§4.5 末尾的 outbox 用法)。
换成任何别的库都会变成跨库双写 —— 崩在两次写中间,要么丢任务,要么留下一条没有
业务对应的幽灵任务。想动存储之前先回答这个问题。

## 2. mars 里已经有的两个东西

先确认没在重复造轮子:

- **`qq.CreatMsgMission` / `qq.GetSendMsgMission`**(`qq/qq.go:1411`,路由
  `POST /api/qq/hook/mission`)—— 已经是拉模式的外发队列,QCDN 告警在用。
  形状对了,但不能直接复用:它 SELECT 到之后**立刻另发一条 UPDATE 置 sended**
  (两条独立语句,不是原子领取;机器人还没发就标已发,崩了就丢)、一次只给一条、
  队列空时返回 400(轮询会刷一片错误日志)、无鉴权、`Content` 是拼好的纯文本
  (没法表达"发图"/"拉群")。
  → 结论:**照着它的思路,但另起一张表**,不动 QQ 那条正在跑的路。
- **`httpbot`**(`httpbot/httpbot.go`)—— mars 主动 HTTP 调外部机器人。方向反了,
  要求 mars 能连到 Mac,回到 §0 第 1 条。

鉴权照抄 `InternalAITicketsAuth`(`app/mars/internal_aitickets.go`):mars.json 里一个
静态 token + `subtle.ConstantTimeCompare`,没配就 503。不用 `BearerTokenAuth`
(每次请求都要外呼 `bearer.qiniu.io`,长轮询下白白多一跳)。

## 3. 总体形状

```
mars 业务代码 --wecomtask.Enqueue()--+
                                     |
外部调用 --POST /tasks---------------+
                                     |
                                     v
                          +------------------------+
                          |  wecom_tasks  (MySQL)  |
                          +------------------------+
                                ^           |
        (2) start --------------+           | (1) claim
        (3) ack ----------------+           v  -> lease_token
                          +------------------------+
                          |  Mac: wecom-svr        | --> 企微客户端
          长轮询 (25s) <--|  mars_tasks 循环       | --> 官方 API
                          +------------------------+
```

一句话:**mars 只负责把"要发什么"落库,Mac 自己来取、报告开工、执行、回报。**

那个 (2) `start` 看着多余,其实是整套设计的支点 —— 见 §4.2。

## 4. mars 侧

### 4.1 表 `wecom_tasks`

```go
type WecomTask struct {
    ID        uint      `gorm:"primary_key"`      // 注意是 primary_key:仓库用的是 2016 年的 GORM v1
    CreatedAt time.Time
    UpdatedAt time.Time

    // worker 和 created_by 同时是 dedup_key 的命名空间 —— 见下面第 4 条
    Worker    string  `gorm:"column:worker;size:64;not null;index;unique_index:uix_wecom_dedup"`
    Action    string  `gorm:"column:action;size:64;not null"`         // 见 §4.5
    Payload   string  `gorm:"column:payload;type:text"`               // JSON,原样透传
    DedupKey  *string `gorm:"column:dedup_key;size:128;unique_index:uix_wecom_dedup"`  // 可空

    Status      int    `gorm:"column:status;not null;index"`
    Attempts    int    `gorm:"column:attempts;not null"`
    MaxAttempts int    `gorm:"column:max_attempts;not null;default:3"`
    LeaseOwner  string `gorm:"column:lease_owner;size:96;index"`       // "<worker>:<128bit nonce>"
    LeaseUntil  *time.Time `gorm:"column:lease_until"`
    StartedAt   *time.Time `gorm:"column:started_at"`                  // 真正动手的时刻
    ExpireAt    *time.Time `gorm:"column:expire_at;index"`             // 过了就别发了

    Result    string `gorm:"column:result;type:text"`
    Error     string `gorm:"column:error;type:text"`
    CreatedBy string `gorm:"column:created_by;size:64;unique_index:uix_wecom_dedup"`
}
```

四个非写不可的细节:

- **被索引的 string 必须写 `size:`。** GORM **v1 全系列**对没有 `size` 的 string
  生成 `longtext`,然后无前缀建索引会失败 —— 表建出来了但关键索引没有,而
  AutoMigrate 不会报到你脸上。不是仓库这个 2016 版本特有的:最后一版 v1(v1.9.16,
  2020)那段逻辑逐字节相同,所以**在 v1 里升级不解决这个问题**。只有 v2
  (`gorm.io/gorm`)会给"带索引且没写 size"的字段自动用 `varchar(191)`,但那是换库
  不是升版本(48 个文件 + 要把 `go 1.16` 抬到 1.18)。现在这个坑的后果已经被
  §7 第 4 步的启动校验堵死了 —— 索引缺了整条路 503,不会再静默带病上线。
- **标签是 `primary_key` 不是 `primaryKey`。** 后者在这个版本里根本不被识别
  (本例因为字段名恰好是 `ID` 会被约定兜底,但标签是无效的)。
- **`DedupKey` 必须是 `*string`。** MySQL 唯一索引允许多个 `NULL`,但不允许多个
  `''`。用值类型的话,第二条不带 dedup_key 的任务就会撞唯一索引失败。
- **`dedup_key` 的唯一键是复合的 `(worker, created_by, dedup_key)`,不是全局唯一。**
  全局唯一时两个不相干的调用方碰巧用了同一个 key,后来那条会被当成"已入队"静默
  吞掉。命名空间要跟进唯一键,不能指望调用方自觉加前缀。**副作用:`created_by`
  从"给人看的"变成了身份的一部分** —— 同一个调用方要一直用同一个值。

### 4.2 状态机 —— `executing` 是重点

```
      enqueue
      |
      v
    0 pending --claim--> 1 leased --start--> 5 executing --ack ok--> 2 done
      ^                       |                    |
      |                       +-- ack retry --+    +-- ack 失败 ----> 3 failed
      |                                       |    +-- 租约超时 ----> 3 failed(结果未知,不重投)
      |   租约超时 且 attempts < max          |
      +---------------------------------------+

    超过 expire_at(仅 pending / leased)--> 4 expired
```

`1 leased` 和 `5 executing` 的区别就是**这套设计能不能兑现"宁可漏发"**:

- 只有 `leased`(领了还没动手)的租约超时才重投 —— 那时什么都还没发生,重投是安全的。
- `executing` 的租约超时**绝不重投**,判 `failed` 并写明 "outcome unknown"。
  mars 不知道那条消息到底出没出去,重投就是赌"没出去",赌输了是对方屏幕上多一条。

**少了这个状态,Mac 侧再怎么记都拦不住重发** —— 本机记着"我发过了",但 mars 那边
租约一到期就把任务放回队列,下一轮本机自己又领回来了。这是评审里最值钱的一条。

这条规则由**两处**共同守着,缺一处就是漏的:

- reaper:`executing` + 租约超时 → failed(§4.4 第 1 步);
- `Ack`:`retry=true` **只接受 `status=leased`**,从 `executing` 想放回队列一律回
  `ErrRetryFromExecuting`(409)。

只守住 reaper 是不够的:那样"executing 不重投"就只剩一条**约定** —— 靠"Mac 只在
动手前喊忙"兜着,而 schema 拦不住任何人违反它。

`2 done` / `3 failed` / `4 expired` 是终态。除租约超时外,**"要不要重试"全由执行方
在 ack 里说** —— 只有它知道错误的性质("客户端界面正忙"该重试,"不认识这个 action"
重试一万次也一样)。

### 4.3 认领协议与 fencing 令牌

一个**短事务**内完成,绝不把长轮询的等待包进事务:

```sql
START TRANSACTION;

SELECT id FROM wecom_tasks
 WHERE worker = ? AND status = 0 AND (expire_at IS NULL OR expire_at > ?)
 ORDER BY id LIMIT ? FOR UPDATE;

UPDATE wecom_tasks
   SET status = 1, lease_owner = ?, lease_until = ?, attempts = attempts + 1,
       started_at = NULL, updated_at = ?
 WHERE id IN (...) AND status = 0;      -- 影响行数对不上就整批回滚

SELECT * FROM wecom_tasks WHERE lease_owner = ? AND status = 1 ORDER BY id;

COMMIT;
```

没用 `UPDATE ... ORDER BY ... LIMIT` 一把梭:那样锁范围取决于执行计划(缺索引时会
锁一大片 pending 行连带间隙),而且带 `LIMIT` 的 UPDATE 对 statement-based binlog
不安全。

`lease_owner` 是 `<worker>:<128 bit 随机数>`,**每次认领新生成**,并且:

- 它随 pull 响应下发给 worker(`lease_token`);
- worker 调 `/start` 和 `/ack` 时必须带回来;
- 服务端一律条件更新 `WHERE id=? AND lease_owner=?`,**影响行数为 0 就回 409**。

这道闸挡的是一个真实的 ABA:worker 卡了五分钟 → 租约被回收 → 任务被重新认领 →
旧 worker 醒过来按 ID 回报,把**新一次租约下的执行**直接结案掉。只有 nonce 而
不把它带进 ack,是防不住这个的。

必需的复合索引(`InitWecomTaskTable` 里用 `AddIndex` 补,AutoMigrate 建不出来):

```
idx_wecom_claim  (worker, status, id)
idx_wecom_reap   (status, lease_until)
idx_wecom_expire (status, expire_at)
```

缺了它们,每次空轮询都要扫一大片再取间隙锁,而这条路是被长轮询持续敲的。

`lease_owner` 不需要索引:认领后是按主键读回来的,`/start` 和 `/ack` 的条件更新
也都以 `id = ?` 打头,令牌只是那一行上的附加条件。

### 4.4 回收(reaper)

每次 `/tasks/pull` 进来先跑一遍,**四步顺序不能重排**:

```sql
-- 1. 已经动手但失联 → 判失败,绝不重投(整套取舍压在这一条上)
UPDATE ... SET status=3, error='...outcome unknown, not retried'
 WHERE status=5 AND lease_until < ?;
-- 2. 领了没动手、且试到头 → 判失败(先做,否则会被下一条无限期放回 pending)
UPDATE ... SET status=3 WHERE status=1 AND lease_until<? AND attempts>=max_attempts;
-- 3. 领了没动手、还有次数 → 回 pending(安全:什么都没发生)
UPDATE ... SET status=0, lease_owner='' WHERE status=1 AND lease_until<?;
-- 4. 过了有效期 → 作废。只扫 0/1;5 已经在动手了,标 expired 会掩盖上面那条真相
UPDATE ... SET status=4 WHERE status IN (0,1) AND expire_at IS NOT NULL AND expire_at<?;
```

放在 pull 里而不是另起 cron:这条路本来就每 25s 被敲一次,不需要第二个调度器。

代价要说清楚:**没人 pull 的时候回收根本不跑。** Mac 关机期间,已经到期的任务会
一直挂在 `pending`/`executing` 上,`GET /tasks` 看到的是过时的。这不影响正确性 ——
`Claim` 自己带了 `expire_at > now` 的条件,过期任务醒来也领不走,不会补发;但监控
和排障看到的状态是滞后的,别把它当实时视图。真要实时,加个独立 reaper(§10.1)。

时间由应用侧传入(`Reap(now)` / `Claim(..., now)`)而不是用 `UTC_TIMESTAMP()`。
代价是依赖 4 个节点的 NTP 同步(毫秒级偏差 vs 120s 租约,不构成问题),
换来的是测试可以随意穿越时间 —— `wecomtask_test.go` 里那些"一小时后再来"
就是这么写的。

### 4.5 接口

全部挂在 `router.Group("/api/internal/wecom", WecomTaskAuth)` 下。

| 接口 | 用途 |
| --- | --- |
| `POST /tasks` | 入队。同 `dedup_key` **且内容相同**则返回原任务 + `duplicated:true`;内容不同是撞 key,回 **409** |
| `POST /tasks/pull` | 认领。`{"worker","max","wait","lease"}`;`wait` 秒内没活返回空数组 **200** |
| `POST /tasks/start` | 开工。`{"id","lease_token"}`;令牌过期回 **409** |
| `POST /tasks/ack` | 回报。`{"id","ok","retry","error","result","lease_token"}`;令牌过期、或想把 `executing` 的任务 `retry` 回队列,都回 **409** |
| `GET  /tasks` | 给人看的列表,按 worker/status 过滤 |

**任务 id 在 body 里,不是 `/tasks/:id/start` 这种路径参数。** 这不是风格选择,是
被 gin 逼的:mars 用的 gin v1.3 路由树**不允许静态兄弟和通配兄弟共存**,
`/tasks/pull` 和 `/tasks/:id/start` 放在一起会在**注册时** panic ——

```
panic: wildcard route ':id' conflicts with existing children
       in path '/api/internal/wecom/tasks/:id/start'
```

而注册发生在启动阶段,所以后果不是这条路 500,是**整个 mars 起不来**。第一版就是
这么写的,发上线才炸,5 个节点回滚。现在整条路一个通配符都不用,以后谁往
`/tasks/` 下面再加静态路由都不会重蹈覆辙,`wecomTasksRoutes` 那个函数单独抽出来
就是为了让 `TestWecomTasksRoutesNoPanic` 能原样注册一遍 —— 这类错误编译器和
handler 单测都看不见,只有真注册才暴露。

空队列必须是 200 而不是 400 —— 这条路一天要被敲几万次,用错误码表达"没活"
会让真正的故障淹在噪声里(`qq/hook/mission` 那条老路就是这么设计的)。

入队 body:

```json
{
  "worker": "mac-cc",
  "action": "message.send",
  "payload": {"to": "张三", "content": "工单 463038 已超时"},
  "expire_in": 600,
  "dedup_key": "ticket-463038-timeout",
  "created_by": "deimos"
}
```

> 不可撤销的 action **强烈建议带 `dedup_key`**,由业务事件稳定生成。它防的是
> "调用方超时重试导致重复入队",跟 claim/执行层的去重是两回事,两者都需要。

长轮询的实现就是"隔一会儿试一次 claim,直到超时":不用 channel、不用 pubsub,
所以 4 个节点跑的是同一份代码,谁接到请求谁服务。间隔从 250ms 指数退避到 2s ——
第一次是立刻查,所以**有活永远是秒回**,退避只影响空转时的库压力(空转才是常态)。
`wait` 上限 50s:前面 openresty 的 `proxy_read_timeout` 默认 60s,别顶着上限
(上线前请实际读一遍 xs4797/yzh1410 的 vhost 确认)。

响应里的时间一律是 **unix 秒**(`expire_at` / `lease_until`),不是 RFC3339 ——
worker 只需要跟 `time.time()` 比大小,给字符串等于逼它做时区解析。

给 mars 内部业务代码留了 `wecomtask.Enqueue(...)`,走库不走 HTTP。

### 4.6 动作表(mars 侧只校验非空,真正认它的是 Mac)

action 名和 wecom-svr 的 HTTP 路由**同构**:带 `app.` 前缀的是自建应用身份,
不带前缀的是本人身份。两边一起改,别只改一边。

| action | 落到 wecom-svr 的哪里 | 身份 |
| --- | --- | --- |
| `app.message.send` | `POST /api/v1/app/message/send` | 自建应用 |
| `message.send` (`msgtype=text/image`) | `POST /api/v1/message/send` | **本人** |
| `message.recall` | `POST /api/v1/message/recall` | **本人** |
| `group.create` | `POST /api/v1/group/create` | **本人** |
| `group.rename` | `POST /api/v1/group/rename` | **本人** |
| `group.nickname` | `POST /api/v1/group/nickname` | **本人** |

`payload` 就是对应接口的 body,原样透传。mars 不重新建模这些字段 —— 建了就会漂。

`message.recall` 的 payload 必须同时包含 `conv_rid` 和 `lid`:

```json
{
  "worker": "mac-cc",
  "action": "message.recall",
  "payload": {"conv_rid": 247764039432344, "lid": 123456789},
  "expire_in": 600,
  "dedup_key": "message-recall-247764039432344-123456789",
  "created_by": "deimos"
}
```

队列不允许省略 `lid`。人工调用 HTTP 接口时可以省略它来选择本人最新消息,
但异步任务可能延迟执行,此时“最新消息”可能已经变化,所以必须精确指定目标。

> 不带前缀的四条默认**不在** worker 的白名单里(§5.2)。"本人身份是默认关的"
> 靠的是白名单,不是名字长得像 —— 加新 action 时别指望前缀替你把门。

### 4.7 发到群

**没有单独的"群发" action。** 群在企微客户端里就是一个会话,所以本人身份发到群
用的还是 `message.send`,只是 payload 指向群:

```json
{
  "worker": "mac-cc",
  "action": "message.send",
  "payload": {"conv_rid": 247764039432344, "content": "工单 463038 已超时"},
  "expire_in": 600,
  "dedup_key": "ticket-463038-timeout",
  "created_by": "deimos"
}
```

**用 `conv_rid`,别用 `to`(群名)。** `LocalSendRequest` 两个都收,但在队列这条路上
只有 RID 是对的:

- `to` 是模糊搜索。多条命中时靠 `resolve_conversation_name` 的启发式挑一个
  (优先 `conversationtype == 1` 的群,还挑不出就取第一条)。手工调接口时你能看
  回执核对,队列里没人看。
- **群名会变,而且这套东西自己就提供 `group.rename`。** 名字一改,任务要么失败,
  要么发进另一个同名会话。
- 发消息不可撤销,发错群比发失败严重得多 —— 跟 §0 第 4 条是同一个取舍。

RID 从两处拿:`POST /api/v1/conversations`(按关键字搜),或
`GET /api/v1/mars/sync/groups`(按最近活跃排序,还标了哪些已在同步白名单里)。
两处返回的都是**裸 RID**。注意 mars 那边 `qq_groups.qq` 存的是
`wework-<RID>`(`mars_sync` 加的前缀,防止和真实 QQ 群号撞号),从那里取值入队
**要把前缀去掉**。

图片也走 `message.send`,payload 使用 `msgtype=image`;图片必须按 `conv_rid` 精准定位会话。

群发比单发更该带 `dedup_key`:多发一条私聊是尴尬,多发一条群消息是一屋子人看着。

#### 应用身份发到群:目前队列做不了

`app.message.send` 用的是 `SendMessageRequest`,只有 `touser`,发不了群。官方的
群发是 `POST /api/v1/app/group/send`(按 `chatid`),而**这条没有对应的 action** ——
§4.6 的表就是全集。

要开这条路得加一个 `app.group.send`(schema 用 `SendGroupRequest`,
`needs_ui=False`),三处一起改:wecom-svr 的 `ACTIONS` 表、mars 的 action 常量、
上面那张表。没提前加是因为还没有用例,而每多一条 action 就多一条不可撤销的
远程能力。

顺带记一下这两条路的能力差(和 README「拉群」那节是同一条边界):appchat 群只有
**本应用建的**才发得进去,群成员也只能是本企业的人;客户群、带外部联系人的群,
应用身份根本看不见,只能走本人身份。

## 5. Mac 侧(wecom-svr)

### 5.1 循环

`wecom_svr/mars_tasks.py`,骨架照 `mars_sync.py`:后台 `asyncio.Task`、`.data/` 状态
文件、`/start` `/stop` `/status` 三件套、循环内吞异常不死。

```
pull(wait=25) --空--> 立刻再 pull
      |
      +--有活--> 逐条: precheck -> 占 UI -> /start -> 执行 -> 落结果 -> /ack -> 清 inflight
```

`run_once` **全程互斥**(模块级 `asyncio.Lock`),后台循环开着时手动 `/run` 直接回
409。两个 `run_once` 并发会重复发送,而令牌拦不住 —— 两边用的是同一个。

**占 UI 在 `/start` 之前**是这条流水线的支点:界面忙必须在 `leased` 阶段暴露,那时
一条消息都没发过,`retry=true` 是安全的。等进了 `executing` 再喊忙,mars 就只能按
发过处理。实现是 `uilock.ui_reserve()`,占住之后调用链里嵌套的 `ui_session` 全部
放行(用 `ContextVar` 而不是重入锁:重入按线程算,而真正操作界面的代码跑在
`asyncio.to_thread` 的工作线程上,不是同一个线程)。

"要不要占界面"是 `ACTIONS` 表里每条 action 的第三项(`Action.needs_ui`),不是
另立一张并行的表 —— 并行表没有任何东西保证两边同步,而写成元组的一项,新增
action 时不回答这个问题根本构造不出来。漏标的后果不对称:少标一个会把"客户端忙"
拖到 `executing` 才暴露,而那时已经不能安全重投。

**串行执行**,不并发:本人身份那几条要独占客户端界面(抢焦点、剪贴板、键盘),
`uilock` 本来就会把并发变成 409。

`precheck`(过期 / 不认识的 action / 不在白名单 / payload 不合法)刻意排在 `/start`
**之前**:这些任务一条消息都不会发,没必要让 mars 把它们记成"结果未知"。

**一次只领 1 条**(`MARS_TASKS_MAX` 默认 1)。执行是串行的,一次领 5 条会让队尾那条
在还没轮到它时租约就到期 —— 然后被 mars 放回队列、被下一轮的自己再领一次,
于是同一条任务在本机排了两遍队。领一条做一条就没有这个类别的问题。

### 5.2 动作白名单(默认只开应用身份)

`WECOM_SVR_MARS_TASKS_ACTIONS` 默认 `app.message.send`。

不带 `app.` 前缀的四条(`message.send` / `message.recall` / `group.create` /
`group.rename`)是**以你本人的身份说话**,让一个远端队列能驱动它,权限边界比
"应用发通知"大一个量级。要开必须显式写进白名单。不在白名单里的 action 直接 ack
失败(`retry=false`),不执行。

payload 用**和 HTTP 路由同一套 pydantic schema** 校验(`SendMessageRequest`、
`LocalSendRequest`、`LocalRecallTaskRequest`…)。撤回任务在 HTTP 模型的基础上要求
显式提供 `lid`,因为异步任务不能依赖“最新一条消息”的动态选择。同一套校验只有一份,
队列这条路不会悄悄比 HTTP 那条松。

### 5.3 崩溃恢复:只落一次盘

**拿到结果才落盘。** 这个文件的用途只有一个:**结果已经有了,ack 还没上去** ——
重启时原样补报,不重新执行。

动手途中本机**什么都不记**。以前这里记两次(`phase=leased` / `phase=started`),
为的是重启时分辨"还没动手"和"崩在半路";但 mars 的 reaper 早就知道答案了:
`executing` + 租约超时 → failed「结果未知」,`leased` + 租约超时 → 回 pending 安全
重投(§4.4 第 1、3 步)。本机再记一遍是冗余的,而**每一次多余的落盘都是一份能写坏、
能被并发读岔的状态**。

代价只有一个:崩在执行途中的失败原因要等租约到期由 reaper 写,在 `GET /tasks` 上
晚一个租约周期出现。结论完全一样。

落盘必须是**原子**的:临时文件 + `fsync` + `os.replace` + 目录 `fsync`。直接
`write_text` 是"先清空再写",崩在中间会留下半截 JSON,而读不动就返回空状态 ——
那条任务的结果就白丢了,要等 reaper 兜底。这个文件存在的唯一目的就是熬过崩溃。

拿到 409(令牌失效)时:**丢掉这条任务**,既不重发也不反复重试。它已经属于另一次
租约了,再插手只会两边都错。

### 5.4 客户端忙

`ClientBusy` 不算失败:ack `ok=false, retry=true`,mars 把它放回 pending 且
**回补 attempts**(claim 时 +1 过)。这不是任务本身的问题,不该消耗重试次数。

**它只可能发生在 `/start` 之前**(§5.1 的 `ui_reserve`)。这不是巧合,是这条路能
存在的前提:`retry=true` 是协议里唯一一条"放回队列"的指令,而只有在 `leased` 阶段
才能证明什么都没发生。真在 `executing` 阶段抢不到锁,说明预占被绕过了 —— 那时
按已发送处理(`retry=false`),并记一条 error 级日志。

退回队列的任务**立刻就能再领**,而这一轮因为拉到了东西不会阻塞在长轮询上 ——
不管的话就是以网络速度空转。所以后台循环看 `RunResult.busy`:这一轮有任务因忙退回
就歇 `MARS_TASKS_IDLE` 秒。剩下的"队首挡着后面"还没治,见 §10.3。

### 5.5 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WECOM_SVR_MARS_BASE_URL` | 空 | 和群同步共用 |
| `WECOM_SVR_MARS_TASKS_TOKEN` | 空 | **留空 = 不拉**。整个功能的总开关 |
| `WECOM_SVR_MARS_TASKS_ENABLED` | false | 随服务启动常驻 |
| `WECOM_SVR_MARS_TASKS_WORKER` | 空 → hostname | 认领用的 worker 名 |
| `WECOM_SVR_MARS_TASKS_ACTIONS` | `app.message.send` | 白名单,逗号分隔;`*` 全开 |
| `WECOM_SVR_MARS_TASKS_WAIT` | 25 | 长轮询秒数(上限 50) |
| `WECOM_SVR_MARS_TASKS_MAX` | 1 | 一次认领几条。见 §5.1 为什么是 1 |
| `WECOM_SVR_MARS_TASKS_LEASE` | 120 | 租约秒数,要盖住最慢的操作(拉群几十秒) |
| `WECOM_SVR_MARS_TASKS_IDLE` | 3 | 出错、或有任务因界面忙退回后歇多久;正常轮次不 sleep |

HTTP 超时由代码算(`wait + mars_timeout`),不给单独的旋钮 —— 配错了表现是
"每 25s 一次超时报错",很难查。

## 6. 失败模式

| 情况 | 结果 |
| --- | --- |
| Mac 关机 | 任务积压在 pending,**不会半夜补发一堆**(`Claim` 自带 `expire_at > now`)。但状态要等下次有人 pull 才被 reaper 改成 expired,期间 `GET /tasks` 显示的是滞后的,见 §4.4 |
| mars 挂了 / 网断 | worker 拉不到,循环记 `last_error` 继续重试,不退出 |
| 崩在 `/start` 之前 | 什么都没发生 → 回队列重做 |
| 崩在执行途中 | 判 failed「结果未知」,**不重投**;mars 侧 `executing` 保证它不会被 reaper 放回来 |
| 崩在 ack 之前 | 结果已落盘,下轮补报,不重复执行 |
| 本机卡过租约后醒来 | start/ack 拿 409,丢掉这条任务(它已属于另一次租约) |
| 企微没登录 / 本地库没解密 | ack 失败 `retry=false`,error 原样带回 mars,`GET /tasks` 里看得见 |
| 同一 worker 名跑了两个实例 | 令牌保证不会互相结案,但顺序不保证,且两个进程会抢同一个状态文件。别这么干 |
| 后台循环开着时又手动 `/run` | `run_once` 全程互斥,且循环在跑时 `/run` 直接回 409 |
| 任务堆积 | `GET /tasks?status=0` 能看见;没有自动丢弃,靠 `expire_at` |

## 7. 上线步骤

1. **mars 侧合并,`mars.json` 配 `wecom_task_token`。** 没配这个 token,整条路直接 503。

2. **建表。** `InitWecomTaskTable()` 跟着 `httpbot.InitHTTPBotTable()` 在 `app.go` 里
   自动跑,但别指望它:**AutoMigrate 只补缺表/缺列/缺索引,不改列类型、不删同名的
   错索引**,而且 4 节点同时启动是 check-then-DDL 的竞态(顶多一个节点报错,不会
   写坏数据)。首次上线执行一次性 DDL:

   ```sql
   CREATE TABLE wecom_tasks (
     id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
     worker VARCHAR(64) NOT NULL,
     action VARCHAR(64) NOT NULL,
     payload TEXT NOT NULL,
     dedup_key VARCHAR(128) NULL,
     status TINYINT NOT NULL DEFAULT 0,
     attempts INT NOT NULL DEFAULT 0,
     max_attempts INT NOT NULL DEFAULT 3,
     lease_owner VARCHAR(96) NULL,
     lease_until DATETIME NULL,
     started_at DATETIME NULL,
     expire_at DATETIME NULL,
     result TEXT NULL,
     error TEXT NULL,
     created_by VARCHAR(64) NOT NULL DEFAULT '',
     created_at DATETIME NOT NULL,
     updated_at DATETIME NOT NULL,
     PRIMARY KEY (id),
     UNIQUE KEY uix_wecom_dedup (worker, dedup_key, created_by),
     KEY idx_wecom_claim (worker, status, id),
     KEY idx_wecom_reap (status, lease_until),
     KEY idx_wecom_expire (status, expire_at)
   ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
   ```

3. **老表升级:必须手工换掉 dedup 的唯一键。** 老表上是单列的
   `uix_wecom_tasks_dedup_key (dedup_key)`,AutoMigrate 不会删它 —— 留着,唯一性就
   还是全局的,§4.1 第 4 条那个 dedup 保证一条都没兑现。

   ```sql
   -- 先确认没有跨命名空间的重复 key（有的话得先人工决定留哪条）
   SELECT worker, created_by, dedup_key, COUNT(*) c
     FROM wecom_tasks WHERE dedup_key IS NOT NULL
    GROUP BY worker, created_by, dedup_key HAVING c > 1;

   ALTER TABLE wecom_tasks
     DROP INDEX uix_wecom_tasks_dedup_key,
     ADD UNIQUE KEY uix_wecom_dedup (worker, dedup_key, created_by);
   ```

4. **核对。** `SHOW CREATE TABLE wecom_tasks`,确认三个 `idx_wecom_*` 都在,且唯一键
   **只剩** `uix_wecom_dedup` 一个。

   启动时 `InitWecomTaskTable` 也会自己校验一遍,**没过就把整条路关掉**(所有
   `/api/internal/wecom/*` 回 503)。缺索引的后果不是报错而是悄悄退化,所以宁可
   整条路不可用,也不要带病服务。性能索引缺了只吼日志,不停用。

   关掉之后**不需要重启才能恢复**:503 状态下 `Ready()` 会限流重查(最多 30s 一次,
   只读不补 DDL),表建好了下一次请求就自动放行。这条是给启动竞态留的 ——
   4 个节点同时 `AutoMigrate` 是 check-then-DDL,输掉的那个启动时确实会失败,
   而那时表其实已经被别人建好了,拿一次几毫秒的竞态换一台机器长期 503 不划算。

5. **Mac 侧配 token**,白名单只留 `app.message.send`,`POST /api/v1/mars/tasks/run`
   手动跑一轮验证。**此时后台循环必须是停的**
   (`GET /api/v1/mars/tasks/status` 看 `running`)—— 循环开着时 `/run` 会直接回 409。

6. 确认无误后开 `MARS_TASKS_ENABLED=true` 常驻。之后就别再手动 `/run` 了。

7. 需要"以本人身份"发时,再把对应 action 加进白名单(§5.2)。

回滚:Mac 侧清掉 token 即可(任务留在库里,不执行);mars 侧路由摘掉,表留着。

## 8. 明确不做

- 不做 WebSocket / SSE(§1.1)。
- 不把这张表挪出 mars 的 MySQL(§1.3)。
- 不做多 worker 抢同一队列。要多台机器就用不同 worker 名。
- 不做优先级、不做延时投递。要定时就在 mars 侧 cron 里到点 enqueue。
- 不做同节点 channel 唤醒:入队多半落在别的节点上,那层缓存换不来什么。
- 不动 `qq/hook/mission` 那条老路。

## 9. 上线前仍需实测的

设计和单测覆盖不了的几件事,别当成已经验证过:

1. `SELECT VERSION(), @@binlog_format, @@tx_isolation;` —— 上面的锁与复制结论
   建立在 InnoDB + ROW/MIXED 之上。
2. xs4797 / yzh1410 的 openresty 实际 `proxy_read_timeout`(文档里的 60s 是假设)。
3. 崩溃点故障注入:在 `/start` 前后、执行途中、outcome 落盘写中、ack 提交后响应
   丢失这几个点分别 `SIGKILL`,确认最终**最多发一次**。注意执行途中崩溃现在没有
   本地记录,靠 mars 的 reaper 兜(§5.3),所以这一条要等租约到期才看得到结果。
4. 表长到十万/百万行后 claim 的 `EXPLAIN`,确认走的是 `idx_wecom_claim`。
5. 故意删掉 `uix_wecom_dedup` 重启一次,确认这条路整个回 503 而不是带病提供服务
   (§7 第 4 步的校验);再把索引加回来,确认**不重启**也能在 30s 内自动恢复。

## 10. 已知缺陷

三条都**不影响正确性**,也都是单 worker、低吞吐下基本踩不到的。留在这里当已知边界。

### 10.1 回收依赖 worker 来 pull

见 §4.4 末尾。影响的是 `GET /tasks` 的实时性,不是对错 —— `Claim` 自带
`expire_at > now`,过期任务醒来也领不走。要修就在 mars 侧起个独立 reaper,
pull 里那次留着当补充。

### 10.2 租约从 claim 开始计时,且 `/start` 不续期

`Start` 只改 `status`/`started_at`,不动 `lease_until`。所以 120s 的租约里,
claim→start 的往返和执行时间是共用的;真做一个几十秒的拉群操作,余量比看上去少。

单 worker 串行时踩不到:执行期间没人 pull,也就没人跑 reaper 去判它超时。要更稳
就在 `/start` 里重设一个独立的执行 deadline,或者加个带令牌的续租接口。

### 10.3 `ClientBusy` 的退避只做了一半

Mac 侧"这一轮有任务因忙退回就歇 `MARS_TASKS_IDLE` 秒"已经止住了空转(§5.4),
但**队首那条仍然挡着后面**:界面被占的那几十秒里,连根本不碰界面的
`app.message.send` 也跟着等。要治得在 mars 侧加 `available_at`,`Claim` 的条件里
带上 `available_at <= now`。

没做是因为这是单 worker、`max=1`、低吞吐,饿死窗口就是界面被占的那几十秒。
等真看到 `app.message.send` 被拖了再加字段。
