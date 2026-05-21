import json
from pathlib import Path

from app.ai.gemini_client import model_pro
from app.ai.rate_limiter import acquire

PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "stage2_extraction" / "v1.0.0.txt"
PROMPT_VERSION = "v1.0.0"
MODEL = "gemini-1.5-pro"

def _load_prompt(document_text: str) -> str:
    """
    프롬프트 파일을 로드하고 문서 텍스트를 삽입해 최종 프롬프트를 반환한다.
    프롬프트를 코드와 분리하여 버전 관리하기 위해 파일로 관리한다.

    Args:
        document_text: 분석할 문서의 텍스트

    Returns:
        {{DOCUMENT_TEXT}} 가 치환된 최종 프롬프트
    """
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return template.replace("{{DOCUMENT_TEXT}}", document_text)

async def _call_gemini(prompt: str, estimated_tokens: int) -> str:
    """
    Rate Limiter 를 통과한 후 Gemini Pro에 프롬프트를 전송하고 응답을 반환한다.
    Rate Limit 초과 시 RuntimeError를 발생시켜 Celery 재시도를 유도한다.

    Args:
        prompt: Gemini에 전송할 최종 프롬프트
        estimated_tokens: 예상 토큰 수
    
    Returns:
        Gemini의 텍스트 응답
    """
    allowed = await acquire(MODEL, estimated_tokens)
    if not allowed:
        raise RuntimeError("Rate limit 초과. Celery 재시도 대기 중.")
    
    response = await model_pro.generate_content_async(prompt)
    return response.text

def _parse_response(raw: str) -> dict:
    """
    Gemini 응답 JSON 을 파싱하고 신뢰도 평균을 계산한다.
    파싱 실패 시 빈 구조와 0 신뢰도를 반환하여 파이프라인이 중단되지 않도록 한다.
    혹시 모를 gemini의 비정상적인 응답(마크다운 백틱)을 강제로 제거한다.

    Args:
        raw: Gemini가 반환한 JSON 문자열
    
    Returns:
        {
            doc_type: str,
            sections: list,
            summary: str,
            avg_confidence: float,
            parse_success: bool,
        }
    """
    try:
        # 마크다운 백틱(```json, ```) 을 강제로 제거
        cleaned_raw = raw.strip()
        if cleaned_raw.startswith("```json"):
            cleaned_raw = cleaned_raw[7:]
        if cleaned_raw.endswith("```"):
            cleaned_raw = cleaned_raw[:-3]
        cleaned_raw = cleaned_raw.strip()

        data = json.loads(cleaned_raw)
        sections = data.get("sections", [])
        confidences = [s.get("confidence", 0) for s in sections]
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0

        return {
            "doc_type": data.get("doc_type", "other"),
            "sections": sections,
            "summary": data.get("summary", ""),
            "avg_confidence": round(avg_confidence, 2),
            "parse_success": True,
        }
    except (json.JSONDecodeError, KeyError):
        return {
            "doc_type": "other",
            "sections": [],
            "summary": "",
            "avg_confidence": 0,
            "parse_success": False,
        }
    
async def run(job_id: str, masked_extracted: dict) -> dict:
    """
    Stage 2 전체 실행 함수.
    PII 마스킹된 텍스트를 Gemini Pro 로 분석하여 문서 구조를 추출한다.
    텍스트 레이어가 없는 이미지 PDF는 Vision 처리로 분기된다.

    Args:
        job_id: 변환 작업 ID
        masked_extracted: Stage 1.5 에서 반환된 마스킹된 상태
    
    Returns:
        {
            success: bool,
            error: str | None,
            doc_type: str,
            sections: list,
            summary: str,
            avg_confidence: float,
            prompt_version: str,
            model_used: str,
        }
    """
    text = masked_extracted.get("text", "")
    has_text_layer = masked_extracted.get("has_text_layer", True)

    if not has_text_layer or not text.strip():
        # 이미지 PDF: Vision 처리는 Stage 3와 통합 예정
        return {
            "success": False,
            "error": "텍스트 레이어가 없습니다. Vision 처리가 필요합니다.",
            "doc_type": "other",
            "sections": [],
            "summary": "",
            "avg_confidence": 0,
            "prompt_version": PROMPT_VERSION,
            "model_used": MODEL,
        }

    estimated_tokens = len(text) // 4 + 1000
    prompt = _load_prompt(text)

    try:
        raw = await _call_gemini(prompt, estimated_tokens)
        parsed = _parse_response(raw)
    except RuntimeError as e:
        return {
            "success": False,
            "error": str(e),
            "doc_type": "other",
            "sections": [],
            "summary": "",
            "avg_confidence": 0,
            "prompt_version": PROMPT_VERSION,
            "model_used": MODEL,
        }

    return {
        "success": parsed["parse_success"],
        "error": None if parsed["parse_success"] else "Gemini 응답 파싱 실패",
        "doc_type": parsed["doc_type"],
        "sections": parsed["sections"],
        "summary": parsed["summary"],
        "avg_confidence": parsed["avg_confidence"],
        "prompt_version": PROMPT_VERSION,
        "model_used": MODEL,
    }
