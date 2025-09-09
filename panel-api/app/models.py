from __future__ import annotations

from datetime import datetime
from sqlalchemy import Column, DateTime, Integer, String, Text, Boolean, ForeignKey
from sqlalchemy.orm import declarative_base, relationship


Base = declarative_base()


class Lead(Base):
    __tablename__ = "leads"
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    name = Column(String(200))
    phone = Column(String(32), unique=True, index=True, nullable=False)
    email = Column(String(200))
    city = Column(String(120))
    source = Column(String(80))
    step = Column(String(80), default="INTRO")
    status = Column(String(80), default="NEW")
    owner = Column(String(120))
    form_token = Column(String(64), unique=True, index=True)

    documents = relationship("Document", back_populates="lead")
    signatures = relationship("Signature", back_populates="lead")


class Document(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), index=True, nullable=False)
    kind = Column(String(40), nullable=False)  # CNH, CRLV, COMPROVANTE, ANTECEDENTES
    gcs_uri = Column(Text)
    status = Column(String(40), default="PENDING")  # PENDING, APPROVED, REJECTED
    notes = Column(Text)
    checked_by = Column(String(120))
    checked_at = Column(DateTime)

    lead = relationship("Lead", back_populates="documents")


class Signature(Base):
    __tablename__ = "signatures"
    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), index=True, nullable=False)
    provider = Column(String(40), default="autentique")
    request_id = Column(String(120), index=True)
    status = Column(String(40), default="CREATED")  # CREATED, SENT, SIGNED, EXPIRED, CANCELED
    sent_at = Column(DateTime)
    signed_at = Column(DateTime)
    expires_at = Column(DateTime)

    lead = relationship("Lead", back_populates="signatures")


class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, default=datetime.utcnow, nullable=False)
    actor = Column(String(80), default="system")  # system|user|agent
    kind = Column(String(80))
    lead_id = Column(Integer, index=True)
    payload = Column(Text)

