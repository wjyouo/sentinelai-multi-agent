import client from './client'

export interface SearchOptions {
  enable_network_search: boolean
  enable_video_hotspots: boolean
  enable_local_knowledge: boolean
  enable_risk_analysis: boolean
  enable_deep_report: boolean
  search_enhancement_mode: 'off' | 'light' | 'full'
}

export function search(query: string, options?: Partial<SearchOptions>) {
  return client.post('/api/search', { query, options })
}

export function fetchLatestResults() {
  return client.get('/api/search/latest')
}
