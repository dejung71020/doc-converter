import fitz # PyMuPDF
import docx
from PIL import Image
import io
from pathlib import Path

from app.core.config import settings
from app.storage import get_gcs_client

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".docx"}
MAX_FILE_SIZE_MB = 100
MIN_IMAGE_DPI = 150

def download_from_gcs(file_path: str) -> bytes:
    """
    GCS에서 파일을 다운로드하여 바이트로 반환한다.
    파이프라인 각 Stage에서 파일을 읽을 때 사용한다.

    Args:
        file_path: GCS 내 파일 경로

    Returns:
        파일 바이트 데이터
    """
    client = get_gcs_client()
    bucket = client.bucket(settings.GCS_BUCKET_NAME)
    blob = bucket.blob(file_path)
    return blob.download_as_bytes()

def validate_file(filename: str, file_bytes: bytes) -> dict:
    """
    파일 형식과 크기를 검증한다.
    지원하지 않는 형식이거나 용량 초과 시 에러를 반환한다.

    Args:
        filename: 원본 파일명
        file_bytes: 파일 바이트 데이터

    Returns:
        {
            valid: bool,
            error: str | None,
            extension: str
        }
    """
    extension = Path(filename).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        return {
            "valid": False,
            "error": f"지원하지 않는 파일 형식입니다. 지원 형식 : .pdf, .jpg, .jpeg, .png, .webp, .docx",
            "extension": extension,
        }
    
    size_mb = len(file_bytes) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        return {
            "valid": False,
            "error": f"파일 크기가 너무 큽니다. 최대 {MAX_FILE_SIZE_MB}MB 까지 지원합니다.",
            "extension": extension,
        }
    
    return {
        "valid": True,
        "error": None,
        "extension": extension
    }

def extract_from_pdf(file_bytes: bytes) -> dict:
    """
    PyMuPDF를 사용하여 PDF에서 텍스트와 메타데이터를 추출한다.
    텍스트 레이어가 있으면 직접 추출하고, 없으면 이미지 PDF로 분류한다.
    AI 호출 없이 처리하므로 빠르고 정확하다.

    Args:
        file_bytes: PDF 파일 바이트 데이터

    Returns:
        { text: str, pages: int, has_text_layer: bool, sections: list}
    """
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    full_text = ""
    sections = []

    for page_num, page in enumerate(doc):
        page_text = page.get_text()
        full_text += page_text
        if page_text.strip():
            sections.append({
                "page": page_num + 1,
                "content": page_text.strip()
            })
    
    has_text_layer = len(full_text.strip()) > 0

    return {
        "text": full_text,
        "pages": len(doc),
        "has_text_layer": has_text_layer,
        "sections": sections,
    }

def extract_from_docx(file_bytes: bytes) -> dict:
    """
    python-docx를 사용하여 Word 파일에서 텍스트와 구조를 추출한다.
    제목, 본문, 표를 구분하여 추출하며 스타일 정보도 함께 반환한다.
    SmartArt, 도형, 수식은 무시하고 텍스트만 추출한다.

    Args:
        file_bytes: docx 파일 바이트 데이터

    Returns:
        { text: str, sections: list, has_tables: bool }
    """
    doc = docx.Document(io.BytesIO(file_bytes))
    full_text = ""
    sections = []

    for para in doc.paragraphs:
        if para.text.strip():
            full_text += para.text + "\n"
            sections.append({
                "style": para.style.name,
                "content": para.text.strip(),
            })

    tables = []
    for table in doc.tables:
        table_data = []
        for row in table.rows:
            row_data = [cell.text.strip() for cell in row.cells]
            table_data.append(row_data)
        tables.append(table_data)

    return {
        "text": full_text,
        "sections": sections,
        "has_tables": len(tables) > 0,
        "tables": tables,
    }

def validate_image(file_bytes: bytes) -> dict:
    """
    이미지 파일의 해상도를 확인하고, 필요 시 업스케일한다.
    150 DPI 미만이면 경고를 반환하고, Lanczos 알고리즘으로 업스케일한다.

    Args:
        file_bytes: 이미지 파일 바이트 데이터
    
    Returns:
        { valid: bool, warning: str | None, enhanced_bytes: bytes }
    """
    image = Image.open(io.BytesIO(file_bytes))
    width, height = image.size

    dpi_tuple = image.info.get("dpi", (72, 72))
    dpi = dpi_tuple[0] if dpi_tuple[0] > 0 else 72

    warning = None

    original_format = image.format or "PNG"


    if dpi < MIN_IMAGE_DPI:
        warning = f"이미지 해상도가 낮습니다.({dpi} DPI) : 변환 정확도가 떨어질 수 있습니다."
        scale_factor = MIN_IMAGE_DPI / dpi
        new_size = (int(width * scale_factor), int(height * scale_factor))
        image = image.resize(new_size, Image.Resampling.LANCZOS)

    output = io.BytesIO()
    image.save(output, format=original_format)

    return {
        "valid": True,
        "warning": warning,
        "enhanced_bytes": output.getvalue(),
        "width": image.size[0],
        "height": image.size[1],
    }

def run(file_path: str, filename: str) -> dict:
    """
    Stage 1 전체 실행 함수.
    GCS에서 파일을 다운로드하고 형식에 맞게 검증 및 추출을 수행한다.
    Celery Worker의 run_conversion_job 태스크에서 호출된다.

    Args:
        file_path: GCS 내 파일 경로
        filename: 원본 파일명

    Returns:
        {
            success: bool,
            error: str | None,
            extension: str,
            extracted: dict,  # 추출된 텍스트 및 구조
            warning: str | None,
        }
    """
    try:
        file_bytes = download_from_gcs(file_path)
    except Exception as e:
        return {
            "success": False,
            "error": f"GCS 다운로드 실패: {str(e)}",
            "extension": "",
            "extracted": {},
            "warning": None,
        }

    # 1. 파일 검증
    validation = validate_file(filename, file_bytes)
    extension = validation["extension"]

    if not validation["valid"]:
        return {
            "success": False,
            "error": validation["error"],
            "extension": extension,
            "extracted": {},
            "warning": None,
        }

    # 2. 포맷별 분기 처리
    warning = None
    try:
        if extension == ".pdf":
            extracted = extract_from_pdf(file_bytes)
        elif extension == ".docx":
            extracted = extract_from_docx(file_bytes)
        else:
            result = validate_image(file_bytes)
            extracted = {"enhanced_bytes": result["enhanced_bytes"]}
            warning = result["warning"]
    except Exception as e:
        return {
            "success": False,
            "error": f"파일 처리 실패: {str(e)}",
            "extension": extension,
            "extracted": {},
            "warning": None,
        }

    # 3. 최종 반환 (단일 출구 원칙)
    return {
        "success": True,
        "error": None,
        "extension": extension,
        "extracted": extracted,
        "warning": warning,
    }