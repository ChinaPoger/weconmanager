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

## 1. 为什么不是 WebSocket

记下来,免得半年后有人重新提这个问题。

| | WebSocket / SSE | 任务队列 + 长轮询 |
| --- | --- | --- |
| mars 侧改动 | hub + 心跳 + 重连 + 背压,还要改 xs4797/yzh1410 的 openresty 放 Upgrade、调长超时 | 一张表 + 4 个接口 |
| 4 节点 | 连接只在一个节点上,其余节点要 Redis pubsub 才能推过去 | 共享 MySQL,天然可用 |
| Mac 休眠/重启 | 断连期间的命令直接丢 | 任务留在表里,醒来接着做 |
| 排障 | 要看连接状态 | `select * from wecom_tasks` |
| 实时性 | 亚秒 | 长轮询下**同样是亚秒**(有活立刻返回) |

WebSocket 唯一真正的优势是"mars 主动说话"的延迟,而长轮询在有活时是立刻返回的,
这点优势并不存在。它换来的是一堆有状态的东西。

另一条被否掉的是**内网隧道**(frp / tailscale,让 mars 直接 HTTP 调 Mac):
mars 侧几乎零改动,是最干净的,但这台 Mac 进内网只有跳板机一条路,而跳板机
**禁止端口转发**。除非愿意在某个生产节点上常驻 frps 并开端口,否则走不通。

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
mars 业务代码 ──wecomtask.Enqueue()──┐
                                     ├──► wecom_tasks (MySQL)
外部调用 ──POST /tasks───────────────┘      ▲    │
                                            │    │ ① claim(拿到 lease_token)
                              ③ ack ────────┤    ▼
                              ② start ──────┤  ┌─────────────────────┐
                                            └──┤  Mac: wecom-svr     │──► 企微客户端
                                  长轮询 ◄─────┤  mars_tasks 循环    │──► 官方 API
                                    (25s)      └─────────────────────┘
```

一句话:**mars 只负责把"要发什么"落库,Mac 自己来取、报告开工、执行、回报。**

那个 ② `start` 看着多余,其实是整套设计的支点 —— 见 §4.2。

## 4. mars 侧

### 4.1 表 `wecom_tasks`

```go
type WecomTask struct {
    ID        uint      `gorm:"primary_key"`      // 注意是 primary_key:仓库用的是 2016 年的 GORM v1
    CreatedAt time.Time
    UpdatedAt time.Time

    Worker    string  `gorm:"column:worker;size:64;not null;index"`   // 哪台机器来做
    Action    string  `gorm:"column:action;size:64;not null"`         // 见 §4.5
    Payload   string  `gorm:"column:payload;type:text"`               // JSON,原样透传
    DedupKey  *string `gorm:"column:dedup_key;size:128;unique_index"`  // 可空;同 key 只入队一次

    Status      int    `gorm:"column:status;not null;index"`
    Attempts    int    `gorm:"column:attempts;not null"`
    MaxAttempts int    `gorm:"column:max_attempts;not null;default:3"`
    LeaseOwner  string `gorm:"column:lease_owner;size:96;index"`       // "<worker>:<128bit nonce>"
    LeaseUntil  *time.Time `gorm:"column:lease_until"`
    StartedAt   *time.Time `gorm:"column:started_at"`                  // 真正动手的时刻
    ExpireAt    *time.Time `gorm:"column:expire_at;index"`             // 过了就别发了

    Result    string `gorm:"column:result;type:text"`
    Error     string `gorm:"column:error;type:text"`
    CreatedBy string `gorm:"column:created_by;size:64"`
}
```

三个非写不可的细节:

- **被索引的 string 必须写 `size:`。** 这个 GORM 版本(`5174cc5c`,2016)对没有
  `size` 的 string 生成 `longtext`,然后无前缀建索引会失败 —— 表建出来了但关键
  索引没有,而 AutoMigrate 不会报到你脸上。
- **标签是 `primary_key` 不是 `primaryKey`。** 后者在这个版本里根本不被识别
  (本例因为字段名恰好是 `ID` 会被约定兜底,但标签是无效的)。
- **`DedupKey` 必须是 `*string`。** MySQL 唯一索引允许多个 `NULL`,但不允许多个
  `''`。用值类型的话,第二条不带 dedup_key 的任务就会撞唯一索引失败。

### 4.2 状态机 —— `executing` 是重点

```
        enqueue
           │
           ▼
    0 pending ──claim──► 1 leased ──start──► 5 executing ──ack ok──► 2 done
        ▲                   │   │                  │
        │                   │   └──ack retry───────┼──► 回 0 pending(attempts 回补)
        │                   │                      │
        │   租约超时且 ──────┘                      └──ack 失败 ─► 3 failed
        └── attempts<max                            │
                                租约超时 ───────────┘─► 3 failed「结果未知,不重投」

                            超过 expire_at(仅 0/1)─► 4 expired
```

`1 leased` 和 `5 executing` 的区别就是**这套设计能不能兑现"宁可漏发"**:

- 只有 `leased`(领了还没动手)的租约超时才重投 —— 那时什么都还没发生,重投是安全的。
- `executing` 的租约超时**绝不重投**,判 `failed` 并写明 "outcome unknown"。
  mars 不知道那条消息到底出没出去,重投就是赌"没出去",赌输了是对方屏幕上多一条。

**少了这个状态,Mac 侧再怎么记都拦不住重发** —— 本机记着"我发过了",但 mars 那边
租约一到期就把任务放回队列,下一轮本机自己又领回来了。这是评审里最值钱的一条。

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

时间由应用侧传入(`Reap(now)` / `Claim(..., now)`)而不是用 `UTC_TIMESTAMP()`。
代价是依赖 4 个节点的 NTP 同步(毫秒级偏差 vs 120s 租约,不构成问题),
换来的是测试可以随意穿越时间 —— `wecomtask_test.go` 里那些"一小时后再来"
就是这么写的。

### 4.5 接口

全部挂在 `router.Group("/api/internal/wecom", WecomTaskAuth)` 下。

| 接口 | 用途 |
| --- | --- |
| `POST /tasks` | 入队。带 `dedup_key` 且已存在则返回原任务 + `duplicated:true` |
| `POST /tasks/pull` | 认领。`{"worker","max","wait","lease"}`;`wait` 秒内没活返回空数组 **200** |
| `POST /tasks/:id/start` | 开工。`{"lease_token"}`;令牌过期回 **409** |
| `POST /tasks/:id/ack` | 回报。`{"ok","retry","error","result","lease_token"}`;令牌过期回 **409** |
| `GET  /tasks` | 给人看的列表,按 worker/status 过滤 |

空队列必须是 200 而不是 400 —— 这条路一天要被敲几万次,用错误码表达"没活"
会让真正的故障淹在噪声里(`qq/hook/mission` 那条老路就是这么设计的)。

入队 body:

```json
{
  "worker": "mac-cc",
  "action": "local.message.send",
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

| action | 落到 wecom-svr 的哪里 | 身份 |
| --- | --- | --- |
| `message.send` | `POST /api/v1/message/send` | 自建应用 |
| `local.message.send` | `POST /api/v1/local/message/send` | **本人** |
| `local.image.send` | `POST /api/v1/local/image/send` | **本人** |
| `local.group.create` | `POST /api/v1/local/group/create` | **本人** |
| `local.group.rename` | `POST /api/v1/local/group/rename` | **本人** |

`payload` 就是对应接口的 body,原样透传。mars 不重新建模这些字段 —— 建了就会漂。

### 4.7 建表

`InitWecomTaskTable()` 跟着 `httpbot.InitHTTPBotTable()` 在 `app.go` 里调,
AutoMigrate + 补两个复合索引。但要清楚它的边界:**AutoMigrate 只补缺表/缺列/缺索引,
不改列类型、不删同名的错索引**,而且 4 节点同时启动是 check-then-DDL 的竞态
(顶多一个节点报错,不会写坏数据)。首次上线或改结构时,优先执行一次性 DDL:

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
  UNIQUE KEY uix_wecom_tasks_dedup_key (dedup_key),
  KEY idx_wecom_claim (worker, status, id),
  KEY idx_wecom_reap (status, lease_until),
  KEY idx_wecom_expire (status, expire_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

建完 `SHOW CREATE TABLE wecom_tasks` 核一遍索引真的在。

## 5. Mac 侧(wecom-svr)

### 5.1 循环

`wecom_svr/mars_tasks.py`,骨架照 `mars_sync.py`:后台 `asyncio.Task`、`.data/` 状态
文件、`/start` `/stop` `/status` 三件套、循环内吞异常不死。

```
pull(wait=25) ──空──► 立刻再 pull
      │
      └─有活─► 逐条: precheck → 落 inflight → /start → 执行 → 写结果 → /ack → 清 inflight
```

**串行执行**,不并发:`local.*` 要独占客户端界面(抢焦点、剪贴板、键盘),
`uilock` 本来就会把并发变成 409。

`precheck`(过期 / 不认识的 action / 不在白名单 / payload 不合法)刻意排在 `/start`
**之前**:这些任务一条消息都不会发,没必要让 mars 把它们记成"结果未知"。

**一次只领 1 条**(`MARS_TASKS_MAX` 默认 1)。执行是串行的,一次领 5 条会让队尾那条
在还没轮到它时租约就到期 —— 然后被 mars 放回队列、被下一轮的自己再领一次,
于是同一条任务在本机排了两遍队。领一条做一条就没有这个类别的问题。

### 5.2 动作白名单(默认只开应用身份)

`WECOM_SVR_MARS_TASKS_ACTIONS` 默认 `message.send`。

`local.*` 是**以你本人的身份说话**,让一个远端队列能驱动它,权限边界比"应用发通知"
大一个量级。要开必须显式写进白名单。不在白名单里的 action 直接 ack 失败
(`retry=false`),不执行。

payload 用**和 HTTP 路由同一套 pydantic schema** 校验(`SendMessageRequest`、
`LocalSendRequest`…)。同一套校验只有一份,队列这条路不会悄悄比 HTTP 那条松。

### 5.3 崩溃恢复:inflight 的三种 phase

执行前把 inflight 落盘,执行完写上 `outcome`,ack 成功后清掉。重启时按 phase 结案:

| inflight 状态 | 含义 | 怎么结 |
| --- | --- | --- |
| 有 `outcome` | 做完了,只是 ack 没上去 | 原样补报 |
| `phase=started`,无 outcome | **崩在执行途中,不知道发出去没有** | `ok=false, retry=false`,写明"按已发送处理" |
| `phase=leased`,无 outcome | 还没动手就崩了,什么都没发生 | `ok=false, retry=true`,让它回队列 |

分这两种 phase 就是"宁可漏发"和"别白丢任务"之间那条线:**只有真的可能已经产生
副作用的才认赔**。而 mars 侧的 `executing` 状态保证了认赔之后它不会被重投回来。

落盘必须是**原子**的:临时文件 + `fsync` + `os.replace` + 目录 `fsync`。直接
`write_text` 是"先清空再写",崩在中间会留下半截 JSON,而读不动就返回空状态 ——
inflight 没了,那条任务会被重新执行一遍。这个文件存在的唯一目的就是熬过崩溃。

拿到 409(令牌失效)时:**丢掉这条任务**,既不重发也不反复重试。它已经属于另一次
租约了,再插手只会两边都错。

### 5.4 客户端忙

`ClientBusy` 不算失败:ack `ok=false, retry=true`,mars 把它放回 pending 且
**回补 attempts**(claim 时 +1 过)。这不是任务本身的问题,不该消耗重试次数。

### 5.5 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WECOM_SVR_MARS_BASE_URL` | 空 | 和群同步共用 |
| `WECOM_SVR_MARS_TASKS_TOKEN` | 空 | **留空 = 不拉**。整个功能的总开关 |
| `WECOM_SVR_MARS_TASKS_ENABLED` | false | 随服务启动常驻 |
| `WECOM_SVR_MARS_TASKS_WORKER` | 空 → hostname | 认领用的 worker 名 |
| `WECOM_SVR_MARS_TASKS_ACTIONS` | `message.send` | 白名单,逗号分隔;`*` 全开 |
| `WECOM_SVR_MARS_TASKS_WAIT` | 25 | 长轮询秒数(上限 50) |
| `WECOM_SVR_MARS_TASKS_MAX` | 1 | 一次认领几条。见 §5.1 为什么是 1 |
| `WECOM_SVR_MARS_TASKS_LEASE` | 120 | 租约秒数,要盖住最慢的操作(拉群几十秒) |
| `WECOM_SVR_MARS_TASKS_IDLE` | 3 | 出错后歇多久再拉;正常轮次不 sleep |

HTTP 超时由代码算(`wait + mars_timeout`),不给单独的旋钮 —— 配错了表现是
"每 25s 一次超时报错",很难查。

## 6. 失败模式

| 情况 | 结果 |
| --- | --- |
| Mac 关机 | 任务积压在 pending;过了 `expire_at` 自动判 expired,**不会半夜补发一堆** |
| mars 挂了 / 网断 | worker 拉不到,循环记 `last_error` 继续重试,不退出 |
| 崩在 `/start` 之前 | 什么都没发生 → 回队列重做 |
| 崩在执行途中 | 判 failed「结果未知」,**不重投**;mars 侧 `executing` 保证它不会被 reaper 放回来 |
| 崩在 ack 之前 | 结果已落盘,下轮补报,不重复执行 |
| 本机卡过租约后醒来 | start/ack 拿 409,丢掉这条任务(它已属于另一次租约) |
| 企微没登录 / 本地库没解密 | ack 失败 `retry=false`,error 原样带回 mars,`GET /tasks` 里看得见 |
| 同一 worker 名跑了两个实例 | 令牌保证不会互相结案,但顺序不保证,且两个进程会抢同一个状态文件。别这么干 |
| 任务堆积 | `GET /tasks?status=0` 能看见;没有自动丢弃,靠 `expire_at` |

## 7. 上线顺序

1. mars 侧合并 → mars.json 配 `wecom_task_token` → 建表(优先用 §4.7 的 DDL),
   `SHOW CREATE TABLE` 核对索引。
2. Mac 侧配 token,白名单只留 `message.send`,`POST /api/v1/mars/tasks/run` 手动跑一轮验证。
3. 确认无误后开 `MARS_TASKS_ENABLED=true` 常驻。
4. 需要"以本人身份"发时,再把对应 action 加进白名单。

回滚:Mac 侧清掉 token 即可(任务留在库里,不执行);mars 侧路由摘掉,表留着。

## 8. 明确不做

- 不做 WebSocket / SSE(§1)。
- 不做多 worker 抢同一队列。要多台机器就用不同 worker 名。
- 不做优先级、不做延时投递。要定时就在 mars 侧 cron 里到点 enqueue。
- 不做同节点 channel 唤醒:入队多半落在别的节点上,那层缓存换不来什么。
- 不动 `qq/hook/mission` 那条老路。

## 9. 上线前仍需实测的

设计和单测覆盖不了的几件事,别当成已经验证过:

1. `SELECT VERSION(), @@binlog_format, @@tx_isolation;` —— 上面的锁与复制结论
   建立在 InnoDB + ROW/MIXED 之上。
2. xs4797 / yzh1410 的 openresty 实际 `proxy_read_timeout`(文档里的 60s 是假设)。
3. 崩溃点故障注入:在 inflight 写前/写中、`/start` 前后、发送后 outcome 写中、
   ack 提交后响应丢失这几个点分别 `SIGKILL`,确认最终**最多发一次**。
4. 表长到十万/百万行后 claim 的 `EXPLAIN`,确认走的是 `idx_wecom_claim`。
