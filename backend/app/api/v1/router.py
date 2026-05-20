from fastapi import APIRouter
from app.api.v1.endpoints import health

router = APIRouter(prefix="/api/v1")

router.include_router(health.router, prefix="/health", tags=["health"])