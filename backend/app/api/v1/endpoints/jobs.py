import uuid
from fastapi import APIRouter
from pydantic import BaseModel

from app.workers.tasks import run_conversion_job

router = APIRouter()


class JobCreateRequest(BaseModel):
    a_file_path: str
    a_filename: str
    b_file_path: str
    b_filename: str


class JobCreateResponse(BaseModel):
    job_id: str
    status: str
    message: str


@router.post("", response_model=JobCreateResponse)
async def create_job(request: JobCreateRequest):
    """
    문서 변환 작업을 생성하고 Celery 파이프라인을 시작한다.
    A 파일(변환할 문서)과 B 파일(목표 형식)의 GCS 경로를 받아
    비동기로 파이프라인을 실행하고 즉시 job_id를 반환한다.

    실시간 진행 상황은 WebSocket /ws/v1/jobs/{job_id}로 수신 가능하다.

    Args:
        a_file_path: GCS 내 A 파일 경로 (upload presigned-url로 업로드 후 반환된 경로)
        a_filename: A 파일 원본명
        b_file_path: GCS 내 B 파일 경로
        b_filename: B 파일 원본명

    Returns:
        job_id: 생성된 작업 ID (WebSocket 연결 및 상태 조회에 사용)
        status: queued
        message: 안내 메시지
    """
    job_id = str(uuid.uuid4())

    run_conversion_job.delay(
        job_id=job_id,
        a_file_path=request.a_file_path,
        a_filename=request.a_filename,
        b_file_path=request.b_file_path,
        b_filename=request.b_filename,
    )

    return JobCreateResponse(
        job_id=job_id,
        status="queued",
        message=f"변환 작업이 시작됐습니다. WebSocket /ws/v1/jobs/{job_id}로 진행 상황을 확인하세요.",
    )
