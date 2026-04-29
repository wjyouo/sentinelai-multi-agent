"""Search routes."""

from fastapi import APIRouter, HTTPException, Request

from services.search_service import search as search_service

router = APIRouter(tags=["search"])


@router.post("/api/search")
async def search(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无效的请求体")

    query = data.get("query", "").strip()
    result = search_service(query)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result
