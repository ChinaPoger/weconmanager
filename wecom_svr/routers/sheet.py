"""在线表格 —— 驱动外部 wecom-cli(上游 online/ 包)。"""
from fastapi import APIRouter, Depends

from ..online_bridge import invoke
from ..schemas_online import (
    AddSubsheetRequest,
    AppendRowRequest,
    CreateSheetRequest,
    DeleteSubsheetRequest,
    DocTarget,
    UpdateRangeRequest,
)
from ..security import require_token

router = APIRouter(prefix="/api/v1/sheet", tags=["sheet"])


@router.post("/create", summary="新建表格")
def create_sheet(req: CreateSheetRequest, _: None = Depends(require_token)) -> dict:
    return invoke(lambda m: m.sheets.create_sheet, name=req.name, confirmed=req.confirmed)


@router.post("/info", summary="查表格结构(只读)")
def sheet_info(req: DocTarget, _: None = Depends(require_token)) -> dict:
    return invoke(lambda m: m.sheets.get_info, docid=req.docid, url=req.url)


@router.post("/subsheet/add", summary="新增子表")
def add_subsheet(req: AddSubsheetRequest, _: None = Depends(require_token)) -> dict:
    return invoke(
        lambda m: m.sheets.add_subsheet,
        docid=req.docid, url=req.url, title=req.title,
        row_count=req.row_count, column_count=req.column_count,
        index=req.index, confirmed=req.confirmed,
    )


@router.post("/subsheet/delete", summary="删除子表")
def delete_subsheet(req: DeleteSubsheetRequest, _: None = Depends(require_token)) -> dict:
    return invoke(
        lambda m: m.sheets.delete_subsheet,
        docid=req.docid, url=req.url, sheet_id=req.sheet_id, confirmed=req.confirmed,
    )


@router.post("/row/append", summary="追加一行")
def append_row(req: AppendRowRequest, _: None = Depends(require_token)) -> dict:
    return invoke(
        lambda m: m.sheets.append_row,
        docid=req.docid, url=req.url, sheet_id=req.sheet_id,
        values=req.values, confirmed=req.confirmed,
    )


@router.post("/range/update", summary="按区域批量写入")
def update_range(req: UpdateRangeRequest, _: None = Depends(require_token)) -> dict:
    return invoke(
        lambda m: m.sheets.update_range,
        docid=req.docid, url=req.url, sheet_id=req.sheet_id,
        start_row=req.start_row, start_column=req.start_column,
        rows=req.rows, confirmed=req.confirmed,
    )
