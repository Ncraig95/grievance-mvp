from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DocumentRequest(BaseModel):
    doc_type: str
    template_key: str | None = None
    requires_signature: bool = False
    signers: list[str] | None = None


class IntakeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    request_id: str = Field(..., description="Client-generated idempotency key")
    grievance_id: str | None = Field(default=None, description="External grievance identifier such as 2026001")
    grievance_number: str | None = None
    contract: str = Field(..., description="AT&T or COJ (or other)")
    grievant_firstname: str
    grievant_lastname: str
    grievant_email: str
    grievant_phone: str | None = None
    work_location: str | None = None
    supervisor: str | None = None
    incident_date: str | None = None
    narrative: str
    template_data: dict[str, object] = Field(default_factory=dict, description="Optional template merge fields")
    documents: list[DocumentRequest] = Field(default_factory=list)


class DocumentStatus(BaseModel):
    document_id: str
    doc_type: str
    status: str
    signing_link: str | None = None


class IntakeResponse(BaseModel):
    case_id: str
    grievance_id: str
    status: str
    documents: list[DocumentStatus]


class CaseStatusResponse(BaseModel):
    case_id: str
    grievance_id: str
    status: str
    approval_status: str
    grievance_number: str | None = None
    documents: list[DocumentStatus]


class ResendNotificationRequest(BaseModel):
    template_key: str = Field(..., description="Template key, e.g. reminder_signature")
    idempotency_key: str = Field(..., description="Client-controlled idempotency key for resend")
    document_id: str | None = None
    recipients: list[str] | None = None
    context_overrides: dict[str, str] = Field(default_factory=dict)


class ResendNotificationResult(BaseModel):
    recipient_email: str
    status: str
    deduped: bool
    graph_message_id: str | None = None
    resend_count: int


class ApprovalDecisionRequest(BaseModel):
    approver_email: str
    approve: bool
    grievance_number: str | None = None
    notes: str | None = None


class ApprovalDecisionResponse(BaseModel):
    case_id: str
    status: str
    approval_status: str
    grievance_number: str | None = None


class AssignGrievanceNumberRequest(BaseModel):
    grievance_number: str
    assigned_by: str | None = None
