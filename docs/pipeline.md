# 문서 변환 파이프라인 상세 문서

> 각 Stage의 목적, 입출력, 핵심 결정, 에러 처리를 기록한다.
> 코드 변경 시 이 문서도 함께 업데이트해야 한다.

---

## 전체 흐름 요약

```
[사용자]
  POST /api/v1/upload/presigned-url  ← A, B 파일 GCS 업로드
  POST /api/v1/jobs                  ← 파이프라인 시작
  WS   /ws/v1/jobs/{job_id}          ← 실시간 진행 수신
      │
      ▼ (Celery Worker: asyncio.run(_run_pipeline(...)))
┌─────────────────────────────────────────────────┐
│  Stage 1   → 입력 검증 + 전처리 (동기, to_thread) │
│  Stage 1.5 → PII 감지 + 마스킹  (async)          │
│  Stage 2   → A 콘텐츠 추출       ┐ asyncio.gather│
│  Stage 3   → B 템플릿 분석+검증  ┘ 병렬 실행     │
│  Stage 4   → 의미론적 매핑 (Flash+Pro 앙상블)     │
│  [Human Checkpoint #1: 일치율 < 50% 시 발동]     │
│  Stage 5   → 콘텐츠 생성 + Self-RAG (Semaphore)  │
│  Stage 6   → HTML 조립 (동기, 렌더링 없음)        │
│  Stage 7   → SSIM 검증 + PII 복원 + PDF 생성     │
│  [Human Checkpoint #2: SSIM < 98% 시 권장/필수]  │
│  → GCS 업로드 → job_complete 이벤트 발행         │
└─────────────────────────────────────────────────┘
```

---

## Stage 1: 입력 검증 및 전처리

**파일:** `backend/app/pipeline/stage1_validation.py`

### 목적
사용자가 업로드한 파일이 시스템에서 처리 가능한지 검증하고,
파일 형식에 맞는 방식으로 텍스트/이미지를 추출한다.
여기서 실패하면 이후 단계를 실행하지 않고 즉시 사용자에게 피드백한다.

### 입력
```python
file_path: str    # GCS 내 파일 경로
filename:  str    # 원본 파일명 (확장자 판별용)
```

### 처리 흐름
```
GCS 다운로드
    │
    ▼
파일 형식 + 크기 검증
  지원: .pdf, .jpg, .jpeg, .png, .webp, .docx
  최대: 100MB
    │
    ├─ .pdf  → PyMuPDF로 텍스트 레이어 추출 시도
    │          텍스트 있으면 → sections 구조화
    │          텍스트 없으면 → has_text_layer: false (이미지 PDF)
    │
    ├─ .docx → python-docx로 단락/표/스타일 추출
    │          SmartArt, 도형, 수식은 무시하고 텍스트만 추출
    │
    └─ 이미지 → DPI 확인 (150 미만이면 Lanczos 업스케일)
               original_format 저장 후 resize (포맷 증발 방지)
```

### 출력
```python
{
    "success":   bool,
    "error":     str | None,
    "extension": str,           # ".pdf" | ".docx" | ".jpg" 등
    "extracted": {
        # PDF/DOCX
        "text":           str,
        "sections":       list,
        "has_text_layer": bool,
        "pages":          int,
        "tables":         list,   # DOCX 전용
        # 이미지
        "enhanced_bytes": bytes,
    },
    "warning":   str | None,    # 저해상도 경고 등
}
```

### 주요 설계 결정
- `extract_from_pdf`와 `extract_from_docx`는 AI 호출 없이 처리 → 속도, 비용 절감
- 이미지 resize 전 `original_format` 저장 → JPEG→PNG 변환으로 인한 용량 폭발 방지
- 모든 반환 경로에서 동일한 5개 키 보장 → 호출부 KeyError 방지

---

## Stage 1.5: PII 감지 및 마스킹

**파일:** `backend/app/pipeline/stage1_5_pii.py`

### 목적
외부 AI API(Gemini)로 문서를 전송하기 전에 개인정보를 서버 로컬에서 마스킹한다.
API 제공자 포함 누구도 실제 개인정보를 볼 수 없게 보호하며,
Stage 7에서 최종 문서 생성 후 원본으로 복원한다.

### 입력
```python
job_id:    str    # Redis 키 구분용
extracted: dict   # Stage 1 출력 딕셔너리
```

### 처리 흐름
```
extracted 딕셔너리 복사 (원본 변형 방지)
    │
    ▼
enhanced_bytes 분리 (bytes → JSON 직렬화 불가)
    │
    ▼
딕셔너리 전체 JSON 직렬화 (text + sections + tables 한 번에 마스킹)
    │
    ▼
6개 정규식 패턴으로 PII 감지
  SSN:          \d{6}-[1-4]\d{6}
  PHONE:        01[0-9]-\d{3,4}-\d{4}
  EMAIL:        [a-zA-Z0-9._%+\-]+@...
  ACCOUNT:      \d{3,6}-\d{2,6}-\d{4,6}
  PASSPORT:     [A-Z]{1,2}\d{7,8}
  CREDIT_CARD:  \d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}
    │
    ▼
토큰 치환: "901010-1234567" → "[PII_SSN_001]"
    │
    ▼
토큰 맵을 Redis에 저장 (TTL: 1시간)
  key: "pii:{job_id}"
    │
    ▼
enhanced_bytes 재결합 → 반환
```

### 출력
```python
{
    "success":         bool,
    "error":           str | None,
    "masked_extracted": dict,   # 마스킹된 extracted
    "pii_counts":      dict,    # { "SSN": 1, "PHONE": 2 } 감사 로그용
}
```

### 주요 설계 결정
- **async def run()**: 이전 asyncio.run() 방식은 Celery 이벤트 루프와 충돌. tasks.py에서 await로 직접 호출
- **JSON 직렬화 전략**: text만 마스킹하면 sections/tables 안의 PII 누출. 전체를 json.dumps → 마스킹 → json.loads로 3줄에 해결
- **Redis TTL 의존, 명시적 삭제 금지**: Celery max_retries=3 환경에서 Stage 7 복원 후 재시도 발생 가능. 삭제하면 토큰 복원 불가 → 최종 문서에 [PII_PHONE_001] 그대로 인쇄됨
- **원본 dict 복사**: extracted.pop()이 호출부 원본을 변형하지 않도록 dict(extracted)로 복사 후 처리

---

## Stage 2: A 콘텐츠 추출

**파일:** `backend/app/pipeline/stage2_extraction.py`
**프롬프트:** `backend/prompts/stage2_extraction/v1.0.0.txt`

### 목적
Stage 1에서 추출한 텍스트를 Gemini Pro에게 보내 문서의 의미적 구조를 파악한다.
단순 텍스트가 아닌 "이 문서에서 경력, 학력, 기술스택이 어디에 있는지"를
AI가 이해하고 구조화된 JSON으로 반환한다.
Stage 4 매핑의 입력이 된다.

### Stage 3와 병렬 실행
Stage 2(A 분석)와 Stage 3(B 분석)은 서로 의존성이 없으므로 병렬로 실행된다.

### 입력
```python
job_id:          str    # 작업 ID (로깅용)
masked_extracted: dict  # Stage 1.5 출력 (마스킹된 텍스트)
```

### 처리 흐름
```
텍스트 레이어 확인
  has_text_layer: false → 실패 반환 (Vision 처리 필요 - Stage 3와 통합 예정)
    │
    ▼
Rate Limiter 통과 확인 (gemini-1.5-pro RPM/TPM)
  실패 → RuntimeError → Celery 재시도 유도
    │
    ▼
프롬프트 로드 + 문서 텍스트 삽입 ({{DOCUMENT_TEXT}} 치환)
    │
    ▼
Gemini Pro Vision 비동기 호출 (generate_content_async)
    │
    ▼
JSON 파싱 (마크다운 백틱 방어 제거 포함)
    │
    ▼
섹션별 신뢰도 평균 계산 → avg_confidence
```

### 출력
```python
{
    "success":        bool,
    "error":          str | None,
    "doc_type":       str,      # resume | report | form | contract | other
    "sections":       list,     # [{ name, content, confidence }]
    "summary":        str,
    "avg_confidence": float,
    "prompt_version": str,      # LLMOps 추적용
    "model_used":     str,
}
```

### 주요 설계 결정
- **generate_content_async 사용**: async 함수 안에서 동기 generate_content 호출 시 이벤트 루프 블로킹 발생
- **PII 토큰 보존 프롬프트**: [PII_...] 토큰을 수정/번역/삭제하지 말도록 명시 → Stage 7 복원 보장
- **JSON 마크다운 금지**: Gemini가 ```json 블록으로 감싸면 파싱 실패 → 프롬프트에 명시 + 방어 코드 이중 적용

---

## Stage 3: B 템플릿 분석 + 자기검증 루프

**파일:** `backend/app/pipeline/stage3_template.py`
**프롬프트:** `backend/prompts/stage3_template/v1.0.0.txt`

### 목적
사용자가 지정한 목표 형식(B)을 분석해 HTML/CSS 템플릿을 생성한다.
B 형식 98% 일치가 최우선 목표이므로 자기검증 루프로 정확도를 높인다.

### 입력
```python
job_id:      str    # 작업 ID
b_file_path: str    # GCS 내 B 파일 경로
b_filename:  str    # 원본 파일명 (확장자 판별용)
```

### 처리 흐름
```
GCS에서 B 파일 다운로드
    │
    ├─ B가 PDF →
    │    pdfplumber로 폰트명/좌표/테이블 구조 추출 (정확, 무료)
    │    + PyMuPDF로 1페이지 PNG 이미지 변환 (2x 스케일)
    │    → 구조 텍스트 + 이미지를 함께 Gemini에 전달
    │    → 이미지 B보다 폰트명 정확도 대폭 향상
    │
    └─ B가 이미지 →
         MIME 타입 감지 (매직 바이트 기반)
         → 이미지 단독으로 Gemini Vision 분석
         → 폰트는 Google Fonts 유사 후보 3개 추정 제공

    ▼
Rate Limiter → Gemini Pro Vision 호출 (generate_content_async)
    │
    ▼
JSON 스키마 파싱
  {page_size, fields, layout, font_candidates, complexity_score, css_features}
    │
    ▼
complexity_score → complexity_warning 결정 (렌더러는 항상 Playwright)
  0~70:  경고 없음
  70+:   "레이아웃이 매우 복잡합니다" 경고 메시지
    │
    ▼
┌─────────────────────────────────────────────────┐
│ 자기검증 루프 (최대 2회)                         │
│                                                 │
│  JSON 스키마 → _build_html_template             │
│      → Playwright 렌더링                        │
│         (page_size 기반 viewport, 30초 타임아웃) │
│         (document.fonts.ready 폰트 로딩 대기)   │
│      → SSIM 유사도 측정 (Stage 7과 동일 기준)   │
│      → 95% 이상: 루프 종료                      │
│      → 95% 미만: Gemini 재분석 요청             │
│         → JSON 스키마 보정                      │
└─────────────────────────────────────────────────┘
    │
    ▼
최종 HTML 템플릿 + 검증 결과 반환
```

### 출력
```python
{
    "success":               bool,
    "error":                 str | None,
    "schema":                dict,     # 최종 JSON 스키마
    "html_template":         str,      # Stage 6에서 콘텐츠 채워넣을 HTML
    "complexity_warning":    str | None, # 복잡도 높을 때 경고 메시지
    "renderer_warning":      str | None,
    "complexity_score":      int,
    "b_image_bytes":          bytes,    # Stage 7 SSIM 비교용으로 전달
    "verification_score":    float,    # 최종 SSIM 유사도
    "verification_attempts": int,      # 루프 실행 횟수
    "prompt_version":        str,
    "model_used":            str,
}
```

### HTML 템플릿 구조
```html
<!-- Stage 6에서 {{field_id}} 플레이스홀더에 실제 콘텐츠를 채워넣는다 -->
<div class="field_001" data-field-id="field_001">{{field_001}}</div>
<div class="field_002" data-field-id="field_002">{{field_002}}</div>
```

### 주요 설계 결정
- **PDF → pdfplumber 우선**: 이미지 분석만으로는 폰트명 추정 불가. PDF 파일 구조에서 직접 추출
- **SSIM 유사도**: Stage 7과 동일한 알고리즘 사용 → 자기검증과 최종 검증의 기준 일관성 유지
- **page_size 기반 렌더링**: 추출된 page_size로 viewport 설정해 원본 B와 동일한 크기로 비교
- **타임아웃 30초 + 폰트 대기**: `document.fonts.ready` 대기로 폰트 미로딩 스크린샷 방지
- **_refine_schema Fallback**: Rate Limit 초과 또는 파싱 실패 시 현재 스키마 유지 → 파이프라인 중단 방지
- **프롬프트 캐싱**: 모듈 로드 시 1회만 디스크 읽기 (_PROMPT_TEMPLATE 모듈 상수)

---

## Stage 4: 의미론적 매핑

**파일:** `backend/app/pipeline/stage4_mapping.py`

### 목적
Stage 2의 A 콘텐츠와 Stage 3의 B 필드 스키마를 받아
"A의 어떤 내용이 B의 어느 필드로 가야 하는지" 결정한다.
Flash + Pro 앙상블로 매핑의 정확도를 높인다.

### 핵심 설계
```
Step 1: asyncio.gather로 Flash + Pro 병렬 매핑 생성

Step 2: 필드별 일치율 계산
        일치율 ≥ 90% → Pro 결과 자동 채택, mismatch_fields=[]
        일치율 50~90% → Pro 결과 채택 + 불일치 필드 사용자 알림
        일치율 < 50%  → Human Checkpoint #1 강제 발동

Step 3: 각 매핑에 transform 타입 추론
        summarize: 긴 내용 → 짧은 필드
        expand:    짧은 내용 → 긴 필드
        direct:    그대로 복사
        skip:      해당 없음
```

---

## Stage 5: 콘텐츠 생성 + Self-RAG 검증 (구현 예정)

**파일:** `backend/app/pipeline/stage5_generation.py`

### 목적
Stage 4 매핑 결과를 바탕으로 각 B 필드에 들어갈 실제 텍스트를 생성한다.
Self-RAG로 생성된 내용이 원문 A에 근거하는지 검증해 환각을 방지한다.

### Self-RAG 흐름
```
Flash로 콘텐츠 생성
    │
    ▼
Pro로 각 문장의 원문 근거 확인
  grounded: true  → 통과
  grounded: false → HALLUCINATION → 해당 문장 삭제 후 재생성
    │
    ▼
grounding_score = grounded 문장 수 / 전체 문장 수 × 100
grounding_score < 80% → 사용자 경고
```

---

## Stage 6: 문서 조립

**파일:** `backend/app/pipeline/stage6_assembly.py`

### 목적
Stage 3의 HTML 템플릿에 Stage 5의 생성 콘텐츠를 채워넣는 문자열 조립만 수행한다.
**PDF 렌더링은 Stage 7에서만 한 번 수행한다** (렌더링 중복 방지).

### 핵심 흐름
```
{{field_id}} 플레이스홀더를 regex 단일 패스로 치환
  (순차 교체 시 콘텐츠 안의 {{...}}가 재교체되는 버그 방지)
    │
    ▼
_clean_and_format_text 텍스트 정제
  마크다운 볼드(**) 제거
  HTML 이스케이프 (XSS 방지)
  \n → <br> 변환
    │
    ▼
filled_html 반환 (PII 마스킹 상태, Stage 7에서 복원)
```

### 주요 설계 결정
- **동기 함수 (def, not async)**: await 없음. Celery에서 직접 호출
- **렌더링 없음**: Stage 7이 Playwright 단일 세션으로 스크린샷+PDF 동시 처리하므로 여기서 렌더링하면 2배 낭비

---

## Stage 7: 형식 검증 + PII 복원

**파일:** `backend/app/pipeline/stage7_validation.py`

### 목적
최종 HTML을 원본 B와 SSIM으로 시각적 유사도를 검증하고,
PII를 복원한 후 최종 PDF를 생성해 GCS에 업로드한다.

### 핵심 흐름
```
PII 복원 (stage1_5_pii.restore 호출)
  Redis TTL 의존, 명시적 삭제 안 함 (Celery 재시도 대비)
    │
    ▼
단일 Playwright 세션으로:
  1. 스크린샷 (마스킹 상태 HTML → SSIM 비교용)
  2. 최종 PDF (복원된 HTML → 사용자 전달용)
  30초 타임아웃 + document.fonts.ready 대기
  page_size 기반 viewport, full_page=True, 멀티페이지 지원
    │
    ▼
SSIM 유사도 측정
  98% 이상 → 자동 완료
  90~98%   → 비주얼 에디터 권장
  90% 미만 → 비주얼 에디터 필수
    │
    ▼
최종 PDF GCS 업로드 → final_pdf_path 반환
```

---

## Human Checkpoint

### Checkpoint #1: 매핑 계획 확인
Stage 4 앙상블 일치율 < 50% 또는 필수 필드 미매핑 시 발동.
사용자가 A 섹션과 B 필드 연결을 드래그로 수정 가능.

### Checkpoint #2: 비주얼 에디터
Stage 7 검증 후 픽셀 유사도가 목표치 미달 시 발동.
사용자가 클릭으로 폰트, 여백, 색상, 위치를 직접 수정.
수정 사항은 프로필에 저장되어 다음 변환에 자동 반영.

---

## 공통 원칙

| 원칙 | 내용 |
|---|---|
| 단일 출구 | 모든 run() 함수는 동일한 키 구조를 가진 단 하나의 return 형태 |
| 부분 완료 | 필드 하나 실패해도 전체 중단 없이 [확인 필요] 플레이스홀더로 대체 |
| Rate Limiter 필수 | 모든 Gemini 호출 전 rate_limiter.acquire() 통과 |
| 비동기 일관성 | Gemini 호출은 generate_content_async() 사용, 동기 함수 안 호출 금지 |
| PII 안전 | 감사 로그에 실제 PII 값 절대 포함 금지, Redis TTL 의존 |
| 프롬프트 캐싱 | 모듈 상수로 로드 (_PROMPT_TEMPLATE), 매 호출마다 디스크 읽기 금지 |
| Gemini 싱글톤 | gemini_client.py의 model_pro/model_flash 재사용, 매 호출마다 생성 금지 |
| 프롬프트 버전 | 모든 AI 호출에 PROMPT_VERSION 기록 (LLMOps 추적) |

## Celery ↔ Async 경계

```
Celery Task (동기):
  asyncio.run(_run_pipeline(...))  ← 단일 진입점
      │
  _run_pipeline (비동기):
      Stage 1:   asyncio.to_thread(stage1_validation.run, ...)  ← 동기 함수
      Stage 1.5: await stage1_5_pii.run(...)                   ← 비동기
      Stage 2+3: await asyncio.gather(...)                     ← 병렬 비동기
      Stage 4~7: await stage_X.run(...)                        ← 비동기
      Stage 6:   stage6_assembly.run(...)                      ← 동기 (await 없음)
```
