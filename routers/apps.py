"""Streamlit app management routes."""

from datetime import datetime

from fastapi import APIRouter, HTTPException

from services.system_service import (
    processes, STREAMLIT_SCRIPTS,
    check_app_status,
    start_streamlit_app, stop_streamlit_app,
    wait_for_app_startup,
    read_log_from_file, write_log_to_file,
)
from services.forum_service import start_forum_engine, stop_forum_engine

router = APIRouter(tags=["apps"])


@router.get("/api/status")
def get_status():
    check_app_status()
    return {
        app_name: {
            "status": info["status"],
            "port": info["port"],
            "output_lines": len(info["output"]),
        }
        for app_name, info in processes.items()
    }


@router.get("/api/start/{app_name}")
def start_app(app_name: str):
    if app_name not in processes:
        raise HTTPException(status_code=400, detail="未知应用")

    if app_name == "forum":
        try:
            start_forum_engine()
            processes["forum"]["status"] = "running"
            return {"success": True, "message": "ForumEngine已启动"}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    script_path = STREAMLIT_SCRIPTS.get(app_name)
    if not script_path:
        raise HTTPException(status_code=400, detail="该应用不支持启动操作")

    success, message = start_streamlit_app(app_name, script_path, processes[app_name]["port"])
    if success:
        startup_success, startup_message = wait_for_app_startup(app_name, 15)
        if not startup_success:
            message += f" 但启动检查失败: {startup_message}"

    return {"success": success, "message": message}


@router.get("/api/stop/{app_name}")
def stop_app(app_name: str):
    if app_name not in processes:
        raise HTTPException(status_code=400, detail="未知应用")

    if app_name == "forum":
        try:
            stop_forum_engine()
            processes["forum"]["status"] = "stopped"
            return {"success": True, "message": "ForumEngine已停止"}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    success, message = stop_streamlit_app(app_name)
    return {"success": success, "message": message}


@router.get("/api/output/{app_name}")
def get_output(app_name: str):
    if app_name not in processes:
        raise HTTPException(status_code=400, detail="未知应用")

    if app_name == "forum":
        forum_log = read_log_from_file("forum")
        return {"success": True, "output": forum_log, "total_lines": len(forum_log)}

    output_lines = read_log_from_file(app_name)
    return {"success": True, "output": output_lines}


@router.get("/api/test_log/{app_name}")
def test_log(app_name: str):
    if app_name not in processes:
        raise HTTPException(status_code=400, detail="未知应用")

    test_msg = f"[{datetime.now().strftime('%H:%M:%S')}] 测试日志消息 - {datetime.now()}"
    write_log_to_file(app_name, test_msg)

    from services.event_bus import publish
    publish("console_output", {"app": app_name, "line": test_msg})

    return {"success": True, "message": f"测试消息已写入 {app_name} 日志"}
