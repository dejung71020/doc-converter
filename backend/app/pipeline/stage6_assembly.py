import html as html_lib
import re


def _clean_and_format_text(text: str) -> str:
    """
    AI가 생성한 텍스트를 HTML에 안전하게 삽입할 수 있도록 변환한다.

    처리 순서:
      1. 마크다운 볼드(**text**) 제거 — AI가 종종 마크다운 형식으로 텍스트를 생성함
      2. HTML 이스케이프 — <, >, & 등 특수문자를 안전하게 변환 (XSS 방지)
      3. 줄바꿈(\n) → <br> 변환 — HTML에서 줄바꿈이 무시되는 것을 방지

    Args:
        text: AI가 생성한 원본 텍스트

    Returns:
        HTML에 안전하게 삽입 가능한 형식으로 변환된 텍스트
    """
    if not text:
        return ""

    cleaned = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    escaped = html_lib.escape(cleaned)
    return escaped.replace("\n", "<br>")


def _fill_template(html_template: str, field_contents: dict) -> str:
    """
    HTML 템플릿의 {{field_id}} 플레이스홀더를 실제 콘텐츠로 교체한다.
    정규식으로 단일 패스(single-pass) 교체를 수행해 콘텐츠 안에
    {{field_id}} 형태의 문자열이 있어도 의도치 않게 재교체되는 것을 방지한다.

    Stage 3 HTML 템플릿의 플레이스홀더 형식: {{field_001}}, {{field_002}} 등

    Args:
        html_template: Stage 3 출력 HTML ({{field_id}} 플레이스홀더 포함)
        field_contents: Stage 5 출력 { field_id: content }

    Returns:
        콘텐츠가 채워진 HTML 문자열
    """
    def replace_match(match: re.Match) -> str:
        field_id = match.group(1)
        content = field_contents.get(field_id, "")
        return _clean_and_format_text(content)

    return re.sub(r'\{\{(\w+)\}\}', replace_match, html_template)


def run(
    job_id: str,
    html_template: str,
    field_contents: dict,
) -> dict:
    """
    Stage 6 전체 실행 함수.
    Stage 5 콘텐츠를 Stage 3 HTML 템플릿에 채워 넣는 문자열 조립만 수행한다.

    PDF 렌더링은 Stage 7에서 단 한 번만 수행한다.
    Stage 6에서 렌더링하지 않는 이유:
      Stage 7은 이미 Playwright로 스크린샷(검증용) + PDF(최종 출력)를
      단일 세션으로 처리한다. Stage 6에서 추가로 렌더링하면 브라우저 실행이
      중복되어 시간과 자원이 2배로 낭비된다.

    출력된 filled_html은 PII 마스킹 상태이며 Stage 7에서 복원된다.

    Args:
        job_id: 변환 작업 ID (로깅용)
        html_template: Stage 3 HTML 템플릿 ({{field_id}} 플레이스홀더 포함)
        field_contents: Stage 5 출력 { field_id: content }

    Returns:
        {
            success: bool,
            error: str | None,
            filled_html: str,  Stage 7 입력용 (PII 마스킹 상태)
        }
    """
    if not html_template:
        return {
            "success": False,
            "error": "HTML 템플릿이 비어있습니다.",
            "filled_html": "",
        }

    filled_html = _fill_template(html_template, field_contents)

    return {
        "success": True,
        "error": None,
        "filled_html": filled_html,
    }
