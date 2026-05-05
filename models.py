from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, relationship
import datetime

Base = declarative_base()


class Doctor(Base):
    """
    Врач/клиент. Может быть создан двумя путями:
      1) Excel-загрузкой админа — тогда telegram_id = 'PENDING-<phone>'
         до того момента, пока клиент сам не зайдёт в бот и не отправит телефон.
      2) Регистрацией через бот — клиент шлёт контакт, его телефон ищется
         в БД, при совпадении PENDING-запись «оживает» (telegram_id обновляется).
    """
    __tablename__ = 'doctors'

    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True, nullable=False, index=True)
    doctor_id = Column(String, unique=True, nullable=True)
    full_name = Column(String, nullable=True)
    phone = Column(String, nullable=True, index=True)
    address = Column(String, nullable=True)
    doctor_code = Column(String, nullable=True)
    specialty = Column(String, nullable=True)
    current_balance = Column(Float, default=0.0)
    monthly_bonus = Column(Float, default=0.0)
    yearly_bonus = Column(Float, default=0.0)
    last_update = Column(DateTime, default=datetime.datetime.utcnow)
    language = Column(String, default='ru')


class BonusUpload(Base):
    """Метаданные одной загрузки Excel-файла админом."""
    __tablename__ = 'bonus_uploads'

    id = Column(Integer, primary_key=True)
    file_name = Column(String, nullable=True)
    uploaded_by = Column(String, nullable=True)
    uploaded_at = Column(DateTime, default=datetime.datetime.utcnow)
    total_rows = Column(Integer, default=0)
    unique_clients = Column(Integer, default=0)
    notified_count = Column(Integer, default=0)
    pending_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)

    entries = relationship('BonusEntry', back_populates='upload', cascade='all, delete-orphan')


class BonusEntry(Base):
    """Одна строка из Excel-файла, сохранённая «как есть»."""
    __tablename__ = 'bonus_entries'

    id = Column(Integer, primary_key=True)
    upload_id = Column(Integer, ForeignKey('bonus_uploads.id'), index=True)
    row_date = Column(DateTime, nullable=True)
    organization = Column(String, nullable=True)
    manager = Column(String, nullable=True)
    oblast = Column(String, nullable=True)
    region = Column(String, nullable=True)
    object_name = Column(String, nullable=True)
    group_name = Column(String, nullable=True)
    client_name = Column(String, nullable=True)
    phone = Column(String, nullable=True, index=True)
    specialty = Column(String, nullable=True)
    amount = Column(Float, default=0.0)
    doctor_ref_id = Column(Integer, ForeignKey('doctors.id'), nullable=True)

    upload = relationship('BonusUpload', back_populates='entries')
