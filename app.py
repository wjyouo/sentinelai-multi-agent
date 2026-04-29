"""
Flask主应用 - 统一管理三个Streamlit应用

Phase 1 refactored: business logic extracted to services/,
Flask routes are thin wrappers, Socket.IO wired via event_bus.
"""

import os
import sys

os.environ['PYTHONIOENCODING'] = 'utf-8'
os.environ['PYTHONUTF8'] = '1'
os.environ['PYTHONUNBUFFERED'] = '1'

import threading
import time
import json
from datetime import datetime

from flask import Flask, render_template, request, jsonify, Response
from flask_socketio import SocketIO, emit
import atexit
from loguru import logger

# --- Service imports ---
from services.event_bus import subscribe as event_bus_subscribe
from services.system_service import (
    processes, STREAMLIT_SCRIPTS,
    read_config_values, write_config_values,
    _get_system_state, _set_system_state,
    _prepare_system_start, _mark_shutdown_requested,
    initialize_system_components,
    start_streamlit_app, stop_streamlit_app,
    wait_for_app_startup, check_app_status,
    cleanup_processes, cleanup_processes_concurrent,
    _start_async_shutdown, _describe_running_children,
    _log_shutdown_step,
    read_log_from_file, write_log_to_file,
    LOG_DIR,
)
from services.forum_service import (
    init_forum_log, start_forum_log_monitor,
    start_forum_engine, stop_forum_engine,
    get_forum_log, get_forum_log_history,
    parse_forum_log_line,
)
from services.graph_service import get_graph_data, get_latest_graph, query_graph
from services.search_service import search as search_service
from utils.knowledge_logger import init_knowledge_log

# --- ReportEngine ---
try:
    from ReportEngine.flask_interface import report_bp, initialize_report_engine
    REPORT_ENGINE_AVAILABLE = True
except ImportError as e:
    logger.error(f"ReportEngine导入失败: {e}")
    REPORT_ENGINE_AVAILABLE = False

# --- Flask app setup ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'atguigu-sentinelai-analysis-platform-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*")

# --- Register Socket.IO as event bus subscriber ---
def _socketio_event_handler(event_type: str, data: dict):
    """Forward event bus events to Socket.IO clients."""
    try:
        socketio.emit(event_type, data)
    except Exception:
        pass

event_bus_subscribe(_socketio_event_handler)

# --- eventlet disconnect patch ---
def _patch_eventlet_disconnect_logging():
    try:
        import eventlet.wsgi
    except Exception:
        logger.debug("eventlet 不可用，跳过断开补丁")
        return
    try:
        original_finish = eventlet.wsgi.HttpProtocol.finish
    except Exception:
        logger.debug("eventlet 缺少 HttpProtocol.finish，跳过断开补丁")
        return

    def _safe_finish(self, *args, **kwargs):
        try:
            return original_finish(self, *args, **kwargs)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
            try:
                environ = getattr(self, 'environ', {}) or {}
                method = environ.get('REQUEST_METHOD', '')
                path = environ.get('PATH_INFO', '')
                logger.warning(f"客户端已主动断开，忽略异常: {method} {path} ({exc})")
            except Exception:
                logger.warning(f"客户端已主动断开，忽略异常: {exc}")
            return
    eventlet.wsgi.HttpProtocol.finish = _safe_finish
    logger.info("已对 eventlet 连接中断进行安全防护")

_patch_eventlet_disconnect_logging()

# --- Register ReportEngine Blueprint ---
if REPORT_ENGINE_AVAILABLE:
    app.register_blueprint(report_bp, url_prefix='/api/report')
    logger.info("ReportEngine接口已注册")
else:
    logger.info("ReportEngine不可用，跳过接口注册")

# --- Initialize services ---
init_forum_log()
init_knowledge_log()
start_forum_log_monitor()

atexit.register(cleanup_processes)

# --- Flask Routes ---

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def get_status():
    check_app_status()
    return jsonify({
        app_name: {
            'status': info['status'],
            'port': info['port'],
            'output_lines': len(info['output'])
        }
        for app_name, info in processes.items()
    })


@app.route('/api/start/<app_name>')
def start_app(app_name):
    if app_name not in processes:
        return jsonify({'success': False, 'message': '未知应用'})

    if app_name == 'forum':
        try:
            start_forum_engine()
            processes['forum']['status'] = 'running'
            return jsonify({'success': True, 'message': 'ForumEngine已启动'})
        except Exception as exc:
            logger.exception("手动启动ForumEngine失败")
            return jsonify({'success': False, 'message': f'ForumEngine启动失败: {exc}'})

    script_path = STREAMLIT_SCRIPTS.get(app_name)
    if not script_path:
        return jsonify({'success': False, 'message': '该应用不支持启动操作'})

    success, message = start_streamlit_app(app_name, script_path, processes[app_name]['port'])
    if success:
        startup_success, startup_message = wait_for_app_startup(app_name, 15)
        if not startup_success:
            message += f" 但启动检查失败: {startup_message}"

    return jsonify({'success': success, 'message': message})


@app.route('/api/stop/<app_name>')
def stop_app(app_name):
    if app_name not in processes:
        return jsonify({'success': False, 'message': '未知应用'})

    if app_name == 'forum':
        try:
            stop_forum_engine()
            processes['forum']['status'] = 'stopped'
            return jsonify({'success': True, 'message': 'ForumEngine已停止'})
        except Exception as exc:
            logger.exception("手动停止ForumEngine失败")
            return jsonify({'success': False, 'message': f'ForumEngine停止失败: {exc}'})

    success, message = stop_streamlit_app(app_name)
    return jsonify({'success': success, 'message': message})


@app.route('/api/output/<app_name>')
def get_output(app_name):
    if app_name not in processes:
        return jsonify({'success': False, 'message': '未知应用'})

    if app_name == 'forum':
        try:
            forum_log_content = read_log_from_file('forum')
            return jsonify({
                'success': True,
                'output': forum_log_content,
                'total_lines': len(forum_log_content)
            })
        except Exception as e:
            return jsonify({'success': False, 'message': f'读取forum日志失败: {str(e)}'})

    output_lines = read_log_from_file(app_name)
    return jsonify({'success': True, 'output': output_lines})


@app.route('/api/test_log/<app_name>')
def test_log(app_name):
    if app_name not in processes:
        return jsonify({'success': False, 'message': '未知应用'})

    test_msg = f"[{datetime.now().strftime('%H:%M:%S')}] 测试日志消息 - {datetime.now()}"
    write_log_to_file(app_name, test_msg)

    # Still use direct socketio emit for this test endpoint
    socketio.emit('console_output', {'app': app_name, 'line': test_msg})

    return jsonify({'success': True, 'message': f'测试消息已写入 {app_name} 日志'})


@app.route('/api/forum/start')
def start_forum_monitoring_api():
    try:
        success = start_forum_engine()
        if success:
            return jsonify({'success': True, 'message': 'ForumEngine论坛已启动'})
        else:
            return jsonify({'success': False, 'message': 'ForumEngine论坛启动失败'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'启动论坛失败: {str(e)}'})


@app.route('/api/forum/stop')
def stop_forum_monitoring_api():
    try:
        stop_forum_engine()
        return jsonify({'success': True, 'message': 'ForumEngine论坛已停止'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'停止论坛失败: {str(e)}'})


@app.route('/api/forum/log')
def get_forum_log_api():
    try:
        result = get_forum_log()
        return jsonify({'success': True, **result})
    except Exception as e:
        return jsonify({'success': False, 'message': f'读取forum.log失败: {str(e)}'})


@app.route('/api/forum/log/history', methods=['POST'])
def get_forum_log_history_api():
    try:
        data = request.get_json()
        start_position = data.get('position', 0)
        max_lines = data.get('max_lines', 1000)
        result = get_forum_log_history(start_position, max_lines)
        return jsonify({'success': True, **result})
    except Exception as e:
        return jsonify({'success': False, 'message': f'读取forum历史失败: {str(e)}'})


@app.route('/api/search', methods=['POST'])
def search():
    data = request.get_json()
    query = data.get('query', '').strip()
    result = search_service(query)
    status_code = 200 if result.get('success') else 400
    return jsonify(result), status_code


@app.route('/api/config', methods=['GET'])
def get_config():
    try:
        config_values = read_config_values()
        return jsonify({'success': True, 'config': config_values})
    except Exception as exc:
        logger.exception("读取配置失败")
        return jsonify({'success': False, 'message': f'读取配置失败: {exc}'}), 500


@app.route('/api/config', methods=['POST'])
def update_config():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict) or not payload:
        return jsonify({'success': False, 'message': '请求体不能为空'}), 400

    updates = {}
    for key, value in payload.items():
        if key in read_config_values():
            updates[key] = value if value is not None else ''

    if not updates:
        return jsonify({'success': False, 'message': '没有可更新的配置项'}), 400

    try:
        write_config_values(updates)
        updated_config = read_config_values()
        return jsonify({'success': True, 'config': updated_config})
    except Exception as exc:
        logger.exception("更新配置失败")
        return jsonify({'success': False, 'message': f'更新配置失败: {exc}'}), 500


@app.route('/api/system/status')
def get_system_status():
    state = _get_system_state()
    return jsonify({
        'success': True,
        'started': state['started'],
        'starting': state['starting']
    })


@app.route('/api/system/start', methods=['POST'])
def start_system():
    allowed, message = _prepare_system_start()
    if not allowed:
        return jsonify({'success': False, 'message': message}), 400

    try:
        success, logs, errors = initialize_system_components()
        if success:
            _set_system_state(started=True)
            return jsonify({'success': True, 'message': '系统启动成功', 'logs': logs})

        _set_system_state(started=False)
        return jsonify({
            'success': False,
            'message': '系统启动失败',
            'logs': logs,
            'errors': errors
        }), 500
    except Exception as exc:
        logger.exception("系统启动过程中出现异常")
        _set_system_state(started=False)
        return jsonify({'success': False, 'message': f'系统启动异常: {exc}'}), 500
    finally:
        _set_system_state(starting=False)


@app.route('/api/system/shutdown', methods=['POST'])
def shutdown_system():
    state = _get_system_state()
    if state['starting']:
        return jsonify({'success': False, 'message': '系统正在启动/重启，请稍候'}), 400

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
        return jsonify({'success': True, 'message': detail, 'ports': target_ports})

    running = _describe_running_children()
    if running:
        _log_shutdown_step("开始关闭系统，正在等待子进程退出: " + ", ".join(running))
    else:
        _log_shutdown_step("开始关闭系统，未检测到存活子进程")

    try:
        _set_system_state(started=False, starting=False)
        _start_async_shutdown(cleanup_timeout=6.0, stop_callback=lambda: socketio.stop())
        message = '关闭系统指令已下发，正在停止进程'
        if running:
            message = f"{message}: {', '.join(running)}"
        if target_ports:
            message = f"{message}（端口: {', '.join(target_ports)}）"
        return jsonify({'success': True, 'message': message, 'ports': target_ports})
    except Exception as exc:
        logger.exception("系统关闭过程中出现异常")
        return jsonify({'success': False, 'message': f'系统关闭异常: {exc}'}), 500


# --- GraphRAG API ---

@app.route('/api/graph/<report_id>')
def api_get_graph_data(report_id):
    result = get_graph_data(report_id)
    status_code = 200 if result.get('success') else 404
    return jsonify(result), status_code


@app.route('/api/graph/latest')
def api_get_latest_graph():
    result = get_latest_graph()
    status_code = 200 if result.get('success') else 404
    return jsonify(result), status_code


@app.route('/graph-viewer')
@app.route('/graph-viewer/')
@app.route('/graph-viewer/<report_id>')
def graph_viewer(report_id=None):
    return render_template('graph_viewer.html', report_id=report_id)


@app.route('/api/graph/query', methods=['POST'])
def api_query_graph():
    data = request.get_json() or {}
    result = query_graph(data)
    status_code = 200 if result.get('success') else 500
    return jsonify(result), status_code


# --- Socket.IO ---

@socketio.on('connect')
def handle_connect():
    emit('status', 'Connected to Flask server')


@socketio.on('request_status')
def handle_status_request():
    check_app_status()
    emit('status_update', {
        app_name: {'status': info['status'], 'port': info['port']}
        for app_name, info in processes.items()
    })


# --- Main ---

if __name__ == '__main__':
    from config import settings
    HOST = settings.HOST
    PORT = settings.PORT

    logger.info("等待配置确认，系统将在前端指令后启动组件...")
    logger.info(f"Flask服务器已启动，访问地址: http://{HOST}:{PORT}")

    try:
        socketio.run(app, host=HOST, port=PORT, debug=False)
    except KeyboardInterrupt:
        logger.info("\n正在关闭应用...")
        cleanup_processes()
