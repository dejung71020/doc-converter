import time
from app.redis_client import get_redis

LIMITS = {
    "gemini-1.5-pro": {
        "rpm": 360,
        "tpm": 4_000_000
    },
    "gemini-1.5-flash": {
        "rpm": 1000,
        "tpm": 4_000_000
    }
}

async def acquire(model: str, estimated_tokens: int) -> bool:
    """
    Gemini API 호출 전 Rate Limit 여유를 확인하는 Token Bucket 함수.
    Redis에서 글로벌하게 RPM 과 TPM 을 관리하여 모든 Celery Worker가 공유한다.
    한도 초과 시 이미 증가된 카운트를 롤백하여 블락을 방지한다.
    False 반환 시 Celery 태스크는 60초 후 재시도해야 한다.

    Args:
        model: 사용할 Gemini 모델명 예: gemini-1.5-pro
        estimated_tokens: 이번 호출에서 예상되는 총 토큰 수

    Returns:
        True: API 호출 가능
        False: 한도 초과, 재시도 필요
    """
    redis = await get_redis()
    minute_bucket = int(time.time() // 60)

    rpm_key=f"rl:{model}:rpm:{minute_bucket}"
    tpm_key=f"rl:{model}:tpm:{minute_bucket}"

    async with redis.pipeline() as pipe:
        pipe.incr(rpm_key)
        pipe.expire(rpm_key, 60)
        pipe.incrby(tpm_key, estimated_tokens)
        pipe.expire(tpm_key, 60)
        results = await pipe.execute()

    rpm_count = results[0]
    tpm_count = results[2]

    # 한도 초과시 rollback
    if rpm_count > LIMITS[model]["rpm"] or tpm_count > LIMITS[model]["tpm"]:
        async with redis.pipeline() as rollback_pipe:
            rollback_pipe.decr(rpm_key)
            rollback_pipe.decrby(tpm_key, estimated_tokens)
            await rollback_pipe.execute()
        return False
    return True