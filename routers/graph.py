"""GraphRAG knowledge graph routes."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services.graph_service import get_graph_data, get_latest_graph, query_graph

router = APIRouter(tags=["graph"])


def _json_result(result: dict) -> JSONResponse:
    """Return JSON with status code matching Flask convention."""
    success = result.get("success", False)
    if success:
        return JSONResponse(content=result)
    message = result.get("message", "")
    if "未找到" in message:
        return JSONResponse(content=result, status_code=404)
    return JSONResponse(content=result, status_code=500)


@router.get("/api/graph/{report_id}")
def api_get_graph(report_id: str):
    return _json_result(get_graph_data(report_id))


@router.get("/api/graph/latest")
def api_get_latest_graph():
    return _json_result(get_latest_graph())


@router.post("/api/graph/query")
async def api_query_graph(request: Request):
    data = await request.json() or {}
    return _json_result(query_graph(data))
