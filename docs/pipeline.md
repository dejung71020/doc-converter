# 문서 변환 파이프라인 상세 문서

> 각 Stage의 목적, 입출력, 핵심 결정, 에러 처리를 기록한다.
> 코드 변경 시 이 문서도 함께 업데이트해야 한다.

---

## 전체 흐름 요약

```
[사용자 업로드]
  A 파일 + B 파일
      │
      ▼
┌─────────────────────────────────────────────────┐
│ PROFILE 생성 (최초 1회)                          │
│                                                 │
│  Stage 1   → 입력 검증 + 파일 전처리             │
│  Stage 1.5 → PII 감지 + 마스킹                  │
│  Stage 2   → A 콘텐츠 추출          ┐ 병렬      │
│  Stage 3   → B 템플릿 분석 + 검증   ┘           │
│  Stage 4   → 의미론적 매핑 (앙상블)              │
│  [Human Checkpoint #1: 매핑 계획 확인]           │
│  → 변환 프로필 저장                              │
└─────────────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────────────┐
│ 변환 적용 (반복 사용)                            │
│                                                 │
│  Stage 1   → 새 문서 검증 + 전처리               │
│  Stage 1.5 → PII 마스킹                         │
│  Stage 5   → 콘텐츠 생성 + Self-RAG 검증        │
│  Stage 6   → 문서 조립 (Playwright 렌더링)       │
│  Stage 7   → 형식 검증 + PII 복원               │
│  [Human Checkpoint #2: 비주얼 에디터]            │
│  → 최종 PDF 출력                                │
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
complexity_score → 렌더러 결정
  0~40:  WeasyPrint
  40~70: WeasyPrint + 경고 메시지
  70+:   Playwright (headless Chrome)
    │
    ▼
┌─────────────────────────────────────┐
│ 자기검증 루프 (최대 2회)             │
│                                     │
│  JSON 스키마 → _build_html_template │
│      → Playwright 렌더링            │
│      → PIL 픽셀 유사도 측정          │
│      → 95% 이상: 루프 종료          │
│      → 95% 미만: Gemini 재분석 요청 │
│         (원본 B + 렌더링 결과 비교)  │
│         → JSON 스키마 보정          │
└─────────────────────────────────────┘
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
    "preferred_renderer":    str,      # weasyprint | playwright
    "renderer_warning":      str | None,
    "complexity_score":      int,
    "verification_score":    float,    # 최종 픽셀 유사도
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
- **자기검증 루프**: 한 번 생성으로 끝내지 않고 실제 렌더링 결과를 원본과 비교해 AI가 스스로 교정
- **_refine_schema Fallback**: Rate Limit 초과 또는 파싱 실패 시 현재 스키마 유지 → 파이프라인 중단 방지
- **타임아웃 10초**: 구글 폰트 서버 장애나 잘못된 CSS 무한 루프 방지

---

## Stage 4: 의미론적 매핑 (구현 예정)

**파일:** `backend/app/pipeline/stage4_mapping.py`

### 목적
Stage 2의 A 콘텐츠와 Stage 3의 B 필드 스키마를 받아
"A의 어떤 내용이 B의 어느 필드로 가야 하는지" 결정한다.
Flash + Pro 앙상블로 매핑의 정확도를 높인다.

### 핵심 설계 (예정)
```
Step 1: A doc_type + B doc_type으로 변환 의도 파악
        예) resume → cover_letter = "경력을 확장해서 회사 맞춤 자기소개로"

Step 2: Flash 1회 + Pro 1회 병렬 매핑 생성
        일치율 ≥ 90% → Pro 결과 자동 채택
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

## Stage 6: 문서 조립 (구현 예정)

**파일:** `backend/app/pipeline/stage6_assembly.py`

### 목적
Stage 3의 HTML 템플릿에 Stage 5의 생성 콘텐츠를 채워넣고
Playwright로 최종 PDF를 렌더링한다.

### 핵심 흐름
```
HTML 템플릿의 {{field_id}} 플레이스홀더에 콘텐츠 삽입
    │
    ▼
렌더러 선택 (Stage 3에서 결정된 preferred_renderer)
  weasyprint → WeasyPrint PDF 변환
               실패 시 → Playwright 자동 폴백
  playwright → Playwright headless Chrome PDF 변환
    │
    ▼
페이지 오버플로우 감지 → 폰트 크기 자동 축소
    │
    ▼
초안 PDF + HTML 미리보기 반환
```

---

## Stage 7: 형식 검증 + PII 복원 (구현 예정)

**파일:** `backend/app/pipeline/stage7_validation.py`

### 목적
최종 출력물의 형식이 B와 일치하는지 검증하고
Stage 1.5에서 마스킹한 PII를 원본으로 복원한다.

### 핵심 흐름
```
Gemini Pro로 레이아웃 픽셀 Diff 검증
  유사도 98% 이상 → 자동 완료
  90~98%         → 비주얼 에디터 권장
  90% 미만       → 해당 섹션 재조립 후 비주얼 에디터 필수
    │
    ▼
PII 복원 (stage1_5_pii.restore 호출)
  Redis에서 토큰맵 로드 → 토큰을 원본값으로 치환
  주의: redis.delete 하지 않음 (Celery 재시도 대비)
    │
    ▼
최종 PDF GCS 업로드 → 다운로드 URL 반환
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
| 프롬프트 버전 | 모든 AI 호출에 PROMPT_VERSION 기록 (LLMOps 추적) |
