import uuid
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.storage import generate_upload_signed_url, generate_input_path

router = APIRouter()

ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class UploadRequest(BaseModel):
    filename: str
    content_type: str


class UploadResponse(BaseModel):
    job_id: str
    upload_url: str
    file_path: str


@router.post("/presigned-url", response_model=UploadResponse)
async def get_upload_url(request: UploadRequest):
    """
    클라이언트가 GCS에 직접 파일을 업로드할 수 있는 Signed URL을 발급한다.
    서버를 거치지 않아 대용량 파일도 빠르게 처리 가능하며,
    서버 메모리와 네트워크 부하를 줄일 수 있다.

    지원 형식: PDF, JPG, PNG, WEBP, DOCX

    Returns:
        job_id: 이 업로드에 할당된 작업 ID
        upload_url: GCS PUT 업로드용 Signed URL (15분 유효)
        file_path: GCS 내 저장 경로
    """
    if request.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"지원하지 않는 파일 형식입니다. 지원 형식: PDF, JPG, PNG, WEBP, DOCX",
        )

    job_id = str(uuid.uuid4())
    file_path = generate_input_path(job_id, request.filename)

    upload_url = generate_upload_signed_url(
        destination_path=file_path,
        content_type=request.content_type,
    )

    return UploadResponse(
        job_id=job_id,
        upload_url=upload_url,
        file_path=file_path,
    )