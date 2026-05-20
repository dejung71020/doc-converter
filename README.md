# Doc Converter

AI 기반 문서 형식 변환 플랫폼. 입력 문서(A)와 목표 형식(B)을 업로드하면 AI가 A의 내용을 분석해 B의 형식에 맞게 자동 변환한다. 변환 프로필을 저장해 동일한 형식 변환을 반복 적용할 수 있다.

---

## 주요 기능

- **자유로운 형식 변환** — PDF, Word, 이미지 등 다양한 입력을 어떤 목표 형식으로든 변환
- **B 형식 완벽 일치** — Playwright 기반 렌더링으로 픽셀 수준 레이아웃 재현 (목표: 98%)
- **비주얼 에디터** — AI가 처리하지 못한 부분(폰트, 여백 등)을 클릭으로 직접 수정
- **실시간 파이프라인** — WebSocket으로 변환 진행 상황을 단계별로 실시간 표시
- **환각 검증 (Self-RAG)** — 생성된 내용이 원문에 근거하는지 AI가 자동 검증
- **PII 보호** — 개인정보를 마스킹한 후 AI API에 전송, 최종 출력에서 복원
- **변환 프로필 재사용** — 한 번 학습한 A→B 변환 규칙을 저장해 반복 적용

---

## 기술 스택

| 레이어 | 기술 |
|---|---|
| Frontend | Next.js 14, TypeScript, TailwindCSS |
| Backend | Python FastAPI, Celery |
| AI | Gemini 1.5 Pro / Flash |
| 문서 처리 | PyMuPDF, pdfplumber, python-docx |
| 렌더링 | Playwright (기본), WeasyPrint |
| DB | PostgreSQL 16 |
| Cache / Queue | Redis 7 (Pub/Sub + Rate Limiter + Celery 브로커) |
| 파일 저장소 | Google Cloud Storage |
| 배포 | Google Cloud Run (Docker) |

---

## 로컬 실행 방법

### 사전 요구사항
- Docker Desktop
- Node.js 20+
- Python 3.12+

### 설치 및 실행

```bash
# 1. 저장소 클론
git clone https://github.com/dejung71020/doc-converter.git
cd doc-converter

# 2. 환경변수 설정
cp .env.example .env
# .env 파일에서 GEMINI_API_KEY, SECRET_KEY 등 입력

# 3. Docker 실행
docker-compose up -d --build

# 4. DB 마이그레이션
docker-compose exec backend alembic upgrade head
```

### 접속
| 서비스 | URL |
|---|---|
| Frontend | http://localhost:3000 |
| Backend API | http://localhost:8000/api/docs |
| Health Check | http://localhost:8000/api/v1/health |

---

## 프로젝트 구조

```
doc-converter/
├── backend/
│   ├── app/
│   │   ├── api/v1/          # FastAPI 라우터
│   │   ├── pipeline/        # Stage 1~7 파이프라인
│   │   ├── ai/              # Gemini 클라이언트, Rate Limiter
│   │   ├── models/          # SQLAlchemy ORM 모델
│   │   ├── schemas/         # Pydantic 스키마
│   │   └── workers/         # Celery 태스크
│   ├── alembic/             # DB 마이그레이션
│   └── prompts/             # 프롬프트 버전 관리
├── frontend/
│   └── app/                 # Next.js App Router
├── docker-compose.yml
├── DESIGN.md                # 전체 기술 설계서
└── CLAUDE.md                # Claude Code 가이드
```

---

## 파이프라인 구조

```
Stage 1   → 입력 검증 + 전처리 (PDF/Word/이미지)
Stage 1.5 → PII 감지 + 마스킹
Stage 2   → A 콘텐츠 추출           ┐ 병렬
Stage 3   → B 템플릿 분석            ┘
Stage 4   → 의미론적 매핑 (Flash + Pro 앙상블)
Stage 5   → 콘텐츠 생성 + Self-RAG 환각 검증
Stage 6   → 문서 조립 (Playwright 렌더링)
Stage 7   → 형식 검증 + PII 복원
```

---

## 개발 진행 상황

### ✅ Phase 1: 기반 구축 (완료)
- [x] Docker + 프로젝트 구조 설정
- [x] PostgreSQL 스키마 마이그레이션 (5개 테이블)
- [x] FastAPI 라우터 + DB 세션 연결
- [x] Redis Pub/Sub WebSocket 브로드캐스팅
- [x] Celery 작업 큐 구성
- [x] Global Rate Limiter (Token Bucket, 롤백 포함)

### 🔄 Phase 2: AI 파이프라인 (진행 예정)
- [ ] Stage 1: 입력 검증 + PyMuPDF/python-docx 전처리
- [ ] Stage 1.5: PII 감지 + 마스킹
- [ ] Stage 2: A 콘텐츠 추출 (Gemini Pro)
- [ ] Stage 3: B 템플릿 분석 + 복잡도 스코어
- [ ] GCS 파일 업로드 연동

### ⬜ Phase 3: 매핑 + 검증
### ⬜ Phase 4: 문서 생성
### ⬜ Phase 5: 프론트엔드
### ⬜ Phase 6: 안정화 + LLMOps

---

## 설계 문서

전체 기술 설계는 [DESIGN.md](./DESIGN.md) 참고.
