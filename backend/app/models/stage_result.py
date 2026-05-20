import uuid
from datetime import datetime
from sqlalchemy import String, Text, Integer, Numeric, TIMESTAMP, func, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

class StageResult(Base):
    """
    파이프라인 스테이지 실행 결과 모델.
    Stage 1-7 각각의 실행 결과, 신뢰도, AI 비용, 환각 검증 결과를 기록한다.
    실패한 Stage만 재시도할 때 이전 결과를 참고하며,
    LLMOps 대시보드에서 스테이지별 품질 및 비용 분석에 활용된다.
    """
    __tablename__ = "stage_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversion_jobs.id"),
        nullable=False
    )
    stage_number: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="실행된 스테이지 번호 (1, 1.5는 15로 저장, 2-7)"
    )
    stage_name: Mapped[str] = mapped_column(
        String(100), nullable=False,
        comment="스테이지 이름 예 : stage1_validation, stage5_generation"
    )

    # 실행 결과
    status: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="success | failed | retried"
    )
    confidence: Mapped[float | None] = mapped_column(
        Numeric(5, 2), nullable=True,
        comment="스테이지 결과 신뢰도 0.00 - 100.00"
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, default=0,
        comment="재시도 횟수. 3회 초과 시 사용자 개입 요청"
    )
    model_used: Mapped[str | None] = mapped_column(
        String(100), nullable=True,
        comment="호출한 Gemini 모델명 예 : gemini-1.5-pro"
    )
    prompt_version: Mapped[str | None] = mapped_column(
        String(50), nullable=True,
        comment="사용된 프롬프트 버전 예: v1.0.0. LLMOps 회귀 추적에 활용"
    )

    # API 비용 추적
    input_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        comment="Gemini API 입력 토큰 수"
    )
    output_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        comment="Gemini API 출력 토큰 수"
    )
    cost_usd: Mapped[float | None] = mapped_column(
        Numeric(10, 4), nullable=True,
        comment="이 스테이지에서 소모된 API 비용 (달러)"
    )

    # 환각 검증 결과 (Stage 5 전용)
    hallucination_flags: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment="Self-RAG 검증 결과. {sentence, grounded, soruce} 목록"
    )
    grounding_score: Mapped[float | None] = mapped_column(
        Numeric(5, 2), nullable=True,
        comment="원문 근거 보존율. 80% 미만 시 사용자 경고"
    )

    # 입출력 데이터
    input_summary: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment="이 스테이지의 입력 요약 (디버깅 및 재시도 참고용)"
    )
    output_data: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment="이 스테이지의 산출물"
    )
    error_detail: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment="실패 시 에러 상세 정보"
    )
    duration_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        comment="스테이지 실행 시간 (밀리초)"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )

    # 관계
    job: Mapped["ConversionJob"] = relationship(
        "ConversionJob", back_populates="stage_results"
    )