from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class OutreachStatusFields(BaseModel):
    membership_type: str | None = None
    employment_status: str | None = None
    status_detail: str | None = None
    status_bucket: str | None = None
    status_source_text: str | None = None


class OutreachContactUpsertRequest(OutreachStatusFields):
    email: str
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    work_location: str | None = None
    work_group: str | None = None
    department: str | None = None
    bargaining_unit: str | None = None
    local_number: str | None = None
    steward_name: str | None = None
    rep_name: str | None = None
    active: bool = True
    notes: str | None = None
    source: str | None = None
    extra_fields: dict[str, str] = Field(default_factory=dict)


class OutreachContactRow(OutreachContactUpsertRequest):
    id: int
    created_at_utc: str
    updated_at_utc: str


class OutreachContactListResponse(BaseModel):
    rows: list[OutreachContactRow]
    count: int


class OutreachTemplateUpsertRequest(BaseModel):
    template_key: str
    name: str
    template_type: str
    subject_template: str
    body_template: str
    active: bool = True


class OutreachTemplateRow(OutreachTemplateUpsertRequest):
    id: int
    seeded: bool
    created_at_utc: str
    updated_at_utc: str


class OutreachTemplateListResponse(BaseModel):
    rows: list[OutreachTemplateRow]
    count: int


class OutreachStopUpsertRequest(BaseModel):
    location_name: str
    visit_date_local: str
    start_time_local: str
    end_time_local: str
    timezone: str | None = None
    audience_location: str | None = None
    audience_work_group: str | None = None
    audience_status_bucket: str | None = None
    notice_subject: str | None = None
    reminder_subject: str | None = None
    notice_send_at_local: str | None = None
    reminder_send_at_local: str | None = None
    status: str = "draft"


class OutreachStopRow(BaseModel):
    id: int
    location_name: str
    visit_date_local: str
    start_time_local: str
    end_time_local: str
    timezone: str
    audience_location: str | None = None
    audience_work_group: str | None = None
    audience_status_bucket: str | None = None
    notice_subject: str | None = None
    reminder_subject: str | None = None
    notice_send_at_utc: str
    reminder_send_at_utc: str
    notice_send_at_local: str
    reminder_send_at_local: str
    status: str
    created_at_utc: str
    updated_at_utc: str


class OutreachStopListResponse(BaseModel):
    rows: list[OutreachStopRow]
    count: int


class OutreachManualContactRequest(OutreachStatusFields):
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    work_location: str | None = None
    work_group: str | None = None
    department: str | None = None
    bargaining_unit: str | None = None
    local_number: str | None = None
    steward_name: str | None = None
    rep_name: str | None = None
    source: str | None = None
    notes: str | None = None
    extra_fields: dict[str, str] = Field(default_factory=dict)


class OutreachPreviewRequest(BaseModel):
    template_id: int
    stop_id: int
    contact_id: int | None = None
    recipient_email: str | None = None
    manual_contact: OutreachManualContactRequest | None = None


class OutreachPreviewResponse(BaseModel):
    subject: str
    text_body: str
    html_body: str
    missing_fields: list[str] = Field(default_factory=list)
    placeholder_catalog: list[str] = Field(default_factory=list)


class OutreachTestSendRequest(OutreachPreviewRequest):
    recipient_email: str


class OutreachOneOffSendRequest(BaseModel):
    template_id: int
    stop_id: int
    recipient_email: str
    contact_id: int | None = None
    manual_contact: OutreachManualContactRequest | None = None


class OutreachQuickMessageRequest(BaseModel):
    stop_id: int
    recipient_email: str
    subject_template: str
    body_template: str
    contact_id: int | None = None
    manual_contact: OutreachManualContactRequest | None = None


class OutreachSendResult(BaseModel):
    send_log_id: int
    recipient_email: str
    status: str
    graph_message_id: str | None = None
    error_text: str | None = None


class OutreachRunDueResponse(BaseModel):
    processed_count: int
    sent_count: int
    failed_count: int
    skipped_suppressed_count: int
    skipped_existing_count: int
    rows: list[OutreachSendResult] = Field(default_factory=list)


class OutreachSuppressionRow(BaseModel):
    id: int
    email: str
    contact_id: int | None = None
    reason: str
    created_at_utc: str


class OutreachSuppressionListResponse(BaseModel):
    rows: list[OutreachSuppressionRow]
    count: int


class OutreachSendLogRow(BaseModel):
    id: int
    stop_id: int | None = None
    template_id: int | None = None
    contact_id: int | None = None
    recipient_email: str
    email_type: str
    subject: str
    status: str
    scheduled_for_utc: str | None = None
    attempted_at_utc: str | None = None
    sent_at_utc: str | None = None
    failed_at_utc: str | None = None
    graph_message_id: str | None = None
    internet_message_id: str | None = None
    error_text: str | None = None
    location_name: str | None = None
    visit_date_local: str | None = None
    created_at_utc: str


class OutreachSendLogListResponse(BaseModel):
    rows: list[OutreachSendLogRow]
    count: int


class OutreachImportFieldMapping(BaseModel):
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    work_location: str | None = None
    work_group: str | None = None
    department: str | None = None
    bargaining_unit: str | None = None
    local_number: str | None = None
    steward_name: str | None = None
    rep_name: str | None = None


class OutreachImportStatusMapping(BaseModel):
    mode: str = "combined"
    combined_status_column: str | None = None
    membership_type_column: str | None = None
    employment_status_column: str | None = None
    status_detail_column: str | None = None


class OutreachImportMapping(BaseModel):
    field_mapping: OutreachImportFieldMapping = Field(default_factory=OutreachImportFieldMapping)
    status_mapping: OutreachImportStatusMapping = Field(default_factory=OutreachImportStatusMapping)


class OutreachImportInspectRequest(BaseModel):
    filename: str
    content_base64: str
    sheet_name: str | None = None
    mapping: OutreachImportMapping | None = None


class OutreachImportRequest(OutreachImportInspectRequest):
    replace_existing: bool = False


class OutreachImportSheetRow(BaseModel):
    name: str
    row_count: int
    selected: bool = False


class OutreachImportPreview(BaseModel):
    imported_count: int
    updated_count: int
    skipped_count: int
    ignored_count: int
    bucket_counts: dict[str, int] = Field(default_factory=dict)
    skipped_reasons: dict[str, int] = Field(default_factory=dict)
    ignored_reasons: dict[str, int] = Field(default_factory=dict)


class OutreachImportInspectResponse(BaseModel):
    sheets: list[OutreachImportSheetRow] = Field(default_factory=list)
    selected_sheet_name: str
    headers: list[str] = Field(default_factory=list)
    sample_rows: list[dict[str, str]] = Field(default_factory=list)
    suggested_mapping: OutreachImportMapping = Field(default_factory=OutreachImportMapping)
    remembered_mapping: OutreachImportMapping | None = None
    effective_mapping: OutreachImportMapping = Field(default_factory=OutreachImportMapping)
    preview: OutreachImportPreview


class OutreachImportResponse(OutreachImportPreview):
    selected_sheet_name: str
    errors: list[str] = Field(default_factory=list)
    saved_mapping: bool = False


class OutreachSendReadiness(BaseModel):
    enabled: bool
    ready: bool
    sender_user_id: str | None = None
    public_base_url: str | None = None
    reply_to_address: str | None = None
    dry_run: bool = False
    issues: list[str] = Field(default_factory=list)


class OutreachSummaryResponse(BaseModel):
    sent_count: int
    failed_count: int
    suppressed_count: int
    stop_count: int
    active_contact_count: int


class OutreachPageBootstrap(BaseModel):
    contacts: OutreachContactListResponse
    templates: OutreachTemplateListResponse
    stops: OutreachStopListResponse
    suppressions: OutreachSuppressionListResponse
    send_log: OutreachSendLogListResponse
    summary: OutreachSummaryResponse
    send_readiness: OutreachSendReadiness
    placeholder_catalog: list[str] = Field(default_factory=list)


class OutreachUnsubscribeResult(BaseModel):
    email: str
    status: str
    reason: str


class OutreachMutableRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
