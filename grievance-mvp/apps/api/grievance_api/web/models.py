from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class DocumentRequest(BaseModel):
    doc_type: str
    template_key: str | None = None
    requires_signature: bool = False
    signers: list[str] | None = None


class ClientSuppliedFile(BaseModel):
    file_name: str = Field(
        validation_alias=AliasChoices("file_name", "filename", "name"),
        description="Original filename supplied by client/forms",
    )
    download_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("download_url", "downloadUrl", "url"),
        description="Temporary HTTPS download URL for file transfer",
    )
    content_base64: str | None = Field(
        default=None,
        validation_alias=AliasChoices("content_base64", "contentBase64", "contentBytes"),
        description="Optional base64-encoded content for small files",
    )


class IntakeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    request_id: str = Field(..., description="Client-generated idempotency key")
    grievance_id: str | None = Field(default=None, description="External grievance identifier such as 2026001")
    grievance_number: str | None = None
    contract: str = Field(..., description="AT&T or COJ (or other)")
    grievant_firstname: str
    grievant_lastname: str
    grievant_email: str | None = None
    grievant_phone: str | None = None
    work_location: str | None = None
    supervisor: str | None = None
    incident_date: str | None = None
    narrative: str
    document_command: str | None = Field(
        default=None,
        description="Optional single-doc command for workflow routing (e.g. statement_of_occurrence)",
    )
    template_data: dict[str, object] = Field(default_factory=dict, description="Optional template merge fields")
    documents: list[DocumentRequest] = Field(default_factory=list)
    client_supplied_files: list[ClientSuppliedFile] = Field(default_factory=list)


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


class StandaloneDocumentStatus(BaseModel):
    document_id: str
    form_key: str
    status: str
    signing_link: str | None = None
    document_link: str | None = None


class StandaloneSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    request_id: str = Field(..., description="Client-generated idempotency key")
    form_key: str = Field(..., description="Standalone form key, for example att_mobility_bargaining_suggestion")
    local_president_signer_email: str | None = Field(
        None,
        description="Optional signer email override for signer1; if omitted, the form config default is used",
    )
    template_data: dict[str, object] = Field(default_factory=dict, description="Template merge fields for standalone form")


class StandaloneSubmissionResponse(BaseModel):
    submission_id: str
    form_key: str
    form_title: str
    status: str
    documents: list[StandaloneDocumentStatus]


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


class OfficerCaseCreateRequest(BaseModel):
    grievance_id: str | None = None
    grievance_number: str | None = None
    member_name: str
    member_email: str | None = None
    department: str | None = None
    steward: str | None = None
    occurrence_date: str | None = None
    issue_summary: str | None = None
    first_level_request_sent_date: str | None = None
    second_level_request_sent_date: str | None = None
    officer_assignee: str | None = None
    officer_notes: str | None = None
    officer_status: str | None = None
    contract: str | None = None
    updated_by: str | None = None


class OfficerCaseUpdateRequest(BaseModel):
    grievance_number: str | None = None
    member_name: str | None = None
    member_email: str | None = None
    department: str | None = None
    steward: str | None = None
    occurrence_date: str | None = None
    issue_summary: str | None = None
    first_level_request_sent_date: str | None = None
    second_level_request_sent_date: str | None = None
    officer_assignee: str | None = None
    officer_notes: str | None = None
    officer_status: str | None = None
    updated_by: str | None = None


class OfficerCaseBulkUpdateRequest(OfficerCaseUpdateRequest):
    case_ids: list[str]


class OfficerCaseBulkDeleteRequest(BaseModel):
    case_ids: list[str]


class OfficerCaseRow(BaseModel):
    case_id: str
    grievance_id: str
    grievance_number: str | None = None
    display_grievance: str
    member_name: str
    member_email: str | None = None
    department: str | None = None
    steward: str | None = None
    occurrence_date: str | None = None
    issue_summary: str | None = None
    first_level_request_sent_date: str | None = None
    second_level_request_sent_date: str | None = None
    officer_assignee: str | None = None
    officer_notes: str | None = None
    officer_status: str
    workflow_status: str
    approval_status: str
    officer_source: str
    officer_closed_at_utc: str | None = None
    officer_closed_by: str | None = None
    created_at_utc: str


class OfficerCaseListResponse(BaseModel):
    rows: list[OfficerCaseRow]
    roster: list[str] = Field(default_factory=list)
    count: int


class OfficerCaseDeleteResponse(BaseModel):
    case_id: str
    grievance_id: str
    grievance_number: str | None = None
    display_grievance: str
    deleted_case_count: int
    deleted_document_count: int
    deleted_document_stage_count: int
    deleted_stage_artifact_count: int
    deleted_stage_field_value_count: int
    deleted_event_count: int
    deleted_outbound_email_count: int


class OfficerCaseBulkUpdateResponse(BaseModel):
    selected_case_count: int
    updated_case_count: int
    case_ids: list[str] = Field(default_factory=list)
    changed_fields: list[str] = Field(default_factory=list)


class OfficerCaseBulkDeleteResponse(BaseModel):
    selected_case_count: int
    deleted_case_count: int
    deleted_case_ids: list[str] = Field(default_factory=list)
    deleted_document_count: int
    deleted_document_stage_count: int
    deleted_stage_artifact_count: int
    deleted_stage_field_value_count: int
    deleted_event_count: int
    deleted_outbound_email_count: int
