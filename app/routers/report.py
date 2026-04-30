"""Report Engine routes — FastAPI router."""

import asyncio
import json
import os
import time
from datetime import datetime
from queue import Empty

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, Response
from loguru import logger

from app.services import report_service as svc

router = APIRouter(prefix="/api/report", tags=["report"])


# ── SSE helpers ────────────────────────────────────────────────────────────

async def _sse_event_generator(task_id: str, request: Request, last_event_id: int | None = None):
    """Async SSE generator: replays history, then streams live events with heartbeat."""
    task = svc._get_task(task_id)
    if not task:
        yield f"event: error\ndata: {json.dumps({'error': '任务不存在'})}\n\n"
        return

    queue = svc._register_stream(task_id)
    last_data_ts = time.time()

    try:
        # Replay history for reconnecting clients
        history = task.history_since(last_event_id)
        for event in history:
            yield svc._format_sse(event)
            if event.get("type") != "heartbeat":
                last_data_ts = time.time()

        finished = task.status in svc.STREAM_TERMINAL_STATUSES
        while True:
            if finished:
                break
            if await request.is_disconnected():
                logger.info(f"SSE客户端已断开，停止推送: {task_id}")
                break

            event = None
            try:
                event = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: queue.get(timeout=svc.STREAM_HEARTBEAT_INTERVAL)
                )
            except Empty:
                if task.status in svc.STREAM_TERMINAL_STATUSES:
                    logger.info(f"任务 {task_id} 已结束且无新事件，SSE自动收口")
                    break
                event = {
                    "id": f"hb-{int(time.time() * 1000)}",
                    "type": "heartbeat",
                    "task_id": task_id,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "payload": {"status": task.status},
                }

            if event is None:
                logger.warning(f"SSE推送获取事件失败（task {task_id}），提前结束")
                break

            try:
                yield svc._format_sse(event)
                if event.get("type") != "heartbeat":
                    last_data_ts = time.time()
            except GeneratorExit:
                logger.info(f"SSE生成器关闭，停止任务 {task_id} 推送")
                break
            except Exception as exc:
                event_type = event.get("type") if isinstance(event, dict) else "unknown"
                logger.exception(f"SSE推送失败（task {task_id}, event {event_type}）: {exc}")
                break

            if event.get("type") in ("completed", "error", "cancelled"):
                finished = True
            else:
                finished = finished or task.status in svc.STREAM_TERMINAL_STATUSES

            if task.status in svc.STREAM_TERMINAL_STATUSES:
                idle_for = time.time() - last_data_ts
                if idle_for > svc.STREAM_IDLE_TIMEOUT:
                    logger.info(f"任务 {task_id} 已终态且空闲 {int(idle_for)}s，主动关闭SSE")
                    break
    finally:
        svc._unregister_stream(task_id, queue)


# ── GET /status ────────────────────────────────────────────────────────────

@router.get("/status")
def get_status():
    try:
        data = svc.get_status_dict()
        return JSONResponse(content={"success": True, **data})
    except Exception as e:
        logger.exception(f"获取Report Engine状态失败: {str(e)}")
        return JSONResponse(content={"success": False, "error": str(e)}, status_code=500)


# ── POST /generate ─────────────────────────────────────────────────────────

@router.post("/generate")
async def generate_report(request: Request):
    try:
        data = await request.json() or {}
        if not isinstance(data, dict):
            data = {}
        query = data.get("query", "智能舆情分析报告")
        custom_template = data.get("custom_template", "")

        svc.clear_report_log()

        if not svc.report_agent:
            return JSONResponse(
                content={"success": False, "error": "Report Engine未初始化"},
                status_code=500,
            )

        engines_status = svc.check_engines_ready()
        if not engines_status["ready"]:
            return JSONResponse(
                content={
                    "success": False,
                    "error": "输入文件未准备就绪",
                    "missing_files": engines_status.get("missing_files", []),
                },
                status_code=400,
            )

        try:
            task = svc.create_task(query, custom_template)
        except RuntimeError as e:
            return JSONResponse(
                content={"success": False, "error": str(e)},
                status_code=400,
            )

        svc.start_task_thread(task, query, custom_template)

        return JSONResponse(content={
            "success": True,
            "task_id": task.task_id,
            "message": "报告生成已启动",
            "task": task.to_dict(),
            "stream_url": f"/api/report/stream/{task.task_id}",
        })

    except Exception as e:
        logger.exception(f"开始生成报告失败: {str(e)}")
        return JSONResponse(content={"success": False, "error": str(e)}, status_code=500)


# ── GET /progress/{task_id} ────────────────────────────────────────────────

@router.get("/progress/{task_id}")
def get_progress(task_id: str):
    try:
        task = svc._get_task(task_id)
        if not task:
            return JSONResponse(content={
                "success": True,
                "task": {
                    "task_id": task_id,
                    "status": "completed",
                    "progress": 100,
                    "error_message": "",
                    "has_result": True,
                    "report_file_ready": False,
                    "report_file_name": "",
                    "report_file_path": "",
                    "state_file_ready": False,
                    "state_file_path": "",
                },
            })
        return JSONResponse(content={"success": True, "task": task.to_dict()})
    except Exception as e:
        logger.exception(f"获取报告生成进度失败: {str(e)}")
        return JSONResponse(content={"success": False, "error": str(e)}, status_code=500)


# ── GET /stream/{task_id} (SSE) ────────────────────────────────────────────

@router.get("/stream/{task_id}")
async def stream_task(task_id: str, request: Request):
    task = svc._get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    last_event_header = request.headers.get("Last-Event-ID")
    try:
        last_event_id = int(last_event_header) if last_event_header else None
    except (ValueError, TypeError):
        last_event_id = None

    return StreamingResponse(
        _sse_event_generator(task_id, request, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── GET /result/{task_id} ──────────────────────────────────────────────────

@router.get("/result/{task_id}")
def get_result(task_id: str):
    try:
        task = svc._get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.status != "completed":
            return JSONResponse(
                content={"success": False, "error": "报告尚未完成", "task": task.to_dict()},
                status_code=400,
            )
        return Response(content=task.html_content, media_type="text/html")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"获取报告生成结果失败: {str(e)}")
        return JSONResponse(content={"success": False, "error": str(e)}, status_code=500)


# ── GET /result/{task_id}/json ─────────────────────────────────────────────

@router.get("/result/{task_id}/json")
def get_result_json(task_id: str):
    try:
        task = svc._get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.status != "completed":
            return JSONResponse(
                content={"success": False, "error": "报告尚未完成", "task": task.to_dict()},
                status_code=400,
            )
        return JSONResponse(content={
            "success": True,
            "task": task.to_dict(),
            "html_content": task.html_content,
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"获取报告生成结果失败: {str(e)}")
        return JSONResponse(content={"success": False, "error": str(e)}, status_code=500)


# ── GET /download/{task_id} ────────────────────────────────────────────────

@router.get("/download/{task_id}")
def download_report(task_id: str):
    try:
        task = svc._get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.status != "completed" or not task.report_file_path:
            return JSONResponse(
                content={"success": False, "error": "报告尚未完成或尚未保存"},
                status_code=400,
            )
        if not os.path.exists(task.report_file_path):
            return JSONResponse(
                content={"success": False, "error": "报告文件不存在或已被删除"},
                status_code=404,
            )

        from fastapi.responses import FileResponse
        download_name = task.report_file_name or os.path.basename(task.report_file_path)
        return FileResponse(
            task.report_file_path,
            media_type="text/html",
            filename=download_name,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"下载报告失败: {str(e)}")
        return JSONResponse(content={"success": False, "error": str(e)}, status_code=500)


# ── POST /cancel/{task_id} ─────────────────────────────────────────────────

@router.post("/cancel/{task_id}")
def cancel_task(task_id: str):
    try:
        if svc.cancel_task_by_id(task_id):
            return JSONResponse(content={"success": True, "message": "任务已取消"})
        return JSONResponse(
            content={"success": False, "error": "任务不存在或无法取消"},
            status_code=404,
        )
    except Exception as e:
        logger.exception(f"取消报告生成任务失败: {str(e)}")
        return JSONResponse(content={"success": False, "error": str(e)}, status_code=500)


# ── GET /templates ─────────────────────────────────────────────────────────

@router.get("/templates")
def get_templates():
    try:
        if not svc.report_agent:
            return JSONResponse(
                content={"success": False, "error": "Report Engine未初始化"},
                status_code=500,
            )
        data = svc.get_templates_list()
        return JSONResponse(content={"success": True, **data})
    except Exception as e:
        logger.exception(f"获取可用模板列表失败: {str(e)}")
        return JSONResponse(content={"success": False, "error": str(e)}, status_code=500)


# ── GET /log ───────────────────────────────────────────────────────────────

@router.get("/log")
def get_report_log():
    try:
        log_lines = svc.get_report_log_lines()
        return JSONResponse(content={"success": True, "log_lines": log_lines})
    except PermissionError as e:
        logger.error(f"读取日志权限不足: {str(e)}")
        return JSONResponse(content={"success": False, "error": "读取日志权限不足"}, status_code=403)
    except UnicodeDecodeError as e:
        logger.error(f"日志文件编码错误: {str(e)}")
        return JSONResponse(content={"success": False, "error": "日志文件编码错误"}, status_code=500)
    except Exception as e:
        logger.exception(f"读取日志失败: {str(e)}")
        return JSONResponse(content={"success": False, "error": f"读取日志失败: {str(e)}"}, status_code=500)


# ── POST /log/clear ────────────────────────────────────────────────────────

@router.post("/log/clear")
def clear_log():
    try:
        svc.clear_report_log()
        return JSONResponse(content={"success": True, "message": "日志已清空"})
    except Exception as e:
        logger.exception(f"清空日志失败: {str(e)}")
        return JSONResponse(content={"success": False, "error": f"清空日志失败: {str(e)}"}, status_code=500)


# ── GET /export/md/{task_id} ───────────────────────────────────────────────

@router.get("/export/md/{task_id}")
def export_markdown(task_id: str):
    try:
        from fastapi.responses import FileResponse

        info = svc.export_markdown_for_task(task_id)
        return FileResponse(
            info["file_path"],
            media_type="text/markdown",
            filename=info["file_name"],
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="任务不存在")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"导出Markdown失败: {str(e)}")
        return JSONResponse(content={"success": False, "error": f"导出Markdown失败: {str(e)}"}, status_code=500)


# ── GET /export/pdf/{task_id} ──────────────────────────────────────────────

@router.get("/export/pdf/{task_id}")
def export_pdf(task_id: str, optimize: bool = True):
    try:
        from ReportEngine.utils.dependency_check import check_pango_available

        pango_available, pango_message = check_pango_available()
        if not pango_available:
            return JSONResponse(
                content={
                    "success": False,
                    "error": "PDF 导出功能不可用：缺少系统依赖",
                    "details": "请安装PDF 导出依赖",
                    "help_url": "",
                    "system_message": pango_message,
                },
                status_code=503,
            )

        task = svc.tasks_registry.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")

        pdf_bytes = svc.export_pdf_for_task(task_id, optimize=optimize)

        with open(task.ir_file_path, "r", encoding="utf-8") as f:
            document_ir = json.load(f)
        topic = document_ir.get("metadata", {}).get("topic", "report")
        pdf_filename = f"report_{topic}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

        return Response(
            pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{pdf_filename}"'},
        )

    except HTTPException:
        raise
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception(f"导出PDF失败: {str(e)}")
        return JSONResponse(content={"success": False, "error": f"导出PDF失败: {str(e)}"}, status_code=500)


# ── POST /export/pdf-from-ir ───────────────────────────────────────────────

@router.post("/export/pdf-from-ir")
async def export_pdf_from_ir(request: Request):
    try:
        from ReportEngine.utils.dependency_check import check_pango_available

        pango_available, pango_message = check_pango_available()
        if not pango_available:
            return JSONResponse(
                content={
                    "success": False,
                    "error": "PDF 导出功能不可用：缺少系统依赖",
                    "details": "请安装PDF 导出依赖",
                    "help_url": "",
                    "system_message": pango_message,
                },
                status_code=503,
            )

        data = await request.json() or {}
        if not isinstance(data, dict) or "document_ir" not in data:
            return JSONResponse(
                content={"success": False, "error": "缺少document_ir参数"},
                status_code=400,
            )

        document_ir = data["document_ir"]
        optimize = data.get("optimize", True)

        pdf_bytes = svc.export_pdf_from_ir(document_ir, optimize=optimize)

        topic = document_ir.get("metadata", {}).get("topic", "report")
        pdf_filename = f"report_{topic}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

        return Response(
            pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{pdf_filename}"'},
        )

    except Exception as e:
        logger.exception(f"从IR导出PDF失败: {str(e)}")
        return JSONResponse(content={"success": False, "error": f"导出PDF失败: {str(e)}"}, status_code=500)
