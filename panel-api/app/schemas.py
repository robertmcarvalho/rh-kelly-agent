from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Optional, List


class LeadUpsertRequest(BaseModel):
    phone: str = Field(..., min_length=8, max_length=32)
    name: Optional[str] = None
    email: Optional[str] = None
    city: Optional[str] = None
    source: Optional[str] = None


class LeadResponse(BaseModel):
    id: int
    phone: str
    name: Optional[str] = None
    email: Optional[str] = None
    city: Optional[str] = None
    step: Optional[str] = None
    status: Optional[str] = None
    form_token: Optional[str] = None


class LeadsListResponse(BaseModel):
    items: List[LeadResponse]
    total: int


class SignedUrlRequest(BaseModel):
    lead_id: int
    kind: str  # CNH, CRLV, COMPROVANTE, ANTECEDENTES
    filename: str
    mode: str = "upload"  # upload|download
    content_type: Optional[str] = None


class SignedUrlResponse(BaseModel):
    url: str
    method: str
    expires_in: int
    object_name: str

