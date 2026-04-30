"""
Graph service — knowledge graph data retrieval and querying.

Extracted from app.py GraphRAG API endpoints.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from app.utils.knowledge_logger import append_knowledge_log, compact_records


def _format_node_tooltip(node) -> str:
    lines = [f"<b>{node.label or node.id}</b>"]
    lines.append(f"类型: {node.type}")
    props = node.properties or {}
    if 'summary' in props:
        lines.append(f"摘要: {props['summary'][:100]}...")
    if 'content' in props:
        lines.append(f"内容: {props['content'][:80]}...")
    if 'url' in props:
        lines.append(f"链接: {props['url']}")
    if 'query' in props:
        lines.append(f"查询: {props['query']}")
    return "<br>".join(lines)


def _graph_to_vis(graph) -> Dict[str, Any]:
    """Convert internal Graph to Vis.js format {nodes, edges, stats}."""
    vis_nodes = []
    vis_edges = []
    for node_id, node in graph.nodes.items():
        vis_nodes.append({
            'id': node_id,
            'label': node.label or node_id,
            'group': node.type,
            'title': _format_node_tooltip(node),
            'properties': node.properties,
        })
    for edge in graph.edges:
        vis_edges.append({
            'from': edge.source,
            'to': edge.target,
            'label': edge.relation,
            'arrows': 'to',
        })
    return {'nodes': vis_nodes, 'edges': vis_edges, 'stats': graph.get_stats()}


def get_graph_data(report_id: str) -> Dict[str, Any]:
    try:
        from ReportEngine.graphrag import GraphStorage

        storage = GraphStorage()
        graph_path = storage.find_graph_by_report_id(report_id)
        if not graph_path or not graph_path.exists():
            return {'success': False, 'message': f'未找到报告 {report_id} 的知识图谱数据'}

        graph = storage.load(graph_path)
        if graph is None:
            return {'success': False, 'message': f'图谱文件损坏或格式错误: {report_id}'}

        return {'success': True, 'graph': _graph_to_vis(graph)}
    except Exception as e:
        logger.exception(f"获取图谱数据失败: {e}")
        return {'success': False, 'message': f'获取图谱数据失败: {str(e)}'}


def get_latest_graph() -> Dict[str, Any]:
    try:
        from ReportEngine.graphrag import GraphStorage

        storage = GraphStorage()
        latest_path = storage.find_latest_graph()
        if not latest_path or not latest_path.exists():
            return {'success': False, 'message': '暂无可用的知识图谱数据'}

        graph = storage.load(latest_path)
        if graph is None:
            return {'success': False, 'message': '图谱文件损坏或格式错误'}

        report_id = latest_path.parent.name if latest_path.parent else 'unknown'
        return {'success': True, 'report_id': report_id, 'graph': _graph_to_vis(graph)}
    except Exception as e:
        logger.exception(f"获取最新图谱失败: {e}")
        return {'success': False, 'message': f'获取最新图谱失败: {str(e)}'}


def query_graph(data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from ReportEngine.graphrag import GraphStorage, QueryEngine, QueryParams

        report_id = data.get('report_id')
        storage = GraphStorage()

        append_knowledge_log('GRAPH_QUERY', {
            'report_id': report_id,
            'keywords': data.get('keywords', []),
            'node_types': data.get('node_types'),
            'depth': data.get('depth', 1),
            'engine_filter': data.get('engine_filter'),
        })

        if report_id:
            graph_path = storage.find_graph_by_report_id(report_id)
        else:
            graph_path = storage.find_latest_graph()

        if not graph_path or not graph_path.exists():
            return {'success': False, 'message': '未找到可用的知识图谱'}

        graph = storage.load(graph_path)
        if graph is None:
            return {'success': False, 'message': '图谱文件损坏或格式错误'}

        query_engine = QueryEngine(graph)
        params = QueryParams(
            keywords=data.get('keywords', []),
            node_types=data.get('node_types'),
            engine_filter=data.get('engine_filter'),
            depth=data.get('depth', 1),
        )
        result = query_engine.query(params)

        try:
            append_knowledge_log('GRAPH_QUERY_RESULT', {
                'report_id': report_id or 'latest',
                'counts': {
                    'matched_sections': len(result.matched_sections),
                    'matched_queries': len(result.matched_queries),
                    'matched_sources': len(result.matched_sources),
                    'total_nodes': result.total_nodes,
                },
                'query_params': result.query_params,
                'matched_sections': compact_records(result.matched_sections),
                'matched_queries': compact_records(result.matched_queries),
                'matched_sources': compact_records(result.matched_sources),
            })
        except Exception:
            logger.warning("Knowledge Query: 结果写日志失败")

        return {
            'success': True,
            'result': {
                'matched_sections': result.matched_sections,
                'matched_queries': result.matched_queries,
                'matched_sources': result.matched_sources,
                'total_nodes': result.total_nodes,
                'query_params': result.query_params,
                'summary': result.get_summary(),
            },
        }
    except Exception as e:
        logger.exception(f"图谱查询失败: {e}")
        return {'success': False, 'message': f'图谱查询失败: {str(e)}'}
