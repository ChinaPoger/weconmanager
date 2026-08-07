"""群聊接口的请求模型。

贯穿全篇的约定: `members` 里写什么都行 —— 中文名、userid、邮箱、或者「我」,
由 directory 统一解析成 userid。解析不出或撞名会 400 并给出候选,不替你猜。
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MEMBERS_DESC = "成员:中文名 / userid / 邮箱 / 「我」,大小写与顺序都不敏感"


class CreateGroupRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"name": "临时讨论组", "members": ["陈涛", "陈挺"], "include_self": True}
    })

    name: str = Field(..., min_length=1, max_length=50, description="群名")
    members: list[str] = Field(..., min_length=1, description=MEMBERS_DESC)
    include_self: bool = Field(True, description="把自己拉进群(默认 true)")
    owner: str | None = Field(None, description="群主,不填则默认自己;自己也认不出时交给服务端随机")
    chatid: str | None = Field(None, description="自定义群 ID(1-32 位 0-9a-zA-Z),不填则随机")


class ResolveMembersRequest(BaseModel):
    """干跑:只看名字会被解析成谁,不建群。"""

    members: list[str] = Field(..., min_length=1, description=MEMBERS_DESC)
    include_self: bool = True


class GroupTarget(BaseModel):
    chatid: str = Field(..., min_length=1)


class UpdateGroupRequest(GroupTarget):
    name: str | None = Field(None, max_length=50, description="新群名")
    owner: str | None = Field(None, description="新群主(同样支持中文名/「我」)")
    add: list[str] = Field(default_factory=list, description=f"要拉进来的{MEMBERS_DESC}")
    remove: list[str] = Field(default_factory=list, description=f"要移出的{MEMBERS_DESC}")

    @model_validator(mode="after")
    def _something_to_change(self):
        if not (self.name or self.owner or self.add or self.remove):
            raise ValueError("name / owner / add / remove 至少给一个")
        return self


class SendGroupRequest(GroupTarget):
    content: str = Field(..., min_length=1)
    msgtype: Literal["text", "markdown"] = "text"


class SearchMemberRequest(BaseModel):
    keyword: str = Field(..., min_length=1, description="按名字/userid/邮箱模糊搜本地通讯录")
