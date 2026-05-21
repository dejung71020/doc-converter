import json
import asyncio
from pathlib import Path

import google.generativeai as genai

from app.core.config import settings
from app.ai.rate_limiter import acquire

genai.configure(api_key=settings.GEMINI_API_KEY)

PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "stage4_mapping" / "v1.0.0.txt"
PROMPT_VERSION = "v1.0.0"
MODEL_PRO = "gemini-1.5-pro"
MODEL_FLASH = "gemini-1.5-flash"

AUTO_THRESHOLD = 90.0
CHECKPOINT_THRESHOLD = 50.0


def _format_a_sections(sections: list) -> str:
    """
    Stage 2 섹션 목록을 프롬프트에 삽입할 텍스트로 변환한다.
    각 섹션의 ID, 이름, 신뢰도, 내용을 Gemini가 이해하기 쉬운 형식으로 정리한다.

    Args:
        sections: Stage 2 출력의 sections 리스트

    Returns:
        프롬프트에 삽입할 포맷된 섹션 텍스트
    """
    lines = []
    for i, section in enumerate(sections):
        name = section.get("name", f"Section_{i}")
        confidence = section.get("confidence", 0)
        content = section.get("content", "")[:300]
        lines.append(
            f"[Section_{i}] {name} (신뢰도: {confidence})\n{content}"
        )
    return "\n\n".join(lines)


def _format_b_fields(fields: list) -> str:
    """
    Stage 3 필드 목록을 프롬프트에 삽입할 텍스트로 변환한다.
    각 필드의 ID, 라벨, 위치, 폰트 스타일, 필수 여부를 포함해
    Gemini가 필드의 목적과 크기를 판단할 수 있게 한다.

    Args:
        fields: Stage 3 schema의 fields 리스트

    Returns:
        프롬프트에 삽입할 포맷된 필드 텍스트
    """
    lines = []
    for field in fields:
        fid = field.get("id", "unknown")
        label = field.get("label", "")
        required = "필수" if field.get("required", False) else "선택"
        pos = field.get("position", {})
        width = pos.get("width", 0)
        height = pos.get("height", 0)
        lines.append(
            f"[{fid}] {label} ({required})\n"
            f"  크기: {width}×{height}px"
        )
    return "\n\n".join(lines)


def _load_prompt(
    a_doc_type: str,
    a_sections_str: str,
    b_doc_type: str,
    b_fields_str: str,
) -> str:
    """
    프롬프트 파일을 로드하고 A/B 문서 정보를 삽입해 최종 프롬프트를 반환한다.

    Args:
        a_doc_type: A 문서 타입 (예: resume, report)
        a_sections_str: 포맷된 A 섹션 텍스트
        b_doc_type: B 문서 타입 (예: cover_letter_form)
        b_fields_str: 포맷된 B 필드 텍스트

    Returns:
        완성된 프롬프트 문자열
    """
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return (
        template
        .replace("{{A_DOC_TYPE}}", a_doc_type)
        .replace("{{A_SECTIONS}}", a_sections_str)
        .replace("{{B_DOC_TYPE}}", b_doc_type)
        .replace("{{B_FIELDS}}", b_fields_str)
    )


async def _call_gemini(prompt: str, model: str, estimated_tokens: int) -> str:
    """
    Rate Limiter 통과 후 지정된 Gemini 모델로 프롬프트를 전송한다.
    Flash와 Pro를 각각 호출할 때 모두 이 함수를 사용한다.
    Rate Limit 초과 시 RuntimeError로 Celery 재시도를 유도한다.

    Args:
        prompt: 전송할 프롬프트
        model: 사용할 모델명 (gemini-1.5-pro | gemini-1.5-flash)
        estimated_tokens: 예상 토큰 수

    Returns:
        Gemini 텍스트 응답
    """
    allowed = await acquire(model, estimated_tokens)
    if not allowed:
        raise RuntimeError(f"Rate limit 초과 ({model}). Celery 재시도 대기 중.")

    gemini_model = genai.GenerativeModel(model)
    response = await gemini_model.generate_content_async(prompt)
    return response.text


def _parse_mapping(raw: str) -> dict:
    """
    Gemini 매핑 응답 JSON을 파싱한다.
    마크다운 백틱을 방어적으로 제거한 후 파싱한다.
    실패 시 빈 매핑 구조를 반환해 파이프라인이 중단되지 않도록 한다.

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
        return {"success": True, "data": json.loads(cleaned.strip())}
    except (json.JSONDecodeError, KeyError):
        return {"success": False, "data": {}}


def _calculate_match_rate(pro_mappings: list, flash_mappings: list) -> float:
    """
    Flash와 Pro의 매핑 결과를 비교해 일치율을 계산한다.
    b_field_id 기준으로 매핑된 a_source 세트와 transform이 동일하면 일치로 판정한다.
    일치율이 높을수록 두 모델이 같은 판단을 내린 것이므로 신뢰도가 높다.

    Args:
        pro_mappings: Pro 모델이 생성한 mappings 리스트
        flash_mappings: Flash 모델이 생성한 mappings 리스트

    Returns:
        일치율 0.0 ~ 100.0
    """
    if not pro_mappings:
        return 0.0

    flash_map = {
        m.get("b_field_id"): m
        for m in flash_mappings
        if m.get("b_field_id")
    }

    matches = 0
    for pro_m in pro_mappings:
        fid = pro_m.get("b_field_id")
        flash_m = flash_map.get(fid)

        if not flash_m:
            continue

        pro_source = set(pro_m.get("a_source", []))
        flash_source = set(flash_m.get("a_source", []))
        same_source = pro_source == flash_source
        same_transform = pro_m.get("transform") == flash_m.get("transform")

        if same_source and same_transform:
            matches += 1

    return round(matches / len(pro_mappings) * 100, 2)


async def run(job_id: str, stage2_result: dict, stage3_result: dict) -> dict:
    """
    Stage 4 전체 실행 함수.
    Flash + Pro 앙상블로 A→B 의미론적 매핑 계획을 수립한다.

    Flash와 Pro를 asyncio.gather로 병렬 호출해 속도를 최적화한다.
    일치율을 기반으로 자동 진행 또는 Human Checkpoint를 결정한다.

    일치율 기준:
    90% 이상 → Pro 결과 자동 채택, 진행
    50~90%   → Pro 결과 채택, 불일치 필드 사용자 알림
    50% 미만 → Human Checkpoint #1 강제 발동

    Args:
        job_id: 변환 작업 ID (로깅용)
        stage2_result: Stage 2 출력 (A 콘텐츠)
        stage3_result: Stage 3 출력 (B 템플릿 스키마)

    Returns:
        {
            success: bool,
            error: str | None,
            transformation_intent: str,
            mappings: list,
            unmapped_b_fields: list,
            unused_a_sections: list,
            ensemble_match_rate: float,
            requires_checkpoint: bool,
            mismatch_fields: list,
            prompt_version: str,
            model_used: str,
        }
    """
    FAILURE_BASE = {
        "success": False,
        "transformation_intent": "",
        "mappings": [],
        "unmapped_b_fields": [],
        "unused_a_sections": [],
        "ensemble_match_rate": 0.0,
        "requires_checkpoint": True,
        "mismatch_fields": [],
        "prompt_version": PROMPT_VERSION,
        "model_used": MODEL_PRO,
    }

    a_doc_type = stage2_result.get("doc_type", "other")
    a_sections = stage2_result.get("sections", [])
    b_schema = stage3_result.get("schema", {})
    b_fields = b_schema.get("fields", [])
    b_doc_type = b_schema.get("doc_type", "other")

    a_sections_str = _format_a_sections(a_sections)
    b_fields_str = _format_b_fields(b_fields)
    prompt = _load_prompt(a_doc_type, a_sections_str, b_doc_type, b_fields_str)

    estimated_tokens = len(prompt) // 4 + 1000

    # Flash + Pro 병렬 호출
    try:
        flash_raw, pro_raw = await asyncio.gather(
            _call_gemini(prompt, MODEL_FLASH, estimated_tokens),
            _call_gemini(prompt, MODEL_PRO, estimated_tokens),
        )
    except RuntimeError as e:
        return {**FAILURE_BASE, "error": str(e)}

    flash_parsed = _parse_mapping(flash_raw)
    pro_parsed = _parse_mapping(pro_raw)

    if not pro_parsed["success"]:
        return {**FAILURE_BASE, "error": "Gemini Pro 응답 파싱 실패"}

    pro_data = pro_parsed["data"]
    flash_data = flash_parsed["data"] if flash_parsed["success"] else {}

    pro_mappings = pro_data.get("mappings", [])
    flash_mappings = flash_data.get("mappings", [])

    # 앙상블 일치율 계산
    match_rate = _calculate_match_rate(pro_mappings, flash_mappings)

    # 불일치 필드 추출 (사용자 알림용)
    flash_map = {m.get("b_field_id"): m for m in flash_mappings}
    mismatch_fields = [
        m.get("b_field_id")
        for m in pro_mappings
        if m.get("b_field_id") not in flash_map
        or set(m.get("a_source", [])) != set(flash_map[m["b_field_id"]].get("a_source", []))
    ]

    requires_checkpoint = match_rate < CHECKPOINT_THRESHOLD

    # AUTO_THRESHOLD 미만일 때만 불일치 필드를 사용자에게 알림
    # 90% 이상이면 두 모델이 충분히 일치 → 알림 불필요
    notify_mismatches = match_rate < AUTO_THRESHOLD
    mismatch_fields = mismatch_fields if notify_mismatches else []

    return {
        "success": True,
        "error": None,
        "transformation_intent": pro_data.get("transformation_intent", ""),
        "mappings": pro_mappings,
        "unmapped_b_fields": pro_data.get("unmapped_b_fields", []),
        "unused_a_sections": pro_data.get("unused_a_sections", []),
        "ensemble_match_rate": match_rate,
        "requires_checkpoint": requires_checkpoint,
        "mismatch_fields": mismatch_fields,
        "prompt_version": PROMPT_VERSION,
        "model_used": MODEL_PRO,
    }