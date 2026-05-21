from fastapi import APIRouter
from app.api.v1.endpoints import health, upload, jobs

router = APIRouter(prefix="/api/v1")

router.include_router(health.router, prefix="/health", tags=["health"])
router.include_router(upload.router, prefix="/upload", tags=["upload"])
router.include_router(jobs.router, prefix="/jobs", tags=["jobs"])