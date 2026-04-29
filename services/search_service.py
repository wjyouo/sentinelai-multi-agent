"""
Search service — unified search across Insight/Media/Query engines.
"""

from typing import Any, Dict

import requests
from loguru import logger

from services.system_service import check_app_status, processes

API_PORTS = {'insight': 8501, 'media': 8502, 'query': 8503}


def search(query: str) -> Dict[str, Any]:
    if not query.strip():
        return {'success': False, 'message': '搜索查询不能为空'}

    check_app_status()
    running_apps = [name for name, info in processes.items() if info['status'] == 'running']

    if not running_apps:
        return {'success': False, 'message': '没有运行中的应用'}

    results = {}
    for app_name in running_apps:
        try:
            api_port = API_PORTS[app_name]
            response = requests.post(
                f"http://localhost:{api_port}/api/search",
                json={'query': query},
                timeout=10,
            )
            if response.status_code == 200:
                results[app_name] = response.json()
            else:
                results[app_name] = {'success': False, 'message': 'API调用失败'}
        except Exception as e:
            results[app_name] = {'success': False, 'message': str(e)}

    return {'success': True, 'query': query, 'results': results}
