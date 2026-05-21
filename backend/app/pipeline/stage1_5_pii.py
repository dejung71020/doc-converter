import re
import json
import asyncio

from app.redis_client import get_redis

# 대괄호는 숫자 선택, 중괄호는 숫자자릿수
PII_PATTERNS = {
    "SSN":         r"\d{6}-[1-4]\d{6}",
    "PHONE":       r"01[0-9]-\d{3,4}-\d{4}",
    "EMAIL":       r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    "ACCOUNT":     r"\d{3,6}-\d{2,6}-\d{4,6}",
    "PASSPORT":    r"[A-Z]{1,2}\d{7,8}",
    "CREDIT_CARD": r"\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}",
}

async def mask(job_id: str, text: str) -> dict:
    """
    텍스트에서 PII를 감지하고 토큰으로 치환한다.
    치환된 토큰과 원본 값의 매핑을 Redis에 저장하여 Stage7에서 복원할수 있게 한다.
    Redis TTL은 1시간이며, 재시도 발생을 고려하여 삭제하지 않는다.
    
    Args:
        job_id: 변환 작업 ID (Redis 키 구분용)
        text: 마스킹할 원본 텍스트

    Returns:
        {
            masked_text: str,
            token_map: dict,   # { "PII_SSN_001": "원본값" }
            pii_counts: dict,  # { "SSN": 2, "PHONE": 1 } 감사 로그용
        }
    """
    masked_text = text
    token_map = {}
    pii_counts = {}
    counters = {pii_type: 0 for pii_type in PII_PATTERNS}

    for pii_type, pattern in PII_PATTERNS.items():
        matches = re.findall(pattern, masked_text)
        if not matches:
            continue

        pii_counts[pii_type] = len(matches)

        for match in matches:
            counters[pii_type] += 1
            token = f"[PII_{pii_type}_{counters[pii_type]:03d}]"
            token_map[token] = match
            masked_text = masked_text.replace(match, token, 1)

    if token_map:
        redis = await get_redis()
        await redis.setex(
            f"pii:{job_id}",
            3600,
            json.dumps(token_map),
        )
    
    return {
        "masked_text": masked_text,
        "token_map": token_map,
        "pii_counts": pii_counts,
    }

async def restore(job_id: str, text: str) -> str:
    """
    마스킹된 텍스트에서 PII 토큰을 원본 값으로 복원한다.
    Redis 키를 삭제하지 않고 TTL(1시간) 자동 소멸에 의존한다.
    Celery 재시도 환경에서 복원이 여러 번 발생할 수 있기 때문이다.

    Args:
        job_id: 변환 작업 ID
        text: 토큰이 포함된 마스킹된 텍스트

    Returns:
        PII가 복원된 원본 텍스트
    """
    redis = await get_redis()
    raw = await redis.get(f"pii:{job_id}")

    if not raw:
        return text

    token_map: dict = json.loads(raw)
    restored_text = text

    for token, original_value in token_map.items():
        restored_text = restored_text.replace(token, original_value)

    return restored_text


def run(job_id: str, extracted: dict) -> dict:
    """
    Stage 1.5 동기 래퍼 함수.
    extracted 딕셔너리 안의 모든 텍스트 필드에 마스킹을 적용한다.
    Celery Worker(동기 환경)에서 호출되므로 asyncio.run()으로 비동기 함수를 실행한다.

    Args:
        job_id: 변환 작업 ID
        extracted: Stage 1에서 추출된 콘텐츠 딕셔너리

    Returns:
        {
            success: bool,
            error: str | None,
            masked_extracted: dict,
            pii_counts: dict,
        }
    """
    if not extracted:
        return {
            "success": True,
            "error": None,
            "masked_extracted": extracted,
            "pii_counts": {},
        }
    
    # 복사본으로 작업
    extracted_copy = dict(extracted)

    # JSON 직렬화 불가 객체(bytes) 임시 분리
    enhanced_bytes = extracted_copy.pop("enhanced_bytes", None)
    
    # 딕셔너리 전체를 JSON 문자열로 만들어 한 방에 마스킹
    raw_json_str = json.dumps(extracted_copy, ensure_ascii=False)
    result = asyncio.run(mask(job_id, raw_json_str))

    # 마스킹된 JSON 문자열을 다시 딕셔너리로 복구
    masked_extracted = json.loads(result["masked_text"])

    # 분리해두었던 바이너리 데이터(bytes) 다시 결합
    if enhanced_bytes is not None:
        masked_extracted["enhanced_bytes"] = enhanced_bytes

    return {
        "success": True,
        "error": None,
        "masked_extracted": masked_extracted,
        "pii_counts": result["pii_counts"],
    }