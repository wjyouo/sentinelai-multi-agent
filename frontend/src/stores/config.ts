import { defineStore } from 'pinia'
import { ref, reactive } from 'vue'
import * as configApi from '@/api/config'

export interface ConfigFieldDef {
  key: string
  label: string
  type: 'text' | 'number' | 'password' | 'select'
  hint?: string
  options?: { value: string; label: string }[]
  condition?: { key: string; value: string }
}

export interface ConfigGroupDef {
  title: string
  subtitle: string
  fields: ConfigFieldDef[]
}

export const useConfigStore = defineStore('config', () => {
  const values = reactive<Record<string, any>>({})
  const dirty = ref(false)
  const modalOpen = ref(false)
  const lastLoaded = ref<string | null>(null)

  const groups: ConfigGroupDef[] = [
    {
      title: '服务器配置',
      subtitle: 'Server Settings',
      fields: [
        { key: 'HOST', label: '主机地址', type: 'text' },
        { key: 'PORT', label: '端口号', type: 'number' },
      ],
    },
    {
      title: '数据库配置',
      subtitle: 'Database Settings',
      fields: [
        { key: 'DB_DIALECT', label: '数据库类型', type: 'text' },
        { key: 'DB_HOST', label: '数据库主机', type: 'text' },
        { key: 'DB_PORT', label: '数据库端口', type: 'number' },
        { key: 'DB_USER', label: '数据库用户', type: 'text' },
        { key: 'DB_PASSWORD', label: '数据库密码', type: 'password' },
        { key: 'DB_NAME', label: '数据库名称', type: 'text' },
        { key: 'DB_CHARSET', label: '字符集', type: 'text' },
      ],
    },
    {
      title: 'Insight Agent (LLM)',
      subtitle: 'Insight Engine LLM Settings',
      fields: [
        { key: 'INSIGHT_ENGINE_API_KEY', label: 'Insight Agent API Key', type: 'password', hint: '用于 Insight Agent 的主 LLM，保存到项目根目录 .env。' },
        { key: 'INSIGHT_ENGINE_BASE_URL', label: 'Insight Agent Base URL', type: 'text', hint: 'OpenAI 兼容接口地址，例如 Moonshot/Kimi。' },
        { key: 'INSIGHT_ENGINE_MODEL_NAME', label: 'Insight Agent 模型名称', type: 'text' },
      ],
    },
    {
      title: 'Media Agent (LLM)',
      subtitle: 'Media Engine LLM Settings',
      fields: [
        { key: 'MEDIA_ENGINE_API_KEY', label: 'Media Agent API Key', type: 'password', hint: '用于 Media Agent 的视频/传播分析 LLM，保存到项目根目录 .env。' },
        { key: 'MEDIA_ENGINE_BASE_URL', label: 'Media Agent Base URL', type: 'text' },
        { key: 'MEDIA_ENGINE_MODEL_NAME', label: 'Media Agent 模型名称', type: 'text' },
      ],
    },
    {
      title: 'Query Agent (LLM)',
      subtitle: 'Query Engine LLM Settings',
      fields: [
        { key: 'QUERY_ENGINE_API_KEY', label: 'Query Agent API Key', type: 'password', hint: '用于 Query Agent 的事实核查/可信来源分析 LLM，保存到项目根目录 .env。' },
        { key: 'QUERY_ENGINE_BASE_URL', label: 'Query Agent Base URL', type: 'text' },
        { key: 'QUERY_ENGINE_MODEL_NAME', label: 'Query Agent 模型名称', type: 'text' },
      ],
    },
    {
      title: 'Report Agent (LLM)',
      subtitle: 'Report Engine LLM Settings',
      fields: [
        { key: 'REPORT_ENGINE_API_KEY', label: 'Report Agent API Key', type: 'password', hint: '用于 Report Agent 的长报告生成/修复，保存到项目根目录 .env。' },
        { key: 'REPORT_ENGINE_BASE_URL', label: 'Report Agent Base URL', type: 'text' },
        { key: 'REPORT_ENGINE_MODEL_NAME', label: 'Report Agent 模型名称', type: 'text' },
      ],
    },
    {
      title: 'SentinelSpider Agent',
      subtitle: 'SentinelSpider LLM Settings',
      fields: [
        { key: 'SENTINEL_SPIDER_API_KEY', label: 'SentinelSpider Agent API Key', type: 'password', hint: '用于本地采集工具的 LLM 辅助能力，保存到项目根目录 .env。' },
        { key: 'SENTINEL_SPIDER_BASE_URL', label: 'SentinelSpider Agent Base URL', type: 'text' },
        { key: 'SENTINEL_SPIDER_MODEL_NAME', label: 'SentinelSpider Agent 模型名称', type: 'text' },
      ],
    },
    {
      title: 'Forum Host',
      subtitle: 'Forum Host LLM Settings',
      fields: [
        { key: 'FORUM_HOST_API_KEY', label: 'Forum Host API Key', type: 'password', hint: '用于 Forum Host 总结多 Agent 讨论，保存到项目根目录 .env。' },
        { key: 'FORUM_HOST_BASE_URL', label: 'Forum Host Base URL', type: 'text' },
        { key: 'FORUM_HOST_MODEL_NAME', label: 'Forum Host 模型名称', type: 'text' },
      ],
    },
    {
      title: 'Keyword Optimizer',
      subtitle: 'Keyword Optimizer LLM Settings',
      fields: [
        { key: 'KEYWORD_OPTIMIZER_API_KEY', label: 'Keyword Optimizer API Key', type: 'password', hint: '用于 Insight Agent 查询改写/关键词优化，保存到项目根目录 .env。' },
        { key: 'KEYWORD_OPTIMIZER_BASE_URL', label: 'Keyword Optimizer Base URL', type: 'text', hint: '例如硅基流动或其他 OpenAI 兼容服务地址。' },
        { key: 'KEYWORD_OPTIMIZER_MODEL_NAME', label: 'Keyword Optimizer 模型名称', type: 'text' },
      ],
    },
    {
      title: '网络搜索配置',
      subtitle: 'Search API Settings',
      fields: [
        { key: 'TAVILY_API_KEY', label: 'Tavily Search API Key', type: 'password', hint: '用于 Query/Media/TrendScope 的公开网络搜索。' },
        {
          key: 'SEARCH_TOOL_TYPE',
          label: '搜索工具',
          type: 'select',
          options: [
            { value: 'TavilyAPI', label: 'TavilyAPI（默认）' },
            { value: 'AnspireAPI', label: 'AnspireAPI' },
            { value: 'BochaAPI', label: 'BochaAPI' },
          ],
        },
        { key: 'BOCHA_BASE_URL', label: 'Bocha Base URL', type: 'text', condition: { key: 'SEARCH_TOOL_TYPE', value: 'BochaAPI' } },
        { key: 'BOCHA_WEB_SEARCH_API_KEY', label: 'Bocha Search API Key', type: 'password', hint: '用于 Bocha 公开搜索。', condition: { key: 'SEARCH_TOOL_TYPE', value: 'BochaAPI' } },
        { key: 'ANSPIRE_BASE_URL', label: 'Anspire Base URL', type: 'text', condition: { key: 'SEARCH_TOOL_TYPE', value: 'AnspireAPI' } },
        { key: 'ANSPIRE_API_KEY', label: 'Anspire Search API Key', type: 'password', hint: '用于 Anspire 公开搜索。', condition: { key: 'SEARCH_TOOL_TYPE', value: 'AnspireAPI' } },
      ],
    },
  ]

  async function refreshFromServer() {
    try {
      const res = await configApi.fetchConfig()
      if (res.data.success) {
        Object.assign(values, res.data.config)
        lastLoaded.value = new Date().toISOString()
      }
    } catch {
      // ignore
    }
  }

  async function saveUpdates(updates: Record<string, any>) {
    const res = await configApi.saveConfig(updates)
    if (res.data.success) {
      Object.assign(values, res.data.config)
      dirty.value = false
    }
    return res.data
  }

  function openModal() { modalOpen.value = true }
  function closeModal() { modalOpen.value = false }

  return {
    values, groups, dirty, modalOpen, lastLoaded,
    refreshFromServer, saveUpdates, openModal, closeModal,
  }
})
