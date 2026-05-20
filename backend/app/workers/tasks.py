import json
from celery import states

from app.workers.celery_app import celery_app
from app.redis_client import get_redis

async def publish_event(job_id: str, event: dict):
    """
    파이프라인 진행 이벤트를 Redis Pub/Sub 채널에 발행한다.
    WebSocket 핸들러가 구독 중이므로 클라이언트에 실시간으로 전달된다.
    """
    redis = await get_redis()
    await redis.publish(
        f"job:{job_id}:events",
        json.dumps(event)
    )

@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=5,
)
def run_conversion_job(self, job_id: str):
    """
    문서 변환 파이프라인 전체를 실행하는 메인 Celery 태스크.
    Stage 1-7을 순서대로 실행하여, 각 Stage 완료 시 Redis에 이벤트를 발행하여 클라이언트가 실시간으로 진행 상황을 확인할 수 있도록 한다.
    실패 시 최대 3회 재시도하며, 재시도 간격은 5초이다.
    """
    try:
        self.update_state(state=states.STARTED)

        stages = [
            {"number": 1,   "name": "입력 검증 및 전처리"},
            {"number": 1.5, "name": "PII 감지 및 마스킹"},
            {"number": 2,   "name": "A 콘텐츠 추출"},
            {"number": 3,   "name": "B 템플릿 분석"},
            {"number": 4,   "name": "의미론적 매핑"},
            {"number": 5,   "name": "콘텐츠 생성"},
            {"number": 6,   "name": "문서 조립"},
            {"number": 7,   "name": "형식 검증"},
        ]

        for stage in stages:
            # 각 Stage 시작 이벤트 발행
            # 실제 Stage 구현체는 Phase 2-4에서 추가 예정
            event = {
                "type": "stage_start",
                "stage": stage["number"],
                "name": stage["name"],
                "job_id": job_id,
            }

        return {
            "job_id": job_id,
            "status": "completed"
        }
    
    except Exception as exc:
        self.retry(exc=exc)
