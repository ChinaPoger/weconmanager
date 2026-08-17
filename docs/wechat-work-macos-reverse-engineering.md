# 企微 macOS 客户端通信方式逆向分析

> 分析目标：`/Applications/企业微信.app/Contents/MacOS/企业微信`
> 分析方法：strings + plutil + 二进制符号提取
> 分析时间：2025-01

---

## 1. 企微 macOS 客户端架构概述

企微 macOS 客户端基于 **Flutter** 框架开发，使用 **Dart** 编写业务逻辑，C++ 处理底层通信。

核心架构层：
- **Flutter/Dart** — UI 和业务逻辑
- **WeWorkService (Dart RPC)** — 原生层与 Flutter 层通信
- **Mach Port JSON-IPC** — 进程间通信（JSON 格式）
- **JSAPI** — H5 页面调用原生功能
- **URL Scheme** — 系统级路由（`wxwork://`）

---

## 2. 暴露的通信方式

### 2.1 Mach Port JSON-IPC ⭐

**端口信息**
| 项 | 值 |
|---|---|
| 端口名 | `88L2Q4487U.WeWorkMac.WEWMachPortServer` |
| 协议 | `NSPortMessage` 外层 + 按 `msgid/srcType` 分流；部分分支内部使用 `jsonipc::IPCPackage` |
| 验证 | 使用真实 `NSPortMessage` 可收到服务端响应；裸 JSON 在普通分支返回 `from wecom mac reqid: N not support.` |
| 签名 | TeamID `88L2Q4487U`（WeCom Team） |

**重要纠正（5.0.9 实测）**

`88L2Q4487U.WeWorkMac.WEWMachPortServer` 不是可直接接收
`{"method":"qy__...","params":...}` 的通用 JSAPI 端口。`WEWPortMessageThread`
先读取 `NSPortMessage.msgid` 作为 `srcType`，然后仅分流以下 5 类：

| `msgid/srcType` | Handler |
|---:|---|
| 1 | `p_handleTxMeetingPortMsg:` |
| 2 | `p_handleUnSupportReqMsg:` |
| 3 | `p_handleWeClawPortMsg:` |
| 4 | `p_handleIOAPortMsg:` |
| 5 | `p_handleMeetingPortMsg:` |

外部测试程序使用 Foundation `NSPortMessage` 发送一个 `NSData` component，并设置
receive port 后，`msgid=2` 可稳定收到：

```text
from wecom mac reqid: 2 not support.
```

这证明 Mach 消息外层已构造正确，同时也证明此前 OOL 发送返回
`KERN_SUCCESS` 只代表消息入队，并不代表 JSON 被 JSAPI 分发。`msgid=1/3/4/5`
进入各专用协议分支；其中 `srcType=3` 的 WeClaw 分支内部才使用
`jsonipc::IPCPackage`，不是 `qy__sendChatMessage` 所在的 JSAPI 调用链。

**JSAPI 数据格式**（Handler 静态分析）

`qy__sendChatMessage` 是 `WEWJSAPIHandlerManager` 注册的 JSAPI 别名，实际
`wwJsApiName` 为 `sendChatMessage`。Handler 从当前 Web/侧边栏会话 wrapper 获取
上下文，而不是从参数中读取任意 `convId`。已确认参数字段为：

| 字段 | 类型 | 说明 |
|---|---|---|
| `msgtype` | string | `text`、`image`、`video`、`file`、`news`、`miniprogram`、`channels_shop_product`、`channels_shop_order` |
| `content` | object/string | 消息体；文本内容位于此字段对应的数据中 |
| `mediaid` | string | 图片、视频或文件媒体 ID |

Handler 中还存在明确错误 `mp without context of external chat.` 和
`without context of external chat.`，说明该 API 依赖已授权网页/侧边栏的外部联系人
会话上下文。此前推测的 `convId/msgType/content` 扁平结构不成立。

**源码路径**
- `src/mac/Application/AppConnect/MachPort/WEWPortMessageServer.mm`
- `src/mac/Application/AppConnect/MachPort/WEWIOAWeComConnectManager.mm`
- `src/mac/Application/AppConnect/MachPort/WEWPingPangChecker.mm`
- `src/mac/Application/AppConnect/JsonIPC/ipc_package.cpp`
- `src/mac/Application/AppConnect/JsonIPC/WEWIPCLogicBase.mm`
- `src/mac/Application/AppConnect/JsonIPC/WEWWeClawIPCLogic.mm`

**关键函数**
- `WEWPortMessageServer` — Mach Port 服务端
- `handleMessage` — 处理 JSON-IPC 消息，检查 `method` 合法性
- `handleMessageNotify` — 处理消息通知
- `WEWIPCLogicBase` — IPC 逻辑基类
- `WEWWeClawIPCLogic::doDispatchJsonReq` — WeClaw AI 助手 JSON 分发
- `WEWTxMeetingIPCLogic` — 腾讯会议 IPC 逻辑

**已发现的 JSON-IPC 方法名（`qy__` 前缀）**
| 方法 | 用途 | 相关 Handler |
|---|---|---|
| `qy__sendChatMessage` | 发送聊天消息 | `WEWJSAPISendChatMessageHandler` |
| `qy__selectEnterpriseContact` | 选择企业联系人 | `WEWJSAPISelectEnterpriseContactHandler` |
| `qy__selectExternalContact` | 选择外部联系人 | `WEWJSAPISelectExternalContactHandler` |
| `qy__openUserProfile` | 打开用户资料 | `WEWJSAPIOpenUserProfileHandler` |
| `qy__openEnterpriseChat` | 打开企业聊天 | `WEWJSAPIOpenEnterpriseChatHandler` |
| `qy__openThirdAppServiceChat` | 打开第三方应用聊天 | `WEWJSAPIOpenThirdAppServiceChatHandler` |
| `qy__createDoc` | 创建文档 | `WEWJSAPICreateDocHandler` |
| `qy__login` | 登录 | `WEWJSAPIQYLoginHandler` |
| `qy__getSystemInfo` | 获取系统信息 | `WEWJSAPIQYGetSystemInfoHandler` |
| `qy__getUserInfo` | 获取用户信息 | — |
| `qy__getSetting` | 获取设置 | `WEWJSAPIQYGetSettingHandler` |
| `qy__getContext` | 获取上下文 | `WEWJSAPIGetContextHandler` |
| `qy__openUrl` | 打开 URL | `WEWJSAPIOpenUrlHandler` |
| `qy__navigateToApp` | 导航到应用 | `WEWJSAPIAppNavigationHandler` |
| `qy__startMeeting` | 发起会议 | `WEWJSAPIStartMeetingHandler` |
| `qy__startLiving` | 发起直播 | `WEWJSAPIStartLivingHandler` |
| `qy__selectDatazoneDoc` | 选择数据域文档 | — |
| `qy__selectDatazoneFile` | 选择数据域文件 | — |
| `qy__wedocSelectDoc` | 选择 WeDoc | `WEWJSAPIWeDriveThirdAPIHandler` |
| `qy__wedriveSelectFile` | 选择 WeDrive 文件 | — |
| `qy__openSystemBrowserWithSt` | 打开系统浏览器 | `WEWJSAPIQYOpenSystemBrowserWithSt` |
| `qy__shareToExternalContact` | 分享到外部联系人 | `WEWJSAPIShareToExternalContactHandler` |
| `qy__canIUsePrivate` | 检查私聊权限 | `WEWJSAPIQYCanUsePrivateHandler` |
| `qy__checkSession` | 检查会话 | `WEWJSAPIQYCheckSessionHandler` |
| `qy__checkSchedule` | 检查日程 | `WEWJSAPICheckScheduleHandler` |
| `qy__operatePrivate` | 操作私聊 | `WEWJSAPIQYOperatePrivateHandler` |
| `qy__updateEnterpriseChat` | 更新企业聊天 | `WEWJSAPIQYUpdateEnterpriseChatHandler` |
| `qy__openAppComment` | 打开应用评论 | `WEWJSAPIWorkBenchOpenAppCommentHandler` |
| `qy__printFile` | 打印文件 | `WEWJSAPIPrintFileHandler` |
| `qy__replayLiving` | 回放直播 | `WEWJSAPIReplayLivingHandler` |
| `qy__setShareAttr` | 设置分享属性 | `WEWJSAPISetShareAttrHandler` |
| `qy__getAvatar` | 获取头像 | — |
| `qy__getEmail` | 获取邮箱 | — |
| `qy__getMobile` | 获取手机号 | — |
| `qy__getQrCode` | 获取二维码 | — |
| `qy__getShareInfo` | 获取分享信息 | `WEWJSAPIGetShareInfoHandler` |

**另外的 `wework_` 前缀方法**（Flutter ↔ 原生桥接）
| 方法 | 用途 |
|---|---|
| `wework_jsapi` | JSAPI 调用入口 |
| `wework_universalEvent` | 通用事件处理 |
| `wework_getConversationInfo` | 获取会话信息 |
| `wework_getRecentContacts` | 获取最近联系人 |
| `wework_selectContacts` | 选择联系人 |
| `wework_jumpToConversation` | 跳转到会话 |
| `wework_startConversation` | 发起会话 |
| `wework_viewProfile` | 查看用户资料 |
| `wework_openUrl` | 打开 URL |
| `wework_cgi` / `wework_cgiRequest` | CGI 请求 |
| `wework_focusMainWindow` | 聚焦主窗口 |
| `wework_showDockWindow` | 显示 Dock 窗口 |
| `wework_hideOneDiscussWindow` | 隐藏讨论窗口 |
| `wework_showDiscussWindow` | 显示讨论窗口 |
| `wework_didLogin` / `wework_didLogout` | 登录/登出事件 |
| `wework_unreadCountChange` | 未读数变化 |
| `wework_ping` / `wework_ping_interval` | 心跳 |

**WeClaw IPC**（AI 助手 WeClaw）
| 方法 | 用途 |
|---|---|
| `handleInit` | 初始化 |
| `handleUpdatePaths` | 更新路径 |
| `handleGatewayStarted` | 网关启动 |
| `handleGatewayAborted` | 网关中止 |
| `handlePing` | 心跳 |
| `handleEcho` | 回声 |
| `doDispatchJsonReq` | 分发 JSON 请求 |

**JSON-RPC 请求格式**（`handlePost` 处理）
```
POST / MCP Server
Content-Type: application/json
Authorization: Bearer <token>

{
  "jsonrpc": "2.0",
  "method": "<method>",
  "params": {...},
  "id": <request_id>
}
```

**handleProxy JSON-RPC 方法**（MCP 协议）
- `tools/list` — 获取可用工具列表
- `tools/call` — 调用工具
- `resources/list` — 获取资源列表
- `resources/read` — 读取资源
- `prompts/list` — 获取提示词列表
- `prompts/get` — 获取提示词

---

### 2.2 Flutter Platform Channel

**协议**
- 通道前缀：`rpc://flutter/channel`
- 通道列表：
  - `/wxwork_channels.WxWorkBaseChannels/*`
  - `/wxwork_channels.WeNoteChannels/*`
  - `/global_search_channels.GlobalSearchChannels/*`

**通道列表**
| 通道 | 用途 |
|---|---|
| `OpenFlutterWindow` | 打开 Flutter 窗口 |
| `ToWeworkAdmin` | 跳转到管理后台 |
| `setFlutterWindowBarColor` | 设置窗口栏颜色 |
| `onFlutterKeyboardEvent` | 键盘事件 |
| `createWeNoteFlutterInstance` | 创建笔记实例 |

**源码路径**
- `src/mac/Flutter/ChannelImp/BaseChannel/WWKDMessageApiMacImpl.mm`
- `src/mac/Flutter/ChannelImp/BaseChannel/WWKDConversationApiMacImpl.mm`
- `src/mac/Flutter/ChannelImp/BaseChannel/WWKDUserApiMacImpl.mm`
- `src/mac/Flutter/ChannelImp/BaseChannel/WWKDFileApiMacImpl.mm`

---

### 2.3 Dart RPC Service

**服务名**：`WeWorkService.WeWorkRpcService`

**暴露的 RPC 方法**
| 方法 | 用途 |
|---|---|
| `CdnDownloadFileToPath` | CDN 下载文件 |
| `CdnUploadFileOnPath` | CDN 上传文件 |
| `DefaultOwnerAdminGroup` | 默认群主/管理员 |
| `EstimateCurrentServerTime` | 估算服务器时间 |
| `FetchMyCustomerStat` | 获取客户统计 |
| `FetchStaffList` | 获取员工列表 |
| `FtnDownloadFileToPathPlain` | 下载文件（明文） |
| `FtnDownloadScaleImageFileId` | 下载缩放图片 |
| `FtnUploadFileOnPath` | 上传文件 |
| `FtnUploadVideo` | 上传视频 |
| `GetCorpInfoInt64ValueByKey` | 获取企业配置 |
| `GetCurrentProfileCorpInfo` | 获取当前用户企业信息 |
| `GetDockerEnv` | 获取 Docker 环境 |
| `GetLoggerPath` | 获取日志路径 |
| `GetMyCustomerStat` | 获取客户统计 |
| `GetSystemConfig` | 获取系统配置 |
| `GetSystemExtraInfoInt64ValueByKey` | 获取系统额外信息 |
| `GetSystemInfoInt64ValueByKey` | 获取系统信息 |
| `GetSystemInfoStringValueByKey` | 获取系统信息（字符串） |
| `IsRuleGroupCharger` | 检查规则组管理员 |
| `LogOfflineOssPbStringInternal` | 日志上报 |
| `UploadStaffList` | 上传通讯录 |

**关键函数**
- `WeWorkService::InvokeRpcMethod` — RPC 调用
- `WeWorkService::PostMessageToDart` — 发消息到 Dart 层
- `WeWorkService::RunOnDartThread` — 在 Dart 线程运行
- `WeWorkService::DartInitService token:` — Dart 服务初始化

**源码路径**
- `src/mac/Flutter/ChannelImp/Advance/Impl/WWKDartAdvanceFuncChannelApiMacImpl.mm`
- `src/mac/Flutter/ChannelImp/AiBot/WWKDAiBotChannelsMacImpl.mm`

---

### 2.4 URL Scheme

**注册的 Scheme**
| Scheme | 说明 |
|---|---|
| `wxwork://` | 主协议 |
| `wxworksaas://` | SaaS 版协议 |
| `wxworkwedrive://` | WeDrive 协议 |

**支持的 target**（通过 `wxwork://jump?target=xxx`）
| target | 参数 | 用途 | 状态 |
|---|---|---|---|
| `conv` | `type`, `roomid`, `msgid`, `fwid` | 打开会话 | ✅ 可用 |
| `openchat` | 无 | 发起群聊 | ❌ 内部锁定 |
| `input_conv_name` | `conv_id` | 打开改名输入框 | ⚠️ 部分可用 |
| `user_profile` | `vid` | 查看用户资料 | ✅ 可用 |
| `markdown_talk` | 无 | Markdown 会话 | ✅ 可用 |
| `captcha` | 无 | 验证码 | ✅ 可用 |
| `global_external_auth` | `vid` | 外部认证 | ✅ 可用 |
| `weclaw_handle_attachment` | `param` | WeClaw 附件 | ✅ 可用 |
| `wedrive-file-transfer-station` | 无 | WeDrive 传输站 | ✅ 可用 |
| `wwkcalendarbookshare` | `cal_id` | 日历分享 | ✅ 可用 |

**其他 Scheme**
| Scheme | 用途 |
|---|---|
| `wxwork://action?type=1` | 内部动作 |
| `wxwork://action?type=3` | 内部动作 |
| `wxwork://calendar` | 日历 |
| `wxwork://conversationlist` | 会话列表 |
| `wxwork://feedback` | 反馈 |
| `wxwork://get_login` | 获取登录信息 |
| `wxwork://closeurl` | 关闭 URL |
| `wxwork://openurl` | 打开 URL |
| `wxwork://sharelinktouser` | 分享链接到用户 |
| `wxwork://show_download_mobile_app_qrcode` | 下载 App 二维码 |
| `wxwork://swtichcorp` | 切换企业 |
| `wxwork://voiceroom?type=addmember` | 语音室 |
| `wxwork://wewactive` | 活动 |
| `wxwork://ai_summary_mac_stop_regular_update` | AI 总结 |
| `wxwork://message?uin=` | 消息 |
| `wxwork://msgauditinfo?` | 消息审计 |

---

### 2.5 WebSocket 调试端口

| 项 | 值 |
|---|---|
| 端口 | `ws://127.0.0.1:20001` |
| 状态 | 当前未监听（需特定条件触发） |

---

### 2.6 JSAPI

**暴露的 Handler**
| Handler | 用途 |
|---|---|
| `WEWJSAPISendChatMessageHandler` | 发送聊天消息 |
| `WEWJSAPIOpenChatWithMsgHandler` | 打开聊天并发送消息 |
| `WEWJSAPIOpenExistedChatWithMsgHandler` | 打开已有聊天并发送消息 |
| `WEWJSAPICreateChatWithMessageHandler` | 创建聊天并发送消息 |
| `WEWJSAPISelectEnterpriseContactHandler` | 选择企业联系人 |
| `WEWJSAPIAppNavigationHandler` | 应用导航 |
| `WEWJSAPIAppNavigationManager` | 应用导航管理 |

**局限**：JSAPI 仅用于 H5 页面调用，不能从外部进程直接调用。

---

## 3. 消息发送相关实现

### 3.1 消息发送路径

**网页 JSAPI 调用链（更正：不是外部 Mach IPC）**
```
已授权 H5/侧边栏 → WEWWeixinJSBridge → WEWWKJSAPICallWrapper
  → WEWJSAPIHandlerManager → qy__sendChatMessage alias
  → WEWJSAPISendChatMessageHandler
```

`WEWWKJSAPICallWrapper` 的构造方法为
`initNativeCode:withParams:withCallbackID:`；但 wrapper 的 delegate、browser source、
app ID、session permission 和聊天 context 均由现有 webview bridge 注入。外部进程无法
仅凭 RID 构造这些对象。`WEWJSAPIUIContextManager` 只维护 applet context，接口为
`updateCurContext:source:conv:`、`appletContextWithAppId:`，没有按 RID 查询任意会话的接口。

**内部处理链**
```
用户输入 → Flutter UI → Dart 层 → [SendMsg] SendMessageLocal → DoSendMessage → ChatTask::OnSendMessageCallback
```

**关键 Handler**（源码：`src/mac/UI/Conversation/OpenAPI/Handlers/App/WEWJSAPISendChatMessageHandler.mm`）
- `WEWJSAPISendChatMessageHandler` — JSAPI 发送消息 Handler
  - `excuteWithWrapper:` — 主入口
  - `wk_contentFromSendMsg:` — 提取文本内容（从 `sendMsg` dict）
  - `wk_mediaidFromSendMsg:` — 提取媒体 ID（从 `sendMsg` dict）
  - `wk_miniprogramFromSendMsg:` — 提取小程序消息（从 `sendMsg` dict）
  - `wk_handleWeAppMsgFromSendMsg:` — 处理微信应用消息（从 `sendMsg` dict）
- `WEWJSAPIOpenChatWithMsgHandler` — 打开聊天并发送消息
  - `wk_openWithChatId:withSendMsg:` — 打开聊天
  - `wk_openWithUserId:andOuterUserIds:withName:withSendMsg:` — 打开用户聊天
  - `wk_messageFromSendMsg:` — 从发送消息中提取消息
  - `wk_linkDeskFromSendMsg:` — 从发送消息中提取链接
- `WEWJSAPIOpenExistedChatWithMsgHandler` — 打开已有聊天并发送消息
- `WEWJSAPICreateChatWithMessageHandler` — 创建聊天并发送消息
  - `createLinkPreviewMsg` — 创建链接预览消息

**WeClaw（AI 助手）发送路径**
- `[OpenClawService] SendChatMessage` — AI 助手消息发送
- `[OpenClaw::BotMessageListener] BeforeSendMessageLocal:` — 发送前监听
- `weclaw/mcp_tool/mcp_tool_dispatcher.cpp` — MCP 工具分发

**消息类型**（从二进制字符串提取的 `k` 前缀常量）
- `kText`, `kImage`, `kVideo`, `kFile`, `kLink`, `kNews`, `kRichText`, `kMarkDownMsg`, `kMixedMsg`, `kLocation`, `kVoice`, `kWeAPPMsg`, `kContentFile`, `kContentImage`, `kContentVideo`, `kContentVoice`
- `kAppTextCard`, `kAppCardMsg`, `kCardBoth`, `kCardPck`, `kFile`, `kHongBaoMsg`, `kMeetingInviteMsg`, `kTodoCard`, `kWorkLog`, `kSurvey`, `kVotec`
- 消息体通过 protobuf 序列化
- CDN 发送：`sendmsg_via_cdn`

**内部发送流程**
```
用户输入 → Flutter UI → Dart 层
  → [SendMsg] SendMessageLocal → DoSendMessage
  → ChatTask::OnSendMessageCallback
  → DynamicSendMsgTask::DoSendMessages conv=<convId>
  → DynamicSendMsgTask::SplitAndSendParallel: parallel_size=<N>```

---

### 3.2 消息发送的 Flutter Channel

**相关 Channel**
- `/wxwork_channels.WxWorkBaseChannels/WWKDMessageApiMacImpl`
- `/wxwork_channels.WxWorkBaseChannels/WWKDConversationApiMacImpl`

**暴露的方法**（从 FlutterError 签名推断）
- `SendMessage` — 发送消息
- `GetMessages` — 获取消息
- `SearchMessageOfConv` — 搜索会话消息
- `GetTopMessage` — 获取置顶消息

---

## 4. 改名相关实现

### 4.1 改名路径

**内部处理链**
```
用户输入 → Flutter UI → setConvName: → ModifyGroupInfo → 同步到服务器
```

**关键实现**
- `setConvName:` — 设置会话名称
- `ModifyGroupInfo type:` — 修改群信息（包括群名）
- `wxwork://jump?target=input_conv_name&conv_id=` — 打开改名输入框
- `[WEWPreferenceAddChatGroupWindowControllerDelegator]` — 群设置代理

**相关 UI 组件**
- `setConvNameField:` — 设置名称输入框
- `setConvNameTextField:` — 名称文本框
- `setConvNameLabel:` — 名称标签
- `setConvNameBackgroundView:` — 名称背景视图
- `setConvNameTipsLabel:` — 名称提示标签
- `WEWModifyGroupPushAvatarView` — 修改群头像视图
- `WEWModifyGroupNickNameMenuItem:` — 修改群昵称菜单项

**权限相关**
- `ExternalConversation.OnlyGroupManagerCanModifyGroupTitle` — 仅群主可修改群名
- `ExternalConversation.ForbiddenModifyGroupName` — 禁止修改群名
- `Common.MsgView.ModifyGroupNameAs` — 群名修改显示格式

---

## 5. 建群相关实现

### 5.1 建群路径

**内部处理链**
```
用户输入 → Flutter UI → p_createGroupChatWithUserList → CreateConversationInner → 同步到服务器
```

**关键实现**
- `p_createGroupChatWithUserList` — 通过用户列表创建群（内部方法）
  - `p_createGroupChatWithUserList service mgr nil` — 服务管理器为空
  - `p_createGroupChatWithUserList fail, folder or accountInfo is nullptr` — 失败原因
  - `p_createGroupChatWithUserList createPromise err:` — 创建错误
- `CreateConversationWithoutCheck` — 创建会话（不检查）
- `CreateConversationMergeWxFriend` — 合并微信好友创建会话
  - `CreateConversationMergeWxFriend fail: CreateConversationInner fail`
  - `CreateConversationMergeWxFriend succ: need_verify:`
- `dev.wework.flutter.GlobalSearchApi.openCreateGroupWindowBySearchKey` — 通过搜索打开建群窗口

**协议层建群相关**
- `CreateGroupRequestPackage` / `CreateGroupResponsePackage` — 创建群请求/响应
- `EnterGroupRequestPackage` / `EnterGroupResponsePackage` — 进入群
- `ExitGroupRequestPackage` / `ExitGroupResponsePackage` — 退出群
- `CheckGroupRequestPackage` / `CheckGroupResponsePackage` — 检查群
- `ChangeSceneRequestPackage` / `ChangeSceneResponsePackage` — 改变场景
- `RefreshKeyRequestPackage` / `RefreshKeyResponsePackage` — 刷新密钥
- `RefreshCertRequestPackage` / `RefreshCertResponsePackage` — 刷新证书
- `TmSyncMemberRequestPackage` / `TmSyncMemberResponsePackage` — 同步成员
- `VoiceRedirectRequestPackage` / `VoiceRedirectResponsePackage` — 语音重定向
- `RejectEnterGroupRequestPackage` / `RejectEnterGroupResponsePackage` — 拒绝入群
- `CancelCreateGroupRequestPackage` / `CancelCreateGroupResponsePackage` — 取消建群
- `SubscribeLargeVideoRequestPackage` / `SubscribeLargeVideoResponsePackage` — 订阅大屏
- `AuthRequestPackage` / `AuthResponsePackage` — 认证
- `SyncRequestPackage` / `SyncResponsePackage` — 同步
- `ReportRequestPackage` / `ReportResponsePackage` — 上报
- `DocShareRequestPackage` / `DocShareResponsePackage` — 文档分享
- `HelloGroupRequestPackage` / `HelloGroupResponsePackage` — 群握手

**Flutter Channel 建群**
- `dev.wework.flutter.GlobalSearchApi.openCreateGroupWindowBySearchKey` — 打开建群窗口
- `wework_createWedoc` — 创建文档
- `Calendar.CreateChatGroup` — 日历创建群

**其他相关**
- `CreateConversationWithoutCheck` — 创建会话（不检查）
- `AddMemebersMergeWxFriend` — 合并微信好友添加成员
- `checkAndCreateConversation` — 检查并创建会话
- `reportCreateConv:` — 创建会话上报
- `maxCountWithCreateConv` — 创建会话最大数量

---

## 6. 方案对比与可行性分析

### 6.1 现有方案（AppleScript + URL Scheme）

| 能力 | 实现方式 | 可行性 |
|---|---|---|
| 发消息 | `wxwork://jump?target=conv&...` + AppleScript 粘贴/回车 | ✅ 稳定 |
| 改名 | 新版：AX TextField；旧版：`input_conv_name` + AppleScript | ✅ 可用 |
| 建群 | AppleScript 操控面板（AX 树 + 坐标点击） | ✅ 可用 |

**优点**
- 已经稳定运行
- 发消息已用 URL Scheme 替代坐标
- 不依赖内部协议

**缺点**
- 建群和旧版改名仍需坐标点击
- 无送达回执（本人身份）
- 依赖辅助功能权限

---

### 6.2 Mach Port JSON-IPC

| 能力 | 可行性 | 说明 |
|---|---|---|
| 发消息 | ✅ **已知 method** | `qy__sendChatMessage`（需逆向 params 格式） |
| 改名 | ⚠️ 需逆向 | 内部用 `ModifyGroupInfo type:`, `setConvName:`（非 JSON-IPC 方法） |
| 建群 | ❌ **无对应 JSON-IPC** | 内部用 `p_createGroupChatWithUserList`（C++ 内部方法，无 JSON-IPC 入口） |

**已确认的 JSON-IPC 方法**
- `qy__sendChatMessage` — 发送消息（Handler: `WEWJSAPISendChatMessageHandler`）
  - 源码路径：`src/mac/UI/Conversation/OpenAPI/Handlers/App/WEWJSAPISendChatMessageHandler.mm`
  - Handler 方法：
    - `excuteWithWrapper:` — 主入口
    - `wk_contentFromSendMsg:` — 提取文本内容
    - `wk_mediaidFromSendMsg:` — 提取媒体 ID
    - `wk_miniprogramFromSendMsg:` — 提取小程序消息
    - `wk_handleWeAppMsgFromSendMsg:` — 处理微信应用消息
- `WEWJSAPIOpenChatWithMsgHandler` — 打开聊天并发送消息
  - 源码路径：`src/mac/UI/Conversation/OpenAPI/Handlers/App/WEWJSAPISendChatMessageHandler.mm`
  - Handler 方法：
    - `wk_openWithChatId:withSendMsg:` — 打开聊天
    - `wk_openWithUserId:andOuterUserIds:withName:withSendMsg:` — 打开用户聊天
    - `wk_messageFromSendMsg:` — 从发送消息中提取消息
    - `wk_linkDeskFromSendMsg:` — 从发送消息中提取链接

**`sendMsg` 参数结构推断**（基于 Handler 方法签名）
```json
{
  "sendMsg": {
    "convId": <int64>,           // 会话 ID（群/私聊）
    "roomId": <int64>,           // 兼容字段，等价于 convId
    "msgType": <int>,            // 消息类型（kText=1, kImage=2, kVideo=3, kFile=4, kLink=5, kNews=6, kMarkDownMsg=7, kMixedMsg=8, kLocation=9, kVoice=10, kWeAPPMsg=11, 等）
    "content": "<string>",       // 消息内容（文本、链接描述等）
    "mediaid": "<string>",       // 媒体文件 ID（图片/视频/文件）
    "miniprogramInfo": {},       // 小程序信息
    "linkDeskInfo": {}           // 链接卡片信息
  }
}
```

**数据库消息字段**（用于交叉验证）
- `rid`, `seq`, `room_id`, `sender`, `sender_name`, `message_type`, `content_type`, `blob`

**未找到 JSON-IPC 方法的操作**
- 改名 — 内部使用 Objective-C 方法 `setConvName:`, `ModifyGroupInfo type:`，无对应 `qy__` 方法
- 建群 — 内部使用 C++ 方法 `p_createGroupChatWithUserList`, `CreateConversationInner`，无对应 `qy__` 方法

**JSON-IPC 消息格式**
```json
{
  "method": "qy__sendChatMessage",
  "params": {
    "sendMsg": {
      "convId": <int64>,
      "msgType": <int>,
      "content": "<string>",
      "mediaid": "<string>"  // 可选，用于媒体消息
    }
  },
  "timestamp": 1234567890,
  "version": "1.0"
}
```

**JSON-RPC/MCP 格式**（`handleProxy` 处理）
```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {"name": "...", "arguments": {...}},
  "id": 1
}
```

**优点**
- `qy__sendChatMessage` 已确认存在
- 如果 params 格式可推断，发消息可直接用 JSON-IPC

**缺点**
- 改名和建群无 JSON-IPC 入口
- params 格式需逆向（JSAPI handler 源码级）
- TeamID 签名验证
- 企微升级可能破坏协议
- 逆向成本高

---

### 6.3 Flutter Platform Channel

| 能力 | 可行性 | 说明 |
|---|---|---|
| 发消息 | ❌ 不可用 | 没有消息发送 Channel |
| 改名 | ❌ 不可用 | 没有改名 Channel |
| 建群 | ❌ 不可用 | 没有建群 Channel |

**优点**
- 无

**缺点**
- 没有暴露相关 Channel

---

### 6.4 Dart RPC Service

| 能力 | 可行性 | 说明 |
|---|---|---|
| 发消息 | ❌ 不可用 | 没有消息发送 RPC |
| 改名 | ❌ 不可用 | 没有改名 RPC |
| 建群 | ❌ 不可用 | 没有建群 RPC |

**优点**
- 无

**缺点**
- 暴露的方法主要是文件/系统操作

---

### 6.5 WebSocket 调试端口

| 能力 | 可行性 | 说明 |
|---|---|---|
| 发消息 | ❌ 不可用 | 端口未激活 |
| 改名 | ❌ 不可用 | 端口未激活 |
| 建群 | ❌ 不可用 | 端口未激活 |

**优点**
- 无

**缺点**
- 端口未激活，需调试模式

---

### 6.6 JSAPI

| 能力 | 可行性 | 说明 |
|---|---|---|
| 发消息 | ❌ 不可用 | 仅限 H5 页面 |
| 改名 | ❌ 不可用 | 仅限 H5 页面 |
| 建群 | ❌ 不可用 | 仅限 H5 页面 |

**优点**
- 无

**缺点**
- 仅限 H5 页面调用

---

## 7. 消息类型常量

从二进制字符串中提取的消息类型常量（`k` 前缀，用于 `msgType` / `message_type` 字段）：

**基础类型**
| 类型 | 常量 | 说明 |
|---|---|---|
| 文本 | `kText` | 纯文本消息 |
| 图片 | `kImage`, `kContentImage`, `kContentImageCDN`, `kContentImageP2P` | 图片消息 |
| 视频 | `kVideo`, `kContentVideo`, `kContentVideoWWFTN` | 视频消息 |
| 文件 | `kFile`, `kContentFile`, `kContentFileCDN`, `kContentFileP2P` | 文件消息 |
| 链接 | `kLink` | 链接预览消息 |
| 语音 | `kVoice`, `kContentVoice`, `kContentVoiceWWFTN` | 语音消息 |
| 位置 | `kLocation` | 位置消息 |

**富媒体类型**
| 类型 | 常量 | 说明 |
|---|---|---|
| 小程序 | `kWeAPPMsg` | 微信应用消息 |
| 图文 | `kMixedMsg`, `kMixedMsg2` | 混合消息 |
| 富文本 | `kRichText` | 富文本消息 |
| Markdown | `kMarkDownMsg` | Markdown 消息 |

**卡片类型**
| 类型 | 常量 | 说明 |
|---|---|---|
| 文本卡片 | `kAppTextCard` | 文本卡片 |
| 应用卡片 | `kAppCardMsg`, `kCardApp` | 应用卡片 |
| 任务卡片 | `kAppTaskCard`, `kAppTaskCardV2` | 任务卡片 |
| 新闻卡片 | `kAppNewsk` | 新闻卡片 |
| 双面卡片 | `kCardBoth`, `kCardPck` | 双面卡片 |

**业务类型**
| 类型 | 常量 | 说明 |
|---|---|---|
| 红包 | `kHongBaoMsg`, `kHongBaoAck`, `kHongBaoSysMsg` | 红包消息 |
| 投票 | `kVote`, `kVoteAck` | 投票消息 |
| 问卷 | `kSurvey` | 问卷消息 |
| 日程 | `kMeetingInviteMsg` | 会议邀请 |
| Todo | `kTodoCard`, `kTodoChangeNotify` | Todo 卡片 |
| 日志 | `kWorkLog` | 工作日志 |
| 签到 | `kCheckinNotice` | 签到通知 |

**微信相关**
| 类型 | 常量 | 说明 |
|---|---|---|
| 微信文件 | `kWechatFile` | 微信文件 |
| 微信图片 | `kWechatImage` | 微信图片 |
| 微信语音 | `kWechatVoice` | 微信语音 |
| 微信视频 | `kWechatVideo` | 微信视频 |

**其他类型**
| 类型 | 常量 | 说明 |
|---|---|---|
| 表情 | `kEmotion`, `kWechatEmotion` | 表情消息 |
| 系统通知 | `kSystemInfoNotify`, `kContentIdSystemTips` | 系统通知 |
| 撤回消息 | `kRevokeMsg` | 撤回消息 |
| 转发 | `kForward` | 转发消息 |
| 合并消息 | `kPVMergeMultiMsg`, `kPVMergeSingleMsg` | 合并转发 |

---

## 8. 结论

### 8.1 当前推荐方案

**继续使用现有方案（URL Scheme + AppleScript）**

理由：
1. 发消息已经用 URL Scheme 替代了坐标，稳定可靠
2. 只有建群和旧版改名仍需坐标，但已是最优解
3. Mach Port JSON-IPC 逆向成本高，且企微升级可能破坏协议
4. 其他方案（Flutter Channel、Dart RPC、WebSocket、JSAPI）均不可用

### 8.2 未来探索方向

如果必须替换坐标点击（建群/改名），可探索：

1. **JSAPI `qy__sendChatMessage` bridge/context 研究**
   - 已确认 Mach Port 不分发 `qy__` 方法，停止穷举 Mach payload
   - 已确认参数为 `msgtype` + `content`/`mediaid`，目标会话来自授权 webview context
   - 若继续 PoC，需要企微自身已加载且获授权的聊天侧边栏 H5；普通群聊天主界面没有 `AXWebArea`
   - 在“不注入/不重签/不修改官方二进制”约束下，不能从外部构造合法 wrapper
   - 结论：不能替换 URL Scheme + AX 的本人身份发消息流程

2. **改名/建群的替代方案**
   - 改名：`setConvName:` 是 Objective-C 方法，无 JSON-IPC 入口
   - 建群：`p_createGroupChatWithUserList` 是 C++ 内部方法，无 JSON-IPC 入口
   - 结论：这两个操作**无法通过 JSON-IPC 实现**，仍需 UI 自动化（AppleScript/AX）

3. **Flutter DevTools 调试**
   - 企微使用 Flutter/Dart 作为 UI 框架
   - 尝试连接 Flutter DevTools（可能需特定条件）
   - 查看 Flutter 层暴露的 MethodChannel
   - 可能找到更高层的调用入口

4. **CEF DevTools**
   - 企微的 UI 部分基于 CEF（Chrome Embedded Framework）
   - 尝试连接 CEF DevTools
   - 可能找到 JavaScript 调用入口（JSAPI 已证实仅限 H5 页面）

5. **WeClaw IPC 逆向**
   - 企微新版引入了 WeClaw AI 助手
   - WeClaw 使用 JSON-IPC 与原生层通信
   - `WEWWeClawIPCLogic::doDispatchJsonReq` 可能提供新的 JSON 消息格式参考

---

---

## 9. 附录：完整方法列表（续）

### 9.1 Flutter Platform Channel 完整方法列表

| 方法 | 用途 |
|---|---|
| `dev.wework.flutter.AppApi.getFiltedAppList` | 获取过滤应用列表 |
| `dev.wework.flutter.AppApi.getShouldShowConvApp` | 获取应显示的会话应用 |
| `dev.wework.flutter.ConversationApi.openConversation` | 打开会话 |
| `dev.wework.flutter.ConversationApi.previewConversation` | 预览会话 |
| `dev.wework.flutter.ConversationApi.openApp` | 打开应用 |
| `dev.wework.flutter.ConversationApi.getConversationName` | 获取会话名称 |
| `dev.wework.flutter.FileApi.showInFinder` | 在 Finder 中显示 |
| `dev.wework.flutter.FileApi.copy2board` | 复制到剪贴板 |
| `dev.wework.flutter.FileApi.docCopy2board` | 文档复制到剪贴板 |
| `dev.wework.flutter.FileApi.openFile` | 打开文件 |
| `dev.wework.flutter.FileApi.openFileByPb` | 按 protobuf 打开文件 |
| `dev.wework.flutter.FileApi.openDoc` | 打开文档 |
| `dev.wework.flutter.FileApi.saveAs` | 另存为 |
| `dev.wework.flutter.FlutterContainerApi.close` | 关闭 Flutter 容器 |
| `dev.wework.flutter.GlobalSearchApi.openCreateGroupWindowBySearchKey` | 通过搜索打开建群窗口 |
| `dev.wework.flutter.GlobalSearchApi.filter` | 过滤搜索 |
| `dev.wework.flutter.GlobalSearchApi.getFileSavePath` | 获取文件保存路径 |
| `dev.wework.flutter.GlobalSearchApi.openFilter` | 打开过滤器 |
| `dev.wework.flutter.GlobalSearchApi.search` | 搜索 |
| `dev.wework.flutter.GlobalSearchApi.switchChatRecordTab` | 切换聊天记录标签 |
| `dev.wework.flutter.GlobalSearchApi.switchMailTab` | 切换邮件标签 |
| `dev.wework.flutter.LogCatch.catchError` | 捕获错误 |
| `dev.wework.flutter.MessageApi.forward` | 转发消息 |
| `dev.wework.flutter.MessageApi.docForward` | 转发文档 |
| `dev.wework.flutter.MessageApi.collect` | 收藏消息 |
| `dev.wework.flutter.MessageApi.save2wedrive` | 保存到 WeDrive |
| `dev.wework.flutter.PlatformApi.isNetworkConnected` | 检查网络连接 |
| `dev.wework.flutter.PlatformApi.isWifiConnected` | 检查 WiFi |
| `dev.wework.flutter.PlatformApi.setLocalConfigLong` | 设置本地配置（long） |
| `dev.wework.flutter.PlatformApi.getLocalConfigLong` | 获取本地配置（long） |
| `dev.wework.flutter.PlatformApi.setLocalConfigString` | 设置本地配置（string） |
| `dev.wework.flutter.PlatformApi.getLocalConfigString` | 获取本地配置（string） |
| `dev.wework.flutter.PlatformApi.systemVersion` | 获取系统版本 |
| `dev.wework.flutter.SystemApi.showImage` | 显示图片 |
| `dev.wework.flutter.SystemApi.pickImageAndVideo` | 选择图片和视频 |
| `dev.wework.flutter.SystemApi.showVideo` | 显示视频 |
| `dev.wework.flutter.SystemApi.callPhone` | 打电话 |
| `dev.wework.flutter.SystemApi.tackPicture` | 截图 |
| `dev.wework.flutter.SystemApi.openCommonWebView` | 打开通用 WebView |
| `dev.wework.flutter.UserApi.commonChoose` | 通用选择 |
| `dev.wework.flutter.UserApi.departmentChoose` | 部门选择 |
| `dev.wework.flutter.UserApi.toUserProfile` | 查看用户资料 |
| `dev.wework.flutter.UserFormatApi.getCorpNameByCorpId` | 获取企业名称（CorpId） |
| `dev.wework.flutter.UserFormatApi.getCorpNameByVid` | 获取企业名称（Vid） |
| `dev.wework.flutter.UserFormatApi.getUserAvatarConfig` | 获取头像配置 |
| `dev.wework.flutter.UserFormatApi.getUserCompleteName` | 获取用户全名 |
| `dev.wework.flutter.UserFormatApi.getUserDisplayName` | 获取用户显示名 |
| `dev.wework.flutter.UserFormatApi.getUserFullDepartmentsChain` | 获取用户完整部门链 |
| `dev.wework.flutter.WeworkUiApi.getCachedNetworkImagePath` | 获取缓存网络图片路径 |

### 9.2 已发现的 JSON-IPC 方法完整列表（`qy__` 前缀）

| 方法 | 用途 |
|---|---|
| `qy__sendChatMessage` | **发送聊天消息**（核心方法） |
| `qy__selectEnterpriseContact` | 选择企业联系人 |
| `qy__selectExternalContact` | 选择外部联系人 |
| `qy__selectPrivilegedContact` | 选择特权联系人 |
| `qy__openUserProfile` | 打开用户资料 |
| `qy__openEnterpriseChat` | 打开企业聊天 |
| `qy__openThirdAppServiceChat` | 打开第三方应用聊天 |
| `qy__createDoc` | 创建文档 |
| `qy__login` | 登录 |
| `qy__getSystemInfo` | 获取系统信息 |
| `qy__getUserInfo` | 获取用户信息 |
| `qy__getSetting` | 获取设置 |
| `qy__getContext` | 获取上下文 |
| `qy__getShareInfo` | 获取分享信息 |
| `qy__getAvatar` | 获取头像 |
| `qy__getEmail` | 获取邮箱 |
| `qy__getMobile` | 获取手机号 |
| `qy__getQrCode` | 获取二维码 |
| `qy__openUrl` | 打开 URL |
| `qy__openSystemBrowserWithSt` | 打开系统浏览器 |
| `qy__navigateToApp` | 导航到应用 |
| `qy__navigateBackApp` | 返回应用 |
| `qy__openAppComment` | 打开应用评论 |
| `qy__openAppManage` | 打开应用管理 |
| `qy__openAppDeviceDataAuth` | 打开设备数据授权 |
| `qy__openAppPurchase` | 打开应用购买 |
| `qy__shareToExternalContact` | 分享到外部联系人 |
| `qy__setShareAttr` | 设置分享属性 |
| `qy__startMeeting` | 发起会议 |
| `qy__startLiving` | 发起直播 |
| `qy__replayLiving` | 回放直播 |
| `qy__printFile` | 打印文件 |
| `qy__selectDatazoneDoc` | 选择数据域文档 |
| `qy__selectDatazoneFile` | 选择数据域文件 |
| `qy__wedocSelectDoc` | 选择 WeDoc |
| `qy__wedriveSelectFile` | 选择 WeDrive 文件 |
| `qy__wedriveSelectDir` | 选择 WeDrive 目录 |
| `qy__wedriveSelectFileForDownload` | 选择文件下载 |
| `qy__wedriveSelectFileForShare` | 选择文件分享 |
| `qy__canIUsePrivate` | 检查私聊权限 |
| `qy__checkSession` | 检查会话 |
| `qy__checkSchedule` | 检查日程 |
| `qy__operatePrivate` | 操作私聊 |
| `qy__updateEnterpriseChat` | 更新企业聊天 |

### 9.3 协议层建群相关包

| 包 | 类型 |
|---|---|
| `CreateGroupRequestPackage` / `CreateGroupResponsePackage` | 创建群 |
| `EnterGroupRequestPackage` / `EnterGroupResponsePackage` | 进入群 |
| `ExitGroupRequestPackage` / `ExitGroupResponsePackage` | 退出群 |
| `CheckGroupRequestPackage` / `CheckGroupResponsePackage` | 检查群 |
| `ChangeSceneRequestPackage` / `ChangeSceneResponsePackage` | 改变场景 |
| `RefreshKeyRequestPackage` / `RefreshKeyResponsePackage` | 刷新密钥 |
| `RefreshCertRequestPackage` / `RefreshCertResponsePackage` | 刷新证书 |
| `TmSyncMemberRequestPackage` / `TmSyncMemberResponsePackage` | 同步成员 |
| `VoiceRedirectRequestPackage` / `VoiceRedirectResponsePackage` | 语音重定向 |
| `RejectEnterGroupRequestPackage` / `RejectEnterGroupResponsePackage` | 拒绝入群 |
| `CancelCreateGroupRequestPackage` / `CancelCreateGroupResponsePackage` | 取消建群 |
| `SubscribeLargeVideoRequestPackage` / `SubscribeLargeVideoResponsePackage` | 订阅大屏 |
| `AuthRequestPackage` / `AuthResponsePackage` | 认证 |
| `SyncRequestPackage` / `SyncResponsePackage` | 同步 |
| `ReportRequestPackage` / `ReportResponsePackage` | 上报 |
| `DocShareRequestPackage` / `DocShareResponsePackage` | 文档分享 |
| `HelloGroupRequestPackage` / `HelloGroupResponsePackage` | 群握手 |

---

## 10. 参考

**源码路径**
- `src/mac/Application/AppConnect/MachPort/WEWPortMessageServer.mm`
- `src/mac/Application/AppConnect/MachPort/WEWIOAWeComConnectManager.mm`
- `src/mac/Application/AppConnect/MachPort/WEWPingPangChecker.mm`
- `src/mac/Application/AppConnect/JsonIPC/ipc_package.cpp`
- `src/mac/Application/AppConnect/JsonIPC/WEWIPCLogicBase.mm`
- `src/mac/Application/AppConnect/JsonIPC/WEWWeClawIPCLogic.mm`
- `src/mac/UI/Conversation/OpenAPI/Handlers/App/WEWJSAPISendChatMessageHandler.mm`
- `src/mac/UI/Conversation/OpenAPI/Handlers/App/WEWJSAPIOpenChatWithMsgHandler.mm`
- `src/mac/UI/Conversation/OpenAPI/Handlers/App/WEWJSAPICreateChatWithMessageHandler.mm`
- `src/mac/UI/Conversation/OpenAPI/WEWJSAPISendAppMessageHandler.mm`
- `src/mac/Flutter/ChannelImp/Advance/Impl/WWKDartAdvanceFuncChannelApiMacImpl.mm`
- `src/mac/Flutter/ChannelImp/BaseChannel/WWKDMessageApiMacImpl.mm`
- `src/mac/Flutter/ChannelImp/BaseChannel/WWKDConversationApiMacImpl.mm`

**客户端文件**
- `/Applications/企业微信.app/Contents/Info.plist`
- `/Applications/企业微信.app/Contents/MacOS/企业微信`
