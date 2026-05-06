from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ReferralSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    request_id: str
    referrer_name: str
    referrer_address: str
    referrer_phone: str
    referrer_email: str | None = None
    referrer_group: str
    referred_name: str
    referred_group: str | None = None
    referred_att_uid: str | None = None
    referral_notes: str | None = None


class ReferralRow(BaseModel):
    id: str
    request_id: str
    created_at_utc: str
    updated_at_utc: str
    status: str
    assignee: str | None = None
    officer_notes: str | None = None
    reminder_due_at_utc: str
    reminder_attempted_at_utc: str | None = None
    reminder_sent_at_utc: str | None = None
    reminder_error: str | None = None
    referrer_name: str
    referrer_address: str
    referrer_phone: str
    referrer_email: str | None = None
    referrer_group: str
    referred_name: str
    referred_group: str | None = None
    referred_att_uid: str | None = None
    referral_notes: str | None = None


class ReferralSubmissionResponse(BaseModel):
    referral_id: str
    status: str
    reminder_due_at_utc: str


class ReferralListResponse(BaseModel):
    rows: list[ReferralRow] = Field(default_factory=list)
    count: int


class ReferralUpdateRequest(BaseModel):
    status: str | None = None
    assignee: str | None = None
    officer_notes: str | None = None
    referred_group: str | None = None
    referred_att_uid: str | None = None
    reminder_due_at_utc: str | None = None


class ReferralReminderResultRow(BaseModel):
    referral_id: str
    recipient_count: int
    status: str
    graph_message_id: str | None = None
    error_text: str | None = None


class ReferralRunDueResponse(BaseModel):
    processed_count: int
    sent_count: int
    failed_count: int
    skipped_count: int = 0
    rows: list[ReferralReminderResultRow] = Field(default_factory=list)


class ReferralProgramSettingsResponse(BaseModel):
    enabled: bool
    sunset_date: str
    is_active: bool
    updated_by: str | None = None
    updated_at_utc: str | None = None


class ReferralProgramSettingsUpdateRequest(BaseModel):
    sunset_date: str
