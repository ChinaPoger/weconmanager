"""文档 / 表格 / 智能表接口的请求模型。

两个贯穿全篇的约定,都来自上游 online/ 包:
  - 目标文档用 docid 或 url 指定,**必须二选一**
  - 写操作要显式 confirmed=true(上游 require_confirmation 的闸门)
"""
from typing import Any

from pydantic import BaseModel, Field, model_validator

CONFIRM_DESC = "线上写操作确认闸门,必须显式传 true"


class Confirmable(BaseModel):
    confirmed: bool = Field(False, description=CONFIRM_DESC)


class DocTarget(BaseModel):
    """docid / url 二选一。"""

    docid: str | None = None
    url: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self):
        if bool(self.docid) == bool(self.url):
            raise ValueError("docid 与 url 必须二选一")
        return self


class ConfirmableDocTarget(DocTarget):
    confirmed: bool = Field(False, description=CONFIRM_DESC)


# ─── 文档 ──────────────────────────────────────────────────────────────

class CreateDocRequest(Confirmable):
    name: str = Field(..., min_length=1, description="文档名")


class WriteDocRequest(ConfirmableDocTarget):
    content: str = Field("", description="Markdown 正文")


class SmartPageRequest(Confirmable):
    title: str = Field(..., min_length=1)
    pages: list[dict[str, Any]] = Field(default_factory=list)
    auto_file: bool = Field(False, description="true 则用 +smartpage_create(自动归档)")


# ─── 表格 ──────────────────────────────────────────────────────────────

class CreateSheetRequest(Confirmable):
    name: str = Field(..., min_length=1)


class AddSubsheetRequest(ConfirmableDocTarget):
    title: str = ""
    row_count: int = 100
    column_count: int = 20
    index: int | None = None


class DeleteSubsheetRequest(ConfirmableDocTarget):
    sheet_id: str = Field(..., min_length=1)


class AppendRowRequest(ConfirmableDocTarget):
    sheet_id: str = Field(..., min_length=1)
    values: list[Any] = Field(
        default_factory=list,
        description="单元格值。数字→NUMBER,其余→TEXT;也可传 {type:link|formula|number|text}",
    )


class UpdateRangeRequest(ConfirmableDocTarget):
    sheet_id: str = Field(..., min_length=1)
    start_row: int = 0
    start_column: int = 0
    rows: list[list[Any]] = Field(default_factory=list)


# ─── 智能表 ────────────────────────────────────────────────────────────

class CreateSmartsheetRequest(Confirmable):
    name: str = Field(..., min_length=1)


class SmartsheetAddSheetRequest(ConfirmableDocTarget):
    title: str | None = None


class SmartsheetUpdateSheetRequest(ConfirmableDocTarget):
    sheet_id: str = Field(..., min_length=1)
    title: str = ""


class SmartsheetSheetIdRequest(ConfirmableDocTarget):
    sheet_id: str = Field(..., min_length=1)


class SmartsheetGetFieldsRequest(DocTarget):
    sheet_id: str = Field(..., min_length=1)


class SmartsheetFieldsRequest(ConfirmableDocTarget):
    sheet_id: str = Field(..., min_length=1)
    fields: list[dict[str, Any]] = Field(default_factory=list)


class SmartsheetDeleteFieldsRequest(ConfirmableDocTarget):
    sheet_id: str = Field(..., min_length=1)
    field_ids: list[str] = Field(default_factory=list)


class SmartsheetAddRecordsRequest(ConfirmableDocTarget):
    sheet_id: str = Field(..., min_length=1)
    records: list[dict[str, Any]] = Field(default_factory=list)
    auto_file: bool = False


class SmartsheetUpdateRecordsRequest(SmartsheetAddRecordsRequest):
    key_type: str | None = None


class SmartsheetDeleteRecordsRequest(ConfirmableDocTarget):
    sheet_id: str = Field(..., min_length=1)
    record_ids: list[str] = Field(default_factory=list)


# ─── 本地兜底 ──────────────────────────────────────────────────────────

class LocalDocSearchRequest(BaseModel):
    keyword: str = Field(..., min_length=1)
    roots: list[str] | None = Field(None, description="搜索根目录,默认企业微信缓存+下载/文档/桌面")
    max_results: int = 5
    limit: int = 8000


class LocalDocReadRequest(BaseModel):
    path: str = Field(..., min_length=1)
    limit: int = 8000
