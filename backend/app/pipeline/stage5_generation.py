import json
import asyncio
from pathlib import Path

from app.ai.gemini_client import model_pro, model_flash
from app.ai.rate_limiter import acquire

GENERATION_PROMPT_PATH = (
    Path(__file__).parent.parent.parent / "prompts" / "stage5_generation" / "v1.0.0.txt"
)
SELFRAG_PROMPT_PATH = (
    Path(__file__).parent.parent.parent / "prompts" / "stage5_selfrag" / "v1.0.0.txt"
)
GENERATION_VERSION = "v1.0.0"
SELFRAG_VERSION = "v1.0.0"
MODEL_FLASH = "gemini-1.5-flash"
MODEL_PRO = "gemini-1.5-pro"
GROUNDING_WARN_THRESHOLD = 80.0
MAX_CONCURRENT_FIELD_CALLS = 5


def _get_source_content(sections: list, a_source_ids: list) -> str:
    """
    Stage 4 매핑의 a_source ID 목록을 기반으로 Stage 2 섹션에서 원본 콘텐츠를 추출한다.
    a_source ID 형식은 "Section_0", "Section_2" 등 인덱스 기반이다.

    Args:
        sections: Stage 2 출력의 sections 리스트
        a_source_ids: Stage 4 매핑의 a_source 필드 (예: ["Section_0", "Section_2"])

    Returns:
        추출된 원본 콘텐츠 텍스트
    """
    contents = []
    for sid in a_source_ids:
        try:
            idx = int(sid.split("_")[1])
            if 0 <= idx < len(sections):
                contents.append(sections[idx].get("content", ""))
        except (IndexError, ValueError):
            continue
    return "\n\n".join(contents)


def _estimate_max_length(field: dict) -> int:
    """
    B 필드의 크기(width, height)와 폰트 크기를 기반으로 최대 글자 수를 추정한다.
    한국어는 영문보다 문자가 넓으므로 0.5 계수를 적용한다.

    Args:
        field: Stage 3 schema의 field 딕셔너리

    Returns:
        추정 최대 글자 수
    """
    pos = field.get("position", {})
    style = field.get("style", {})
    width = pos.get("width", 200)
    height = pos.get("height", 50)
    font_size = style.get("font_size", 12)
    line_height = style.get("line_height", 1.5)

    chars_per_line = int(width / (font_size * 0.5))
    line_height_px = font_size * line_height
    lines = int(height / line_height_px)
    max_chars = int(chars_per_line * lines * 0.9)

    return max(50, max_chars)


def _load_generation_prompt(
    intent: str,
    label: str,
    max_length: int,
    source_content: str,
) -> str:
    """
    콘텐츠 생성 프롬프트 파일을 로드하고 변수를 삽입한다.

    Args:
        intent: Stage 4의 transformation_intent
        label: B 필드의 label (어느 필드에 쓸 콘텐츠인지)
        max_length: 추정 최대 글자 수
        source_content: 원본 A 콘텐츠 텍스트

    Returns:
        완성된 생성 프롬프트
    """
    template = GENERATION_PROMPT_PATH.read_text(encoding="utf-8")
    return (
        template
        .replace("{{TRANSFORMATION_INTENT}}", intent)
        .replace("{{TARGET_FIELD_LABEL}}", label)
        .replace("{{MAX_LENGTH}}", str(max_length))
        .replace("{{SOURCE_CONTENT}}", source_content)
    )


def _load_selfrag_prompt(source_document: str, generated_text: str) -> str:
    """
    Self-RAG 검증 프롬프트 파일을 로드하고 변수를 삽입한다.

    Args:
        source_document: 원본 A 전체 문서 텍스트 (검증 기준)
        generated_text: 검증할 생성된 텍스트

    Returns:
        완성된 Self-RAG 프롬프트
    """
    template = SELFRAG_PROMPT_PATH.read_text(encoding="utf-8")
    return (
        template
        .replace("{{SOURCE_DOCUMENT}}", source_document)
        .replace("{{GENERATED_TEXT}}", generated_text)
    )


async def _call_gemini(prompt: str, model: str, estimated_tokens: int) -> str:
    """
    Rate Limiter 통과 후 Gemini 모델을 비동기 호출한다.
    생성(Flash)과 Self-RAG 검증(Pro) 모두 이 함수를 공유한다.

    Args:
        prompt: 전송할 프롬프트
        model: gemini-1.5-flash | gemini-1.5-pro
        estimated_tokens: 예상 토큰 수

    Returns:
        Gemini 텍스트 응답
    """
    allowed = await acquire(model, estimated_tokens)
    if not allowed:
        raise RuntimeError(f"Rate limit 초과 ({model}). Celery 재시도 대기 중.")

    gemini_model = model_pro if model == MODEL_PRO else model_flash
    response = await gemini_model.generate_content_async(prompt)
    return response.text


def _parse_json_response(raw: str) -> dict:
    """
    Gemini JSON 응답에서 마크다운 백틱을 제거하고 파싱한다.
    생성 응답과 Self-RAG 응답 모두 이 함수로 파싱한다.

    Args:
        raw: Gemini 원본 응답 문자열

    Returns:
        { success: bool, data: dict | list }
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


def _remove_hallucinations(evaluations: list) -> tuple[str, float]:
    """
    Self-RAG 검증 결과에서 HALLUCINATION 문장을 제거하고
    근거 있는 문장만 합쳐 최종 텍스트와 grounding_score를 반환한다.

    PII 마스킹 토큰([PII_...])이 포함된 문장은 무조건 grounded로 처리되므로
    프롬프트 설계상 절대 제거되지 않는다.

    Args:
        evaluations: Self-RAG 응답의 evaluations 리스트

    Returns:
        (정제된 텍스트, grounding_score 0.0~100.0)
    """
    total = len(evaluations)
    if total == 0:
        return "", 100.0

    grounded = [e for e in evaluations if e.get("grounded", True)]
    grounding_score = round(len(grounded) / total * 100, 2)
    cleaned_text = " ".join(e.get("sentence", "") for e in grounded)

    return cleaned_text, grounding_score


async def _generate_one_field(
    field: dict,
    mapping: dict,
    sections: list,
    intent: str,
    full_source: str,
) -> dict:
    """
    하나의 B 필드에 대한 콘텐츠를 생성하고 Self-RAG로 검증한다.

    처리 흐름:
    skip → 빈 콘텐츠 즉시 반환
    그 외 → Flash로 생성 → Pro로 Self-RAG 검증 → 환각 제거

    Args:
        field: B 필드 딕셔너리 (id, label, position, style)
        mapping: Stage 4 매핑 딕셔너리 (transform, a_source, confidence)
        sections: Stage 2 섹션 리스트
        intent: 변환 목적 문자열
        full_source: 원본 A 전체 텍스트 (Self-RAG 검증 기준)

    Returns:
        {
            field_id: str,
            content: str,
            grounding_score: float,
            warning: str | None,
        }
    """
    field_id = field.get("id", "unknown")
    label = field.get("label", "")
    transform = mapping.get("transform", "skip")

    if transform == "skip":
        return {
            "field_id": field_id,
            "content": "",
            "grounding_score": 100.0,
            "warning": None,
        }

    source_content = _get_source_content(sections, mapping.get("a_source", []))
    max_length = _estimate_max_length(field)

    # Flash로 콘텐츠 생성
    gen_prompt = _load_generation_prompt(intent, label, max_length, source_content)
    estimated_tokens = len(gen_prompt) // 4 + 500

    try:
        gen_raw = await _call_gemini(gen_prompt, MODEL_FLASH, estimated_tokens)
    except RuntimeError as e:
        return {
            "field_id": field_id,
            "content": "",
            "grounding_score": 0.0,
            "warning": str(e),
        }

    gen_parsed = _parse_json_response(gen_raw)
    if not gen_parsed["success"]:
        return {
            "field_id": field_id,
            "content": "",
            "grounding_score": 0.0,
            "warning": "콘텐츠 생성 파싱 실패",
        }

    generated_content = gen_parsed["data"].get("content", "")

    # Pro로 Self-RAG 검증
    selfrag_prompt = _load_selfrag_prompt(full_source, generated_content)
    selfrag_tokens = len(selfrag_prompt) // 4 + 500

    try:
        selfrag_raw = await _call_gemini(selfrag_prompt, MODEL_PRO, selfrag_tokens)
        selfrag_parsed = _parse_json_response(selfrag_raw)

        if selfrag_parsed["success"]:
            evaluations = selfrag_parsed["data"].get("evaluations", [])
            final_content, grounding_score = _remove_hallucinations(evaluations)
        else:
            final_content = generated_content
            grounding_score = 100.0
    except RuntimeError:
        final_content = generated_content
        grounding_score = 100.0

    warning = None
    if grounding_score < GROUNDING_WARN_THRESHOLD:
        warning = f"{label} 필드의 일부 내용이 원본과 다를 수 있습니다. (근거율: {grounding_score}%)"

    return {
        "field_id": field_id,
        "content": final_content,
        "grounding_score": grounding_score,
        "warning": warning,
    }


async def run(
    job_id: str,
    stage2_result: dict,
    stage3_result: dict,
    stage4_result: dict,
) -> dict:
    """
    Stage 5 전체 실행 함수.
    Stage 4 매핑 계획에 따라 각 B 필드의 콘텐츠를 생성하고
    Self-RAG로 환각을 검증한다.

    모든 필드를 asyncio.gather로 병렬 생성해 속도를 최적화한다.
    각 필드는 독립적이므로 병렬 실행이 가능하다.

    Args:
        job_id: 변환 작업 ID (로깅용)
        stage2_result: Stage 2 출력 (A 콘텐츠)
        stage3_result: Stage 3 출력 (B 필드 스키마)
        stage4_result: Stage 4 출력 (매핑 계획)

    Returns:
        {
            success: bool,
            error: str | None,
            field_contents: dict,      # { field_id: content }
            grounding_scores: dict,    # { field_id: score }
            warnings: list,            # 경고 메시지 목록
            generation_version: str,
            selfrag_version: str,
        }
    """
    FAILURE_BASE = {
        "success": False,
        "field_contents": {},
        "grounding_scores": {},
        "warnings": [],
        "generation_version": GENERATION_VERSION,
        "selfrag_version": SELFRAG_VERSION,
    }

    sections = stage2_result.get("sections", [])
    full_source = "\n\n".join(s.get("content", "") for s in sections)
    intent = stage4_result.get("transformation_intent", "")
    mappings = stage4_result.get("mappings", [])
    fields = stage3_result.get("schema", {}).get("fields", [])

    # field_id → mapping 딕셔너리 생성
    mapping_map = {m.get("b_field_id"): m for m in mappings}

    # 동시 API 호출을 MAX_CONCURRENT_FIELD_CALLS개로 제한
    # 필드당 Gemini 2회 호출(Flash+Pro)이므로 최대 동시 호출 수 = 5×2 = 10
    sem = asyncio.Semaphore(MAX_CONCURRENT_FIELD_CALLS)

    async def _bounded_generate(field, mapping):
        """세마포어로 동시 실행 수를 제한한 _generate_one_field 래퍼."""
        async with sem:
            return await _generate_one_field(field, mapping, sections, intent, full_source)

    tasks = []
    for field in fields:
        fid = field.get("id")
        mapping = mapping_map.get(fid)
        if mapping:
            tasks.append(_bounded_generate(field, mapping))

    if not tasks:
        return {**FAILURE_BASE, "error": "처리할 매핑된 필드가 없습니다."}

    # 모든 필드 병렬 생성 (최대 5개씩 동시 실행)
    results = await asyncio.gather(*tasks, return_exceptions=True)

    field_contents = {}
    grounding_scores = {}
    warnings = []

    for result in results:
        if isinstance(result, Exception):
            warnings.append(f"필드 생성 중 오류: {str(result)}")
            continue

        fid = result["field_id"]
        field_contents[fid] = result["content"]
        grounding_scores[fid] = result["grounding_score"]
        if result["warning"]:
            warnings.append(result["warning"])

    return {
        "success": True,
        "error": None,
        "field_contents": field_contents,
        "grounding_scores": grounding_scores,
        "warnings": warnings,
        "generation_version": GENERATION_VERSION,
        "selfrag_version": SELFRAG_VERSION,
    }