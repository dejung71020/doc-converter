import io

import numpy as np
from PIL import Image
from playwright.async_api import async_playwright
from skimage.metrics import structural_similarity

from app.core.config import settings
from app.storage import get_gcs_client, generate_output_path
from app.pipeline.stage1_5_pii import restore as restore_pii

AUTO_THRESHOLD = 98.0
RECOMMENDED_THRESHOLD = 90.0
PLAYWRIGHT_TIMEOUT = 30000


async def _render_screenshot_and_pdf(
    html_for_shot: str,
    html_for_pdf: str,
    page_width: int,
    page_height: int,
) -> tuple[bytes, bytes]:
    """
    단일 Playwright 브라우저 세션에서 스크린샷과 PDF를 모두 생성한다.
    브라우저를 두 번 실행하는 기존 방식 대비 Chromium 실행 비용을 절반으로 줄인다.

    html_for_shot: 픽셀 비교용 스크린샷에 사용할 HTML (PII 마스킹 상태)
    html_for_pdf:  최종 PDF에 사용할 HTML (PII 복원 완료 상태)

    Args:
        html_for_shot: 스크린샷용 HTML (마스킹 상태)
        html_for_pdf: PDF 생성용 HTML (PII 복원 완료)
        page_width: Stage 3 page_size 기반 페이지 너비 (px)
        page_height: Stage 3 page_size 기반 페이지 높이 (px)

    Returns:
        (screenshot_bytes, pdf_bytes)
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        # 스크린샷: 원본 B와 동일한 viewport로 시각적 유사도 비교
        shot_page = await browser.new_page(
            viewport={"width": page_width, "height": page_height}
        )
        await shot_page.set_content(
            html_for_shot, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT
        )
        await shot_page.evaluate("document.fonts.ready")
        screenshot = await shot_page.screenshot(full_page=True)
        await shot_page.close()

        # PDF: PII 복원된 HTML로 최종 출력물 생성
        pdf_page = await browser.new_page()
        await pdf_page.set_content(
            html_for_pdf, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT
        )
        await pdf_page.evaluate("document.fonts.ready")
        pdf_bytes = await pdf_page.pdf(
            width=f"{page_width}px",
            print_background=True,
        )
        await pdf_page.close()

        await browser.close()

    return screenshot, pdf_bytes


def _ssim_similarity(img1_bytes: bytes, img2_bytes: bytes) -> float:
    """
    SSIM(Structural Similarity Index)으로 두 이미지의 시각적 유사도를 측정한다.
    PIL mean diff는 픽셀 밝기 평균 차이만 보지만, SSIM은 밝기/대비/구조를
    분리해서 측정하므로 인간 시각 기준의 유사도에 더 가깝다.

    예: 약간 밝기가 다른 동일 레이아웃 → PIL은 낮게, SSIM은 높게 측정

    Args:
        img1_bytes: 기준 이미지 (원본 B)
        img2_bytes: 비교 이미지 (렌더링 결과)

    Returns:
        SSIM 유사도 0.00 ~ 100.00
    """
    img1 = Image.open(io.BytesIO(img1_bytes)).convert("RGB")
    img2 = Image.open(io.BytesIO(img2_bytes)).convert("RGB")

    if img1.size != img2.size:
        img2 = img2.resize(img1.size, Image.Resampling.LANCZOS)

    arr1 = np.array(img1)
    arr2 = np.array(img2)

    score, _ = structural_similarity(arr1, arr2, channel_axis=2, full=True)
    return round(score * 100, 2)


def _determine_checkpoint(score: float) -> dict:
    """
    SSIM 유사도 점수를 기반으로 Human Checkpoint 필요 여부를 결정한다.

    기준:
      98% 이상 → 자동 완료, Checkpoint 불필요
      90~98%   → 비주얼 에디터 권장 (선택)
      90% 미만 → 비주얼 에디터 필수

    Args:
        score: SSIM 유사도 점수

    Returns:
        { required: bool, recommended: bool }
    """
    if score >= AUTO_THRESHOLD:
        return {"required": False, "recommended": False}
    elif score >= RECOMMENDED_THRESHOLD:
        return {"required": False, "recommended": True}
    else:
        return {"required": True, "recommended": True}


def _upload_pdf(job_id: str, pdf_bytes: bytes) -> str:
    """
    최종 PDF를 GCS에 업로드하고 저장 경로를 반환한다.

    Args:
        job_id: 변환 작업 ID
        pdf_bytes: 업로드할 PDF 바이트

    Returns:
        GCS 내 저장 경로
    """
    client = get_gcs_client()
    output_path = generate_output_path(job_id)
    bucket = client.bucket(settings.GCS_BUCKET_NAME)
    blob = bucket.blob(output_path)
    blob.upload_from_string(pdf_bytes, content_type="application/pdf")
    return output_path


async def run(
    job_id: str,
    html_template: str,
    b_image_bytes: bytes,
    page_size: dict,
) -> dict:
    """
    Stage 7 전체 실행 함수.
    최종 HTML을 원본 B와 SSIM으로 시각적 유사도를 검증하고,
    PII 토큰을 원본 값으로 복원한 후 최종 PDF를 생성한다.

    Playwright를 단일 세션으로 실행해 스크린샷과 PDF를 한 번에 처리하여
    Chromium 실행 비용을 최소화한다.

    SSIM 유사도 기준:
      98% 이상 → 자동 완료
      90~98%   → 비주얼 에디터 권장
      90% 미만 → 비주얼 에디터 필수

    PII 복원 시 Redis 키를 삭제하지 않는 이유:
      Celery 재시도 환경에서 이 Stage가 여러 번 실행될 수 있다.
      TTL(1시간) 자동 소멸에 의존한다.

    Args:
        job_id: 변환 작업 ID
        html_template: Stage 6에서 콘텐츠가 채워진 HTML (PII 마스킹 상태)
        b_image_bytes: Stage 3에서 보존한 원본 B 이미지
        page_size: Stage 3 schema의 page_size { width_px, height_px }

    Returns:
        {
            success: bool,
            error: str | None,
            ssim_score: float,
            checkpoint_required: bool,
            checkpoint_recommended: bool,
            final_pdf_path: str | None,
        }
    """
    FAILURE_BASE = {
        "success": False,
        "ssim_score": 0.0,
        "checkpoint_required": True,
        "checkpoint_recommended": True,
        "final_pdf_path": None,
    }

    page_width = page_size.get("width_px", 794)
    page_height = page_size.get("height_px", 1123)

    # PII 복원 (마스킹 토큰 → 원본 값)
    try:
        restored_html = await restore_pii(job_id, html_template)
    except Exception as e:
        return {**FAILURE_BASE, "error": f"PII 복원 실패: {str(e)}"}

    # 스크린샷(마스킹 상태) + PDF(복원 상태) 단일 Playwright 세션으로 생성
    try:
        screenshot, pdf_bytes = await _render_screenshot_and_pdf(
            html_for_shot=html_template,
            html_for_pdf=restored_html,
            page_width=page_width,
            page_height=page_height,
        )
    except Exception as e:
        return {**FAILURE_BASE, "error": f"렌더링 실패: {str(e)}"}

    # SSIM 시각적 유사도 측정
    ssim_score = _ssim_similarity(b_image_bytes, screenshot)
    checkpoint = _determine_checkpoint(ssim_score)

    # 최종 PDF GCS 업로드
    try:
        final_pdf_path = _upload_pdf(job_id, pdf_bytes)
    except Exception as e:
        return {**FAILURE_BASE, "error": f"PDF 업로드 실패: {str(e)}"}

    return {
        "success": True,
        "error": None,
        "ssim_score": ssim_score,
        "checkpoint_required": checkpoint["required"],
        "checkpoint_recommended": checkpoint["recommended"],
        "final_pdf_path": final_pdf_path,
    }
