"""智能表 —— 驱动外部 wecom-cli(上游 online/ 包)。"""
from fastapi import APIRouter, Depends

from ..online_bridge import invoke
from ..schemas_online import (
    CreateSmartsheetRequest,
    DocTarget,
    SmartsheetAddRecordsRequest,
    SmartsheetAddSheetRequest,
    SmartsheetDeleteFieldsRequest,
    SmartsheetDeleteRecordsRequest,
    SmartsheetFieldsRequest,
    SmartsheetGetFieldsRequest,
    SmartsheetSheetIdRequest,
    SmartsheetUpdateRecordsRequest,
    SmartsheetUpdateSheetRequest,
)
from ..security import require_token

router = APIRouter(prefix="/api/v1/smartsheet", tags=["smartsheet"])


@router.post("/create", summary="新建智能表")
def create_smartsheet(req: CreateSmartsheetRequest, _: None = Depends(require_token)) -> dict:
    return invoke(lambda m: m.smartsheets.create_smartsheet, name=req.name, confirmed=req.confirmed)


@router.post("/sheets", summary="查子表列表(只读)")
def get_sheets(req: DocTarget, _: None = Depends(require_token)) -> dict:
    return invoke(lambda m: m.smartsheets.get_sheets, docid=req.docid, url=req.url)


@router.post("/sheet/add", summary="新增子表")
def add_sheet(req: SmartsheetAddSheetRequest, _: None = Depends(require_token)) -> dict:
    return invoke(
        lambda m: m.smartsheets.add_sheet,
        docid=req.docid, url=req.url, title=req.title, confirmed=req.confirmed,
    )


@router.post("/sheet/update", summary="改子表标题")
def update_sheet(req: SmartsheetUpdateSheetRequest, _: None = Depends(require_token)) -> dict:
    return invoke(
        lambda m: m.smartsheets.update_sheet,
        docid=req.docid, url=req.url, sheet_id=req.sheet_id,
        title=req.title, confirmed=req.confirmed,
    )


@router.post("/sheet/delete", summary="删除子表")
def delete_sheet(req: SmartsheetSheetIdRequest, _: None = Depends(require_token)) -> dict:
    return invoke(
        lambda m: m.smartsheets.delete_sheet,
        docid=req.docid, url=req.url, sheet_id=req.sheet_id, confirmed=req.confirmed,
    )


# ─── 字段 ──────────────────────────────────────────────────────────────

@router.post("/fields/get", summary="查字段(只读)")
def get_fields(req: SmartsheetGetFieldsRequest, _: None = Depends(require_token)) -> dict:
    return invoke(
        lambda m: m.smartsheets.get_fields,
        docid=req.docid, url=req.url, sheet_id=req.sheet_id,
    )


@router.post("/fields/add", summary="新增字段")
def add_fields(req: SmartsheetFieldsRequest, _: None = Depends(require_token)) -> dict:
    return invoke(
        lambda m: m.smartsheets.add_fields,
        docid=req.docid, url=req.url, sheet_id=req.sheet_id,
        fields=req.fields, confirmed=req.confirmed,
    )


@router.post("/fields/update", summary="改字段")
def update_fields(req: SmartsheetFieldsRequest, _: None = Depends(require_token)) -> dict:
    return invoke(
        lambda m: m.smartsheets.update_fields,
        docid=req.docid, url=req.url, sheet_id=req.sheet_id,
        fields=req.fields, confirmed=req.confirmed,
    )


@router.post("/fields/delete", summary="删字段")
def delete_fields(req: SmartsheetDeleteFieldsRequest, _: None = Depends(require_token)) -> dict:
    return invoke(
        lambda m: m.smartsheets.delete_fields,
        docid=req.docid, url=req.url, sheet_id=req.sheet_id,
        field_ids=req.field_ids, confirmed=req.confirmed,
    )


# ─── 记录 ──────────────────────────────────────────────────────────────

@router.post("/records/add", summary="新增记录")
def add_records(req: SmartsheetAddRecordsRequest, _: None = Depends(require_token)) -> dict:
    return invoke(
        lambda m: m.smartsheets.add_records,
        docid=req.docid, url=req.url, sheet_id=req.sheet_id,
        records=req.records, confirmed=req.confirmed, auto_file=req.auto_file,
    )


@router.post("/records/update", summary="改记录")
def update_records(req: SmartsheetUpdateRecordsRequest, _: None = Depends(require_token)) -> dict:
    return invoke(
        lambda m: m.smartsheets.update_records,
        docid=req.docid, url=req.url, sheet_id=req.sheet_id,
        records=req.records, key_type=req.key_type,
        confirmed=req.confirmed, auto_file=req.auto_file,
    )


@router.post("/records/delete", summary="删记录")
def delete_records(req: SmartsheetDeleteRecordsRequest, _: None = Depends(require_token)) -> dict:
    return invoke(
        lambda m: m.smartsheets.delete_records,
        docid=req.docid, url=req.url, sheet_id=req.sheet_id,
        record_ids=req.record_ids, confirmed=req.confirmed,
    )
