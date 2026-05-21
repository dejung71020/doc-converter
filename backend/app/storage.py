import uuid
from datetime import timedelta
from google.cloud import storage
from app.core.config import settings

gcs_client: storage.Client | None = None

def get_gcs_client() -> storage.Client:
    """
    GCS 클라이언트 인스턴스를 반환한다.
    앱 전체에서 하나의 클라이언트를 재사용하여 커넥션 낭비를 방지한다.
    """
    global gcs_client
    if gcs_client is None:
        gcs_client = storage.Client(project=settings.GCS_PROJECT_ID)
    return gcs_client

def generate_upload_signed_url(
        destination_path: str,
        content_type: str,
        expiration_minutes: int = 15,
) -> str:
    """
    클라이언트가 GCS에 직접 파일을 업로드 할 수 있는 Signed URL을 발급한다.
    서버를 거치지 않아 대용량 파일도 빠르게 처리 가능하다.

    Args:
        destination_path: GCS 내 저장 경로 예 : inputs/job_id/a_file.pdf
        content_type: MIME 타입 예: application/pdf
        expiration_minutes: URL 유효 시간 (기본 15분)

    Returns:
        클라이언트가 PUT 요청으로 파일을 업로드 할 수 있는 Signed URL
    """
    client = get_gcs_client()
    bucket = client.bucket(settings.GCS_BUCKET_NAME)
    blob = bucket.blob(destination_path)

    url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=expiration_minutes),
        method="PUT",
        content_type=content_type,
    )
    return url

def generate_download_signed_url(
        file_path: str,
        expiration_minutes: int = 60,
) -> str:
    """
    클라이언트가 GCS에서 파일을 다운로드할 수 있는 Signed URL을 발급한다.
    최종 PDF 다운로드 시에 사용한다.

    Args:
        file_path: GCS 내 파일 경로
        expiration_minutes: URL 유효 시간 (기본 60분)
    
    Returns:
        클라이언트가 GET 요청으로 파일을 다운로드할 수 있는 Signed URL
    """
    client = get_gcs_client()
    bucket = client.bucket(settings.GCS_BUCKET_NAME)
    blob = bucket.blob(file_path)

    url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=expiration_minutes),
        method="GET",
    )
    return url

def generate_input_path(job_id: str, filename: str) -> str:
    """
    입력 파일의 GCS 저장 경로를 생성한다.
    job_id별로 폴더를 분리해 파일 충돌을 방지한다.

    Args:
        job_id: 변환 작업 ID
        filename: 원본 파일명

    Returns:
        GCS 저장 경로 예: inputs/job_id/filename
    """
    return f"inputs/{job_id}/{filename}"


def generate_output_path(job_id: str) -> str:
    """
    최종 출력 PDF의 GCS 저장 경로를 생성한다.

    Args:
        job_id: 변환 작업 ID

    Returns:
        GCS 저장 경로 예: outputs/job_id/result.pdf
    """
    return f"outputs/{job_id}/result.pdf"