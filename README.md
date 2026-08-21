# wecom-svr

[![CI](https://github.com/rwifeng/wecom-svr/actions/workflows/ci.yml/badge.svg)](https://github.com/rwifeng/wecom-svr/actions/workflows/ci.yml)
[![coverage](https://img.shields.io/endpoint?url=https%3A%2F%2Fgist.githubusercontent.com%2Frwifeng%2F3b34e6b1df2227d192e646fbf8949b01%2Fraw%2Fcoverage.json)](https://github.com/rwifeng/wecom-svr/actions/workflows/ci.yml)

企业微信消息发送服务(FastAPI)。本仓库只做服务层封装:HTTP 接口、参数校验、鉴权、错误语义。

两套发消息互补:

| 接口 | 身份 | 实现 |
| --- | --- | --- |
| `POST /api/v1/app/message/send` | **自建应用** —— 对方看到的是「应用」 | 调 wecom-agent 的 `wecom.py`(官方 API) |
| `POST /api/v1/message/send` | **本人登录账号** —— 对方看到的是你 | 读解密数据库 + AppleScript 操控 macOS 客户端 |

另有在线文档/表格/智能表接口(`/api/v1/doc|sheet|smartsheet/*`),
封装上游 `online/` 包 —— 见[文档与表格](#文档与表格)。

另外两条是和 mars 的双向通道:

- 把本地群聊消息准实时**推**给 mars(`/api/v1/mars/sync/*`)——
  见[群消息同步到 mars](#群消息同步到-mars)
- 反过来从 mars **拉**任务并执行(`/api/v1/mars/tasks/*`)——
  见[从 mars 拉任务](#从-mars-拉任务反向通道)

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

本机同时只该有一份服务。入口会先抢 `127.0.0.1:8090` 和 `.data/wecom-svr.lock`,再跑启动预检;
端口或锁已被占用时立刻退出,不会再去打开企业微信面板。不要用
`launchctl submit --keepalive` 叠第二份实例 —— 那会在「先弹框再 bind 失败」上打成死循环。常驻请用:

```bash
scripts/launchd.sh start    # KeepAlive 只在非 0 退出时拉起,并至少隔 10 秒
scripts/launchd.sh status
scripts/launchd.sh stop
```

### 新机器首次启动

默认采用“自动初始化 + 严格门禁”:先占住监听端口,再完成以下检查:

1. 用本机 key 刷新解密 `Info.db` / `Session.db`。
2. key 不可用时自动运行 `decrypt/macos/find_key_fast.py`,再重新验证数据库。
3. 打开并立即取消“发起群聊”面板,验证辅助功能/自动化权限并校准本机 `+` 偏移;
   首次成功后生成 `.data/ui_offsets.json`。之后再启动只要这份文件还在,就不会再打开面板。

如果企业微信上残留了上一次操作留下的弹出面板,启动预检和后续发起群聊会先自动
取消它;已知的选人面板点击“取消”,其它面板发送一次 Escape 并确认已经关闭。
无法确认关闭时仍会停止,避免把按键发到错误的窗口。

第 3 步不会选择联系人或创建群。数据库初始化失败会终止启动；UI 校准失败则只记
warning，服务仍会继续监听，`/health` 中的 `ui_ready` 会是 `false`，真正调用本地
UI 接口时再返回错误。这样不会因为 macOS 权限或客户端窗口暂时不可用而让整个服务退出。
自动取 key 仍要求企业微信正在运行且已登录;
若系统报 `task_for_pid` 权限错误,需按 wecom-agent 文档正确重签客户端后再启动,
服务不会自动执行重签。

按部署用途可显式降级:

- 纯官方 API: `WECOM_SVR_STARTUP_PREFLIGHT_ENABLED=false`
- 只采集/解密消息、不操控 UI: `WECOM_SVR_STARTUP_UI_CHECK=false`
- 要求人工预先准备 key: `WECOM_SVR_STARTUP_AUTO_INIT=false`

启动成功后 `/health` 的 `startup_preflight` 会返回数据库、取 key、通讯录和 UI 的检查
结果。其中通讯录(`contacts_ready`,来自 `Session.db` 的 `USER` 表)是**软门禁**:
它只影响按名字查人和官方建群,纯发消息/纯采集的部署用不到,所以查不到只在这里报
`false` 并记一条 warning,不挡启动。

## 配置

优先级:环境变量 / `.env` > `$WECOM_AGENT_HOME/config.json` > 默认值。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WECOM_AGENT_HOME` | `vendor/wecom-agent` | wecom-agent checkout 路径 |
| `WECOM_SVR_HOST` / `WECOM_SVR_PORT` | `127.0.0.1` / `8090` | 监听地址 |
| `WECOM_SVR_API_TOKEN` | 空 | 留空则**不鉴权**(仅本地,勿暴露公网) |
| `WECOM_SVR_STARTUP_PREFLIGHT_ENABLED` | `true` | 启动前检查本地数据库和客户端 UI;数据库失败阻止启动,UI 失败降级 |
| `WECOM_SVR_STARTUP_AUTO_INIT` | `true` | key 无效时自动运行 `find_key_fast.py` |
| `WECOM_SVR_STARTUP_UI_CHECK` | `true` | 首次启动校准 `+` 偏移;已有 `.data/ui_offsets.json` 时跳过打开面板;采集-only 可关闭 |
| `WECOM_SVR_STARTUP_KEY_TIMEOUT` | `180` | 自动取 key 的超时秒数 |
| `WECOM_CORPID` / `WECOM_SECRET` / `WECOM_AGENTID` | 空 | 自建应用凭证,留空回落到 `config.json` |
| `WECOM_SVR_MARS_*` | 见下 | 群消息同步到 mars,默认全关 |
| `WECOM_SVR_UI_PLUS_OFFSET` / `WECOM_SVR_UI_TITLE_OFFSET` / `WECOM_SVR_UI_MORE_OFFSET` | 空 | 手工钉死坐标位置(`"x,y"`);标题项仅兼容旧版客户端;更多项是相对窗口右上角往里 |
| `WECOM_SVR_UI_CALIBRATE` | `true` | 点不中时在附近试候选偏移,试中了记进 `.data/ui_offsets.json` |

## 接口

```bash
# 以应用身份发消息(官方 API)
curl -X POST http://127.0.0.1:8090/api/v1/app/message/send \
  -H 'Content-Type: application/json' \
  -d '{"touser":"zhangsan","msgtype":"text","content":"你好"}'

# 以本人身份发消息(操控客户端)
curl -X POST http://127.0.0.1:8090/api/v1/message/send \
  -H 'Content-Type: application/json' \
  -d '{"conv_rid":3395103030,"content":"你好,我是本人"}'

# 以本人身份发送真实 @ 提醒
curl -X POST http://127.0.0.1:8090/api/v1/message/send \
  -H 'Content-Type: application/json' \
  -d '{"conv_rid":3395103030,"content":"请 @abz 和 @任威风 看一下","mentions":["abz","任威风"]}'

# 查本地会话拿 RID
curl -X POST http://127.0.0.1:8090/api/v1/conversations \
  -H 'Content-Type: application/json' -d '{"keyword":"notify"}'

# 以本人身份发图片(image_path、image_url、image_base64 三选一)
curl -X POST http://127.0.0.1:8090/api/v1/message/send \
  -H 'Content-Type: application/json' \
  -d '{"conv_rid":3395103030,"msgtype":"image","image_url":"https://example.com/a.png"}'

# 以应用身份发图文消息(news,即外链文章卡片)
curl -X POST http://127.0.0.1:8090/api/v1/app/message/send \
  -H 'Content-Type: application/json' \
  -d '{"touser":"zhangsan","msgtype":"news","articles":[{"title":"标题","description":"摘要","url":"https://example.com/article","picurl":"https://example.com/cover.jpg"}]}'

# 查通讯录
curl -X POST http://127.0.0.1:8090/api/v1/app/contact/search \
  -H 'Content-Type: application/json' -d '{"keyword":"张三"}'

# 配了 api_token 时
curl ... -H 'Authorization: Bearer <token>'
```

`mentions` 中的每个姓名必须在 `content` 里以 `@姓名` 出现。服务会操作客户端的
@ 选择器，把它转换为真正的提及消息；只在正文里粘贴普通 `@姓名` 不能保证产生
@ 提醒。`mentions` 可省略，此时服务会自动识别正文里的常见 `@英文/数字/中文姓名`，
但生产调用建议显式传入，避免姓名边界识别不符合预期。找不到对应成员或候选不明确时，
接口返回 HTTP 400，并且不会发送消息。

完整端点:

- `GET  /health` —— 健康检查(公开,含 agent home 解析状态)
- `POST /api/v1/app/message/send` —— 应用身份发消息,`msgtype`: `text` / `markdown` / `news`
- `POST /api/v1/message/send` —— 本人身份发消息,`msgtype`: `text` / `image`;真实 @ 用 `mentions`
  - `text`: `to` 或 `conv_rid` + `content`
  - `image`: `conv_rid` + `image_path` / `image_url` / `image_base64` 三选一
- `POST /api/v1/conversations` —— 查本地会话(body 可省略)
- `POST /api/v1/conversation/open` —— 只打开会话不发送
- `GET  /api/v1/busy` —— 客户端界面正被哪个操作占着(UI 锁的状态)
- `POST /api/v1/group/create` —— 本人身份拉群(操控客户端,能拉外部/微信)
- `POST /api/v1/group/rename` —— 本人身份改群名(官方 API 改不了的群只能走这条)
- `POST /api/v1/app/group/create` —— 应用身份拉群(官方 appchat,**仅限本企业成员**)
- `POST /api/v1/app/group/members/search` —— 按名字搜通讯录候选(建群前用来挑人)
- `POST /api/v1/app/group/members/resolve` —— 干跑:只看名字解析成谁,不建群
- `POST /api/v1/app/group/{get,update,send}` —— 查群 / 改群名换群主加人踢人 / 往群里发消息
- `POST /api/v1/app/contact/search` —— 查通讯录

## 拉群

两条路,能拉的人不一样 —— **这不是实现偏好,是企业微信的硬边界**:

| 接口 | 身份 | 能拉谁 | 有回执 |
| --- | --- | --- | --- |
| `/api/v1/app/group/create` | 自建应用 | **只有本企业成员** | ✅ 返回 `chatid` |
| `/api/v1/group/create` | 你本人 | 同事 + 外部联系人 + 微信用户 | ❌ 见下 |

```bash
# 应用身份(官方 API):成员写中文名/userid/邮箱/「我」都行,离线解析本地通讯录
curl -X POST http://127.0.0.1:8090/api/v1/app/group/create \
  -H 'Content-Type: application/json' \
  -d '{"name":"临时讨论组","members":["陈涛","陈挺"],"include_self":true}'

# 建群前先干跑,核对名字被解析成了谁
curl -X POST http://127.0.0.1:8090/api/v1/app/group/members/resolve \
  -H 'Content-Type: application/json' -d '{"members":["陈涛","陈挺"]}'

# 本人身份(操控客户端):自己自动是群主,不用写进 members
curl -X POST http://127.0.0.1:8090/api/v1/group/create \
  -H 'Content-Type: application/json' \
  -d '{"members":["陈涛","陈挺"],"name":"临时讨论组","nickname":"小王"}'
```

### `POST /api/v1/group/create` 请求 / 响应

**请求**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `members` | 是 | 除自己外至少 2 人。按客户端通讯录搜索,同事 / 外部联系人 / 微信用户都行 |
| `name` | 否 | 群名。面板里没有命名框,所以是先建群再改名;留空则用客户端按成员拼的名字 |
| `nickname` | 否 | 我在本群的昵称。建群(以及改名)成功后再打开群详情设置;留空则不改 |

**响应**(群一旦建出来就是 HTTP 200 / `success: true`。改名或改昵称失败不回滚,错误写在下面两个字段里)

| 字段 | 说明 |
| --- | --- |
| `success` | 群是否建出来 |
| `members` | 实际勾上的人(`query` / `name` / `dept`) |
| `member_count` | 含自己 |
| `rid` | 新群的本地会话 RID;定位失败时为 `null` |
| `name` | 改名成功时的群名 |
| `nickname` | 改昵称成功时的值 |
| `name_error` | 群建好了但改名失败,需要手动改 |
| `nickname_error` | 群建好了但「我在本群的昵称」没改成,需要手动改 |
| `error` | 建群本身失败时的原因 |

```json
{
  "success": true,
  "members": [
    {"query": "陈涛", "name": "陈涛(水淼淼)", "dept": "增长产品部/业务成功部"},
    {"query": "陈挺", "name": "陈挺", "dept": "增长产品部/业务成功部"}
  ],
  "member_count": 3,
  "rid": 10778384972479757,
  "name": "临时讨论组",
  "nickname": "小王",
  "error": null
}
```

CLI 与 HTTP 走同一套代码:

```bash
wecom-svr-local --group "陈涛,陈挺" --group-name "临时讨论组" --group-nickname "小王"
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
定位新群,打开它、改标题、覆写、回车,**最后回库核验那个 RID 的群名真的变了**。
`nickname` 是第三步:打开群详情,改「我在本群的昵称」。**不要点会话标题** —— 那是改群名,
会把昵称写进群名。入口是聊天区标题栏右侧「…」(侧栏开着时最左那个约 28×28 的按钮仍是「…」,
最右是添加成员)。点开约 320 宽的群详情后,再点「我在本群的昵称」那一行,会弹出独立小窗,
写入后点确定。昵称不进本地会话库,核验只能回读这个小窗。

改名/改昵称失败都不回滚群 —— 群已经建出来了,回滚更吓人,接口会在 `name_error` /
`nickname_error` 里说清楚让你手动改。改过群名之后再改昵称,核验用的是**现在的标题**,
不再拿成员拼名去对,否则会把刚改好名的新群误判成别人的群。

差集本身**不足以**证明那个新会话是你建的:这期间别人也可能把你拉进一个群,而 UI 锁
只挡得住本机并发。所以差集之后还有两道:看到第一个新会话再等 `GROUP_APPEAR_SETTLE`
重新取一次差集(同时冒出来两个就放弃改名),以及改名前先把打开的那个会话的标题读
回来,**必须是这几个被选中的人拼出来的**才动手。认不出来只是不改名(带说明让你手动
改),认错了却是把名字改到别人的群上,事后的回库核验查的是同一个错 RID,兜不住。

两个关键的实测结论,踩过才知道:

- **合成点击对 CEF 无效**。`System Events` 的 `click at` 点得动原生控件(会话列表顶部
  的 `+`),但点不动会话标题和成员勾选框 —— 那些是 CEF 自绘的。真实点击靠 JXA 的
  ObjC 桥直接调 CoreGraphics 发 `CGEvent`,零新增依赖,还是同一个 `osascript`。
- **客户端是和人共用的**,请求发出到点击之间会话完全可能被你自己切走。所以改名前
  会先用哨兵值占住剪贴板再全选复制:剪贴板没变 = 没有输入框拿到焦点,直接中止 ——
  否则 `Cmd+V` + 回车会把群名当成一条消息发进别人的聊天里。

只有少数位置没有 AX 元素、只能按主窗口取偏移去点:会话列表顶部的 `+`(相对左上角),
旧版会话标题,以及打不开「…」时的兜底(相对窗口**右边缘**往里)。量出来的常量迟早会被
客户端改版打掉,所以它们都**不是「点了就算」**——点完必须看到成功信号(面板真的开了 /
输入框真的拿到了焦点),看不到就按候选偏移在附近再试,试中了记进 `.data/ui_offsets.json`,
下次第一发就命中(`ui_offsets.py`)。

这些偏移是相对窗口的。**挪动企微窗口一般不用改配置**(每次点击前会重新量 AX);
窗口拉得太窄、全屏、或不在当前桌面,才可能点偏。点不中「…」可设
`WECOM_SVR_UI_MORE_OFFSET`。

试探的边界是刻意划死的,因为「乱点」本身就是风险:

- 候选是**十字**不是网格 —— 布局变化就是这个形状(侧栏宽了整列横移,标题栏高了
  整行竖移),而每多一个候选就多一次真实点击。
- `title` 的候选只在标题带里横向挪一点点:再往右就是通话/更多那几个**有副作用**的
  按钮。宁可校准不到、让人配一次 `WECOM_SVR_UI_TITLE_OFFSET`,也不能瞎点出一通视频通话。
- 标题的试探只做「选中 + 复制」,不打字不回车 —— 一次没点中的试探什么都改不了。
- 点开的要是**别的**面板(不是选人面板),立刻停,不继续在一个陌生面板上试。

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
| `GET /api/v1/mars/sync/group-info/status` | 群信息指纹水位、上次推送 |
| `POST /api/v1/mars/sync/group-info/preview` | 预览待推的群档案/成员快照 |
| `POST /api/v1/mars/sync/group-info/run` | 立即推一轮群信息 |

### 时效性

轮询本地库,但**准实时**:间隔默认 3s,实测消息发出到推达 mars 只差几秒。
关键不在轮询间隔而在 WAL —— 详见[本地库的新鲜度上限](#本地库的新鲜度上限wal--已由-walpy-解决)。
没有新数据时一轮只是两次 `stat`,有变化时增量刷新 ~0.2s。

### 几条设计约束

- **默认白名单是强制的。** 不配 `MARS_SYNC_GROUPS` 一条都不同步。若显式开启
  `WECOM_SVR_MARS_SYNC_ALL_GROUPS=true`,则会同步本机所有群；这个开关慎用,
  因为本机有 380 个群,`kodo-alert-p1` 这种告警群单群 20 万条,不能无差别外推。
- **无名群也要同步。** 企微刚建出来的群库里 `name` 经常是空的(客户端按成员现拼显示名,
  不落库)。丢掉它们,改名后会被当成全新群从 latest 起跳,中间那段消息永久漏掉。
  全量同步和按 RID 点名白名单都会带上无名群;推给 mars 的群名用 `未命名群-{rid}`,
  改名后下一条消息会把档案更新成真名。空串不能原样推 —— mars 用 GroupName 派生
  Slack channel,会撞到一起。
- **群信息另走一条资源接口。** 消息 hook 只会顺手 upsert 群档案和当前发送人,
  完整成员快照(以后还有群公告)走 `PUT {mars}/api/qq/group-infos`。两边在 mars 里
  共用同一套写群规则(已有 UID 不覆盖、空 note 不改公告、空 members 不改花名册);
  差别只是消息增量补发送人,群信息整表替换成员。跟消息共用白名单,但用指纹跳过
  没变化的群。本地库目前还没定位到可靠的群公告字段,`note` 先留空。
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
[图片]                     →  https://<domain>/robot/<群RID>/20260811/103302-1ab50be8.png
103会议室                   →  103会议室
> 引用: [图片]                 > 引用: https://<domain>/...
```

替换而非追加 —— 前端已经把 URL 渲染成图,再留个「[图片]」在图上方是噪音。
反过来,URL 没解析出来时占位符会保留,否则消息就空了。

#### 定位:按 md5,不按文件名

缓存路径是 `Caches/Images/<年-月>/<md5>[_HD|_THUMB]/<文件名>`,**目录名就是发送方
原图的 md5**,而那个 md5 在消息正文的 ASN.1 里就有 —— 所以不用读文件算 hash,也不用
靠文件名去猜:

| 变体 | 是什么 | 字节 md5 == 目录名 |
| --- | --- | --- |
| `<md5>_HD/` | **发送方原始字节** | 3875 / 3876 |
| `<md5>/` | 客户端渲染件 | 702 是 / 3369 否 |
| `<md5>_THUMB/` | 缩略图 | 否 |

- **不能按体积挑**。`_HD` 有 12.1% 比渲染件还小 —— 实测有一张原图 407x138,渲染件却是
  插值放大到 800x271 的,字节多一倍、信息一点没多。旧实现的「同名取大的那份」因此
  挑错了 526 张,推给 mars 的是渲染件。
- **不能按文件名认身份**。`pic.jpg` 一个名字对着 8 张不同的图。
- 正文里解不出 md5 时(实测 17/165)才退回按文件名 glob。
- 本地只缓存到渲染件时(181/3812 ≈ 4.7%)照传,但 key 里的区分位**仍取原图 md5** ——
  否则同一张图会落到两个对象上。代价是以后原图到了不会再替换,这是明确取舍。

#### 没有文件名的那类:CDN 直链

表情包和转发进来的图正文 **protobuf 字段 1** 直接是企微 CDN 直链,拉回来再传。按位置
取而不是全文搜 `http://` —— 合并转发的聊天记录里也有一堆链接,但那些是发言人头像
(`wwhead`/`mmhead`),藏在嵌套消息里。

企微机器人/应用发送的多图还可能被编码成 `content_type=101`,把多条
`imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia` 临时地址放在不同的
protobuf 字段里。这类地址同样不会直接推给 mars:同步器会按出现顺序逐张下载、上传
到 Kodo,再把正文渲染成多个自有图片 URL。

那条直链**不会直接推给 mars**:它是企微的资源,寿命和权限都不归我们管。

#### 对象 key

```
<prefix>/<群 RID>/<YYYYMMDD>/<HHMMSS>-<md5前8位><ext>
robot/1688851234567890/20260811/103302-1ab50be8.png
```

按群分目录、按天再按时刻排序,在 kodo 控制台里能直接翻某个群某天的图。时刻按**本机
时区**渲染。

末尾那 8 位 md5 不是装饰,它挡住两件事:

- **同群同秒会撞车。** 多选发图一次就是好几张。全库实测 5590 张图里有 **123 张挤在
  58 个「同群同秒」的槽位**上,最多一槽 4 张;少了区分位其中 65 张会被静默覆盖 ——
  本地库只有秒级时间,没有更细的刻度可用。
- **桶是公开可读的。** key 全部可推导的话,知道群号按秒遍历一天就能把整个群的图扒
  下来。8 位 md5 = 40 亿分之一,把枚举这条路堵死。

md5 优先取**正文里的原图 md5**;本地只缓存到渲染件时也用它,这样同一张图不会因为
拿到的字节不同而落到两个 key 上。CDN 那条路没有正文 md5,只能拿收到的字节算。

key 是 `(群, 时刻, 内容)` 的**纯函数**,所以重推同一条消息落在同一个对象上,上传前
HEAD 一次就跳过。时刻缺失时退回纪元 0 而不是 `now()` —— 后者会让每次重推都多一个对象。

- 代价一:同一张图出现在两个群会传两份。既然本来就想按群分,这不算损失
- 代价二:**引用回复里那张图的时间戳是回复消息的**(手上只有当前这条的时刻),
  所以它在桶里会另存一份。多出来的是几十 KB,不值得为它把 key 改回内容寻址
- 任何一步失败(缓存被清、网络炸)都只退回占位符,不会让整轮同步失败

> 换 key 方案之前推过的消息指向旧的 `<md5>/<文件名>` 和更早的 `<md5><ext>`,那些
> 对象都没动,老消息照常显示。

#### 为什么要 `MARS_IMAGE_WAIT`

企微收到图片消息时**只写 SQLite 的消息行,不下载图片** —— 图片文件要等你真的点开
那个会话才落到 `Caches/Images/`。实测 8/6–8/11 的 40 条图片消息里,文件比消息晚
30 秒到 10 小时的比比皆是,只有 5 条在 3 秒内到位。默认 3 秒的轮询几乎总是跑在下载
前面,推出去的就永远是 `[图片]`。

而这**没有事后回补的余地**:mars 的 checksum 是 `md5(群号+发送人+时间+正文)`
(`models/qq.go`),把占位符换成 URL 再推是**另一条**记录,不是更新。所以只能在推
之前等 —— 这就是 `WECOM_SVR_MARS_IMAGE_WAIT`(默认 60 秒)。

- 只等**认得出的**:正文里能解析出原图 md5 或文件名、缓存里还没有 = 还能主动取或
  等客户端落盘;两者都没有才不等,否则每条都白等一个窗口
- 有 CDN 直链的也不等,那条路当场就能取到图
- 等超时就带着占位符推走,退化成旧行为 —— 一张永远不会下载的图不能卡死同步链
- 水位是**每群一条**,等图只会推迟本群后续消息,其它群在同一轮继续。`0` = 不等
- `/api/v1/mars/sync/status` 的 `last_result.held` 是本轮被扣下的条数

#### 主动取图:TCP 优先,客户端触发兜底

等待前会主动走两级路径:

1. **BigCDN TCP**:从消息 protobuf 取 `fileid`、AES key、原图 md5/大小,用当前
   BigCDN 会话配置构造两帧 TCP 请求。响应的 `filedata` 用 AES-128-ECB 解密并严格
   校验 PKCS#7、大小和 md5;成功后原子写入 `.data/media-cache/`。
2. **客户端触发**:TCP 配置不存在、认证过期或网络失败时,执行
   `open -g wxwork://jump?...` 后台切到目标群,轮询企微缓存直到完整原图落盘。
   URL Scheme 不抢应用焦点,但会改变企微当前选中的会话;同群有 15 秒默认冷却。

TCP 的 65 字节会话认证值属于本机登录凭据,只保存在 gitignored 的
`.data/media_tcp.json`(0600),不会进入 `.env`、日志或状态接口。用已经验证成功的
tcpdump 抓包导入:

```bash
uv run python -m wecom_svr.media_tcp import-pcap /tmp/wecom-image-final.pcapng
```

CDN IP 或 authkey 变化后,TCP 会校验失败并自动落到客户端路径;重新抓一张图并再导入
即可恢复 TCP。客户端锁屏、退出或目标图片不在会话末尾渲染窗口时仍可能失败,此时保持
pending,直到 `MARS_IMAGE_WAIT` 超时才推占位符。

> 每群水位的迁移、tombstone 与回滚边界见
> [docs/per-group-watermark.md](docs/per-group-watermark.md)。同步链路已同时修复群列表
> 200 上限、`/run` 与后台循环互斥、水位读写 fail closed。

合并转发卡片内部的深层图片不属于顶层图片消息,当前仍不会被客户端会话跳转自动展开;
这类以及只有需鉴权 `wxapp.tc.qq.com` 链接的 QQ 中转图仍会退回占位符。

> ⚠️ **目标 bucket 是公开可读的** —— 拿到 URL 就能打开,无需鉴权。群聊图片传上去
> 等于放到公网上,只是没人知道地址。所以默认关闭,开之前自己权衡。

> mars 前端默认不渲染图片(`{{ message.content }}` 是转义文本)。要看见图,
> 需要改 mars 的 `GroupListView.vue`;本仓库只负责把 URL 送到。

> ⚠️ **这条 hook 是 mars 的公开路由(无鉴权),且有副作用**:它会 upsert
> `qq_groups`(等于在 mars 里建了这个群的档案),并触发告警转发、销售工单等
> 分支。`robot` 默认填本机登录企微账号的 VID(拿不到时退回 `wecom-svr`)。
> 那些分支按 `robot` 号或群配置命中 —— 改 `MARS_ROBOT` 前请先读 mars 的
> `qq/qq.go: forwardMessage`。

### 错误结构

沿用原 `api_server.py` 的语义,而非 FastAPI 默认的 422:

- 本人身份那几条(`message/send`、`conversations`、
  `conversation/open`、`busy`、`group/create`、`group/rename`)
  → `{"success": false, "error": "..."}`
- 其它 → `{"errcode": -1, "errmsg": "..."}`

判定不再是路径前缀 —— 本人身份的接口挪到 `/api/v1/*` 裸路径后没有共同前缀了,
改成按实际注册的路由列表判定(`routers/local.py` 的 `LOCAL_PATHS`),
免得新增一条接口时两处漂。

状态码:

| 码 | 含义 |
| --- | --- |
| `400` | 参数错误 / 业务失败(如会话没找到) |
| `401` | 鉴权失败 |
| `403` | 在线写操作未带 `confirmed: true` |
| `502` | 下游 `wecom-cli` / mars 执行失败或超时(本服务本身正常) |
| `503` | 环境没准备好:本地库未解密/未同步、`wecom-cli` 没装、agent home 解析不了 |

## 从 mars 拉任务(反向通道)

上面那节是本机把群消息**推**给 mars;这节是反过来 —— mars 想让这台 Mac 发一条
消息时怎么办。

这台 Mac 在 NAT 后面、IP 会变、还会合盖休眠,mars 拨不进来;而它拨出去是通的。
所以做成**任务队列 + 长轮询**:mars 把"要发什么"落库(`wecom_tasks`),本机来取、
报告开工、执行、回报。完整设计(为什么不是 WebSocket、状态机、fencing 令牌、
崩溃恢复)见 [docs/mars-task-queue.md](docs/mars-task-queue.md)。
mars 侧代码在 `qbox/mars` 的 `feat/wecom-task-queue` 分支。

```bash
# 1. 配置(.env),token 要和 mars.json 的 wecom_task_token 一致。留空 = 关闭
#    WECOM_SVR_MARS_BASE_URL=https://ts.qiniu.io
#    WECOM_SVR_MARS_TASKS_TOKEN=...

# 2. 手动跑一轮验证(wait=0:队列空就立刻返回)
curl -X POST http://127.0.0.1:8090/api/v1/mars/tasks/run -d '{"wait":0}' \
  -H 'Content-Type: application/json'

# 3. 常驻
curl -X POST http://127.0.0.1:8090/api/v1/mars/tasks/start
curl http://127.0.0.1:8090/api/v1/mars/tasks/status
```

配了 `WECOM_SVR_MARS_TASKS_ENABLED=true` 则随服务启动自动常驻。

| 端点 | 用途 |
| --- | --- |
| `GET /api/v1/mars/tasks/status` | 累计条数、inflight、白名单、上次错误 |
| `POST /api/v1/mars/tasks/run` | 立即拉一轮并执行,`{"wait": 0}` 不等 |
| `POST /api/v1/mars/tasks/start` \| `/stop` | 起停后台循环 |

### 两条不能退化的约束

- **本人身份的 action 默认不在白名单。** `WECOM_SVR_MARS_TASKS_ACTIONS` 默认只有
  `app.message.send`(应用身份)。action 名和 HTTP 路由同构 —— 带 `app.` 前缀的是
  应用身份,不带的(`message.send` / `group.create` / `group.rename`)
  是**以你本人的身份说话**,让一个远端队列能驱动它,权限边界比"应用发通知"
  大一个量级,要开必须显式写进配置。
- **宁可漏发,不重发。** 跟群消息同步的取舍正好相反 —— 那边 mars 有 checksum
  唯一索引兜底,重推是安全的;这边发出去就收不回来。所以真正动手前会先调
  mars 的 `/start` 把任务推进 `executing`,失联时 mars 判"结果未知"而**不重投**;
  本地 inflight 记录也是原子落盘的(临时文件 + fsync + rename),半截文件会让
  这条保证失效。

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
> (`/api/v1/app/message/send` 返回企业微信服务端的 `msgid`)。

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
> `/api/v1/conversations` 等老接口仍直接读解密目录,刷新过后同样受益。

仍然要注意:这是**轮询本地库**,不是回调。要严格的消息级实时投递(以及本机
不开企微也能收),用上游的 `recv_server.py`(需要 `recv_token` / `recv_aeskey` /
公网 `public_url`)。

不启服务也可直接用 CLI:

```bash
wecom-svr-local --search "notify"
wecom-svr-local --rid 3395103030 "测试"
wecom-svr-local --rid 3395103030 --image /tmp/a.png
wecom-svr-local --rid 3395103030 --open-only

# 拉群 / 改群名:和 /api/v1/group/* 是同一套代码,只是不用先起服务
wecom-svr-local --group "陈涛,陈挺" --group-name "临时讨论组" --group-nickname "小王"
wecom-svr-local --rid 3395103030 --rename "客户群-新名字"
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
