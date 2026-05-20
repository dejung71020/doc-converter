from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.core.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.ENVIRONMENT == "development",
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

async def get_db():
    """
    FastAPI 의존성 주입용 DB 세션 생성 함수.
    요청 시작 시 세션을 열고, 요청 종료 시 자동으로 닫는다.
    API 엔드포인트에서 Depends(get_db)로 주입받아 사용한다.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()