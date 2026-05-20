from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import json

from app.core.config import settings
from app.api.v1.router import router

app = FastAPI(
    title="Doc Converter API",
    version="0.1.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
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
    Issue #4에서 Redis Pub/Sub 연동이 추가될 예정.
    """
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(
                json.dumps({
                    "type": "echo",
                    "data": data
                })
            )
    except WebSocketDisconnect:
        pass