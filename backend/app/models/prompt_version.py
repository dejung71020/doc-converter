import uuid
from datetime import datetime
from sqlalchemy import String, Text, Boolean, Integer, Numeric, TIMESTAMP, func, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

class PromptVersion(Base):
    """
    프롬프트 버전 관리 모델.
    각 파이프라인 스테이지에서 사용하는 프롬프트를 코드처럼 버전 관리한다.
    프롬프트 변경 시 Golden Dataset 평가 점수를 기록하고,
    품질 저하가 감지되면 is_active를 이전 버전으로 롤백한다.
    """
    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("stage_name", "version", name="uq_stage_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    stage_name: Mapped[str] = mapped_column(
        String(100), nullable=False,
        comment="적용 대상 스테이지 예 : stage2_extraction, stage5_generation"
    )
    version: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="프롬프트 버전 예 : v1.0.0"
    )
    content: Mapped[str]= mapped_column(
        Text, nullable=False,
        comment="실제 프롬프트 내용"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False,
        comment="현재 프로덕션에서 사용 중인 버전 여부. 스테이지 당 1개만 True"
    )
    golden_score: Mapped[float | None] = mapped_column(
        Numeric(5, 2), nullable=True,
        comment="Golden Dataset 기준 평균 품질 점수. 이전 버전 대비 2% 이상 저하 시 배포 차단"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
        comment="is_active가 True로 변경된 시각"
    )
