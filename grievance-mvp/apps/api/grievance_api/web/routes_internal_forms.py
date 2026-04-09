from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from .hosted_forms_registry import get_hosted_form_definition
from .officer_auth import require_authenticated_officer, require_officer_page_access
from .routes_hosted_forms import render_hosted_form_alias_page, submit_hosted_form

router = APIRouter()

_NON_DISCIPLINE_FORM_KEY = "non_discipline_brief"
_NON_DISCIPLINE_ALIAS_PATH = "/internal/forms/non-discipline-brief"
_NON_DISCIPLINE_ALIAS_SUBMIT_PATH = "/internal/forms/non-discipline-brief/submissions"


class NonDisciplineInternalFormSubmission(BaseModel):
    request_id: str | None = None
    grievant_firstname: str
    grievant_lastname: str
    grievant_email: str
    local_number: str
    local_grievance_number: str | None = None
    location: str
    grievant_or_work_group: str
    grievant_home_address: str
    date_grievance_occurred: str
    date_grievance_filed: str
    date_grievance_appealed_to_executive_level: str | None = None
    issue_or_condition_involved: str
    action_taken: str
    chronology_of_facts: str
    analysis_of_grievance: str
    current_status: str
    union_position: str
    company_position: str
    potential_witnesses: str | None = None
    recommendation: str
    attachment_1: str | None = None
    attachment_2: str | None = None
    attachment_3: str | None = None
    attachment_4: str | None = None
    attachment_5: str | None = None
    attachment_6: str | None = None
    attachment_7: str | None = None
    attachment_8: str | None = None
    attachment_9: str | None = None
    attachment_10: str | None = None
    signer_email: str | None = None


def _build_non_discipline_intake_payload(body: NonDisciplineInternalFormSubmission) -> dict[str, object]:
    definition = get_hosted_form_definition(_NON_DISCIPLINE_FORM_KEY)
    if not definition:
        raise RuntimeError("non_discipline_brief hosted form is not configured")
    return definition.build_payload(body.model_dump())


@router.get(_NON_DISCIPLINE_ALIAS_PATH)
async def non_discipline_internal_form_page(request: Request):
    gate = await require_officer_page_access(request, next_path=_NON_DISCIPLINE_ALIAS_PATH)
    if isinstance(gate, RedirectResponse):
        return gate
    return await render_hosted_form_alias_page(
        form_key=_NON_DISCIPLINE_FORM_KEY,
        submit_path=_NON_DISCIPLINE_ALIAS_SUBMIT_PATH,
        request=request,
        next_path=_NON_DISCIPLINE_ALIAS_PATH,
    )


@router.post(_NON_DISCIPLINE_ALIAS_SUBMIT_PATH)
async def submit_non_discipline_internal_form(
    body: NonDisciplineInternalFormSubmission,
    request: Request,
):
    await require_authenticated_officer(request)
    result = await submit_hosted_form(
        _NON_DISCIPLINE_FORM_KEY,
        body.model_dump(),
        request,
        bypass_visibility=True,
    )
    return {
        "request_id": result["request_id"],
        "document_command": _NON_DISCIPLINE_FORM_KEY,
        "intake_response": result["backend_response"],
    }
