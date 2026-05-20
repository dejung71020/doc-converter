from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    """
    모든 SQLAlchemy 모델의 베이스 클래스.
    이 클래스를 상속 받은 모델을 모두 Alemibc에 넘겨서
    마이그레이션 파일을 생성한다.
    """
    pass