"""FastAPI main application — Phase 1 dual-run alongside Flask."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from services.forum_service import init_forum_log, start_forum_log_monitor
from utils.knowledge_logger import init_knowledge_log

from routers import system, config, apps, forum, search, graph, events


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    init_forum_log()
    init_knowledge_log()
    start_forum_log_monitor()
    logger.info("FastAPI 服务器已启动，共享服务已初始化")
    yield


app = FastAPI(
    title="尚舆分析平台 API",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(system.router)
app.include_router(config.router)
app.include_router(apps.router)
app.include_router(forum.router)
app.include_router(search.router)
app.include_router(graph.router)
app.include_router(events.router)
