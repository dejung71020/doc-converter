from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import json

from app.core.config import settings
from app.api.v1.router import router
from app.redis_client import get_redis, close_redis

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    앱 시작/종료 시 실행되는 lifespan 이벤트 핸들러.
    시작 시 Redis 연결을 초기화하고, 종료 시 안전하게 닫는다.
    """
    await get_redis()
    yield
    await close_redis()

app = FastAPI(
    title="Doc Converter API",
    version="0.1.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

@app.websocket("/ws/v1/jobs/{job_id}")
async def job_progress(websocket: WebSocket, job_id: str):
    """
    변환 작업 실시간 진행 상황을 클라이언트에 전달하는 WebSocket 엔드포인트.
    Redis Pub/Sub을 구독해 Celery Worker가 발행하는 이벤트를 수신하고,
    연결된 클라이언트에게 즉시 전달한다.
    Cloud Run 다중 인스턴스 환경에서도 어느 Worker가 처리하든
    클라이언트가 이벤트를 놓치지 않는다.
    """
    await websocket.accept()
    redis = await get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"job:{job_id}:events")
    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                await websocket.send_text(message["data"])
    except WebSocketDisconnect:
        await pubsub.unsubscribe(f"job:{job_id}:events")
        await pubsub.close()