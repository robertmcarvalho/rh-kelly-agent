from __future__ import annotations

import json
from datetime import timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Header
from sqlalchemy import select

from .config import settings
from .db import session_scope, _engine
from .models import Base, Lead, Event
from .schemas import LeadUpsertRequest, LeadResponse, LeadsListResponse, SignedUrlRequest, SignedUrlResponse
from .utils import gen_token

app = FastAPI(title="CoopMob Panel API", version="0.1.0")


# Initialize tables lazily if engine exists (dev bootstrap). In prod, prefer migrations.
if _engine is not None:
    Base.metadata.create_all(_engine)


def _auth_guard(authorization: Optional[str] = Header(default=None)):
    tok = settings.internal_api_token
    if not tok:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    if authorization.split(" ", 1)[1] != tok:
        raise HTTPException(status_code=403, detail="Invalid token")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "db": bool(_engine is not None),
        "bucket": settings.gcs_bucket,
    }


@app.post("/api/leads", response_model=LeadResponse)
def upsert_lead(payload: LeadUpsertRequest):
    phone = payload.phone.strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone required")
    with session_scope() as s:
        lead = s.execute(select(Lead).where(Lead.phone == phone)).scalar_one_or_none()
        created = False
        if not lead:
            lead = Lead(phone=phone)
            created = True
        if payload.name:
            lead.name = payload.name
        if payload.email:
            lead.email = payload.email
        if payload.city:
            lead.city = payload.city
        if payload.source:
            lead.source = payload.source
        if not lead.form_token:
            lead.form_token = gen_token(16)
        s.add(lead)
        s.flush()

        ev = Event(actor="system", kind="lead_created" if created else "lead_updated", lead_id=lead.id,
                   payload=json.dumps(payload.model_dump(), ensure_ascii=False))
        s.add(ev)
        s.flush()

        return LeadResponse(
            id=lead.id,
            phone=lead.phone,
            name=lead.name,
            email=lead.email,
            city=lead.city,
            step=lead.step,
            status=lead.status,
            form_token=lead.form_token,
        )


@app.get("/api/leads", response_model=LeadsListResponse)
def list_leads(city: Optional[str] = None, status: Optional[str] = None, q: Optional[str] = None, limit: int = 50, offset: int = 0):
    with session_scope() as s:
        stmt = select(Lead)
        if city:
            stmt = stmt.where(Lead.city == city)
        if status:
            stmt = stmt.where(Lead.status == status)
        if q:
            like = f"%{q}%"
            from sqlalchemy import or_
            stmt = stmt.where(or_(Lead.name.ilike(like), Lead.phone.ilike(like), Lead.email.ilike(like)))
        total = s.execute(stmt).scalars().all()
        items = total[offset:offset+limit]
        return LeadsListResponse(
            total=len(total),
            items=[LeadResponse(
                id=x.id, phone=x.phone, name=x.name, email=x.email, city=x.city, step=x.step, status=x.status, form_token=x.form_token
            ) for x in items]
        )


# Signed URL helper (uses ADC in prod). If credentials missing, raise 500 with hint.
def _signed_url(object_name: str, method: str = "PUT", content_type: Optional[str] = None, expires_in: int = 15*60) -> str:
    from google.cloud import storage

    if not settings.gcs_bucket:
        raise HTTPException(status_code=500, detail="GCS_BUCKET not configured")

    client = storage.Client()
    bucket = client.bucket(settings.gcs_bucket)
    blob = bucket.blob(object_name)
    url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=expires_in),
        method=method,
        content_type=content_type if method.upper() == "PUT" else None,
    )
    return url


@app.post("/api/upload/signed-url", response_model=SignedUrlResponse, dependencies=[Depends(_auth_guard)])
def signed_url_endpoint(req: SignedUrlRequest):
    object_name = f"leads/{req.lead_id}/{req.kind}/{req.filename}"
    method = "PUT" if req.mode == "upload" else "GET"
    url = _signed_url(object_name=object_name, method=method, content_type=req.content_type)
    return SignedUrlResponse(url=url, method=method, expires_in=15*60, object_name=object_name)

