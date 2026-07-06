"""Search routes."""

from fastapi import APIRouter, HTTPException, Request

from app.services.search_service import get_latest_results, search_all

router = APIRouter(tags=["search"])


@router.post("/api/search")
async def search(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无效的请求体")

    query = data.get("query", "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="搜索查询不能为空")

    options = data.get("options")
    if options is not None and not isinstance(options, dict):
        raise HTTPException(status_code=400, detail="options must be an object")

    return search_all(query, options)

@router.get("/api/search/latest")
async def latest_search_results():
    return get_latest_results()
