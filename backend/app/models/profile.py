import uuid
from datetime import datetime
from sqlalchemy import String, Text, Integer, Numeric, TIMESTAMP, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

class ConversionProfile(Base):
    """
    변환 프로필 모델.
    사용자가 A + B 쌍을 업도르하여 학습시킨 변환 규칙을 저장한다.
    동일한 B 형식으로 반복 변환시에 이 프로필을 재사용하여 API 비용을 절감한다.
    """
    __tablename__ = "conversion_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # B 템플릿 정보
    b_template_html: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Stage 3 에서 추출한 B 문서의 HTML/CSS 템플릿"
    )
    b_schema: Mapped[dict] = mapped_column(
        JSONB, nullable=False,
        comment="B 문서의 필드 구조 스키마 (필드명, 위치, 필수 여부 등)"
    )
    b_file_url: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
        comment="원본 B 파일의 GCS 저장 URL"
    )
    b_type: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="B 파일 형식 : pdf | image"
    )
    layout_complexity_score: Mapped[float | None] = mapped_column(
        Numeric(5, 2), nullable=True,
        comment="Stage 3에서 산출한 레이아웃 복잡도 점수 (렌더러 선택 기준)"
    )
    preferred_renderer: Mapped[str] = mapped_column(
        String(20), default="playwright",
        comment="사용할 렌더러: 현재 항상 playwright"
    )
    font_candidates: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment="B 분석으로 추정한 유사 Google Fonts 후보 3개"
    )
    editor_overrides: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment="비주얼 에디터에서 사용자가 저장한 폰트/레이아웃 수정사항. 다음 변환에 자동 반영"
    )

    # 메타데이터
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_confidence: Mapped[float | None] = mapped_column(
        Numeric(5, 2), nullable=True,
        comment="이 프로필로 변환 시 평균 신뢰도 점수"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
        comment="NULL 이면 영구 보존, 값이 있으면 해당 시각에 자동 삭제"
    )