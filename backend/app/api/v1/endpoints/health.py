from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.db import get_db
from app.core.config import settings

router = APIRouter()

@router.get("")
async def health_check(db: AsyncSession = Depends(get_db)):
    """
    서버, DB, 환경 상태를 한 번에 확인하는 헬스체크 엔드포인트.
    DB 연결이 실패하면 500 에러를 반환해 서버 이상을 감지할 수 있다.
    로드밸런서와 모니터링 시스템에서 주기적으로 호출된다.
    """
    await db.execute(text("SELECT 1"))
    return {
        "status": "ok",
        "environment": settings.ENVIRONMENT,
        "database": "connected",
    }