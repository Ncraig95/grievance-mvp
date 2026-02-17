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


class ResendNotificationRequest(BaseModel):
    template_key: str = Field(..., description="Template key, for example completion_internal")
    idempotency_key: str = Field(..., description="Client-controlled idempotency key for resend")
    recipients: list[str] | None = Field(default=None, description="Optional explicit recipient override list")
    context_overrides: dict[str, str] = Field(default_factory=dict)


class ResendNotificationResult(BaseModel):
    recipient_email: str
    status: str
    deduped: bool
    graph_message_id: str | None = None
    resend_count: int
