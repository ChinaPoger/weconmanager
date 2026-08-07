# wecom-svr

企业微信消息发送服务(FastAPI)。底层引用 [tzwkb/wecom-agent](https://github.com/tzwkb/wecom-agent) 作为 pkg,
本仓库只做服务层封装:HTTP 接口、参数校验、鉴权、错误语义。

两套发消息互补:

| 接口 | 身份 | 实现 |
| --- | --- | --- |
| `POST /api/v1/message/send` | **自建应用** —— 对方看到的是「应用」 | 调 wecom-agent 的 `wecom.py`(官方 API) |
| `POST /api/v1/local/message/send` | **本人登录账号** —— 对方看到的是你 | 读解密数据库 + AppleScript 操控 macOS 客户端 |

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
- `POST /api/v1/contact/search` —— 查通讯录

### 错误结构

沿用原 `api_server.py` 的语义,而非 FastAPI 默认的 422:

- `/api/v1/local/*` → `{"success": false, "error": "..."}`
- 其它 → `{"errcode": -1, "errmsg": "..."}`

状态码:参数错误/业务失败 `400`,鉴权失败 `401`,本地数据库缺失或未同步 `503`。

## 本人身份发送的前提(仅 macOS)

1. 企业微信客户端已登录并在运行
2. 已用 wecom-agent 解密出 `Info.db` / `Session.db`:
   `python3 $WECOM_AGENT_HOME/decrypt/macos/read_wecom.py`
3. 终端(或服务宿主进程)已获系统设置 → 隐私与安全性 → **辅助功能** 授权

发送过程会激活窗口、占用剪贴板并模拟按键(执行完会还原剪贴板)。
内部有全局 UI 锁,并发请求串行执行。

不启服务也可直接用 CLI:

```bash
wecom-svr-local --search "notify"
wecom-svr-local --rid 3395103030 "测试"
wecom-svr-local --rid 3395103030 --image /tmp/a.png
```

## 测试

```bash
uv run pytest          # 不触碰真实客户端与官方 API
```
