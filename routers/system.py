"""System start/stop/status routes."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.system_service import (
    _get_system_state, _set_system_state,
    _prepare_system_start, _mark_shutdown_requested,
    initialize_system_components,
    _start_async_shutdown, _describe_running_children,
    _log_shutdown_step,
    processes, STREAMLIT_SCRIPTS,
)
from services.forum_service import stop_forum_engine

router = APIRouter(prefix="/api/system", tags=["system"])


class StatusResponse(BaseModel):
    success: bool
    started: bool
    starting: bool


class StartResponse(BaseModel):
    success: bool
    message: str
    logs: list[str] | None = None
    errors: list[str] | None = None


class ShutdownResponse(BaseModel):
    success: bool
    message: str
    ports: list[str] | None = None


@router.get("/status", response_model=StatusResponse)
def get_system_status():
    state = _get_system_state()
    return {"success": True, "started": state['started'], "starting": state['starting']}


@router.post("/start", response_model=StartResponse)
def start_system():
    allowed, message = _prepare_system_start()
    if not allowed:
        raise HTTPException(status_code=400, detail=message)

    try:
        success, logs, errors = initialize_system_components()
        if success:
            _set_system_state(started=True)
            return {"success": True, "message": "系统启动成功", "logs": logs}

        _set_system_state(started=False)
        raise HTTPException(status_code=500, detail={
            "success": False, "message": "系统启动失败", "logs": logs, "errors": errors
        })
    except Exception as exc:
        _set_system_state(started=False)
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        _set_system_state(starting=False)


@router.post("/shutdown", response_model=ShutdownResponse)
def shutdown_system():
    state = _get_system_state()
    if state['starting']:
        raise HTTPException(status_code=400, detail="系统正在启动/重启，请稍候")

    target_ports = [
        f"{name}:{info['port']}"
        for name, info in processes.items()
        if info.get('port')
    ]

    if not _mark_shutdown_requested():
        running = _describe_running_children()
        detail = '关机指令已下发，请稍等...'
        if running:
            detail = f"关机指令已下发，等待进程退出: {', '.join(running)}"
        if target_ports:
            detail = f"{detail}（端口: {', '.join(target_ports)}）"
        return {"success": True, "message": detail, "ports": target_ports}

    running = _describe_running_children()
    if running:
        _log_shutdown_step("开始关闭系统，正在等待子进程退出: " + ", ".join(running))

    try:
        _set_system_state(started=False, starting=False)
        _start_async_shutdown(cleanup_timeout=6.0)
        message = '关闭系统指令已下发，正在停止进程'
        if running:
            message = f"{message}: {', '.join(running)}"
        if target_ports:
            message = f"{message}（端口: {', '.join(target_ports)}）"
        return {"success": True, "message": message, "ports": target_ports}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
