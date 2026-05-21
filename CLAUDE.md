# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## 프로젝트 개요

사용자가 입력 문서(A)와 목표 형식 문서(B)를 업로드하면, AI가 A의 내용을 분석하고 B의 형식에 맞게 자동 변환하는 플랫폼. 변환 프로필을 저장해 이후 동일 형식 변환에 재사용한다.

전체 설계는 `DESIGN.md`(v1.1.0) 참고.

---

## 기술 스택

| 레이어 | 기술 |
|---|---|
| Frontend | Next.js 14 (App Router), TypeScript, TailwindCSS |
| Backend | Python FastAPI (async), Celery 5.x |
| AI | Gemini 1.5 Pro (정확도), Gemini 1.5 Flash (속도) |
| PDF 처리 | PyMuPDF (텍스트 추출), pdfplumber (레이아웃) |
| 문서 생성 | WeasyPrint → Playwright 폴백 (복잡 레이아웃) |
| DB | PostgreSQL 16 (JSONB 활용) |
| Cache / Queue | Redis 7 (캐시 + Celery 브로커 + Pub/Sub + Rate Limiter) |
| 파일 저장소 | Google Cloud Storage |
| 배포 | Google Cloud Run (Docker) |

---

## 프로젝트 구조 (계획)

```
doc-converter/
├── backend/
│   ├── app/
│   │   ├── api/          # FastAPI 라우터 (v1/)
│   │   ├── pipeline/     # Stage 1~7 파이프라인 엔진
│   │   │   ├── stage1_validation.py
│   │   │   ├── stage1_5_pii.py       # PII 마스킹/복원
│   │   │   ├── stage2_extraction.py
│   │   │   ├── stage3_template.py
│   │   │   ├── stage4_mapping.py     # 앙상블 매핑
│   │   │   ├── stage5_generation.py  # Self-RAG 포함
│   │   │   ├── stage6_assembly.py    # WeasyPrint/Playwright
│   │   │   └── stage7_validation.py
│   │   ├── ai/
│   │   │   ├── gemini_client.py      # Gemini API 래퍼
│   │   │   └── rate_limiter.py       # Token Bucket (Redis)
│   │   ├── models/       # SQLAlchemy ORM 모델
│   │   ├── schemas/      # Pydantic 스키마
│   │   └── workers/      # Celery 태스크
│   ├── prompts/          # 프롬프트 버전 관리
│   │   ├── stage2_extraction/
│   │   │   └── v1.0.0.txt
│   │   └── ...
│   ├── golden_dataset/   # LLMOps 회귀 테스트용 (git-lfs)
│   └── tests/
├── frontend/
│   ├── app/              # Next.js App Router
│   ├── components/
│   │   ├── pipeline/     # 파이프라인 진행 UI
│   │   └── checkpoints/  # Human Checkpoint #1, #2 UI
│   └── lib/
└── docker-compose.yml
```

---

## 핵심 아키텍처 결정사항

### 파이프라인 (Stage 1 → 7)

각 Stage는 독립적으로 재시도 가능하다. 전체 파이프라인 재시작 없이 실패한 Stage만 재처리한다.

```
Stage 1   → 입력 검증 + PyMuPDF 전처리
Stage 1.5 → PII 감지 + 마스킹 (Gemini 전송 전 필수)
Stage 2   → A 콘텐츠 추출           ┐ 병렬 실행
Stage 3   → B 템플릿 분석            ┘ (복잡도 스코어 → 렌더러 결정)
Stage 4   → 의미론적 매핑 (Flash + Pro 앙상블)
Stage 5   → 콘텐츠 생성 + Self-RAG 환각 검증
Stage 6   → 문서 조립 (WeasyPrint, 실패 시 Playwright 자동 폴백)
Stage 7   → 형식 검증 + PII 복원
```

### WebSocket 실시간 이벤트 (멀티 인스턴스 대응)

Cloud Run 다중 인스턴스 환경에서 세션 유실 방지를 위해 Redis Pub/Sub 사용.

```python
# Worker: 이벤트 발행
redis.publish(f"job:{job_id}:events", json.dumps(event))

# FastAPI WS 핸들러: 구독 후 클라이언트 전달
await redis.subscribe(f"job:{job_id}:events")
```

### Gemini API 호출 규칙

모든 Gemini 호출은 반드시 `rate_limiter.acquire(model, tokens)` 통과 후 실행.
Token Bucket은 Redis에서 글로벌하게 관리 (분당 RPM + TPM 이중 체크).

```python
# 작업별 모델 할당 원칙
PDF 텍스트 추출  → PyMuPDF (AI 호출 없음)
B 레이아웃 분석  → gemini-1.5-pro (정확도 필수)
매핑 앙상블     → Flash 1회 + Pro 1회 → 비교 후 Pro 채택
콘텐츠 생성     → gemini-1.5-flash (속도)
저신뢰도 재생성  → gemini-1.5-pro
형식 검증       → gemini-1.5-pro
```

### PII 처리 원칙

**외부 AI API에는 PII가 전송되지 않는다.** Stage 1.5에서 마스킹, Stage 7에서 복원.
토큰맵은 Redis에만 저장 (TTL 1시간), DB에는 절대 기록하지 않는다.

### Self-RAG (Stage 5 환각 검증)

생성된 각 문장을 Pro에게 재검토시켜 원문 근거 확인. `grounded: false` 문장은 자동 삭제 후 재생성.
`grounding_score < 80%` 시 사용자 경고.

### Stage 3 B 템플릿 분석 전략

B 파일 타입에 따라 처리 경로가 분기된다:
- **B가 PDF**: pdfplumber로 폰트명/좌표 직접 추출(정확) + 1페이지 Vision 보완
- **B가 이미지**: Gemini Pro Vision 단독 분석 + Google Fonts 유사 폰트 3개 추정

**자기검증 루프 (Self-Verification Loop):**
```
JSON 추출 → HTML 렌더링(Playwright) → 원본 B와 픽셀 Diff 측정
→ 유사도 95% 미만 → 차이 영역 크롭 → Gemini 재분석 → JSON 보정
최대 2회 반복 → 비주얼 에디터로 사용자 전달
```

### 렌더러 선택 (Stage 3 → Stage 6)

complexity_score 기반 자동 결정:
- 스코어 0~40: WeasyPrint
- 스코어 40~70: WeasyPrint + 경고
- 스코어 70+: Playwright (headless Chrome)
- WeasyPrint 런타임 실패 시 → Playwright 자동 폴백

---

## 데이터베이스 핵심 테이블

- `conversion_profiles` — B 템플릿 HTML/CSS + 필드 스키마 저장
- `conversion_jobs` — 변환 작업 상태 관리
- `stage_results` — 스테이지별 결과, 신뢰도, **토큰 수 + 비용(cost_usd)**, hallucination_flags
- `prompt_versions` — 프롬프트 버전 관리 (is_active로 프로덕션 버전 지정)
- `audit_logs` — PII 처리 이벤트 등 감사 로그 (PII 실제 값은 포함 금지)

신뢰도 기준: `>= 90%` 자동 통과 / `50~70%` Pro 재시도 / `< 50%` 사용자 개입.

---

## GitHub 워크플로우

```
브랜치 전략:
  main     ← 항상 배포 가능 상태
  develop  ← 통합 브랜치
  feature/이슈번호-설명  ← 기능 개발

커밋 컨벤션:
  feat(scope):     새 기능       예) feat(pipeline): add Stage 1.5 PII masking (#2)
  fix(scope):      버그 수정
  docs:            문서
  chore:           설정/패키지
  test:            테스트
  refactor:        리팩토링

PR 규칙:
  - Issue 번호 반드시 연결 (closes #N)
  - develop 브랜치로 머지
  - main은 Phase 완료 시점에만 머지
```

Milestone은 Phase 1~6 단위로 관리. 각 Issue는 해당 Milestone에 연결.

---

## LLMOps

`prompts/` 디렉토리의 프롬프트는 코드처럼 버전 관리한다.
프롬프트 변경 시 `golden_dataset/`으로 회귀 테스트 실행 후 품질 저하 > 2% 시 배포 차단.
`stage_results.prompt_version`에 항상 사용 버전 기록.

---

## 성능 목표 (SLA)

| 작업 | P95 목표 |
|---|---|
| 프로필 생성 | 60초 |
| 변환 적용 (캐시) | 30초 |
| B 형식 일치율 | 95% 이상 |
| 콘텐츠 팩트 보존율 | 90% 이상 |
