import uuid
from datetime import datetime
from sqlalchemy import String, TIMESTAMP, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

class AuditLog(Base):
    """
    감사 로그 모델.
    PII 마스킹/복원, 파일 삭제 등 보안 관련 이벤트를 기록한다.
    실제 PII 값은 절대 이 테이블에 저장하지 않으며,
    처리 건수와 타입만 기록해 개인정보를 보호한다.
    """
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversion_jobs.id"),
        nullable=True,
        comment="연관된 변환 작업 ID. 시스템 이벤트는 NULL"
    )
    event_type: Mapped[str] = mapped_column(
        String(100), nullable=False,
        comment="이벤트 종류 예: pii_masked, pii_restored, file_deleted, job_failed"
    )
    detail: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment="이벤트 상세 정보. PII 실제 값 포합 금지. 건수/타입만 기록"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )