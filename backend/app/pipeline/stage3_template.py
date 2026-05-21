import json
import io
from pathlib import Path

import fitz
import pdfplumber
from PIL import Image, ImageChops, ImageStat
from playwright.async_api import async_playwright

from app.core.config import settings
from app.storage import get_gcs_client
from app.ai.gemini_client import model_pro
from app.ai.rate_limiter import acquire

PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "stage3_template" / "v1.0.0.txt"
PROMPT_VERSION = "v1.0.0"
MODEL = "gemini-1.5-pro"
MAX_VERIFICATION_ATTEMPTS = 2
VERIFICATION_THRESHOLD = 95.0


def _download_b_file(b_file_path: str) -> bytes:
    """
    GCS에서 B 파일을 다운로드해 바이트로 반환한다.
    storage.py 싱글톤 클라이언트를 재사용해 커넥션 낭비를 방지한다.

    Args:
        b_file_path: GCS 내 B 파일 경로

    Returns:
        파일 바이트 데이터
    """
    client = get_gcs_client()
    bucket = client.bucket(settings.GCS_BUCKET_NAME)
    blob = bucket.blob(b_file_path)
    return blob.download_as_bytes()


def _get_mime_type(file_bytes: bytes) -> str:
    """
    파일 바이트의 매직 바이트를 확인해 MIME 타입을 반환한다.
    Gemini Vision API 호출 시 정확한 MIME 타입 전달이 필요하다.

    Args:
        file_bytes: 파일 바이트 데이터

    Returns:
        MIME 타입 문자열 (image/png, image/jpeg, image/webp)
    """
    if file_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    elif file_bytes[:2] == b'\xff\xd8':
        return "image/jpeg"
    elif file_bytes[:4] == b'RIFF' and file_bytes[8:12] == b'WEBP':
        return "image/webp"
    return "image/png"


def _pdf_first_page_to_image(pdf_bytes: bytes) -> bytes:
    """
    PyMuPDF를 사용해 PDF 첫 페이지를 고해상도 PNG 이미지로 변환한다.
    2x 스케일로 렌더링해 Gemini Vision 분석 정확도를 높인다.

    Args:
        pdf_bytes: PDF 파일 바이트 데이터

    Returns:
        PNG 이미지 바이트
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    mat = fitz.Matrix(2.0, 2.0)
    pix = page.get_pixmap(matrix=mat)
    return pix.tobytes("png")


def _extract_pdf_structure(pdf_bytes: bytes) -> str:
    """
    pdfplumber를 사용해 PDF에서 폰트명, 텍스트 구조, 테이블 정보를 추출한다.
    이미지 분석만으로는 알 수 없는 정확한 폰트명을 직접 추출할 수 있어
    B가 PDF일 때 이미지 B보다 훨씬 높은 레이아웃 재현 정확도를 달성한다.

    Args:
        pdf_bytes: PDF 파일 바이트 데이터

    Returns:
        Gemini에 전달할 구조 정보 텍스트
    """
    lines = []
    seen_fonts = set()

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages[:3]):
            lines.append(f"=== Page {i + 1} ===")

            text = page.extract_text()
            if text:
                lines.append(text[:500])

            for char in page.chars:
                font = char.get("fontname", "")
                size = char.get("size", "N/A")
                if font and font not in seen_fonts:
                    seen_fonts.add(font)
                    lines.append(f"[Font] {font}, Size: {size}")

            for table in page.extract_tables():
                lines.append(f"[Table] {len(table)} rows × {len(table[0]) if table else 0} cols")

    return "\n".join(lines)


def _load_prompt(context: str) -> str:
    """
    프롬프트 파일을 로드하고 분석 컨텍스트를 삽입해 최종 프롬프트를 반환한다.

    Args:
        context: {{TEMPLATE_DATA}} 자리에 삽입할 컨텍스트 텍스트

    Returns:
        완성된 프롬프트 문자열
    """
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return template.replace("{{TEMPLATE_DATA}}", context)


async def _call_gemini_vision(
    image_bytes: bytes,
    mime_type: str,
    prompt: str,
    estimated_tokens: int,
) -> str:
    """
    Rate Limiter 통과 후 Gemini Pro Vision에 이미지와 프롬프트를 전송한다.
    Rate Limit 초과 시 RuntimeError로 Celery 재시도를 유도한다.

    Args:
        image_bytes: 분석할 이미지 바이트
        mime_type: 이미지 MIME 타입
        prompt: 전송할 프롬프트
        estimated_tokens: 예상 토큰 수 (Rate Limiter 판단 기준)

    Returns:
        Gemini 텍스트 응답
    """
    allowed = await acquire(MODEL, estimated_tokens)
    if not allowed:
        raise RuntimeError("Rate limit 초과. Celery 재시도 대기 중.")

    image_part = {"mime_type": mime_type, "data": image_bytes}
    response = await model_pro.generate_content_async([prompt, image_part])
    return response.text


def _parse_schema(raw: str) -> dict:
    """
    Gemini 응답 JSON을 파싱한다.
    프롬프트에서 마크다운 금지를 명시했지만 혹시 모를 백틱을 방어적으로 제거한다.

    Args:
        raw: Gemini 원본 응답 문자열

    Returns:
        { success: bool, data: dict }
    """
    try:
        cleaned = raw.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        return {"success": True, "data": json.loads(cleaned)}
    except (json.JSONDecodeError, KeyError):
        return {"success": False, "data": {}}


def _build_html_template(schema: dict) -> str:
    """
    Gemini가 추출한 JSON 스키마를 기반으로 HTML/CSS 템플릿을 생성한다.
    각 필드는 절대 위치(position: absolute)로 배치되며,
    {{field_id}} 형태의 플레이스홀더로 Stage 6에서 실제 콘텐츠를 채워넣는다.

    Args:
        schema: Stage 3에서 추출한 레이아웃 스키마 딕셔너리

    Returns:
        완성된 HTML 문자열
    """
    page = schema.get("page_size", {"width_px": 794, "height_px": 1123})
    layout = schema.get("layout", {})
    fields = schema.get("fields", [])
    font_candidates = schema.get("font_candidates", ["Noto Sans KR"])
    primary_font = font_candidates[0] if font_candidates else "Noto Sans KR"

    css_fields = ""
    html_fields = ""

    for field in fields:
        fid = field.get("id", "field_unknown")
        pos = field.get("position", {})
        style = field.get("style", {})

        css_fields += f"""
.{fid} {{
    position: absolute;
    left: {pos.get("x", 0)}px;
    top: {pos.get("y", 0)}px;
    width: {pos.get("width", 100)}px;
    height: {pos.get("height", 30)}px;
    font-size: {style.get("font_size", 12)}px;
    font-weight: {style.get("font_weight", "normal")};
    color: {style.get("color", "#000000")};
    text-align: {style.get("text_align", "left")};
    line-height: {style.get("line_height", 1.5)};
    padding: {style.get("padding", "0")};
    border: {style.get("border", "none")};
    background-color: {style.get("background_color", "transparent")};
    overflow: hidden;
}}"""

        html_fields += f'  <div class="{fid}" data-field-id="{fid}">{{{{{fid}}}}}</div>\n'

    google_font_url = (
        f"https://fonts.googleapis.com/css2?family="
        f"{primary_font.replace(' ', '+')}&display=swap"
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<link href="{google_font_url}" rel="stylesheet">
<style>
body {{
    width: {page.get("width_px", 794)}px;
    height: {page.get("height_px", 1123)}px;
    padding: {layout.get("page_padding", "40px 50px")};
    background-color: {layout.get("background_color", "#ffffff")};
    font-family: '{primary_font}', sans-serif;
    margin: 0;
    position: relative;
    box-sizing: border-box;
}}
{css_fields}
</style>
</head>
<body>
{html_fields}
</body>
</html>"""


async def _render_html(html: str) -> bytes:
    """
    Playwright headless Chrome으로 HTML을 렌더링해 스크린샷을 반환한다.
    자기검증 루프에서 렌더링 결과와 원본 B를 픽셀 비교하는 데 사용한다.

    Args:
        html: 렌더링할 HTML 문자열

    Returns:
        PNG 스크린샷 바이트
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 794, "height": 1123})
        await page.set_content(html, wait_until="networkidle", timeout=10000)
        screenshot = await page.screenshot(full_page=False)
        await browser.close()
    return screenshot


def _pixel_similarity(img1_bytes: bytes, img2_bytes: bytes) -> float:
    """
    두 이미지의 픽셀 유사도를 0~100 사이의 점수로 반환한다.
    PIL ImageChops.difference로 차이를 계산하고 평균 오차를 유사도로 변환한다.
    크기가 다르면 img2를 img1 크기로 리사이즈해 비교한다.

    Args:
        img1_bytes: 기준 이미지 (원본 B)
        img2_bytes: 비교 이미지 (렌더링 결과)

    Returns:
        픽셀 유사도 (0.00 ~ 100.00)
    """
    img1 = Image.open(io.BytesIO(img1_bytes)).convert("RGB")
    img2 = Image.open(io.BytesIO(img2_bytes)).convert("RGB")

    if img1.size != img2.size:
        img2 = img2.resize(img1.size, Image.Resampling.LANCZOS)

    diff = ImageChops.difference(img1, img2)
    stat = ImageStat.Stat(diff)
    mean_diff = sum(stat.mean) / 3
    similarity = (1 - mean_diff / 255) * 100

    return round(similarity, 2)


def _select_renderer(complexity_score: int) -> tuple[str, str | None]:
    """
    complexity_score를 기반으로 렌더러와 경고 메시지를 반환한다.
    스코어 0~40: WeasyPrint / 40~70: WeasyPrint + 경고 / 70+: Playwright

    Args:
        complexity_score: Stage 3에서 산출된 레이아웃 복잡도 점수

    Returns:
        (renderer 이름, 경고 메시지 or None)
    """
    if complexity_score < 40:
        return "weasyprint", None
    elif complexity_score < 70:
        return "weasyprint", "레이아웃이 다소 복잡합니다. 일부 요소가 정확히 재현되지 않을 수 있습니다."
    else:
        return "playwright", None


async def _refine_schema(
    schema: dict,
    b_image_bytes: bytes,
    rendered_bytes: bytes,
    mime_type: str,
) -> dict:
    """
    자기검증 루프에서 픽셀 Diff가 임계값 미만일 때 Gemini에게 스키마 보정을 요청한다.
    원본 B와 렌더링 결과를 동시에 전달해 차이 원인을 AI가 직접 판단하게 한다.
    Rate Limit 초과 시 현재 스키마를 그대로 반환해 파이프라인이 중단되지 않게 한다.

    Args:
        schema: 현재 JSON 스키마
        b_image_bytes: 원본 B 이미지
        rendered_bytes: 현재 HTML 렌더링 결과 스크린샷
        mime_type: 원본 B 이미지 MIME 타입

    Returns:
        보정된 JSON 스키마 딕셔너리
    """
    refine_prompt = f"""당신은 HTML/CSS 레이아웃 정밀 보정 전문 AI입니다.

[상황]
아래 JSON 스키마로 HTML을 렌더링했으나 원본 문서와 시각적 차이가 있습니다.
첫 번째 이미지(원본)와 두 번째 이미지(렌더링 결과)를 비교하여 JSON을 수정해 주세요.

[보정 규칙]
1. 원본의 레이아웃을 기준으로 position, style 수치값만 조정하세요.
2. 필드 id, label, required 구조는 절대 변경하지 마세요.
3. 마크다운 기호 없이 {{ 로 시작해서 }} 로 끝나는 JSON만 반환하세요.

[현재 JSON 스키마]
{json.dumps(schema, ensure_ascii=False, indent=2)}
"""

    estimated_tokens = len(refine_prompt) // 4 + 2000
    allowed = await acquire(MODEL, estimated_tokens)
    if not allowed:
        return schema

    try:
        b_part = {"mime_type": mime_type, "data": b_image_bytes}
        rendered_part = {"mime_type": "image/png", "data": rendered_bytes}
        response = await model_pro.generate_content_async([refine_prompt, b_part, rendered_part])
        result = _parse_schema(response.text)
        return result["data"] if result["success"] else schema
    except Exception:
        return schema


async def run(job_id: str, b_file_path: str, b_filename: str) -> dict:
    """
    Stage 3 전체 실행 함수.
    B 파일 타입에 따라 최적 분석 경로를 선택하고 자기검증 루프로 정확도를 높인다.

    처리 경로:
    B가 PDF → pdfplumber 구조 추출 + 1페이지 이미지 Vision 보완
    B가 이미지 → Gemini Vision 단독 분석

    자기검증 루프 (최대 2회):
    HTML 렌더링 → 원본 B와 픽셀 유사도 측정
    → 95% 미만이면 Gemini에 재분석 요청 → JSON 보정

    Args:
        job_id: 변환 작업 ID
        b_file_path: GCS 내 B 파일 경로
        b_filename: B 파일 원본명

    Returns:
        {
            success: bool,
            error: str | None,
            schema: dict,
            html_template: str,
            preferred_renderer: str,
            renderer_warning: str | None,
            complexity_score: int,
            verification_score: float,
            verification_attempts: int,
            prompt_version: str,
            model_used: str,
        }
    """
    extension = Path(b_filename).suffix.lower()
    b_bytes = _download_b_file(b_file_path)

    FAILURE_BASE = {
        "success": False,
        "schema": {},
        "html_template": "",
        "preferred_renderer": "playwright",
        "renderer_warning": None,
        "complexity_score": 0,
        "verification_score": 0.0,
        "verification_attempts": 0,
        "prompt_version": PROMPT_VERSION,
        "model_used": MODEL,
    }

    # B 파일 타입별 처리 경로
    if extension == ".pdf":
        b_image_bytes = _pdf_first_page_to_image(b_bytes)
        pdf_context = _extract_pdf_structure(b_bytes)
        prompt = _load_prompt(
            f"[PDF 구조 정보 - 폰트명 및 텍스트 구조]\n{pdf_context}"
            f"\n\n위 첨부 이미지는 해당 PDF의 첫 페이지입니다. 이미지와 구조 정보를 함께 분석하세요."
        )
        mime_type = "image/png"
    else:
        b_image_bytes = b_bytes
        mime_type = _get_mime_type(b_bytes)
        prompt = _load_prompt("위 첨부된 이미지를 분석하세요.")

    # 1차 Gemini Vision 분석
    estimated_tokens = len(prompt) // 4 + 2000
    try:
        raw = await _call_gemini_vision(b_image_bytes, mime_type, prompt, estimated_tokens)
    except RuntimeError as e:
        return {**FAILURE_BASE, "error": str(e)}

    parsed = _parse_schema(raw)
    if not parsed["success"]:
        return {**FAILURE_BASE, "error": "Gemini 응답 파싱 실패"}

    schema = parsed["data"]
    complexity_score = schema.get("complexity_score", 0)
    preferred_renderer, renderer_warning = _select_renderer(complexity_score)

    # 자기검증 루프 (최대 2회)
    verification_score = 0.0
    verification_attempts = 0
    html_template = ""

    for attempt in range(MAX_VERIFICATION_ATTEMPTS):
        verification_attempts = attempt + 1
        html_template = _build_html_template(schema)

        try:
            rendered_bytes = await _render_html(html_template)
            verification_score = _pixel_similarity(b_image_bytes, rendered_bytes)
        except Exception:
            break

        if verification_score >= VERIFICATION_THRESHOLD:
            break

        if attempt < MAX_VERIFICATION_ATTEMPTS - 1:
            schema = await _refine_schema(schema, b_image_bytes, rendered_bytes, mime_type)

    return {
        "success": True,
        "error": None,
        "schema": schema,
        "html_template": html_template,
        "preferred_renderer": preferred_renderer,
        "renderer_warning": renderer_warning,
        "complexity_score": complexity_score,
        "verification_score": verification_score,
        "verification_attempts": verification_attempts,
        "prompt_version": PROMPT_VERSION,
        "model_used": MODEL,
    }