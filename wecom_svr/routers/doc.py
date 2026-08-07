"""在线文档 —— 驱动外部 wecom-cli(上游 online/ 包)。

写操作需 confirmed=true,否则 403。全部是同步 def:底层是阻塞的子进程调用。
"""
from fastapi import APIRouter, Depends

from ..online_bridge import OnlineUnavailable, capabilities, invoke, load_online
from ..schemas_online import (
    CreateDocRequest,
    LocalDocReadRequest,
    LocalDocSearchRequest,
    SmartPageRequest,
    WriteDocRequest,
)
from ..security import require_token

router = APIRouter(prefix="/api/v1/doc", tags=["doc"])


@router.get("/capabilities", summary="探测 wecom-cli 版本/登录状态/命令支持")
def doc_capabilities(_: None = Depends(require_token)) -> dict:
    return capabilities()


@router.post("/create", summary="新建文档")
def create_doc(req: CreateDocRequest, _: None = Depends(require_token)) -> dict:
    return invoke(lambda m: m.docs.create_document, name=req.name, confirmed=req.confirmed)


@router.post("/write", summary="以 Markdown 覆写文档正文")
def write_doc(req: WriteDocRequest, _: None = Depends(require_token)) -> dict:
    return invoke(
        lambda m: m.docs.write_document_markdown,
        docid=req.docid, url=req.url, content=req.content, confirmed=req.confirmed,
    )


@router.post("/smartpage", summary="新建智能页")
def create_smartpage(req: SmartPageRequest, _: None = Depends(require_token)) -> dict:
    return invoke(
        lambda m: m.docs.create_smartpage,
        title=req.title, pages=req.pages, confirmed=req.confirmed, auto_file=req.auto_file,
    )


# ─── 本地兜底(不需要 wecom-cli)────────────────────────────────────────

def _local_docs():
    mods = load_online()
    if mods.local_docs is None:
        raise OnlineUnavailable("online.local_docs 不可用(缺 decrypt.read_doc 依赖)")
    return mods.local_docs


@router.post("/local/search", summary="搜本地缓存/已下载的文档(可能过期)")
def search_local_docs(req: LocalDocSearchRequest, _: None = Depends(require_token)) -> dict:
    return _local_docs().search(
        req.keyword, roots=req.roots, max_results=req.max_results, limit=req.limit
    )


@router.post("/local/read", summary="读取本地文档内容(可能过期)")
def read_local_doc(req: LocalDocReadRequest, _: None = Depends(require_token)) -> dict:
    return _local_docs().read_path(req.path, limit=req.limit)
