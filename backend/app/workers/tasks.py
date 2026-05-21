import json
import asyncio

from celery import states

from app.workers.celery_app import celery_app
from app.redis_client import get_redis
from app.pipeline import (
    stage1_validation,
    stage1_5_pii,
    stage2_extraction,
    stage3_template,
    stage4_mapping,
    stage5_generation,
    stage6_assembly,
    stage7_validation,
)


async def _publish_event(job_id: str, event: dict) -> None:
    """
    파이프라인 진행 이벤트를 Redis Pub/Sub 채널에 발행한다.
    WebSocket 핸들러가 구독 중이므로 클라이언트에 실시간으로 전달된다.

    Args:
        job_id: 변환 작업 ID
        event: 전달할 이벤트 딕셔너리
    """
    redis = await get_redis()
    await redis.publish(
        f"job:{job_id}:events",
        json.dumps(event, ensure_ascii=False),
    )


async def _run_pipeline(
    job_id: str,
    a_file_path: str,
    a_filename: str,
    b_file_path: str,
    b_filename: str,
) -> dict:
    """
    Stage 1~7 전체 파이프라인을 순서대로 실행한다.
    각 Stage 완료 시 Redis Pub/Sub으로 이벤트를 발행해 클라이언트에 실시간 진행 상황을 전달한다.

    Stage 2와 Stage 3은 서로 의존성이 없으므로 asyncio.gather로 병렬 실행한다.
    Stage 1은 동기 함수이므로 asyncio.to_thread로 이벤트 루프 블로킹을 방지한다.

    Args:
        job_id: 변환 작업 ID
        a_file_path: GCS 내 A 파일 경로
        a_filename: A 파일 원본명
        b_file_path: GCS 내 B 파일 경로
        b_filename: B 파일 원본명

    Returns:
        { status, final_pdf_path, ssim_score }
    """

    # Stage 1: 입력 검증 + 전처리
    await _publish_event(job_id, {"type": "stage_start", "stage": 1, "name": "입력 검증 및 전처리"})
    stage1_result = await asyncio.to_thread(stage1_validation.run, a_file_path, a_filename)
    if not stage1_result["success"]:
        raise RuntimeError(f"Stage 1 실패: {stage1_result['error']}")
    await _publish_event(job_id, {"type": "stage_complete", "stage": 1})

    # Stage 1.5: PII 감지 + 마스킹
    await _publish_event(job_id, {"type": "stage_start", "stage": 1.5, "name": "PII 감지 및 마스킹"})
    stage1_5_result = await stage1_5_pii.run(job_id, stage1_result["extracted"])
    if not stage1_5_result["success"]:
        raise RuntimeError(f"Stage 1.5 실패: {stage1_5_result['error']}")
    await _publish_event(job_id, {"type": "stage_complete", "stage": 1.5})

    # Stage 2 + 3: 병렬 실행
    await _publish_event(job_id, {"type": "stage_start", "stage": 2, "name": "A 콘텐츠 추출"})
    await _publish_event(job_id, {"type": "stage_start", "stage": 3, "name": "B 템플릿 분석"})
    stage2_result, stage3_result = await asyncio.gather(
        stage2_extraction.run(job_id, stage1_5_result["masked_extracted"]),
        stage3_template.run(job_id, b_file_path, b_filename),
    )
    if not stage2_result["success"]:
        raise RuntimeError(f"Stage 2 실패: {stage2_result['error']}")
    if not stage3_result["success"]:
        raise RuntimeError(f"Stage 3 실패: {stage3_result['error']}")
    await _publish_event(job_id, {"type": "stage_complete", "stage": 2, "confidence": stage2_result.get("avg_confidence", 0)})
    await _publish_event(job_id, {"type": "stage_complete", "stage": 3, "confidence": stage3_result.get("verification_score", 0)})

    # Stage 4: 의미론적 매핑
    await _publish_event(job_id, {"type": "stage_start", "stage": 4, "name": "의미론적 매핑"})
    stage4_result = await stage4_mapping.run(job_id, stage2_result, stage3_result)
    if not stage4_result["success"]:
        raise RuntimeError(f"Stage 4 실패: {stage4_result['error']}")
    await _publish_event(job_id, {"type": "stage_complete", "stage": 4, "confidence": stage4_result.get("ensemble_match_rate", 0)})

    # Human Checkpoint #1: 매핑 일치율이 낮으면 사용자 확인 요청
    if stage4_result.get("requires_checkpoint"):
        await _publish_event(job_id, {
            "type": "human_required",
            "checkpoint": 1,
            "data": {
                "mappings": stage4_result["mappings"],
                "mismatch_fields": stage4_result["mismatch_fields"],
                "transformation_intent": stage4_result["transformation_intent"],
            },
        })
        return {"status": "waiting_input", "checkpoint": 1}

    # Stage 5: 콘텐츠 생성 + Self-RAG 검증
    await _publish_event(job_id, {"type": "stage_start", "stage": 5, "name": "콘텐츠 생성 + Self-RAG 검증"})
    stage5_result = await stage5_generation.run(job_id, stage2_result, stage3_result, stage4_result)
    if not stage5_result["success"]:
        raise RuntimeError(f"Stage 5 실패: {stage5_result['error']}")
    await _publish_event(job_id, {"type": "stage_complete", "stage": 5})

    # Stage 6: 문서 조립
    await _publish_event(job_id, {"type": "stage_start", "stage": 6, "name": "문서 조립"})
    stage6_result = stage6_assembly.run(
        job_id,
        stage3_result["html_template"],
        stage5_result["field_contents"],
    )
    if not stage6_result["success"]:
        raise RuntimeError(f"Stage 6 실패: {stage6_result['error']}")
    await _publish_event(job_id, {"type": "stage_complete", "stage": 6})

    # Stage 7: 형식 검증 + PII 복원 + 최종 PDF 생성
    await _publish_event(job_id, {"type": "stage_start", "stage": 7, "name": "형식 검증 + PII 복원"})
    page_size = stage3_result.get("schema", {}).get("page_size", {"width_px": 794, "height_px": 1123})
    stage7_result = await stage7_validation.run(
        job_id,
        stage6_result["filled_html"],
        stage3_result["b_image_bytes"],
        page_size,
    )
    if not stage7_result["success"]:
        raise RuntimeError(f"Stage 7 실패: {stage7_result['error']}")

    await _publish_event(job_id, {
        "type": "job_complete",
        "result_url": stage7_result["final_pdf_path"],
        "ssim_score": stage7_result["ssim_score"],
        "checkpoint_required": stage7_result["checkpoint_required"],
        "checkpoint_recommended": stage7_result["checkpoint_recommended"],
        "warnings": stage5_result.get("warnings", []),
    })

    return {
        "status": "completed",
        "final_pdf_path": stage7_result["final_pdf_path"],
        "ssim_score": stage7_result["ssim_score"],
    }


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def run_conversion_job(
    self,
    job_id: str,
    a_file_path: str,
    a_filename: str,
    b_file_path: str,
    b_filename: str,
) -> dict:
    """
    문서 변환 파이프라인 전체를 실행하는 메인 Celery 태스크.
    Stage 1~7을 순서대로 실행하며, 각 Stage 완료 시 Redis에 이벤트를 발행한다.
    실패 시 최대 3회 재시도하며 재시도 간격은 60초다.

    Args:
        job_id: 변환 작업 ID
        a_file_path: GCS 내 A 파일 경로
        a_filename: A 파일 원본명
        b_file_path: GCS 내 B 파일 경로
        b_filename: B 파일 원본명

    Returns:
        { status, final_pdf_path, ssim_score }
    """
    try:
        self.update_state(state=states.STARTED)
        return asyncio.run(
            _run_pipeline(job_id, a_file_path, a_filename, b_file_path, b_filename)
        )
    except RuntimeError as exc:
        # Rate Limit 또는 파이프라인 Stage 실패 → 재시도
        asyncio.run(_publish_event(job_id, {
            "type": "job_failed",
            "error": str(exc),
            "will_retry": self.request.retries < self.max_retries,
        }))
        raise self.retry(exc=exc)
    except Exception as exc:
        # 예상치 못한 오류 → 재시도하되 마지막 시도면 최종 실패 처리
        asyncio.run(_publish_event(job_id, {
            "type": "job_failed",
            "error": f"예기치 않은 오류: {str(exc)}",
            "will_retry": self.request.retries < self.max_retries,
        }))
        raise self.retry(exc=exc)
