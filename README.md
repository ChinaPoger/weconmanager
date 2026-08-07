# wecom-svr

[![CI](https://github.com/rwifeng/wecom-svr/actions/workflows/ci.yml/badge.svg)](https://github.com/rwifeng/wecom-svr/actions/workflows/ci.yml)
[![coverage](https://img.shields.io/endpoint?url=https%3A%2F%2Fgist.githubusercontent.com%2Frwifeng%2F3b34e6b1df2227d192e646fbf8949b01%2Fraw%2Fcoverage.json)](https://github.com/rwifeng/wecom-svr/actions/workflows/ci.yml)

企业微信消息发送服务(FastAPI)。本仓库只做服务层封装:HTTP 接口、参数校验、鉴权、错误语义。

两套发消息互补:

| 接口 | 身份 | 实现 |
| --- | --- | --- |
| `POST /api/v1/message/send` | **自建应用** —— 对方看到的是「应用」 | 调 wecom-agent 的 `wecom.py`(官方 API) |
| `POST /api/v1/local/message/send` | **本人登录账号** —— 对方看到的是你 | 读解密数据库 + AppleScript 操控 macOS 客户端 |

另有在线文档/表格/智能表接口(`/api/v1/doc|sheet|smartsheet/*`),
封装上游 `online/` 包 —— 见[文档与表格](#文档与表格)。

反向还有一条:把本地群聊消息准实时同步给 mars 的群消息接口
(`/api/v1/mars/sync/*`)—— 见[群消息同步到 mars](#群消息同步到-mars)。

## 依赖的 pkg:wecom-agent

`vendor/wecom-agent` 是锁定 commit 的 git submodule。运行时按以下优先级解析:

1. `$WECOM_AGENT_HOME` —— 指向本地 checkout(**通常需要这个**:已解密的数据库和 `config.json` 在那里)
2. `vendor/wecom-agent` —— submodule 固定版本

两种目录布局都支持(上游 `legacy/self-built-app/wecom.py` 与旧版根目录 `wecom.py`)。

```bash
git clone <this-repo> && cd wecom-svr
git submodule update --init      # 拉取 vendor/wecom-agent
```

### 跟随上游更新

submodule 跟踪上游 `main` 分支,但**不自动更新** —— pin 住 commit,显式升级:

```bash
scripts/update-agent.sh --check    # 只看上游有什么新 commit
scripts/update-agent.sh            # 更新 + 跑测试(不提交)
scripts/update-agent.sh --commit   # 更新 + 跑测试,通过则提交新 pin
```

上游动过一次目录结构(`wecom.py` → `legacy/self-built-app/wecom.py`),
所以 `tests/test_vendor_contract.py` 锁住了本服务真正依赖的那部分契约:
`wecom.py` 的位置、`HANDLERS` 里的 `contact`/`message`、
`msgtype` → handler method 的映射、以及 `decrypt/macos/read_wecom.py` 的存在。
上游再改结构时,更新脚本会当场测试失败并提示回滚,而不是等到线上发消息才发现。

## 安装与启动

```bash
uv venv && uv pip install -e '.[dev]'
cp .env.example .env             # 填 WECOM_AGENT_HOME 等

wecom-svr                        # 或 python -m wecom_svr
wecom-svr --port 8090 --reload
```

启动后 OpenAPI 文档在 <http://127.0.0.1:8090/docs>。

## 配置

优先级:环境变量 / `.env` > `$WECOM_AGENT_HOME/config.json` > 默认值。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WECOM_AGENT_HOME` | `vendor/wecom-agent` | wecom-agent checkout 路径 |
| `WECOM_SVR_HOST` / `WECOM_SVR_PORT` | `127.0.0.1` / `8090` | 监听地址 |
| `WECOM_SVR_API_TOKEN` | 空 | 留空则**不鉴权**(仅本地,勿暴露公网) |
| `WECOM_CORPID` / `WECOM_SECRET` / `WECOM_AGENTID` | 空 | 自建应用凭证,留空回落到 `config.json` |
| `WECOM_SVR_MARS_*` | 见下 | 群消息同步到 mars,默认全关 |

## 接口

```bash
# 以应用身份发消息(官方 API)
curl -X POST http://127.0.0.1:8090/api/v1/message/send \
  -H 'Content-Type: application/json' \
  -d '{"touser":"zhangsan","msgtype":"text","content":"你好"}'

# 以本人身份发消息(操控客户端)
curl -X POST http://127.0.0.1:8090/api/v1/local/message/send \
  -H 'Content-Type: application/json' \
  -d '{"conv_rid":3395103030,"content":"你好,我是本人"}'

# 查本地会话拿 RID
curl -X POST http://127.0.0.1:8090/api/v1/local/conversations \
  -H 'Content-Type: application/json' -d '{"keyword":"notify"}'

# 发图片(image_path 或 image_base64 二选一)
curl -X POST http://127.0.0.1:8090/api/v1/local/image/send \
  -H 'Content-Type: application/json' \
  -d '{"conv_rid":3395103030,"image_path":"/tmp/a.png"}'

# 查通讯录
curl -X POST http://127.0.0.1:8090/api/v1/contact/search \
  -H 'Content-Type: application/json' -d '{"keyword":"张三"}'

# 配了 api_token 时
curl ... -H 'Authorization: Bearer <token>'
```

完整端点:

- `GET  /health` —— 健康检查(公开,含 agent home 解析状态)
- `POST /api/v1/message/send` —— 应用身份发消息,`msgtype`: `text` / `markdown` / `news`
- `POST /api/v1/local/message/send` —— 本人身份发消息(`to` 或 `conv_rid`)
- `POST /api/v1/local/image/send` —— 本人身份发图片(需 `conv_rid`)
- `POST /api/v1/local/conversations` —— 查本地会话(body 可省略)
- `POST /api/v1/local/conversation/open` —— 只打开会话不发送
- `POST /api/v1/local/group/create` —— 本人身份拉群(操控客户端,能拉外部/微信)
- `POST /api/v1/group/create` —— 应用身份拉群(官方 appchat,**仅限本企业成员**)
- `POST /api/v1/group/members/resolve` —— 干跑:只看名字解析成谁,不建群
- `POST /api/v1/group/{get,update,send}` —— 查群 / 改群名换群主加人踢人 / 往群里发消息
- `POST /api/v1/contact/search` —— 查通讯录

## 拉群

两条路,能拉的人不一样 —— **这不是实现偏好,是企业微信的硬边界**:

| 接口 | 身份 | 能拉谁 | 有回执 |
| --- | --- | --- | --- |
| `/api/v1/group/create` | 自建应用 | **只有本企业成员** | ✅ 返回 `chatid` |
| `/api/v1/local/group/create` | 你本人 | 同事 + 外部联系人 + 微信用户 | ❌ 见下 |

```bash
# 应用身份(官方 API):成员写中文名/userid/邮箱/「我」都行,离线解析本地通讯录
curl -X POST http://127.0.0.1:8090/api/v1/group/create \
  -H 'Content-Type: application/json' \
  -d '{"name":"临时讨论组","members":["陈涛","陈挺"],"include_self":true}'

# 建群前先干跑,核对名字被解析成了谁
curl -X POST http://127.0.0.1:8090/api/v1/group/members/resolve \
  -H 'Content-Type: application/json' -d '{"members":["陈涛","陈挺"]}'

# 本人身份(操控客户端):自己自动是群主,不用写进 members
curl -X POST http://127.0.0.1:8090/api/v1/local/group/create \
  -H 'Content-Type: application/json' \
  -d '{"members":["陈涛","陈挺"],"name":"临时讨论组"}'
```

### 为什么外部成员只能走 UI(实测记录)

想拉个外部微信的人进群,官方路子全是堵的。四条都实测过:

| 路线 | 结果 |
| --- | --- |
| `appchat/create` | `userlist` 只收本企业 userid;外部人的 `external_userid` 塞不进去 |
| 创建客户群的官方接口 | **不存在**。客户群相关只有只读的 `externalcontact/groupchat/list\|get` |
| `wxwork://jump?target=openchat` | 客户端二进制里写死 `not tencent company, ignore`,只对腾讯自家企业开放 |
| JSAPI `openEnterpriseChat` | `jsapi_ticket` 拿得到,但传 `externalUserIds` 需要 `external_userid`,而 `externalcontact/*` 返回 `48002 api forbidden` —— 应用没「外部联系人」权限 |

要解锁官方路线,前置是**管理后台给自建应用开「外部联系人」权限**(JSAPI 还要配可信域名),
这是管理员操作,代码绕不过去。所以外部成员就只剩 `local/group/create` 这一条。

### 本人身份拉群怎么保证不出错

选人不是盲点坐标:面板是主窗口上的 `AXSheet`,搜索框是真 `AXTextField`,结果行能读出
姓名和部门,右侧「N contact(s) selected」能回读 —— **每选一个人都校验计数涨了 1,
对不上就中止并关掉面板,绝不硬按「完成」**。

回车偶尔会落空(焦点被别的窗口抢走),所以计数没涨时最多重试 `SELECT_ATTEMPTS` 次。
两个细节不能省:

- **重试前先等**(`COUNT_SETTLE_TRIES`)。「计数没涨」也可能只是界面还没刷新到,
  这时直接重按回车会把**已经选上的人取消掉** —— 把一次小抖动变成错误的成员名单。
- **计数超出预期就直接停**,不靠再点几下去凑。多选人比少选危险得多。

`name` 是「先建群、再改名」两步(面板里没有命名输入框):建完用建群前后的群 RID 差集
定位新群,打开它、点标题把标题变成输入框、覆写、回车,**最后回库核验那个 RID 的群名
真的变了**。改名失败不回滚群 —— 群已经建出来了,回滚更吓人,接口会在 `name_error`
里说清楚让你手动改。

两个关键的实测结论,踩过才知道:

- **合成点击对 CEF 无效**。`System Events` 的 `click at` 点得动原生控件(会话列表顶部
  的 `+`),但点不动会话标题和成员勾选框 —— 那些是 CEF 自绘的。真实点击靠 JXA 的
  ObjC 桥直接调 CoreGraphics 发 `CGEvent`,零新增依赖,还是同一个 `osascript`。
- **客户端是和人共用的**,请求发出到点击之间会话完全可能被你自己切走。所以改名前
  会先用哨兵值占住剪贴板再全选复制:剪贴板没变 = 没有输入框拿到焦点,直接中止 ——
  否则 `Cmd+V` + 回车会把群名当成一条消息发进别人的聊天里。

剩下的边界:

- **除自己外至少 2 人** —— 2 个人不成群,客户端会开成单聊
- **名字必须唯一**:面板里「搜索框按回车 = 选中第一条」,所以搜出多条会直接
  `400` 并列出候选,不替你猜。搜「陈」会撞上 29 个人,写全名或工号
- **没有 chatid** —— 官方接口不认这种群;但 RID 拿得到(靠 `wal.py` 读未落盘的 WAL)
- 全程占用客户端界面(会把窗口顶到前台),和发消息共用同一把 UI 锁,请求串行

## 文档与表格

封装上游 `online/` 包(文档 / 表格 / 智能表),共 3 组 21 个端点:

| 前缀 | 内容 |
| --- | --- |
| `/api/v1/doc/*` | `create` `write` `smartpage` `capabilities` `local/search` `local/read` |
| `/api/v1/sheet/*` | `create` `info` `subsheet/add` `subsheet/delete` `row/append` `range/update` |
| `/api/v1/smartsheet/*` | `create` `sheets` `sheet/{add,update,delete}` `fields/{get,add,update,delete}` `records/{add,update,delete}` |

**前置条件**:这些接口不走官方 HTTP API,而是驱动外部 `wecom-cli` 二进制,
需要先安装并 `auth login`。没装时所有在线端点返回 `503` 并说明原因;
用 `WECOM_SVR_WECOM_CLI_PATH` 可指定非 PATH 上的二进制。先探测能力:

```bash
curl -s http://127.0.0.1:8090/api/v1/doc/capabilities | jq .
```

两条贯穿全部端点的约定(沿用上游语义):

- 目标文档用 `docid` **或** `url` 指定,必须二选一,否则 `400`
- **写操作必须显式 `"confirmed": true`**,否则 `403` 且不会真的调用 `wecom-cli`

```bash
# 只读:不需要 confirmed
curl -X POST http://127.0.0.1:8090/api/v1/sheet/info \
  -H 'Content-Type: application/json' -d '{"docid":"abc"}'

# 写:必须确认
curl -X POST http://127.0.0.1:8090/api/v1/sheet/row/append \
  -H 'Content-Type: application/json' \
  -d '{"docid":"abc","sheet_id":"s1","values":[42,"hello"],"confirmed":true}'
```

单元格值会按上游 `cell_data` 规则编码:数字 → `NUMBER`,其余 → `TEXT`;
也可显式传 `{"type":"link"|"formula"|"number"|"text", ...}`。

`local/search` 与 `local/read` 读本机缓存/已下载的文档,**不需要 wecom-cli**,
但内容可能滞后于线上版本(响应里的 `freshness` 字段会说明)。

> ⚠️ 写端点的 `confirmed` 只是沿用上游的误操作闸门,**不是权限控制** ——
> 任何调用方都能把它设成 true。真正的边界是 `api_token` 加只监听 127.0.0.1。

## 群消息同步到 mars

把本地群聊消息持续推给 mars 的群消息 hook
(`PUT {mars}/api/qq/hook/v2/message`,QQ 机器人原本用的那条路)。
mars 早就认 `clientType: "Wework"`,所以**不用改 mars**。

```bash
# 1. 看有哪些群可以同步(按最近活跃排序)
curl http://127.0.0.1:8090/api/v1/mars/sync/groups
# 群名带中文时让 curl 自己编码,别直接拼进 URL
curl --get http://127.0.0.1:8090/api/v1/mars/sync/groups --data-urlencode 'search=技术支持'

# 2. 配置(.env),白名单写群名或 RID 都行
#    WECOM_SVR_MARS_BASE_URL=https://ts.qiniu.io
#    WECOM_SVR_MARS_SYNC_GROUPS=某某客户群,247764039432344
#    # 或者直接同步本机所有群(慎用)
#    WECOM_SVR_MARS_SYNC_ALL_GROUPS=true

# 3. 干跑:确认会推什么、hook body 长什么样(不推、不动水位)
curl -X POST http://127.0.0.1:8090/api/v1/mars/sync/preview

# 4. 手动跑一轮 / 常驻同步
curl -X POST http://127.0.0.1:8090/api/v1/mars/sync/run -d '{}' -H 'Content-Type: application/json'
curl -X POST http://127.0.0.1:8090/api/v1/mars/sync/start
curl http://127.0.0.1:8090/api/v1/mars/sync/status
```

配了 `WECOM_SVR_MARS_SYNC_ENABLED=true` 则随服务启动自动常驻。

| 端点 | 用途 |
| --- | --- |
| `GET /api/v1/mars/sync/status` | 水位、累计条数、上次错误、循环是否在跑 |
| `GET /api/v1/mars/sync/groups` | 本地群列表,标出哪些已在白名单 |
| `POST /api/v1/mars/sync/preview` | 预览待推消息及其 hook body |
| `POST /api/v1/mars/sync/run` | 立即跑一轮,`{"dry_run": true}` 只看不推 |
| `POST /api/v1/mars/sync/watermark` | 重设水位:回补历史或跳过积压 |
| `POST /api/v1/mars/sync/start` \| `/stop` | 起停后台循环 |

### 时效性

轮询本地库,但**准实时**:间隔默认 3s,实测消息发出到推达 mars 只差几秒。
关键不在轮询间隔而在 WAL —— 详见[本地库的新鲜度上限](#本地库的新鲜度上限wal--已由-walpy-解决)。
没有新数据时一轮只是两次 `stat`,有变化时增量刷新 ~0.2s。

### 几条设计约束

- **默认白名单是强制的。** 不配 `MARS_SYNC_GROUPS` 一条都不同步。若显式开启
  `WECOM_SVR_MARS_SYNC_ALL_GROUPS=true`,则会同步本机所有群；这个开关慎用,
  因为本机有 380 个群,`kodo-alert-p1` 这种告警群单群 20 万条,不能无差别外推。
- **默认不倒灌历史。** 首次同步的水位贴着当前最新一条。要回补用
  `MARS_SYNC_BACKFILL` 或 `/watermark`。
- **重推是安全的。** mars 落库前算 `md5(群号+发送人+发送时间+正文)` 作唯一索引
  并 `FirstOrCreate`,所以水位丢了、多推一段都不会产生重复行。
- **顺序被严格保持。** mars 的 `SetTimeline` 按入库顺序切问题分组,所以推送
  串行、按 `(send_time, LID)` 升序;中途失败就停在断点,下轮接上。
- **群号带 `wework-` 前缀。** mars 的 `qq_groups.qq` 是全局唯一键,和真实 QQ
  群号共用一张表,裸 RID 有撞号风险。
- **系统消息/事件不推**(撤回、入群提醒、会议加入等),只推人说的话与卡片/文件占位。
- **修正了上游两个标错的消息类型**(`messages.TYPE_OVERRIDES`):`14` 上游标
  「语音」实为图片(全库 4932 条),`16` 上游标「文件」实为语音。
- **引用回复带上被引内容**(`> 引用: …`),靠 protobuf 字段号 `1002` 识别 ——
  `extracontent` 里同时装着链接预览,只抽文本会把带链接的消息全误判成引用。
- **拼串只有一处。** `messages.fetch_since` 只给结构化字段(`text` / `quote_text` /
  `raw` / `quote_raw`),成段的正文由 `mars_sync.message_body` 拼。曾经是数据层
  先拼好、同步层再按行拆回来找 `[图片]` 替换 —— 那样两层各存一份「引用前缀」
  字面量,改一处就无声退化成把 URL 全缀到行尾。加新的渲染规则请加在
  `message_body` 里,别退回去在数据层拼。

> ⚠️ 改动正文渲染有连带后果:mars 的幂等键含正文,**渲染一变就是新 checksum**,
> 重推会新增行而非更新旧行。改完要按「当前渲染的 checksum 集合」反查删掉旧行。

### 群图片(可选)

不配 `WECOM_SVR_KODO_*` 时,图片只推 `[图片]` 占位符 —— 图原本躺在本机企微缓存里,
别人打不开。配了之后会把原图传到对象存储,并**用公网 URL 替换掉占位符**:

```
[图片]                     →  https://<domain>/wecom-group-media/<内容md5>.png
103会议室                   →  103会议室
> 引用: [图片]                 > 引用: https://<domain>/...png
```

替换而非追加 —— 前端已经把 URL 渲染成图,再留个「[图片]」在图上方是噪音。
反过来,URL 没解析出来时占位符会保留,否则消息就空了。

- 缓存路径是 `Caches/Images/<年-月>/<md5>[_HD]/<文件名>`,按**文件名**在两级目录里
  glob(8537 个目录实测 0.1s),同名取 `_HD` 那份
- 对象 key 用**文件内容 md5**,同一张图重复同步只覆盖同一个对象;上传前先 HEAD,
  已存在就跳过。所以引用回复里那张图通常直接命中,不会重传
- **引用回复里的图**也会解析(被引消息的正文在 protobuf 字段 `1002` 的载荷里)
- 任何一步失败(缓存被清、网络炸)都只退回占位符,不会让整轮同步失败

覆盖率取决于本地缓存:实测某群 12 张图里 7 张能拿到原图,其余是缓存已清理、
或 QQ 中转图(正文里只有需鉴权的 `wxapp.tc.qq.com` 链接,没有文件名)。

> ⚠️ **目标 bucket 是公开可读的** —— 拿到 URL 就能打开,无需鉴权。群聊图片传上去
> 等于放到公网上,只是没人知道地址。所以默认关闭,开之前自己权衡。

> mars 前端默认不渲染图片(`{{ message.content }}` 是转义文本)。要看见图,
> 需要改 mars 的 `GroupListView.vue`;本仓库只负责把 URL 送到。

> ⚠️ **这条 hook 是 mars 的公开路由(无鉴权),且有副作用**:它会 upsert
> `qq_groups`(等于在 mars 里建了这个群的档案),并触发告警转发、销售工单等
> 分支。那些分支都按 `robot` 号或群配置命中,默认的 `robot=wecom-svr` 不会撞上
> 任何一条 —— 但改 `MARS_ROBOT` 前请先读 mars 的 `qq/qq.go: forwardMessage`。

### 错误结构

沿用原 `api_server.py` 的语义,而非 FastAPI 默认的 422:

- `/api/v1/local/*` → `{"success": false, "error": "..."}`
- 其它 → `{"errcode": -1, "errmsg": "..."}`

状态码:

| 码 | 含义 |
| --- | --- |
| `400` | 参数错误 / 业务失败(如会话没找到) |
| `401` | 鉴权失败 |
| `403` | 在线写操作未带 `confirmed: true` |
| `502` | 下游 `wecom-cli` / mars 执行失败或超时(本服务本身正常) |
| `503` | 环境没准备好:本地库未解密/未同步、`wecom-cli` 没装、agent home 解析不了 |

## 本人身份发送的前提(仅 macOS)

1. 企业微信客户端已登录并在运行
2. 已用 wecom-agent 解密出 `Info.db` / `Session.db`:
   `python3 $WECOM_AGENT_HOME/decrypt/macos/read_wecom.py`
3. 终端(或服务宿主进程)已获系统设置 → 隐私与安全性 → **辅助功能** 授权

发送过程会激活窗口、占用剪贴板并模拟按键(执行完会还原剪贴板)。
内部有全局 UI 锁,并发请求串行执行。

> ⚠️ **`success: true` 只代表 AppleScript 执行成功,不代表消息已送达。**
> 这条路径是模拟按键,没有任何回执:如果会话窗口打开了但输入框没拿到焦点,
> 粘贴和回车会落空,接口依然返回 success。需要送达保证请用官方 API
> (`/api/v1/message/send` 返回企业微信服务端的 `msgid`)。

### 本地库的新鲜度上限(WAL)—— 已由 `wal.py` 解决

企业微信的 SQLite 开了 **WAL 模式**。客户端把新消息先写进 `Info.db-wal`,
要等 checkpoint 才落进 `Info.db`;而上游解密工具只读 `Info.db`。所以只读主库时:

- 消息表**永远滞后到上一次 checkpoint**,实测可达 1 小时以上
  (`Info.db` mtime 13:52,`Info.db-wal` 14:39,滞后 47 分钟)
- 刚发/刚收的消息,重跑 `read_wecom.py` 也读不到
- 会话与 RID 是稳定的,所以**发送不受影响**;受影响的只是「读消息」

`wecom_svr/wal.py` 把这层补上了:WAL 里的页和主库用的是同一套页级加密,
于是把未 checkpoint 的帧解密后盖到主库副本上,自己算出「客户端此刻看到的库」。
`messages.refresh()` 走的就是这条路,实测最新消息只落后墙上时间 **3~60 秒**
(取决于企微自己刷 WAL 的节奏),增量刷新 ~0.2s。

> 用到 `/api/v1/mars/sync/*` 与 `messages.py` 的路径都已享受到这个新鲜度。
> `/api/v1/local/conversations` 等老接口仍直接读解密目录,刷新过后同样受益。

仍然要注意:这是**轮询本地库**,不是回调。要严格的消息级实时投递(以及本机
不开企微也能收),用上游的 `recv_server.py`(需要 `recv_token` / `recv_aeskey` /
公网 `public_url`)。

不启服务也可直接用 CLI:

```bash
wecom-svr-local --search "notify"
wecom-svr-local --rid 3395103030 "测试"
wecom-svr-local --rid 3395103030 --image /tmp/a.png
```

## 测试与覆盖率

```bash
uv run pytest                       # 不触碰真实客户端与官方 API,默认带覆盖率
uv run pytest tests/test_group.py   # 单文件(覆盖率数字会偏低,属正常)
uv run coverage html && open htmlcov/index.html   # 逐行看没覆盖到的分支
```

覆盖率配置在 `pyproject.toml`(`[tool.coverage.*]`,统计 `wecom_svr` 包、开分支覆盖),
`--cov` 已写进 pytest `addopts`,本地和 CI 口径一致。

契约测试(`test_vendor_contract.py`、`test_online.py`)依赖 pkg 内容,没初始化 submodule
会整体 skip —— 先 `git submodule update --init --recursive`,否则本地绿了不代表 CI 绿。

## 静态检查

```bash
uv run ruff check wecom_svr tests      # lint
uv run ruff check --fix wecom_svr tests
uv run mypy                            # 类型检查(范围见 pyproject 的 files)
```

规则口径写在 `pyproject.toml`:

- **ruff**:只开 `E/W/F/I/UP/B/C4/RUF`,行宽 110。刻意没开 `S`(bandit)、`BLE`
  (blind-except)、`PTH`、`SIM` —— 这个仓库里 `except Exception: pass` 包
  osascript / sqlite 大多是有意为之,开了只会逼出一堆 `noqa`,把真问题淹掉。
- **ruff format 没进 CI**:跑一遍会重排 33 个文件、1000+ 行,主要是把对齐的中文
  行尾注释拉平。要上的话单独开一个 commit,别混在功能改动里。
- **mypy**:非 strict,只守「明确写错」那一档(`check_untyped_defs` + 关掉隐式
  Optional)。上游 wecom-agent 的模块是运行时按路径动态加载的,配了
  `follow_imports = "skip"`,免得上游一次提交就让我们的 CI 变红。

## CI

`.github/workflows/ci.yml`,在 push to main / PR / 手动触发时跑:

- `lint` job:ruff + mypy。mypy 那步带 `if: always()`,前面挂了也照跑,
  一次 PR 就能把两类问题看全。
- `test` job:Python 3.10–3.13(ubuntu)+ 3.12(macOS)矩阵。服务本身只在 macOS 上真跑
  (以本人身份发消息要 osascript),所以额外压一遍 macOS。
- checkout 带 `submodules: recursive`,让上游契约测试真的执行而不是 skip。
- 覆盖率下限由 workflow 的 `COV_MIN` 控制(当前 40%),低于就红。
  **补了测试就把这个数往上抬**,它是棘轮,不是目标。
- py3.12/ubuntu 那一格额外把覆盖率表写进 job summary,并上传 `coverage.xml` 作为 artifact。
