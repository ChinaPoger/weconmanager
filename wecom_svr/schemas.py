"""请求/响应模型。"""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .group import MAX_NAME_LEN


class NewsArticle(BaseModel):
    """企业微信 news 图文消息中的一篇外链文章。"""

    title: str = Field(..., min_length=1, description="标题")
    description: str = Field("", description="摘要")
    url: str = Field(..., min_length=1, description="点击后打开的链接")
    picurl: str = Field("", description="封面图片 URL")


class SendMessageRequest(BaseModel):
    """以「自建应用」身份发消息(官方 API)。"""

    model_config = ConfigDict(json_schema_extra={
        "example": {"touser": "zhangsan", "msgtype": "text", "content": "你好"}
    })

    touser: str = Field(..., min_length=1, description="接收人 userid,多人用 | 分隔")
    msgtype: Literal["text", "markdown", "news"] = "text"
    content: str = ""
    articles: list[NewsArticle] | None = Field(None, min_length=1, max_length=8)

    @model_validator(mode="after")
    def _check_payload(self):
        if self.msgtype == "news":
            if not self.articles:
                raise ValueError("news 类型需要 articles 字段")
        elif not self.content:
            raise ValueError("缺少 content")
        return self

    def to_agent_args(self) -> dict:
        if self.msgtype == "news":
            return {"touser": self.touser, "articles": [a.model_dump() for a in self.articles or []]}
        return {"touser": self.touser, "content": self.content}


class RecallMessageRequest(BaseModel):
    """撤回自建应用发送的消息。"""

    model_config = ConfigDict(json_schema_extra={"example": {"msgid": "msgid-from-send-response"}})

    msgid: str = Field(..., min_length=1, description="发送接口返回的消息 ID")

    @model_validator(mode="after")
    def _check_msgid(self):
        if not self.msgid.strip():
            raise ValueError("msgid 不能为空")
        self.msgid = self.msgid.strip()
        return self


class LocalSendRequest(BaseModel):
    """以「本人登录账号」身份发送文本或图片。"""

    model_config = ConfigDict(json_schema_extra={
        "examples": [
            {"username": "葛波", "msgtype": "text", "content": "你好"},
            {"group_name": "项目讨论群", "msgtype": "text", "content": "大家好"},
            {"conv_rid": 3395103030, "msgtype": "text", "content": "你好,我是本人"},
            {"conv_rid": 3395103030, "msgtype": "image", "image_url": "https://example.com/a.png"},
        ]
    })

    username: str = Field("", description="同事在企业微信客户端中的显示名")
    group_name: str = Field("", description="企业微信群的显示名")
    to: str = Field("", description="兼容字段:会话/群名称(模糊搜索)")
    conv_rid: int | None = Field(None, description="会话 RID(数据库 ID),精准定位")
    msgtype: Literal["text", "image"] = Field("text", description="消息类型,与官方 API 命名一致")
    content: str = Field("", description="text 消息正文")
    mentions: list[str] | None = Field(
        None,
        description=(
            "text 消息中要转换为真实提及的姓名。正文中须出现对应的 @姓名;不传时"
            "正文中的 @ 按普通文本发送"
        ),
    )
    image_path: str = Field("", description="image 消息的服务端本地图片路径")
    image_url: str = Field("", description="image 消息的 HTTP(S) 图片下载地址")
    image_base64: str = Field("", description="image 消息的 base64,支持 data: 前缀")
    filename: str | None = Field(None, description="配合 image_base64,用于推断临时文件后缀")

    @model_validator(mode="after")
    def _check_payload(self):
        named_targets = [
            bool(self.username.strip()),
            bool(self.group_name.strip()),
            bool(self.to.strip()),
        ]
        if not any(named_targets) and self.conv_rid is None:
            raise ValueError("缺少 username、group_name、to 或 conv_rid")
        if sum(named_targets) > 1:
            raise ValueError("username、group_name、to 只能提供一个")
        if (self.username.strip() or self.group_name.strip()) and self.conv_rid is not None:
            raise ValueError("username/group_name 与 conv_rid 只能提供一种定位方式")

        sources = [
            bool(self.image_path.strip()), bool(self.image_url.strip()), bool(self.image_base64.strip()),
        ]
        if self.msgtype == "text":
            if not self.content:
                raise ValueError("text 类型需要 content")
            if any(sources):
                raise ValueError("text 类型不能传 image_path、image_url 或 image_base64")
        else:
            if self.conv_rid is None:
                raise ValueError("image 类型暂时必须指定 conv_rid")
            if sum(sources) != 1:
                raise ValueError("image_path、image_url、image_base64 必须且只能提供一个")
            if self.content or self.mentions is not None:
                raise ValueError("image 类型不能传 content 或 mentions")
        return self


class ConversationsRequest(BaseModel):
    keyword: str = Field("", description="留空则返回最近会话")


class OpenConversationRequest(BaseModel):
    conv_rid: int = Field(..., description="要打开的会话 RID")


class LocalRecallRequest(BaseModel):
    """撤回本人登录账号在本地客户端发出的消息。"""

    conv_rid: int = Field(..., description="会话 RID")
    lid: int | None = Field(None, description="消息 LID;省略时撤回该会话中本人最新消息")

    @model_validator(mode="after")
    def _check_ids(self):
        if self.conv_rid <= 0:
            raise ValueError("conv_rid 必须为正整数")
        if self.lid is not None and self.lid <= 0:
            raise ValueError("lid 必须为正整数")
        return self


class LocalRecallTaskRequest(LocalRecallRequest):
    """mars 异步撤回任务的请求模型,必须显式指定目标消息。"""

    lid: int = Field(..., description="消息 LID,必须显式指定")


class LocalCreateGroupRequest(BaseModel):
    """以「本人登录账号」身份拉群(操控客户端)。"""

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "members": ["伴风望海", "陈涛@技术支持部", "葛波"],
            "name": "三人组",
            "prepare_only": False,
        }
    })

    members: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]] = Field(
        ...,
        min_length=2,
        description="人员名称数组,除自己外至少 2 个不同成员。自己自动计入,也可以写进数组;"
                    "Windows members[0] 必须是外部联系人,先打开其单聊再按数组顺序选择其余人员;"
                    "同名人员用 姓名@部门 或 userid:ID 精确指定,不支持逗号分隔字符串",
    )
    name: str = Field(
        "", max_length=MAX_NAME_LEN, description="群名。留空则用客户端按成员自动生成的名字"
    )
    nickname: str = Field(
        "", max_length=MAX_NAME_LEN,
        description="我在本群内的昵称。留空则不修改;Windows 暂不支持非空值",
    )
    prepare_only: bool = Field(
        False, strict=True, description="仅 Windows:只预演选人并取消,不提交建群、不改群名",
    )


class LocalRenameGroupRequest(BaseModel):
    """给一个已有的群改名(操控客户端)。

    官方 `appchat/update` 只认自己建的 chatid,改不了这类群,所以只能走客户端。
    """

    model_config = ConfigDict(json_schema_extra={
        "example": {"conv_rid": 3395103030, "name": "客户群-新名字"}
    })

    conv_rid: int = Field(..., description="要改名的群会话 RID(见 /local/conversations)")
    name: str = Field(..., min_length=1, max_length=MAX_NAME_LEN, description="新群名")


class LocalSetNicknameRequest(BaseModel):
    """改我在一个已有群里的昵称(操控客户端)。

    官方 API(含 `appchat/update`)没有改群内昵称的能力,只能走客户端群详情。
    """

    model_config = ConfigDict(json_schema_extra={
        "example": {"conv_rid": 3395103030, "nickname": "小王"}
    })

    conv_rid: int = Field(..., description="群会话 RID(见 /local/conversations)")
    nickname: str = Field(
        ..., min_length=1, max_length=MAX_NAME_LEN, description="我在本群的新昵称"
    )


class ContactSearchRequest(BaseModel):
    keyword: str = Field(..., min_length=1)
    department_id: int = 1


# ─── 响应 ──────────────────────────────────────────────────────────────

class Conversation(BaseModel):
    name: str = ""
    rid: int | None = None
    lid: int | None = None
    type: int | None = None
    fw_id: int = 0


class ConversationsResponse(BaseModel):
    success: bool = True
    conversations: list[Conversation] = []


class LocalSendResponse(BaseModel):
    """本地发送结果(失败时 HTTP 400,body 结构不变)。"""

    model_config = ConfigDict(extra="allow")

    success: bool
    error: str | None = None
    conv_name: str | None = None
    conv_rid: int | None = None
    conv_lid: int | None = None
    conv_type: int | None = None
    conv_fw_id: int | None = None
    open_method: str | None = None
    elapsed_ms: int | None = None


class HealthResponse(BaseModel):
    status: str
    service: str = "wecom-message-api"
    agent_home: str | None = None
    wecom_cli: str | None = None
    auth_enabled: bool = False
    credentials_configured: bool = False
    startup_preflight: dict[str, bool] | None = None
    detail: str | None = None
