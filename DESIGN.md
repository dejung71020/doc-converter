# 문서 변환 플랫폼 기술 설계서

- **문서 버전**: v1.1.0
- **작성일**: 2026-05-18
- **최종 수정**: 2026-05-18
- **상태**: Draft

---

## 목차

1. [프로젝트 개요](#1-프로젝트-개요)
2. [목표 및 성공 지표](#2-목표-및-성공-지표)
3. [시스템 아키텍처](#3-시스템-아키텍처)
4. [기술 스택](#4-기술-스택)
5. [데이터 모델](#5-데이터-모델)
6. [API 명세](#6-api-명세)
7. [파이프라인 상세 설계](#7-파이프라인-상세-설계)
8. [AI 모델 전략](#8-ai-모델-전략)
9. [LLMOps 및 AI 품질 관리](#9-llmops-및-ai-품질-관리)
10. [검증 체계](#10-검증-체계)
11. [에러 처리 전략](#11-에러-처리-전략)
12. [UI/UX 설계](#12-uiux-설계)
13. [성능 목표 및 SLA](#13-성능-목표-및-sla)
14. [보안 설계](#14-보안-설계)
15. [배포 전략](#15-배포-전략)
16. [개발 마일스톤](#16-개발-마일스톤)
17. [리스크 분석](#17-리스크-분석)

---

## 1. 프로젝트 개요

### 1.1 배경 및 목적

사용자가 입력 문서(A)와 목표 형식 문서(B)를 업로드하면, AI가 A의 내용을 분석하고 B의 형식에 맞게 자동으로 변환하는 플랫폼이다. 이후 동일한 형식(B)으로 변환이 필요한 문서는 저장된 변환 프로필을 재사용하여 빠르게 처리한다.

### 1.2 핵심 사용 시나리오

| 시나리오 | A (입력) | B (목표 형식) | 변환 방식 |
|---|---|---|---|
| 요약 압축 | 35페이지 PDF 보고서 | 1페이지 이력서 | 핵심 내용 선별 + 압축 |
| 형식 재구성 | 이력서 | C사 자기소개서 양식 | 확장 + 양식 매핑 |
| 구조 변환 | 사진 메모 | 보고서 템플릿 | OCR + 구조화 |
| 반복 변환 | 신입 이력서들 | 공통 채용 양식 | 프로필 재사용 |

### 1.3 범위

**포함:**
- 입력 A: PDF, 이미지(JPG/PNG), 스캔 문서
- 입력 B: PDF, 이미지, 회사 양식
- 출력: PDF (기본), HTML 미리보기
- 실시간 파이프라인 진행 상황 표시
- 변환 프로필 저장 및 재사용

**제외 (v1.0):**
- Word(.docx) 직접 편집 출력
- 모바일 앱
- 다국어 UI

---

## 2. 목표 및 성공 지표

### 2.1 핵심 품질 목표

| 지표 | 목표값 | 측정 방법 |
|---|---|---|
| B 형식 일치율 | 95% 이상 | 레이아웃 픽셀 Diff 스코어 |
| 콘텐츠 정확도 | 90% 이상 | 원문 대비 팩트 보존율 |
| 프로필 생성 시간 | 60초 이내 | 서버 처리 시간 측정 |
| 변환 적용 시간 | 30초 이내 | 프로필 캐시 활용 기준 |
| 파이프라인 성공률 | 92% 이상 | 사용자 개입 없이 완료된 비율 |
| 시스템 가용성 | 99.5% | 월간 업타임 |

### 2.2 사용자 경험 목표

- 업로드 후 첫 번째 중간 산출물 표시: 10초 이내
- 검증 실패 시 부분 완료 후 해당 필드만 재처리 (전체 중단 없음)
- 에러 메시지는 기술 용어 없이 한국어로 표시

---

## 3. 시스템 아키텍처

### 3.1 전체 구성도

```
┌─────────────────────────────────────────────────────────────┐
│                        CLIENT                               │
│              Next.js (SSR + WebSocket)                      │
└──────────────────────┬──────────────────────────────────────┘
                       │ HTTPS / WSS
┌──────────────────────▼──────────────────────────────────────┐
│                     API GATEWAY                             │
│               FastAPI (Python 3.11+)                        │
│         인증 / 라우팅 / Global Rate Limiter                  │
└──────┬───────────────┬─────────────────┬────────────────────┘
       │               │                 │
┌──────▼──────┐ ┌──────▼──────┐ ┌───────▼────────────────────┐
│  File       │ │  Job Queue  │ │   Redis                    │
│  Service    │ │  (Celery)   │ │   ├─ 캐시 (프로필/결과)      │
│  GCS Upload │ │  Workers    │ │   ├─ Pub/Sub (WS 이벤트)    │
└──────┬──────┘ └──────┬──────┘ │   ├─ Rate Limiter (토큰버킷) │
       │               │        │   └─ PII 토큰맵 (TTL 1h)    │
       │               │        └────────────────────────────┘
┌──────▼───────────────▼──────────────────────────────────────┐
│              Pipeline Engine                                 │
│                                                              │
│  Stage 1:   입력 검증 + 전처리                               │
│  Stage 1.5: PII 감지 + 마스킹           ← 신규              │
│  Stage 2:   A 콘텐츠 추출      ──┐ (병렬)                   │
│  Stage 3:   B 템플릿 분석      ──┘ (복잡도 스코어 포함)      │
│  Stage 4:   의미론적 매핑 (앙상블)                           │
│  Stage 5:   콘텐츠 생성 + Self-RAG 환각 검증                │
│  Stage 6:   문서 조립 (WeasyPrint → Playwright 폴백)        │
│  Stage 7:   형식 검증 + PII 복원                            │
└──────────────────┬───────────────────────────────────────────┘
                   │
┌──────────────────▼────────────────────────────┐
│              AI Layer                          │
│  PyMuPDF │ Gemini Flash │ Gemini Pro           │
│  (전처리)  (생성/속도)    (분석/정확도/앙상블)   │
│                                               │
│  Global Rate Limiter (Token Bucket / Redis)   │
│  → 모든 Gemini 호출은 이 레이어를 경유         │
└───────────────────────────────────────────────┘
                   │
┌──────────────────▼────────────────────────────┐
│              Database (PostgreSQL)             │
│  conversion_profiles / conversion_jobs         │
│  stage_results / prompt_versions              │
│  audit_logs                                   │
└───────────────────────────────────────────────┘
```

### 3.2 WebSocket 스케일아웃 구조 (Redis Pub/Sub)

Cloud Run은 다중 인스턴스로 자동 스케일되므로, WebSocket 연결을 유지하는 인스턴스와 실제 Job을 처리하는 Worker 인스턴스가 다를 수 있다. Redis Pub/Sub으로 이를 해결한다.

```
Celery Worker (인스턴스 A)
  → Job 이벤트 발생
  → Redis PUBLISH  channel: "job:{job_id}:events"
                   payload: { type, stage, progress, ... }

FastAPI WS Handler (인스턴스 B)
  → 클라이언트 연결 수립 시 Redis SUBSCRIBE "job:{job_id}:events"
  → 이벤트 수신 즉시 WebSocket으로 클라이언트에 전달

결과: 어느 인스턴스가 Worker이고 WS 서버인지 무관
```

### 3.3 데이터 흐름

```
[프로필 생성 흐름 - 1회]
A + B 업로드
  → Stage 1:   입력 검증 + 전처리
  → Stage 1.5: PII 감지 + 마스킹 (API 전송 전)
  → Stage 2 || Stage 3: 병렬 분석
  → Stage 4:   매핑 계획 (앙상블)
  → [Human Checkpoint #1: 매핑 계획 확인]
  → 프로필 저장 (DB + Redis 캐시)

[변환 적용 흐름 - 반복]
새 문서 업로드 + 프로필 ID 지정
  → 프로필 캐시 로드
  → Stage 1.5: PII 마스킹
  → Stage 5:   콘텐츠 생성 + Self-RAG 검증
  → Stage 6:   문서 조립 (렌더러 자동 선택)
  → Stage 7:   형식 검증 + PII 복원
  → [Human Checkpoint #2: 시각적 Diff 확인]
  → PDF 출력
```

---

## 4. 기술 스택

### 4.1 선택 이유 포함 스택 목록

| 레이어 | 기술 | 버전 | 선택 이유 |
|---|---|---|---|
| Frontend | Next.js | 14 (App Router) | SSR + WebSocket 지원, React 생태계 |
| Frontend | TypeScript | 5.x | 타입 안전성, 대형 프로젝트 유지보수 |
| Frontend | TailwindCSS | 3.x | 빠른 UI 개발, 커스터마이징 |
| Backend | Python FastAPI | 0.110+ | 비동기 지원, 자동 API 문서 |
| Backend | Celery | 5.x | 백그라운드 작업 큐, 재시도 내장 |
| AI | Gemini 1.5 Pro | latest | 최고 Vision 정확도, 1M 토큰 컨텍스트 |
| AI | Gemini 1.5 Flash | latest | 속도 최적화, Pro 3~5배 빠름 |
| PDF 처리 | PyMuPDF | 1.24+ | 고속 텍스트 추출, AI 불필요 |
| PDF 처리 | pdfplumber | 0.11+ | 테이블/레이아웃 구조 추출 |
| 문서 생성 | WeasyPrint | 62+ | HTML/CSS → PDF 변환 (단순~중간 복잡도) |
| 문서 생성 | Playwright | 1.44+ | 복잡한 레이아웃 PDF 변환 (WeasyPrint 폴백) |
| DB | PostgreSQL | 16 | 구조화 데이터, JSONB 지원 |
| Cache / Queue | Redis | 7.x | 세션/캐시/Celery 브로커 통합 |
| 파일 저장소 | Google Cloud Storage | - | Gemini와 동일 생태계 |
| 컨테이너 | Docker + Docker Compose | - | 로컬/프로덕션 환경 일관성 |
| 배포 | Google Cloud Run | - | 서버리스, 자동 스케일링 |

---

## 5. 데이터 모델

### 5.1 conversion_profiles

```sql
CREATE TABLE conversion_profiles (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    description     TEXT,

    -- B 템플릿 정보
    b_template_html TEXT NOT NULL,           -- 추출된 HTML/CSS 템플릿
    b_schema        JSONB NOT NULL,          -- 필드 구조 스키마
    b_file_url      VARCHAR(500),            -- 원본 B 파일 GCS URL
    b_type          VARCHAR(50) NOT NULL,    -- 'pdf' | 'image'

    -- 메타
    usage_count     INTEGER DEFAULT 0,
    avg_confidence  NUMERIC(5,2),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ              -- NULL이면 영구 보존
);
```

### 5.2 conversion_jobs

```sql
CREATE TABLE conversion_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id      UUID REFERENCES conversion_profiles(id),

    -- 입력
    input_file_url  VARCHAR(500) NOT NULL,
    input_type      VARCHAR(50) NOT NULL,    -- 'pdf' | 'image'

    -- 상태
    status          VARCHAR(50) NOT NULL     -- 'queued' | 'processing'
                    DEFAULT 'queued',        -- | 'waiting_input' | 'completed' | 'failed'
    current_stage   INTEGER DEFAULT 0,
    progress        NUMERIC(5,2) DEFAULT 0,

    -- 결과
    result_file_url VARCHAR(500),
    error_message   TEXT,

    -- 시간
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);
```

### 5.3 stage_results

```sql
CREATE TABLE stage_results (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID REFERENCES conversion_jobs(id),
    stage_number    INTEGER NOT NULL,
    stage_name      VARCHAR(100) NOT NULL,

    status          VARCHAR(50) NOT NULL,    -- 'success' | 'failed' | 'retried'
    confidence      NUMERIC(5,2),           -- 0.00 ~ 100.00
    retry_count     INTEGER DEFAULT 0,
    model_used      VARCHAR(100),           -- 사용된 AI 모델명
    prompt_version  VARCHAR(50),            -- 사용된 프롬프트 버전 (예: 'v1.2.0')

    -- 비용 추적 (수익성 분석 및 프롬프트 최적화용)
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cost_usd        NUMERIC(10, 6),         -- 해당 스테이지 API 비용

    -- 환각 검증 결과 (Stage 5 해당)
    hallucination_flags JSONB,              -- [{ sentence, grounded, source }]
    grounding_score NUMERIC(5,2),          -- 원문 근거 보존율

    input_summary   JSONB,                  -- 이 단계의 입력 요약
    output_data     JSONB,                  -- 이 단계의 산출물
    error_detail    JSONB,                  -- 실패 시 상세 정보

    duration_ms     INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

### 5.4 prompt_versions

```sql
CREATE TABLE prompt_versions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stage_name      VARCHAR(100) NOT NULL,  -- 'stage2_extraction' 등
    version         VARCHAR(50) NOT NULL,   -- 'v1.2.0'
    content         TEXT NOT NULL,          -- 실제 프롬프트 내용
    is_active       BOOLEAN DEFAULT false,  -- 현재 프로덕션 사용 여부
    golden_score    NUMERIC(5,2),          -- Golden Dataset 기준 평균 스코어
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    activated_at    TIMESTAMPTZ,
    UNIQUE (stage_name, version)
);
```

### 5.5 audit_logs

```sql
CREATE TABLE audit_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID,
    event_type      VARCHAR(100) NOT NULL,
    detail          JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 6. API 명세

### 6.1 프로필 관련

```
POST   /api/v1/profiles
       Body: { name, b_file (multipart), a_sample_file (multipart) }
       Response: { profile_id, job_id }
       설명: B 분석 + 샘플 A로 매핑 계획 생성 시작

GET    /api/v1/profiles/:id
       Response: { id, name, b_schema, b_template_preview, usage_count }

GET    /api/v1/profiles
       Query: page, limit
       Response: { profiles[], total }

DELETE /api/v1/profiles/:id
       설명: 소프트 삭제, 파일은 24시간 후 GCS에서 제거
```

### 6.2 변환 작업 관련

```
POST   /api/v1/jobs
       Body: { profile_id, input_file (multipart) }
       Response: { job_id, estimated_seconds }

GET    /api/v1/jobs/:id
       Response: { id, status, current_stage, stages[], result_file_url }

GET    /api/v1/jobs/:id/stages
       Response: { stages[{ stage_number, name, status, confidence, output_preview }] }

POST   /api/v1/jobs/:id/approve-mapping
       Body: { approved: bool, overrides: { field_id: mapping } }
       설명: Human Checkpoint #1 - 매핑 계획 승인

POST   /api/v1/jobs/:id/approve-output
       Body: { approved: bool, field_edits: { field_id: content } }
       설명: Human Checkpoint #2 - 최종 출력 승인

GET    /api/v1/jobs/:id/download
       Query: format=pdf|html (기본: pdf)
       Response: 선택한 형식의 파일 스트림

GET    /api/v1/jobs/:id/cost
       Response: { total_input_tokens, total_output_tokens, total_cost_usd }
       설명: 해당 작업의 전체 API 비용 조회
```

### 6.4 출력 형식

```
지원 출력 형식:
  PDF  (기본): WeasyPrint 또는 Playwright 렌더링
  HTML (미리보기): 브라우저 인라인 렌더링, 다운로드 불가

v2.0 예정:
  DOCX: python-docx 기반 (별도 템플릿 엔진 필요)
  PPTX: python-pptx 기반

형식별 렌더러:
  PDF → Stage 3 복잡도 스코어 기반 WeasyPrint / Playwright 자동 선택
  HTML → 항상 직접 렌더링 (렌더러 불필요)
```

### 6.3 WebSocket

```
WS     /ws/v1/jobs/:id

서버 → 클라이언트 이벤트:
  { type: 'stage_start',    stage: 3, name: '매핑 계획 수립' }
  { type: 'stage_progress', stage: 3, progress: 65 }
  { type: 'stage_complete', stage: 3, confidence: 87.3, preview: {...} }
  { type: 'human_required', checkpoint: 1, data: { mapping_plan } }
  { type: 'stage_failed',   stage: 3, retry: true, attempt: 2 }
  { type: 'job_complete',   result_url: '...' }
  { type: 'job_failed',     error: '...' }
```

---

## 7. 파이프라인 상세 설계

### Stage 1: 입력 검증 및 전처리

```
입력: A 파일, B 파일
처리:
  - 파일 형식 확인 (PDF/JPG/PNG/WEBP)
  - 파일 크기 제한 확인 (A: 최대 100MB, B: 최대 20MB)
  - MIME 타입 + 매직 바이트 이중 검증 (파일 위변조 방지)
  - 이미지의 경우 최소 해상도 확인 (300 DPI 권장)
  - 저해상도 이미지 → 자동 업스케일 (Lanczos 알고리즘)
  - PDF → PyMuPDF로 텍스트 레이어 추출 시도
    → 텍스트 레이어 있으면: 텍스트 추출 후 Vision AI 호출 최소화
    → 텍스트 레이어 없으면: 이미지 PDF로 처리 (전체 Vision AI)
출력: 정규화된 입력 객체 { a_text, a_pages, a_type, b_enhanced_image }
```

### Stage 1.5: PII 감지 및 마스킹 (신규)

외부 AI API(Gemini)로 문서를 전송하기 전, 서버 로컬에서 민감 개인정보를 마스킹한다. 조립 완료 후 Stage 7에서 복원한다.

```
감지 대상 (한국 기준):
  주민등록번호:  \d{6}-[1-4]\d{6}
  전화번호:      01[0-9]-\d{3,4}-\d{4}
  이메일:        [a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}
  계좌번호:      \d{3,6}-\d{2,6}-\d{4,6}
  여권번호:      [A-Z]{1,2}\d{7,8}
  신용카드:      \d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}

처리 흐름:
  원문 스캔 → 감지된 PII를 토큰으로 치환
  "901010-1234567" → "[PII_SSN_001]"
  "010-1234-5678"  → "[PII_PHONE_001]"

토큰 맵 저장:
  Redis SET  key: "pii:{job_id}"
             value: { "PII_SSN_001": "901010-1234567", ... }
             TTL: 1시간 (작업 완료 후 즉시 삭제)

복원 (Stage 7 완료 후):
  최종 PDF의 플레이스홀더를 원본 값으로 치환
  Redis 키 즉시 삭제

주의: PII 토큰맵은 절대 DB에 저장하지 않으며, 메모리(Redis)에만 존재
```

### Stage 2 + 3: A 콘텐츠 추출 & B 템플릿 분석 (병렬)

```
[Stage 2: A 콘텐츠 추출]
모델: Gemini 1.5 Pro (이미지 A) / PyMuPDF + Pro (텍스트 PDF A)
프롬프트 전략:
  - 문서 타입 자동 감지 (보고서/이력서/계약서 등)
  - 섹션별 구조화된 JSON 추출
  - 각 필드에 신뢰도 스코어 부여
출력: { doc_type, sections: [{ name, content, confidence }] }

[Stage 3: B 템플릿 분석] ← 최우선 정확도 단계
모델: Gemini 1.5 Pro 전용 (Flash 완전 배제)

B 파일 타입별 처리 경로:
  B가 PDF →
    1단계: pdfplumber로 폰트명/정확한 좌표/텍스트 구조 추출 (무료, 정확)
    2단계: 1페이지 이미지 변환 → Gemini Vision으로 시각적 스타일 보완
    → PDF는 폰트명을 직접 추출 가능하므로 이미지보다 정확도 높음

  B가 이미지 →
    Gemini Vision 단독 분석
    → 폰트는 Google Fonts 유사 후보 3개 추정 제공

자기검증 루프 (Self-Verification Loop):
  1차: Gemini Vision으로 JSON 스키마 추출
  2차: 추출된 JSON으로 HTML/CSS 생성 → Playwright로 렌더링
  3차: 렌더링 결과와 원본 B 픽셀 Diff 측정
  4차: 유사도 95% 미만 → 차이 영역만 크롭해서 Gemini에 재분석 요청 → JSON 보정
  최대 2회 반복 후 비주얼 에디터로 사용자 전달

레이아웃 복잡도 스코어 계산:
  절대 위치 요소 수            × 15점
  컬럼 수 (3 이상)             × 10점
  중첩 테이블 깊이             × 20점
  커스텀 폰트 수               × 5점
  CSS Grid/Flexbox 적용 구역 수 × 10점

렌더러 결정:
  스코어 0~40   → WeasyPrint
  스코어 40~70  → WeasyPrint + 사용자 경고
  스코어 70+    → Playwright (headless Chrome) 자동 선택

출력:
  {
    fields: [...],
    layout: { columns, page_padding, background_color },
    page_size: { format, width_px, height_px },
    font_candidates: [...],
    css_features: [...],
    complexity_score: int,
    preferred_renderer: 'weasyprint'|'playwright',
    html_template: str,
    verification_score: float,   ← 자기검증 루프 최종 픽셀 유사도
    verification_attempts: int,  ← 반복 횟수
  }
```

### Stage 4: 의미론적 매핑 (앙상블)

```
목표: A의 어떤 콘텐츠가 B의 어느 필드로 가는지 결정

앙상블 실행:
  Worker 1: Gemini Flash로 매핑 생성 → mapping_flash
  Worker 2: Gemini Pro로 매핑 생성  → mapping_pro

비교 로직:
  일치율 >= 90% → mapping_pro 채택, 자동 진행
  일치율 50~90% → mapping_pro 채택, 사용자에게 불일치 필드 알림
  일치율 < 50%  → Human Checkpoint #1 강제 발동

매핑 결과 스키마:
  {
    mappings: [
      {
        b_field_id: "work_experience",
        a_source: ["section_3", "section_4"],
        transform: "summarize",   // summarize | expand | direct | skip
        confidence: 91.2,
        note: "3, 4번 섹션을 합산 요약"
      }
    ],
    unmapped_b_fields: [],       // 매핑 못 찾은 B 필드
    unused_a_sections: []        // B에 사용 안 된 A 섹션
  }
```

### Human Checkpoint #1: 매핑 계획 확인

```
발동 조건:
  - 앙상블 일치율 < 50%
  - unmapped_b_fields에 required 필드 존재
  - 사용자 수동 요청

UI 표시:
  - A 섹션과 B 필드를 연결하는 시각적 다이어그램
  - 신뢰도 낮은 매핑은 주황색 강조
  - 미매핑 필드는 빨간색
  - 드래그로 매핑 수정 가능

사용자 액션:
  - 승인 → Stage 5 진행
  - 수정 후 승인 → 수정된 매핑으로 Stage 5 진행
  - 전면 거부 → Stage 4 재실행
```

### Stage 5: 콘텐츠 생성 + Self-RAG 환각 검증

```
모델: Gemini 1.5 Flash (속도 우선) + Pro (환각 검증)

필드별 처리:
  transform = "summarize" → A 내용 압축, B 필드 공간에 맞는 길이 조정
  transform = "expand"    → A 내용 확장, B 문서의 목적에 맞게 작성
  transform = "direct"    → A 내용 그대로 복사 (정확도 검증 포함)
  transform = "skip"      → 빈 필드로 처리

Self-RAG 환각 검증 (콘텐츠 생성 직후):
  생성된 텍스트의 각 문장을 Pro에게 재검토 요청

  프롬프트:
    "다음 생성 텍스트의 각 문장이 [원문 A]에 실제로 있는 내용인지 확인해.
     있으면 원문의 어느 부분인지 인용하고, 없으면 HALLUCINATION으로 표시해."

  출력 예시:
    [
      { "sentence": "3년간 백엔드 개발 경력", "grounded": true,
        "source": "A 문서 3페이지 2번째 문단" },
      { "sentence": "수상 경력 10건 보유", "grounded": false,
        "flag": "HALLUCINATION" }
    ]

  처리:
    HALLUCINATION 문장 → 자동 삭제 후 해당 필드만 재생성
    재생성 후에도 HALLUCINATION → [확인 필요] 플레이스홀더로 대체
    grounding_score = (grounded 문장 수 / 전체 문장 수) × 100
    grounding_score < 80% → 사용자 경고

각 필드 생성 후:
  - 길이 검증 (B 템플릿의 필드 크기 대비)
  - 언어 일관성 확인
  - 신뢰도 < 70% 필드 → Pro로 재생성
  - grounding_score → stage_results에 기록
```

### Stage 6: 문서 조립

```
처리:
  - B HTML 템플릿에 생성된 콘텐츠 삽입
  - Stage 3에서 결정된 renderer로 PDF 변환

  렌더러 실행:
    WeasyPrint 선택 시:
      → HTML/CSS → PDF 변환
      → 폰트 로딩 (Google Fonts 서브셋 캐시 사용)
      → 변환 실패 시 → Playwright로 자동 폴백

    Playwright 선택 시 (또는 WeasyPrint 폴백):
      → Headless Chromium으로 HTML 렌더링
      → page.pdf() 호출
      → 실제 Chrome 엔진 = CSS Grid/Flexbox/절대위치 완전 지원
      → Docker 이미지에 Chromium 포함 필요

  공통:
    - 페이지 오버플로우 자동 감지 및 폰트 크기 자동 축소
    - WeasyPrint 실패 로그 기록 (복잡도 스코어 보정에 활용)

출력: 초안 PDF + HTML 미리보기
```

### Stage 7: 형식 검증

```
모델: Gemini 1.5 Pro
검증 항목:
  1. 레이아웃 Diff: B 원본 이미지 vs 생성 PDF 렌더링 비교
     → 픽셀 유사도 95% 미만 시 → 해당 섹션 재조립
  2. 필드 완성도: 모든 required 필드 채워짐 확인
  3. 텍스트 오버플로우: 텍스트가 박스 밖으로 나가는지 확인
  4. 시각적 품질 스코어 산출 (0~100)

스코어 기준:
  95 이상  → 자동 완료
  85~95   → Human Checkpoint #2 권장
  85 미만  → Human Checkpoint #2 필수
```

### Human Checkpoint #2: 시각적 Diff 확인

```
UI:
  - 좌측: B 원본
  - 우측: 생성 결과 (인라인 편집 가능)
  - 불일치 영역 빨간 오버레이
  - 클릭하면 해당 필드 텍스트 편집 모드

사용자 액션:
  - 확인 후 내보내기 → 최종 PDF 생성
  - 필드 수정 → 실시간 미리보기 반영
  - 섹션 재생성 요청 → 해당 섹션만 Stage 5~6 재실행
```

---

## 8. AI 모델 전략

### 8.1 작업별 모델 할당

| 작업 | 모델 | 이유 |
|---|---|---|
| PDF 텍스트 추출 | PyMuPDF (비AI) | 결정적, 완전 정확, 무료 |
| 이미지 전처리 | 로컬 처리 (Pillow) | API 비용 없음 |
| 이미지 OCR | Gemini 1.5 Pro | 최고 Vision 정확도 |
| B 레이아웃 분석 | Gemini 1.5 Pro | 공간 이해력 필수 |
| 매핑 앙상블 (1차) | Gemini 1.5 Flash | 속도, 비교 기준 |
| 매핑 앙상블 (2차) | Gemini 1.5 Pro | 최종 결정권 |
| 콘텐츠 생성 | Gemini 1.5 Flash | 속도 우선 |
| 저신뢰도 재생성 | Gemini 1.5 Pro | 정확도 복구 |
| 형식 검증 | Gemini 1.5 Pro | 정확도 최우선 |

### 8.2 컨텍스트 캐싱 전략

```
캐싱 대상:
  B 템플릿 분석 결과
  → 동일 B 파일 재사용 시 API 호출 없이 DB에서 로드
  → 예상 비용 절감: 75%

  A 추출 결과 (대용량 문서)
  → 동일 A로 여러 B에 변환 시 재사용
  → TTL: 24시간

Gemini Context Cache API 활용:
  → 반복 사용되는 긴 프롬프트/문서를 Gemini 서버에 캐싱
  → 캐시 히트 시 토큰 비용 대폭 절감
```

### 8.3 신뢰도 기반 자동/수동 분기

```
Field 신뢰도:
  >= 90%  → 자동 통과
  70~89%  → 통과, UI에 주의 표시
  50~69%  → Pro 모델로 1회 재시도
  < 50%   → 재시도 후에도 낮으면 사용자 개입 요청

Stage 전체 신뢰도:
  평균 신뢰도 = 각 필드 신뢰도의 가중 평균
  95 이상 → 자동 완료
  85~95  → Checkpoint 권장
  < 85   → Checkpoint 필수
```

---

## 9. LLMOps 및 AI 품질 관리

### 9.1 프롬프트 버전 관리

프롬프트를 코드처럼 버전 관리한다. 프롬프트 변경 시 Golden Dataset 기준 품질 검증을 통과해야만 프로덕션에 반영된다.

```
디렉토리 구조:
  prompts/
    stage2_extraction/
      v1.0.0.txt    ← 프로덕션 (is_active: true)
      v1.1.0.txt    ← 스테이징 테스트 중
    stage3_template/
      v1.0.0.txt
    stage4_mapping/
      v1.0.0.txt
    stage5_generation/
      v1.0.0.txt
    stage5_selfrag/
      v1.0.0.txt

배포 전 자동 실행 (CI/CD 단계):
  신규 프롬프트 × Golden Dataset 전체 실행
  → 스코어 계산 (레이아웃 일치율, 팩트 보존율, 환각률)
  → 기존 버전 대비 스코어 저하 > 2% 시 → 배포 차단
  → 통과 시 → prompt_versions.is_active 업데이트
```

### 9.2 Golden Dataset 구축

프롬프트 또는 AI 모델 버전 변경 시 회귀 테스트에 사용하는 정답 데이터셋.

```
구성:
  총 30개 A-B 쌍 (각 유형별 10개)
  - 유형 1: 대용량 PDF(10~40p) → 이력서 (압축형)
  - 유형 2: 이력서 → 자기소개서 양식 (확장형)
  - 유형 3: 이미지 문서 → 정형 양식 (구조 변환형)

각 쌍의 구성:
  input_a:        A 파일
  input_b:        B 파일 (목표 형식)
  expected_mapping: 정답 매핑 JSON
  expected_output:  정답 출력 PDF
  eval_criteria:  { layout_score: 95, grounding_score: 90, field_coverage: 100 }

평가 자동화:
  - 레이아웃 일치율: 생성 PDF vs expected_output 픽셀 비교
  - 팩트 보존율:    생성 텍스트 vs 원문 Self-RAG 스코어
  - 필드 커버리지:  required 필드 모두 채워졌는지
  - 환각률:         HALLUCINATION 감지 비율

관리:
  - Golden Dataset은 git-lfs로 버전 관리
  - 새로운 엣지 케이스 발견 시 즉시 추가
  - 분기별 검토 및 갱신
```

### 9.3 모델 버전 업데이트 정책

```
Gemini 신규 버전 출시 시:
  1. 스테이징 환경에서 Golden Dataset 전체 실행
  2. 기존 버전 대비 스코어 비교
  3. 개선된 경우 → 프로덕션 점진적 전환 (10% → 50% → 100% 트래픽)
  4. 저하된 경우 → 현행 유지, 원인 분석

각 변환 Job에 사용된 모델 버전을 stage_results.model_used에 정확히 기록
→ 버전별 품질 추이 분석 가능
```

---

## 10. 검증 체계

### 10.1 단계별 검증 매트릭스

| Stage | 검증 항목 | 실패 기준 | 실패 처리 |
|---|---|---|---|
| 1 | 파일 파싱 가능 여부 | 파싱 불가 | 즉시 사용자 피드백 |
| 1 | 이미지 해상도 | 150 DPI 미만 | 경고 후 계속 or 재업로드 요청 |
| 1.5 | PII 감지 완료 여부 | 감지 스캔 오류 | 전송 차단 + 사용자 알림 |
| 2 | 필드 추출 완성도 | 필수 필드 누락 | 해당 필드 Pro로 재추출 |
| 2 | 텍스트 언어 일관성 | 언어 혼재 | 주요 언어로 통일 처리 |
| 3 | B 템플릿 필드 인식 | 필드 인식률 < 80% | Pro로 재분석 |
| 3 | 레이아웃 복잡도 | 스코어 70+ | Playwright 렌더러 자동 선택 |
| 4 | 매핑 앙상블 일치율 | < 50% | Human Checkpoint 강제 |
| 4 | 필수 B 필드 커버리지 | 미매핑 필수 필드 존재 | 사용자 직접 매핑 |
| 5 | 생성 텍스트 길이 | B 필드 크기 초과 | 자동 재조정 |
| 5 | Self-RAG grounding | < 80% | HALLUCINATION 문장 삭제 + 재생성 |
| 6 | WeasyPrint 렌더링 | 변환 실패 | Playwright 폴백 자동 실행 |
| 6 | 페이지 오버플로우 | 콘텐츠 잘림 | 폰트 크기 자동 축소 |
| 7 | 레이아웃 픽셀 유사도 | 95% 미만 | 불일치 섹션 재조립 |
| 7 | 시각적 완성도 스코어 | 85 미만 | Human Checkpoint 필수 |
| 7 | PII 복원 완료 | 토큰 미복원 존재 | 복원 재시도 후 경고 |

---

## 11. 에러 처리 전략

### 11.1 에러 유형 및 처리

```
[API 오류]
  네트워크 타임아웃    → 지수 백오프 재시도 (1s → 2s → 4s → 8s → 포기)
  Rate Limit 초과     → Celery 큐에서 60초 대기 후 자동 재시도
  모델 오류           → Flash ↔ Pro 전환 후 재시도

[처리 오류]
  특정 필드 추출 실패  → 해당 필드만 재처리, 나머지 계속 진행
  Stage 전체 실패     → 최대 2회 재시도, 이후 사용자 알림
  파이프라인 중단      → 진행 상태 DB 저장, 재접속 시 이어서 진행

[입력 오류]
  판독 불가 파일      → 즉시 사용자 피드백 (기술 용어 없이)
  B 형식 인식 불가    → 더 선명한 이미지 요청 또는 PDF 형식 권장
```

### 11.2 부분 완료 처리

```
전체 실패 대신 부분 완료 원칙:

  실패한 필드 → [작성 필요] 플레이스홀더로 표시
  성공한 필드 → 그대로 출력에 포함
  최종 출력   → 미완성 필드 목록과 함께 다운로드 가능

  사용자 옵션:
    1. 미완성 필드 수동 입력
    2. 해당 필드만 재시도
    3. 그대로 내보내기 (미완성 상태)
```

### 11.3 재시도 정책

```python
RETRY_POLICY = {
    "max_attempts": 3,
    "backoff_factor": 2,          # 1s, 2s, 4s
    "confidence_threshold": 70,   # 이하면 재시도 트리거
    "model_escalation": {
        "attempt_1": "gemini-flash",
        "attempt_2": "gemini-pro",
        "attempt_3": "gemini-pro + 다른 프롬프트 전략"
    }
}
```

### 11.4 글로벌 Rate Limiter (Token Bucket)

Celery Worker 다수가 동시에 Gemini API를 호출할 때 RPM/TPM 한도를 초과하지 않도록 Redis 기반 Token Bucket을 도입한다. 모든 Gemini 호출은 이 레이어를 반드시 통과한다.

```python
class GeminiRateLimiter:
    # Gemini API 유료 플랜 기준
    LIMITS = {
        "gemini-1.5-pro":   {"rpm": 360,  "tpm": 4_000_000},
        "gemini-1.5-flash": {"rpm": 1000, "tpm": 4_000_000},
    }

    def acquire(self, model: str, estimated_tokens: int) -> bool:
        minute_bucket = int(time.time() / 60)
        rpm_key = f"rl:{model}:rpm:{minute_bucket}"
        tpm_key = f"rl:{model}:tpm:{minute_bucket}"

        with redis.pipeline() as pipe:
            pipe.incr(rpm_key);        pipe.expire(rpm_key, 60)
            pipe.incrby(tpm_key, estimated_tokens)
            pipe.expire(tpm_key, 60)
            rpm_count, _, tpm_count, _ = pipe.execute()

        if rpm_count > self.LIMITS[model]["rpm"]:
            return False   # Celery: retry(countdown=60)
        if tpm_count > self.LIMITS[model]["tpm"]:
            return False   # Celery: retry(countdown=60)
        return True

# Celery Task 내 사용 예시
@celery.task(bind=True, max_retries=5)
def call_gemini(self, model, prompt, tokens):
    if not rate_limiter.acquire(model, tokens):
        raise self.retry(countdown=60)
    return gemini_client.generate(prompt)
```

Celery Worker 수와 Rate Limit 연동:
```
Worker 수 × 평균 RPM/Worker = 총 RPM 사용량
총 RPM 사용량 < API 한도의 80% 유지 (안전 마진)

예시: Worker 8개, 작업당 평균 3회 API 호출, 처리시간 20초
  → Worker당 RPM = 3회 × (60초/20초) = 9 RPM
  → 전체 = 8 × 9 = 72 RPM (Flash 한도 1000의 7.2% → 안전)
```

---

## 12. UI/UX 설계

### 12.1 화면 구성

**[화면 1] 홈 / 업로드**
```
┌─────────────────────────────────────────────────────┐
│  문서 변환 플랫폼                                    │
│                                                     │
│  ┌──────────────────┐   ┌──────────────────┐        │
│  │   A 문서 (원본)   │   │  B 문서 (목표형식) │        │
│  │                  │   │                  │        │
│  │  드래그 앤 드롭   │   │  드래그 앤 드롭   │        │
│  │  PDF / 이미지    │   │  PDF / 이미지    │        │
│  │                  │   │                  │        │
│  └──────────────────┘   └──────────────────┘        │
│                                                     │
│  업로드 즉시: 파일 미리보기 + 품질 판정              │
│  "B 이미지 해상도가 낮습니다. 더 선명한 이미지를     │
│   사용하면 형식 일치도가 높아집니다."                │
│                                                     │
│  기존 프로필 사용: [프로필 선택 ▼]                   │
│                                                     │
│                          [변환 시작]                 │
└─────────────────────────────────────────────────────┘
```

**[화면 2] 파이프라인 진행**
```
┌──────────┬────────────────────────────┬──────────────┐
│ 진행 단계 │       현재 처리 중          │  중간 산출물  │
│          │                            │              │
│ ●완 입력  │  Stage 4: 매핑 계획 수립   │  {           │
│ ●완 A분석 │  ████████░░  78%           │   경력→Work  │
│ ●완 B분석 │                            │   학력→Edu  │
│ ◉진 매핑  │  Flash: 완료               │   수상→?    │
│ ○대 생성  │  Pro:   처리중...           │  }           │
│ ○대 조립  │  예상: 14초 남음            │              │
│ ○대 검증  │                            │ 신뢰도: 87%  │
│          │  [일시정지]  [취소]          │              │
└──────────┴────────────────────────────┴──────────────┘
```

**[화면 3] Human Checkpoint #1 - 매핑 확인**
```
┌─────────────────────────────────────────────────────┐
│  AI가 아래와 같이 매핑했습니다. 확인해주세요.         │
│                                                     │
│  A 섹션                        B 필드               │
│  ────────────────────────────────────────           │
│  3페이지: 경력사항 ─────────→  Work Experience ✓    │
│  5페이지: 학력 ──────────────→  Education ✓         │
│  7페이지: 수상내역 ──────────→  ??? (87% 신뢰도) ⚠  │
│  9페이지: 기술스택 ──────────→  Skills ✓            │
│                                                     │
│  [수상내역]을 어느 필드로 매핑할까요?                 │
│  ○ Achievements  ○ Additional Info  ○ 제외          │
│                                                     │
│        [수정 완료, 변환 진행]    [전체 재분석]        │
└─────────────────────────────────────────────────────┘
```

**[화면 4] Human Checkpoint #2 - 최종 Diff**
```
┌──────────────────────┬──────────────────────────────┐
│  B 원본              │  생성 결과          (클릭 편집)│
│                      │                              │
│  ┌────────────────┐  │  ┌──────────────────────┐   │
│  │ 홍길동         │  │  │ 홍길동               │   │
│  │ ───────────── │  │  │ ─────────────────── │   │
│  │ Work          │  │  │ Work Experience      │   │
│  │ 2021-2024     │  │  │ 2021-2024      ←빨간 │   │
│  │ ...           │  │  │ ...내용 상이...테두리 │   │
│  └────────────────┘  │  └──────────────────────┘   │
│                      │                              │
│  레이아웃 일치도: 96%  │  [섹션 재생성] [직접 편집]  │
│                      │                              │
└──────────────────────┴──────────────────────────────┘
│              [PDF 내보내기]    [다시 처리]            │
└─────────────────────────────────────────────────────┘
```

### 12.2 UX 원칙

- **낙관적 UI**: 처리 시작 즉시 예상 구조 스켈레톤 표시
- **점진적 공개**: 섹션 완료될 때마다 순차적으로 노출
- **에러 메시지**: 기술 용어 없이 한국어, 행동 지침 포함
- **취소 가능**: 모든 단계에서 취소 및 일시정지 가능
- **진행 보존**: 브라우저 새로고침 시 이전 상태 복원

---

## 13. 성능 목표 및 SLA

### 13.1 응답 시간 목표

| 작업 | P50 | P95 | P99 |
|---|---|---|---|
| 파일 업로드 완료 | 2s | 5s | 10s |
| 첫 중간 산출물 표시 | 8s | 15s | 20s |
| 프로필 생성 완료 | 40s | 60s | 90s |
| 변환 적용 (캐시 사용) | 20s | 30s | 45s |
| PDF 다운로드 시작 | 1s | 3s | 5s |

### 13.2 품질 목표

| 지표 | 목표 |
|---|---|
| B 형식 레이아웃 일치율 | 95% 이상 |
| 콘텐츠 팩트 보존율 | 90% 이상 |
| Human Checkpoint 없이 완료율 | 70% 이상 |
| 전체 파이프라인 성공률 | 92% 이상 |

### 13.3 인프라 SLA

| 항목 | 목표 |
|---|---|
| 서비스 가용성 | 99.5% / 월 |
| Gemini API 오류 시 복구 | 자동 재시도 30초 이내 |
| 파일 보관 기간 | 처리 완료 후 24시간 |

---

## 14. 보안 설계

### 14.1 데이터 보호

```
전송 구간:
  - 모든 통신 HTTPS/WSS 강제
  - 파일 업로드: Signed URL (GCS) 사용, 직접 서버 경유 없음

저장 구간:
  - GCS 파일: 서버 측 암호화 (AES-256)
  - DB: 민감 필드 암호화 (pgcrypto)

보관 정책:
  - 입력 파일: 처리 완료 후 24시간 뒤 자동 삭제
  - 출력 파일: 다운로드 후 또는 72시간 뒤 삭제
  - 변환 프로필: 사용자가 삭제 시 즉시 제거
  - 로그: 개인 식별 정보 마스킹 후 30일 보관
  - PII 토큰맵: 작업 완료 즉시 Redis에서 삭제 (최대 TTL 1시간)
```

### 14.2 PII 처리 파이프라인

```
원칙: 외부 AI API에는 PII가 전송되지 않는다.

흐름:
  [사용자 업로드]
      ↓
  [Stage 1.5: 서버 로컬에서 PII 마스킹]
      ↓ (마스킹된 텍스트만)
  [Gemini API 호출]
      ↓
  [Stage 7: PII 복원 + Redis 즉시 삭제]
      ↓
  [사용자에게 원본 PII가 포함된 최종 PDF 전달]

감사:
  PII 감지 건수, 마스킹 타입별 통계를 audit_logs에 기록
  (실제 PII 값은 절대 로그에 포함하지 않음)
```

### 14.3 B2B 컴플라이언스 (Google Cloud 엔터프라이즈 약관)

```
API 엔드포인트:
  사용: generativelanguage.googleapis.com  (Google Cloud 경유)
  미사용: generativeai.googleapis.com      (api.google.ai - 소비자용)

Google Cloud 엔터프라이즈 약관 적용 시:
  - 데이터 학습 사용 금지 (Customer Data는 모델 훈련에 사용 불가)
  - 데이터 처리 부칙(DPA) 적용
  - SOC 2, ISO 27001 컴플라이언스 보장

서비스 약관 명시:
  - 개인정보처리방침에 Google Cloud API 사용 및 데이터 비학습 명시
  - B2B 고객 대상 DPA 계약서 제공 가능
```

### 14.4 인증 및 권한

```
인증: JWT (Access Token 1시간, Refresh Token 7일)
권한: 사용자는 자신의 프로필 / 작업만 접근 가능
API: Rate Limiting (IP 기준 분당 30 요청)
파일: 업로드 전 MIME 타입 + 매직 바이트 이중 검증
```

---

## 15. 배포 전략

### 15.1 환경 구성

```
Local:      Docker Compose (Frontend + Backend + PostgreSQL + Redis)
Staging:    Cloud Run (자동 배포, PR 머지 시)
Production: Cloud Run (수동 배포 승인)
```

### 15.2 CI/CD 파이프라인

```
코드 푸시 → GitHub Actions
  → 린트 + 타입 체크
  → 유닛 테스트
  → 프롬프트 변경 감지 시 → Golden Dataset 자동 평가
      → 품질 저하 > 2% 시 → 파이프라인 중단
  → Docker 이미지 빌드
  → Staging 자동 배포
  → E2E 테스트 (핵심 시나리오)
  → [수동 승인] Production 배포
```

### 15.3 모니터링

```
에러 추적:     Sentry
API 모니터링:  Google Cloud Monitoring
로그:         Cloud Logging
알림:         Slack (에러율 > 5% 시 즉시 알림)

LLMOps 대시보드 (별도 구성):
  - 스테이지별 평균 신뢰도 추이
  - 환각 감지율 (grounding_score 분포)
  - 프롬프트 버전별 품질 비교
  - 모델별 비용 추이 (stage_results.cost_usd 집계)
  - Rate Limiter 사용률 (RPM/TPM 소비 현황)
  - WeasyPrint vs Playwright 렌더러 사용 비율
```

---

## 16. 개발 마일스톤

### Phase 1: 기반 구축 (Week 1~2)

- [ ] 프로젝트 초기 세팅 (Docker, DB, 디렉토리 구조)
- [ ] PostgreSQL 스키마 마이그레이션 (prompt_versions 테이블 포함)
- [ ] FastAPI 기본 라우터 + WebSocket
- [ ] Redis Pub/Sub 기반 WebSocket 이벤트 브로드캐스팅
- [ ] GCS 파일 업로드 연동
- [ ] Celery + Redis 작업 큐 구성
- [ ] Global Rate Limiter (Token Bucket) 구현

### Phase 2: AI 파이프라인 (Week 3~4)

- [ ] Stage 1: 입력 검증 + PyMuPDF 전처리
- [ ] Stage 1.5: PII 감지 + 마스킹 파이프라인
- [ ] Stage 2: A 콘텐츠 추출 (Gemini Pro/Flash)
- [ ] Stage 3: B 템플릿 분석 + 복잡도 스코어 + 렌더러 결정
- [ ] 컨텍스트 캐싱 연동
- [ ] 프롬프트 버전 관리 디렉토리 구조 + DB 연동

### Phase 3: 매핑 + 검증 (Week 5)

- [ ] Stage 4: 앙상블 매핑 로직
- [ ] 신뢰도 스코어 시스템
- [ ] Stage 7: 형식 검증 + 픽셀 Diff

### Phase 4: 문서 생성 (Week 6)

- [ ] Stage 5: 콘텐츠 생성 (변환 타입별) + Self-RAG 환각 검증
- [ ] Stage 6: WeasyPrint 조립 + Playwright 폴백
- [ ] Stage 7: PII 복원 로직
- [ ] 부분 완료 + 재시도 로직
- [ ] stage_results 비용 추적 (토큰 수, cost_usd)

### Phase 5: 프론트엔드 (Week 7)

- [ ] 업로드 화면 + 파일 미리보기
- [ ] 실시간 파이프라인 UI (WebSocket)
- [ ] Human Checkpoint #1 (매핑 다이어그램)
- [ ] Human Checkpoint #2 (시각적 Diff)
- [ ] PDF 다운로드

### Phase 6: 안정화 (Week 8~9)

- [ ] Golden Dataset 30개 쌍 구축
- [ ] CI/CD에 Golden Dataset 자동 평가 통합
- [ ] E2E 테스트 (핵심 시나리오 5개)
- [ ] 성능 테스트 + 병목 최적화
- [ ] PII 마스킹 보안 점검 (감사 로그 확인)
- [ ] 에러 처리 강화
- [ ] LLMOps 대시보드 구성
- [ ] 배포 파이프라인 구성

---

## 17. 리스크 분석

| 리스크 | 확률 | 영향도 | 대응 방안 |
|---|---|---|---|
| B 이미지 품질 불량으로 템플릿 추출 실패 | 높음 | 높음 | 품질 사전 검증 + 재업로드 유도 |
| Gemini API 비용 예상 초과 | 중간 | 중간 | 컨텍스트 캐싱 + Flash 우선 + 비용 추적 알림 |
| 35페이지+ 대용량 문서 처리 지연 | 중간 | 중간 | PyMuPDF 전처리로 토큰 절감 |
| 매핑 정확도 목표(90%) 미달 | 중간 | 높음 | 앙상블 + Human Checkpoint 보완 |
| WeasyPrint 복잡한 레이아웃 렌더링 실패 | 중간 | 높음 | 복잡도 스코어 기반 Playwright 자동 폴백 |
| 프롬프트 변경 후 기존 품질 회귀 | 중간 | 높음 | Golden Dataset CI/CD 자동 평가로 배포 차단 |
| WebSocket 다중 인스턴스 세션 유실 | 중간 | 중간 | Redis Pub/Sub 브로드캐스팅으로 해결 |
| PII 마스킹 누락으로 개인정보 API 전송 | 낮음 | 매우 높음 | 정규식 다중 패턴 + 감사 로그 검증 |
| Self-RAG 검증이 합법적 내용을 환각으로 오분류 | 중간 | 중간 | grounding_score 임계값 조정 + 사용자 최종 확인 |
| Celery Worker Rate Limit 동시 초과 | 낮음 | 중간 | Token Bucket 글로벌 Rate Limiter로 사전 차단 |
| Gemini API 장애 | 낮음 | 높음 | 지수 백오프 + 상태 보존 재시도 |

---

*본 설계서는 개발 진행에 따라 업데이트됩니다.*
