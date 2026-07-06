<template>
  <div class="search-section">
    <div class="search-bar">
      <el-input
        v-model="query"
        placeholder="输入人物、地点、事件、品牌或主题"
        size="large"
        clearable
        @keyup.enter="handleSearch"
      >
        <template #append>
          <el-button
            type="primary"
            :loading="searchStore.searching"
            @click="handleSearch"
          >
            开始分析
          </el-button>
        </template>
      </el-input>
    </div>

    <div class="search-actions">
      <label class="template-upload">
        <input
          type="file"
          accept=".md,.txt"
          style="display:none"
          @change="handleFileUpload"
        />
        <el-button size="small" tag="span">
          <el-icon><Upload /></el-icon>
          模板
        </el-button>
      </label>

      <el-popover placement="bottom-end" :width="320" trigger="click">
        <template #reference>
          <el-button size="small" :icon="Setting">
            TrendScope
          </el-button>
        </template>
        <div class="advanced-options">
          <div class="option-row">
            <span class="option-label">搜索增强</span>
            <el-select v-model="options.search_enhancement_mode" size="small" style="width: 150px">
              <el-option label="关闭" value="off" />
              <el-option label="轻量" value="light" />
              <el-option label="完整" value="full" />
            </el-select>
          </div>
          <el-checkbox v-model="options.enable_network_search">启用网络搜索</el-checkbox>
          <el-checkbox v-model="options.enable_video_hotspots">启用视频平台热点分析</el-checkbox>
          <el-checkbox v-model="options.enable_local_knowledge">启用本地知识库</el-checkbox>
          <el-checkbox v-model="options.enable_risk_analysis">启用舆情风险分析</el-checkbox>
          <el-checkbox v-model="options.enable_deep_report">启用深度报告模式</el-checkbox>
        </div>
      </el-popover>

      <el-button
        size="small"
        :icon="Setting"
        @click="configStore.openModal"
      >
        LLM 配置
      </el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { Upload, Setting } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useSearchStore } from '@/stores/search'
import { useConfigStore } from '@/stores/config'

const searchStore = useSearchStore()
const configStore = useConfigStore()

const query = ref('')
const options = reactive({
  enable_network_search: true,
  enable_video_hotspots: true,
  enable_local_knowledge: true,
  enable_risk_analysis: false,
  enable_deep_report: false,
  search_enhancement_mode: 'off' as 'off' | 'light' | 'full',
})

async function handleSearch() {
  const q = query.value.trim()
  if (!q) return
  try {
    await searchStore.performSearch(q, { ...options })
    ElMessage.success('已提交给 TrendScope 自动编排')
  } catch {
    ElMessage.error('搜索请求失败')
  }
}

function handleFileUpload(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (file && file.size <= 1024 * 1024) {
    const reader = new FileReader()
    reader.onload = (ev) => {
      const content = ev.target?.result as string
      configStore.values.custom_template = content
    }
    reader.readAsText(file)
  }
}
</script>

<style scoped>
.search-section {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 20px;
  background: #f0f2f5;
  border-bottom: 1px solid #dcdfe6;
}

.search-bar {
  flex: 1;
  min-width: 0;
}

.search-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

.advanced-options {
  display: grid;
  gap: 6px;
}

.option-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.option-label {
  font-size: 13px;
  color: #606266;
  white-space: nowrap;
}

.advanced-options :deep(.el-checkbox) {
  margin-right: 0;
  height: 24px;
}
</style>
