from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Text, UniqueConstraint
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


class BonusAck(Base):
    """One row per click of «🙏 Спасибо!» on a bonus notification."""
    __tablename__ = 'bonus_acks'

    id = Column(Integer, primary_key=True)
    doctor_ref_id = Column(Integer, ForeignKey('doctors.id'), nullable=True, index=True)
    telegram_id = Column(String, index=True)
    message_id = Column(Integer, index=True, nullable=True)
    acked_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)


class FeedbackMessage(Base):
    """One row per feedback message sent through the «💬 Обратная связь» flow."""
    __tablename__ = 'feedback_messages'

    id = Column(Integer, primary_key=True)
    doctor_ref_id = Column(Integer, ForeignKey('doctors.id'), nullable=True, index=True)
    telegram_id = Column(String, index=True)
    full_name = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    text = Column(Text, nullable=False)
    sent_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)


class BroadcastHistory(Base):
    """One row per broadcast launched from the admin panel."""
    __tablename__ = 'broadcast_history'

    id = Column(Integer, primary_key=True)
    sent_by = Column(String, nullable=True, index=True)   # admin telegram_id
    sent_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    text = Column(Text, nullable=False)
    target_count = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    status = Column(String, default='running')            # 'running' | 'completed' | 'cancelled'
    finished_at = Column(DateTime, nullable=True)


class Survey(Base):
    """One survey = a set of N questions broadcast to doctors."""
    __tablename__ = 'surveys'

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=True)
    sent_by = Column(String, nullable=True, index=True)  # admin telegram_id
    sent_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    status = Column(String, default='draft')  # draft | running | completed | cancelled
    target_count = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    finished_at = Column(DateTime, nullable=True)

    questions = relationship('SurveyQuestion', back_populates='survey',
                             cascade='all, delete-orphan',
                             order_by='SurveyQuestion.order')


class SurveyQuestion(Base):
    """One question within a survey."""
    __tablename__ = 'survey_questions'

    id = Column(Integer, primary_key=True)
    survey_id = Column(Integer, ForeignKey('surveys.id'), index=True)
    order = Column(Integer, default=0)  # 1-based position
    text = Column(Text, nullable=False)

    survey = relationship('Survey', back_populates='questions')


class SurveyResponse(Base):
    """One row per (survey, doctor) — tracks the doctor's progress through the questions."""
    __tablename__ = 'survey_responses'
    __table_args__ = (UniqueConstraint('survey_id', 'doctor_ref_id', name='uq_survey_response_per_doctor'),)

    id = Column(Integer, primary_key=True)
    survey_id = Column(Integer, ForeignKey('surveys.id'), index=True)
    doctor_ref_id = Column(Integer, ForeignKey('doctors.id'), nullable=True, index=True)
    telegram_id = Column(String, index=True)
    started_at = Column(DateTime, default=datetime.datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    status = Column(String, default='in_progress')  # in_progress | completed | cancelled
    current_question_idx = Column(Integer, default=1)  # 1-based; doctor is awaiting answer to Q[current_question_idx]


class SurveyAnswer(Base):
    """One answer from a doctor to a specific question."""
    __tablename__ = 'survey_answers'
    __table_args__ = (UniqueConstraint('response_id', 'question_id', name='uq_survey_answer'),)

    id = Column(Integer, primary_key=True)
    response_id = Column(Integer, ForeignKey('survey_responses.id'), index=True)
    question_id = Column(Integer, ForeignKey('survey_questions.id'), index=True)
    text = Column(Text, nullable=True)
    answered_at = Column(DateTime, default=datetime.datetime.utcnow)
