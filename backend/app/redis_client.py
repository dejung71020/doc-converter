import redis.asyncio as aioredis
from app.core.config import settings

redis_client: aioredis.Redis | None = None

async def get_redis() -> aioredis.Redis:
    """
    Redis 클라이언트 인스턴스를 반환한다.
    앱 전체에서 하나의 연결을 재사용하여 커넥션 낭비를 방지한다.
    FastAPI의 Depends(get_redis)로 주입받아 사용한다.
    """
    global redis_client
    if redis_client is None:
        redis_client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
    return redis_client

async def close_redis():
    """
    앱 종료 시 Redis 연결을 안전하게 닫는다.
    FastAPI의 lifespan 이벤트에서 호출된다.
    """
    global redis_client
    if redis_client:
        await redis_client.close()
        redis_client = None