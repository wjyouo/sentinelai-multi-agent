"""Forum engine routes."""

from fastapi import APIRouter, HTTPException, Request

from app.services.forum_service import (
    start_forum_engine, stop_forum_engine,
    get_forum_log, get_forum_log_history,
)

router = APIRouter(prefix="/api/forum", tags=["forum"])


@router.get("/start")
def forum_start():
    try:
        success = start_forum_engine()
        if success:
            return {"success": True, "message": "ForumEngine论坛已启动"}
        return {"success": False, "message": "ForumEngine论坛启动失败"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stop")
def forum_stop():
    try:
        stop_forum_engine()
        return {"success": True, "message": "ForumEngine论坛已停止"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/log")
def forum_log():
    try:
        result = get_forum_log()
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/log/history")
async def forum_log_history(request: Request):
    try:
        data = await request.json()
        start_position = data.get("position", 0)
        max_lines = data.get("max_lines", 1000)
        result = get_forum_log_history(start_position, max_lines)
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
