# SentinelAI-MultiAgent 项目智能体协作规范

## 项目概览

- 后端：FastAPI，入口和路由位于 `app/`。
- 智能体引擎：LangGraph 工作流位于 `engines/`，当前核心包括 `QueryEngine`、`MediaEngine`、`InsightEngine`、`ReportEngine`、`ForumEngine`。
- 前端：Vue 3 + TypeScript + Vite，位于 `frontend/`。
- TrendScope：多源热点事件洞察聚合入口，负责意图识别、查询改写、自动选择 Agent、公开搜索聚合和轻量本地知识库复用。
- 本地数据：TrendScope 结果记忆默认写入 `data/trendscope/`，报告输出写入 `data/report/`。

## 运行与测试

- 后端启动：`python main.py`
- 后端测试：`pytest`
- 重点后端测试：`pytest tests/test_trendscope_service.py tests/test_app_services.py tests/test_query_engine_e2e.py tests/test_media_engine_e2e.py`
- 前端安装：`cd frontend && npm install`
- 前端开发：`cd frontend && npm run dev`
- 前端构建：`cd frontend && npm run build`
- Docker 启动：`docker compose up --build`

## 配置规则

- 所有密钥和可变配置优先写入项目根目录 `.env`，不要写死到 `config.py` 或业务代码中。
- `.env.example` 只保存示例和备注，不保存真实密钥。
- 前端“LLM 配置”弹窗保存的配置会写回项目根目录 `.env`。
- 关键词优化器缺少配置时，Insight Agent 必须回退为原始查询，不能中断整个分析任务。

## 智能体分工

- 项目总控 / Orchestrator：识别用户需求、判断参与的 Agent、协调输出结构和验收。
- TrendScope Agent：统一入口，负责查询意图识别、查询改写、本地结果记忆、时效复核和总览报告。
- Query Agent：公开搜索、事实核查、可信来源评分、事件时间线辅助。
- Media Agent：视频平台热点、传播趋势、内容爆点和平台信号分析。
- Insight Agent：本地舆情库查询、情感分析、聚类和风险分析。
- Report Agent：长报告生成、结构整理、图表/表格修复。
- Forum Host：可选，用于汇总多个 Agent 的讨论内容。

## TrendScope v1 规则

- 用户不手动选择 Agent，由系统根据输入意图和高级选项自动选择参与者。
- 第一版不做定时任务，不创建长期后台服务。
- 用户输入人物、地点、事件、品牌或主题后，系统自动识别意图、改写查询、聚合公开搜索结果，并按需输出事件时间线。
- 本地知识库采用“结果记忆”模式；热点/最近事件命中缓存后仍需做时效复核。
- 视频平台热点只使用公开搜索聚合，不接入登录爬虫、不处理验证码、不绕过平台风控。

## 开发约束

- 所有新增文件必须位于当前项目目录内。
- 临时脚本或临时文件统一放入 `.codex-temp/`，任务完成后清理。
- 不做全局安装，不修改系统环境变量，不创建长期后台服务。
- 优先沿用项目已有技术栈和依赖，不引入重依赖。
- 不删除用户已有重要代码；如必须删除，需要有明确理由。
- 不实现绕过风控、绕过验证码、攻击、爬取隐私数据或恶意自动化功能。
