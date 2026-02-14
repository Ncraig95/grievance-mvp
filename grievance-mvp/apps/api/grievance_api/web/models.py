from __future__ import annotations

from pydantic import BaseModel, Field

class IntakeRequest(BaseModel):
    request_id: str = Field(..., description="Client-generated idempotency key")
    contract: str = Field(..., description="AT&T or COJ (or other)")
    grievant_firstname: str
    grievant_lastname: str
    grievant_email: str
    grievant_phone: str | None = None

    work_location: str | None = None
    supervisor: str | None = None
    incident_date: str | None = None
    narrative: str

class IntakeResponse(BaseModel):
    grievance_id: str
    status: str
    signing_link: str | None = None
