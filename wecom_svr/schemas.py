"""请求/响应模型。"""
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SendMessageRequest(BaseModel):
    """以「自建应用」身份发消息(官方 API)。"""

    model_config = ConfigDict(json_schema_extra={
        "example": {"touser": "zhangsan", "msgtype": "text", "content": "你好"}
    })

    touser: str = Field(..., min_length=1, description="接收人 userid,多人用 | 分隔")
    msgtype: Literal["text", "markdown", "news"] = "text"
    content: str = ""
    articles: Optional[list[dict[str, Any]]] = None

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
            return {"touser": self.touser, "articles": self.articles}
        return {"touser": self.touser, "content": self.content}


class LocalSendRequest(BaseModel):
    """以「本人登录账号」身份发消息。"""

    model_config = ConfigDict(json_schema_extra={
        "example": {"conv_rid": 3395103030, "content": "你好,我是本人"}
    })

    to: str = Field("", description="会话/群名称(模糊搜索),与 conv_rid 二选一")
    conv_rid: Optional[int] = Field(None, description="会话 RID(数据库 ID),精准定位")
    content: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _check_target(self):
        if not self.to.strip() and self.conv_rid is None:
            raise ValueError("缺少 to 或 conv_rid(会话名称 或 数据库 RID)")
        return self


class LocalImageRequest(BaseModel):
    """以「本人登录账号」身份发图片。"""

    model_config = ConfigDict(json_schema_extra={
        "example": {"conv_rid": 3395103030, "image_path": "/tmp/a.png"}
    })

    conv_rid: int = Field(..., description="会话 RID,发图片必填")
    image_path: str = Field("", description="服务端本地图片路径")
    image_base64: str = Field("", description="图片 base64,支持 data: 前缀")
    filename: Optional[str] = Field(None, description="配合 image_base64,用于推断后缀")

    @model_validator(mode="after")
    def _check_source(self):
        if not self.image_path and not self.image_base64.strip():
            raise ValueError("缺少 image_path 或 image_base64")
        return self


class ConversationsRequest(BaseModel):
    keyword: str = Field("", description="留空则返回最近会话")


class OpenConversationRequest(BaseModel):
    conv_rid: int = Field(..., description="要打开的会话 RID")


class ContactSearchRequest(BaseModel):
    keyword: str = Field(..., min_length=1)
    department_id: int = 1


# ─── 响应 ──────────────────────────────────────────────────────────────

class Conversation(BaseModel):
    name: str = ""
    rid: Optional[int] = None
    lid: Optional[int] = None
    type: Optional[int] = None
    fw_id: int = 0


class ConversationsResponse(BaseModel):
    success: bool = True
    conversations: list[Conversation] = []


class LocalSendResponse(BaseModel):
    """本地发送结果(失败时 HTTP 400,body 结构不变)。"""

    model_config = ConfigDict(extra="allow")

    success: bool
    error: Optional[str] = None
    conv_name: Optional[str] = None
    conv_rid: Optional[int] = None
    conv_lid: Optional[int] = None
    conv_type: Optional[int] = None
    conv_fw_id: Optional[int] = None
    open_method: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    service: str = "wecom-message-api"
    agent_home: Optional[str] = None
    wecom_cli: Optional[str] = None
    auth_enabled: bool = False
    credentials_configured: bool = False
    detail: Optional[str] = None
