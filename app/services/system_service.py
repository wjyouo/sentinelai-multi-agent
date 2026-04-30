"""
System service — process management, config, system state.

Extracted from app.py to be framework-agnostic.
Both Flask (app.py) and FastAPI (routers/) import from here.
"""

import os
import sys
import time
import threading
import importlib
import atexit
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
LOG_DIR = Path('logs')
LOG_DIR.mkdir(exist_ok=True)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_MODULE_NAME = 'config'
CONFIG_FILE_PATH = PROJECT_ROOT / 'config.py'
CONFIG_KEYS = [
    'HOST', 'PORT',
    'DB_DIALECT', 'DB_HOST', 'DB_PORT', 'DB_USER', 'DB_PASSWORD', 'DB_NAME', 'DB_CHARSET',
    'INSIGHT_ENGINE_API_KEY', 'INSIGHT_ENGINE_BASE_URL', 'INSIGHT_ENGINE_MODEL_NAME',
    'MEDIA_ENGINE_API_KEY', 'MEDIA_ENGINE_BASE_URL', 'MEDIA_ENGINE_MODEL_NAME',
    'QUERY_ENGINE_API_KEY', 'QUERY_ENGINE_BASE_URL', 'QUERY_ENGINE_MODEL_NAME',
    'REPORT_ENGINE_API_KEY', 'REPORT_ENGINE_BASE_URL', 'REPORT_ENGINE_MODEL_NAME',
    'FORUM_HOST_API_KEY', 'FORUM_HOST_BASE_URL', 'FORUM_HOST_MODEL_NAME',
    'KEYWORD_OPTIMIZER_API_KEY', 'KEYWORD_OPTIMIZER_BASE_URL', 'KEYWORD_OPTIMIZER_MODEL_NAME',
    'SENTINEL_SPIDER_API_KEY', 'SENTINEL_SPIDER_BASE_URL', 'SENTINEL_SPIDER_MODEL_NAME',
    'TAVILY_API_KEY', 'SEARCH_TOOL_TYPE', 'BOCHA_BASE_URL', 'BOCHA_WEB_SEARCH_API_KEY',
    'ANSPIRE_BASE_URL', 'ANSPIRE_API_KEY',
    'GRAPHRAG_ENABLED', 'GRAPHRAG_MAX_QUERIES',
]


def _load_config_module():
    importlib.invalidate_caches()
    module = sys.modules.get(CONFIG_MODULE_NAME)
    try:
        if module is None:
            module = importlib.import_module(CONFIG_MODULE_NAME)
        else:
            module = importlib.reload(module)
    except ModuleNotFoundError:
        return None
    return module


def read_config_values() -> Dict[str, str]:
    try:
        import config as config_module
        config_module.reload_settings()
        current_settings = config_module.settings
        values = {}
        for key in CONFIG_KEYS:
            value = getattr(current_settings, key, None)
            values[key] = '' if value is None else str(value)
        return values
    except Exception:
        logger.exception("读取配置失败")
        return {}


def write_config_values(updates: Dict[str, Any]) -> None:
    project_root = PROJECT_ROOT
    env_file_path = project_root / ".env"

    env_lines: List[str] = []
    env_key_indices: Dict[str, int] = {}
    if env_file_path.exists():
        env_lines = env_file_path.read_text(encoding='utf-8').splitlines()
        for i, line in enumerate(env_lines):
            line_stripped = line.strip()
            if line_stripped and not line_stripped.startswith('#') and '=' in line_stripped:
                key = line_stripped.split('=')[0].strip()
                env_key_indices[key] = i

    for key, raw_value in updates.items():
        if raw_value is None or raw_value == '':
            env_value = ''
        elif isinstance(raw_value, (int, float)):
            env_value = str(raw_value)
        elif isinstance(raw_value, bool):
            env_value = 'True' if raw_value else 'False'
        else:
            value_str = str(raw_value)
            if ' ' in value_str or '\n' in value_str or '#' in value_str:
                escaped = value_str.replace('\\', '\\\\').replace('"', '\\"')
                env_value = f'"{escaped}"'
            else:
                env_value = value_str

        if key in env_key_indices:
            env_lines[env_key_indices[key]] = f'{key}={env_value}'
        else:
            env_lines.append(f'{key}={env_value}')

    env_file_path.parent.mkdir(parents=True, exist_ok=True)
    env_file_path.write_text('\n'.join(env_lines) + '\n', encoding='utf-8')

    import config as config_module
    config_module.reload_settings()


# ---------------------------------------------------------------------------
# System state
# ---------------------------------------------------------------------------
_system_state_lock = threading.Lock()
_system_state: Dict[str, bool] = {
    'started': False,
    'starting': False,
    'shutdown_in_progress': False,
}


def _set_system_state(*, started: Optional[bool] = None, starting: Optional[bool] = None):
    with _system_state_lock:
        if started is not None:
            _system_state['started'] = started
        if starting is not None:
            _system_state['starting'] = starting


def _get_system_state() -> Dict[str, bool]:
    with _system_state_lock:
        return _system_state.copy()


def _prepare_system_start() -> Tuple[bool, Optional[str]]:
    with _system_state_lock:
        if _system_state['started']:
            return False, '系统已启动'
        if _system_state['starting']:
            return False, '系统正在启动'
        _system_state['starting'] = True
        return True, None


def _mark_shutdown_requested() -> bool:
    with _system_state_lock:
        if _system_state.get('shutdown_in_progress'):
            return False
        _system_state['shutdown_in_progress'] = True
        return True


# ---------------------------------------------------------------------------
# Forum process tracking (minimal — forum is managed by forum_service)
# ---------------------------------------------------------------------------
_forum_status: Dict[str, Any] = {'status': 'stopped'}


def _log_shutdown_step(message: str):
    logger.info(f"[Shutdown] {message}")


def _describe_running_children() -> List[str]:
    running = []
    if _forum_status.get('status') == 'running':
        running.append("forum")
    return running


def write_log_to_file(app_name: str, line: str):
    try:
        log_file_path = LOG_DIR / f"{app_name}.log"
        with open(log_file_path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
            f.flush()
    except Exception:
        logger.exception(f"Error writing log for {app_name}")


def read_log_from_file(app_name: str, tail_lines: Optional[int] = None) -> List[str]:
    try:
        log_file_path = LOG_DIR / f"{app_name}.log"
        if not log_file_path.exists():
            return []
        with open(log_file_path, 'r', encoding='utf-8') as f:
            lines = [line.rstrip('\n\r') for line in f.readlines() if line.strip()]
            if tail_lines:
                return lines[-tail_lines:]
            return lines
    except Exception:
        logger.exception(f"Error reading log for {app_name}")
        return []


def check_app_status():
    """No-op: Streamlit healthchecks removed. Forum status tracked via _forum_status."""
    pass


def cleanup_processes():
    """Stop forum engine and mark system stopped."""
    _log_shutdown_step("清理子进程")
    _forum_status['status'] = 'stopped'
    try:
        from services.forum_service import stop_forum_engine
        stop_forum_engine()
    except Exception:
        logger.exception("停止ForumEngine失败")
    _log_shutdown_step("子进程清理完成")
    _set_system_state(started=False, starting=False)


def cleanup_processes_concurrent(timeout: float = 6.0):
    """Concurrent cleanup wrapper — delegates to cleanup_processes."""
    _log_shutdown_step(f"开始并发清理（超时 {timeout}s）")
    t = threading.Thread(target=cleanup_processes, daemon=True)
    t.start()
    t.join(timeout=timeout)
    _set_system_state(started=False, starting=False)


def _schedule_server_shutdown(delay_seconds: float = 0.1, stop_callback=None):
    def _shutdown():
        time.sleep(delay_seconds)
        if stop_callback:
            stop_callback()
        _log_shutdown_step("服务器停止指令已发送，即将退出主进程")
        os._exit(0)
    threading.Thread(target=_shutdown, daemon=True).start()


def _start_async_shutdown(cleanup_timeout: float = 3.0, stop_callback=None):
    _log_shutdown_step(f"收到关机指令，启动异步清理（超时 {cleanup_timeout}s）")

    def _force_exit():
        _log_shutdown_step("关机超时，触发强制退出")
        os._exit(0)

    hard_timeout = cleanup_timeout + 2.0
    force_timer = threading.Timer(hard_timeout, _force_exit)
    force_timer.daemon = True
    force_timer.start()

    def _cleanup_and_exit():
        try:
            cleanup_processes_concurrent(timeout=cleanup_timeout)
        except Exception:
            logger.exception("关机清理异常")
        finally:
            _log_shutdown_step("清理线程结束，调度主进程退出")
            _schedule_server_shutdown(0.05, stop_callback)

    threading.Thread(target=_cleanup_and_exit, daemon=True).start()


def initialize_system_components() -> Tuple[bool, List[str], List[str]]:
    logs: List[str] = []
    errors: List[str] = []

    try:
        from SentinelSpider.main import SentinelSpider
        spider = SentinelSpider()
        if spider.initialize_database():
            logger.info("数据库初始化成功")
        else:
            logger.error("数据库初始化失败")
    except Exception as exc:
        logs.append(f"数据库初始化异常: {exc}")

    from services.forum_service import start_forum_engine, stop_forum_engine
    try:
        stop_forum_engine()
        logs.append("已停止 ForumEngine 监控器以避免文件冲突")
    except Exception as exc:
        message = f"停止 ForumEngine 时发生异常: {exc}"
        logs.append(message)
        logger.exception(message)

    _forum_status['status'] = 'stopped'

    forum_started = False
    try:
        start_forum_engine()
        _forum_status['status'] = 'running'
        logs.append("ForumEngine 启动完成")
        forum_started = True
    except Exception as exc:
        error_msg = f"ForumEngine 启动失败: {exc}"
        logs.append(error_msg)
        errors.append(error_msg)

    try:
        from services.report_service import initialize_report_engine
        if initialize_report_engine():
            logs.append("ReportEngine 初始化成功")
        else:
            msg = "ReportEngine 初始化失败"
            logs.append(msg)
            errors.append(msg)
    except Exception as exc:
        msg = f"ReportEngine 初始化异常: {exc}"
        logs.append(msg)
        errors.append(msg)

    if errors:
        cleanup_processes()
        if forum_started:
            try:
                stop_forum_engine()
            except Exception:
                logger.exception("停止ForumEngine失败")
        return False, logs, errors

    return True, logs, []


# Register cleanup on exit
atexit.register(cleanup_processes)
