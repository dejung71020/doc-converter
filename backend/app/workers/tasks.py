from app.workers.celery_app import celery_app

@celery_app.task(bind=True, max_retries=3)
def run_conversion_job(self, job_id: str):
    # Stage 1-7 파이프라인 실행 (이후 단계에서 채워나갈 예정)
    pass