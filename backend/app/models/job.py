import uuid
from datetime import datetime
from sqlalchemy import String, Text, Integer, Numeric, TIMESTAMP, func, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

class ConversionJob(Base):
    """
    변환 작업 모델.
    사용자가 새 문서를 업로드하여 변환을 요청할 때마다 생성되는 작업단위.
    파이프라인 전체 생명주기(대기 -> 처리 중 -> 완료/실패)를 추적한다.
    """
    __tablename__ = "conversion_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        ForeignKey("conversion_profiles.id"),
        nullable=False
    )

    # 입력
    input_file_url: Mapped[str] = mapped_column(
        String(500), nullable=False,
        comment="변환할 입력 파일 A의 GCS 저장 URL"
    )
    input_type: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="입력 파일 형식 : pdf | image | docx"
    )

    # 렌더링 품질
    pixel_similarity_score: Mapped[float | None] = mapped_column(
        Numeric(5, 2), nullable=True,
        comment="최종 출력과 B 원본의 픽셀 유사도 (목표 : 98.00% 이상)"
    )
    refinement_attempts: Mapped[int] = mapped_column(
        Integer, default=0,
        comment="자동 정제 루프 실행 횟수 (최대 3회)"
    )

    # 상태
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="queued",
        comment="queued | processing | waiting_input | completed | failed"
    )
    current_stage: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="현재 실행 중인 파이프라인 스테이지 번호 (1-7)"
    )
    progress: Mapped[float] = mapped_column(
        Numeric(5, 2), default=0,
        comment="전체 진행률 0.00 - 100.00"
    )

    # 결과
    result_file_url: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
        comment="생성된 최종 PDF 파일의 GSC 저장 URL"
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 시간
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    # 관계
    profile: Mapped["ConversionProfile"] = relationship(
        "ConversionProfile", back_populates=None
    )
    stage_results: Mapped[list["StageResult"]] = relationship(
        "StageResult", back_populates="job"
    )

    