from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable
from uuid import uuid4


_SAFE_FIELD_NAME = re.compile(r"[^A-Za-z0-9]+")
_PLACEHOLDER = re.compile(r"^<(.+)>$")
_LONG_TEXT_KEY_HINTS = (
    "action",
    "additional_info",
    "analysis",
    "argument",
    "articles",
    "basis",
    "chronology",
    "condition",
    "current_status",
    "demand",
    "disparate_treatment",
    "examples",
    "facts",
    "issue",
    "narrative",
    "outside_remedies",
    "position",
    "recommendation",
    "reason",
    "remedies",
    "settlement",
    "specific_examples",
    "statement",
    "strengths",
    "timeline",
    "union_representation",
    "weaknesses",
    "witnesses",
)
_STATEMENT_OF_OCCURRENCE_CONTRACT_OPTIONS = (
    "City of Jacksonville",
    "Wire Tech",
    "Core Southeastern",
    "Construction",
    "Yellow Pages / Thrive",
    "BellSouth",
    "AT&T Mobility",
    "IHX",
    "BST",
    "Utilities",
)
_FORM_FIELD_ORDERS: dict[str, tuple[str, ...]] = {
    "statement_of_occurrence": (
        "contract",
        "grievant_firstname",
        "grievant_lastname",
        "grievant_email",
        "grievant_phone",
        "work_location",
        "supervisor",
        "incident_date",
        "narrative",
        "home_address",
        "seniority_date",
        "ncs_date",
        "personal_cell",
        "personal_email",
        "department",
        "title",
        "supervisor_phone",
        "supervisor_email",
        "grievants_uid",
        "article",
        "witness_1_name",
        "witness_1_title",
        "witness_1_phone",
        "witness_2_name",
        "witness_2_title",
        "witness_2_phone",
        "witness_3_name",
        "witness_3_title",
        "witness_3_phone",
    ),
    "bellsouth_meeting_request": (
        "grievance_id",
        "grievant_firstname",
        "grievant_lastname",
        "grievant_email",
        "incident_date",
        "narrative",
        "to",
        "grievant_names",
        "grievants_attending",
        "grievants_in_attendance",
        "date_grievance_occurred",
        "issue_contract_section",
        "informal_meeting_date",
        "meeting_requested_date",
        "meeting_requested_time",
        "meeting_requested_place",
        "union_rep_attending",
        "union_reps_in_attendance",
        "company_reps_in_attendance",
        "additional_info",
        "reply_to_name_1",
        "reply_to_name_2",
        "reply_to_address_1",
        "reply_to_address_2",
        "union_rep_email",
    ),
    "mobility_meeting_request": (
        "grievance_id",
        "grievant_firstname",
        "grievant_lastname",
        "grievant_email",
        "incident_date",
        "narrative",
        "to",
        "grievant_names",
        "grievants_attending",
        "grievants_in_attendance",
        "date_grievance_occurred",
        "issue_contract_section",
        "informal_meeting_date",
        "meeting_requested_date",
        "meeting_requested_time",
        "meeting_requested_place",
        "union_rep_attending",
        "union_reps_in_attendance",
        "company_reps_in_attendance",
        "additional_info",
        "reply_to_name_1",
        "reply_to_name_2",
        "reply_to_address_1",
        "reply_to_address_2",
        "union_rep_email",
    ),
    "grievance_data_request": (
        "grievance_id",
        "contract",
        "grievant_firstname",
        "grievant_lastname",
        "grievant_email",
        "articles",
        "company_name",
        "company_rep_name",
        "company_rep_title",
        "due_date",
        "union_phone",
        "union_rep_name",
        "union_rep_title",
    ),
    "data_request_letterhead": (
        "grievance_id",
        "contract",
        "grievant_firstname",
        "grievant_lastname",
        "grievant_email",
        "company_rep_name",
        "company_rep_title",
        "company_rep_email",
        "data_requested",
        "preferred_format",
        "steward_name",
        "steward_email",
        "approver_block",
    ),
    "true_intent_brief": (
        "grievant_firstname",
        "grievant_lastname",
        "grievant_email",
        "grievant_phone",
        "grievant_street",
        "grievant_city",
        "grievant_state",
        "grievant_zip",
        "title",
        "department",
        "seniority_date",
        "local_number",
        "local_phone",
        "local_street",
        "local_city",
        "local_state",
        "local_zip",
        "date_grievance_occurred",
        "grievance_type",
        "issue_involved",
        "articles",
        "management_structure",
        "step1_informal_date",
        "step2_formal_date",
        "appealed_to_state_date",
        "timeline",
        "argument",
        "analysis",
        "company_name",
        "company_position",
        "company_strengths",
        "company_weaknesses",
        "company_proposed_settlement",
        "union_position",
        "union_strengths",
        "union_weaknesses",
        "union_proposed_settlement",
        "attachment_1",
        "attachment_2",
        "attachment_3",
        "attachment_4",
        "attachment_5",
        "attachment_6",
        "attachment_7",
        "attachment_8",
        "attachment_9",
        "attachment_10",
        "signer_email",
    ),
    "non_discipline_brief": (
        "grievant_firstname",
        "grievant_lastname",
        "grievant_email",
        "local_number",
        "local_grievance_number",
        "location",
        "grievant_or_work_group",
        "grievant_home_address",
        "date_grievance_occurred",
        "date_grievance_filed",
        "date_grievance_appealed_to_executive_level",
        "issue_or_condition_involved",
        "action_taken",
        "chronology_of_facts",
        "analysis_of_grievance",
        "current_status",
        "union_position",
        "company_position",
        "potential_witnesses",
        "recommendation",
        "attachment_1",
        "attachment_2",
        "attachment_3",
        "attachment_4",
        "attachment_5",
        "attachment_6",
        "attachment_7",
        "attachment_8",
        "attachment_9",
        "attachment_10",
        "signer_email",
    ),
    "disciplinary_brief": (
        "grievant_firstname",
        "grievant_lastname",
        "grievant_email",
        "grievant_phone",
        "grievant_street",
        "grievant_city",
        "grievant_state",
        "grievant_zip",
        "title",
        "department",
        "seniority_date",
        "local_number",
        "local_phone",
        "local_street",
        "local_city",
        "local_state",
        "local_zip",
        "date_grievance_occurred",
        "date_discipline_grieved",
        "grievance_type",
        "articles",
        "management_structure",
        "step1_informal_date",
        "step2_formal_date",
        "appealed_to_state_date",
        "current_status",
        "disparate_treatment",
        "other_related_grievances",
        "outside_remedies",
        "company_name",
        "company_facts",
        "company_argument",
        "company_proposed_settlement",
        "union_facts",
        "union_argument",
        "union_representation",
        "union_proposed_settlement",
        "attachment_1_name",
        "attachment_1_date",
        "attachment_2_name",
        "attachment_2_date",
        "attachment_3_name",
        "attachment_3_date",
        "attachment_4_name",
        "attachment_4_date",
        "attachment_5_name",
        "attachment_5_date",
        "attachment_6_name",
        "attachment_6_date",
        "attachment_7_name",
        "attachment_7_date",
        "attachment_8_name",
        "attachment_8_date",
        "attachment_9_name",
        "attachment_9_date",
        "attachment_10_name",
        "attachment_10_date",
        "attachment_11_name",
        "attachment_11_date",
        "attachment_12_name",
        "attachment_12_date",
        "attachment_13_name",
        "attachment_13_date",
        "attachment_14_name",
        "attachment_14_date",
        "attachment_15_name",
        "attachment_15_date",
        "attachment_16_name",
        "attachment_16_date",
        "attachment_17_name",
        "attachment_17_date",
        "attachment_18_name",
        "attachment_18_date",
        "signer_email",
    ),
    "settlement_form": (
        "grievance_id",
        "manager_signer_email",
        "steward_signer_email",
        "grievant_firstname",
        "grievant_lastname",
        "grievant_email",
        "informal_meeting_date",
        "company_rep_attending",
        "union_rep_attending",
        "issue_article",
        "issue_text",
        "settlement_text",
    ),
    "mobility_record_of_grievance": (
        "grievance_id",
        "grievant_firstname",
        "grievant_lastname",
        "grievant_email",
        "district_grievance_number",
        "date_grievance_occurred",
        "department",
        "specific_location_state",
        "employee_work_group_name",
        "job_title",
        "ncs_date",
        "union_statement",
        "contract_articles",
        "date_informal",
        "date_first_step_requested",
        "date_first_step_held",
        "union_stage_1_email",
        "company_stage_2_email",
        "union_stage_3_email",
    ),
    "bst_grievance_form_3g3a": (
        "grievance_id",
        "contract",
        "narrative",
        "q1_occurred_date",
        "q1_city_state",
        "q2_employee_name",
        "q2_employee_attuid",
        "q2_department",
        "q2_job_title",
        "q2_payroll_id",
        "q2_seniority_date",
        "q2a_job_title_requested",
        "q2a_requisition_number",
        "q2a_other_department",
        "q3_union_statement",
        "q4_contract_basis",
        "q5_informal_meeting_date",
        "q5_3g3r_issued_date",
        "q5_union_rep_name_attuid",
        "union_stage_1_email",
        "manager_stage_2_email",
        "union_stage_3_email",
    ),
    "att_mobility_bargaining_suggestion": (
        "local_number",
        "demand_from_local",
        "submitting_member_title",
        "submitting_member_name",
        "demand_text",
        "reason_text",
        "specific_examples_text",
        "work_phone",
        "home_phone",
        "non_work_email",
        "local_president_signer_email",
    ),
}


@dataclass(frozen=True)
class HostedFormField:
    name: str
    label: str
    source_scope: str
    source_key: str
    type: str = "text"
    required: bool = False
    placeholder: str = ""
    hint: str = ""
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class HostedFormDefinition:
    form_key: str
    title: str
    description: str
    route_type: str
    target_path: str
    fields: tuple[HostedFormField, ...]
    metadata: tuple[tuple[str, str], ...]
    default_visibility: str
    default_enabled: bool
    build_payload: Callable[[dict[str, str]], dict[str, object]]


@dataclass(frozen=True)
class HostedFormRuntimeSettings:
    form_key: str
    visibility: str
    enabled: bool
    updated_by: str | None = None
    updated_at_utc: str | None = None


_FORM_CATALOG: tuple[dict[str, object], ...] = (
    {
        "key": "statement_of_occurrence",
        "title": "Statement of Occurrence",
        "routeType": "intake",
        "endpointPath": "/intake",
        "documentCommand": "statement_of_occurrence",
        "topLevelFields": {
            "contract": "<Contract>",
            "grievant_firstname": "<Grievant first name>",
            "grievant_lastname": "<Grievant last name>",
            "grievant_email": "<Grievant email>",
            "grievant_phone": "<Grievant phone>",
            "work_location": "<Work location>",
            "supervisor": "<Supervisor name>",
            "incident_date": "<yyyy-mm-dd>",
            "narrative": "<Statement text>",
        },
        "templateDataFields": {
            "home_address": "<Home address>",
            "seniority_date": "<yyyy-mm-dd>",
            "ncs_date": "<yyyy-mm-dd>",
            "personal_cell": "<Personal cell>",
            "personal_email": "<Signer email>",
            "department": "<Department>",
            "title": "<Title>",
            "supervisor_phone": "<Supervisor phone>",
            "supervisor_email": "<Supervisor email>",
            "grievants uid": "<UID>",
            "article": "<Article violated>",
            "witness_1_name": "",
            "witness_1_title": "",
            "witness_1_phone": "",
            "witness_2_name": "",
            "witness_2_title": "",
            "witness_2_phone": "",
            "witness_3_name": "",
            "witness_3_title": "",
            "witness_3_phone": "",
        },
    },
    {
        "key": "bellsouth_meeting_request",
        "title": "BellSouth Meeting Request",
        "routeType": "intake",
        "endpointPath": "/intake",
        "documentCommand": "bellsouth_meeting_request",
        "topLevelFields": {
            "grievance_id": "<Existing grievance id>",
            "contract": "BellSouth",
            "grievant_firstname": "<Grievant first name>",
            "grievant_lastname": "<Grievant last name>",
            "grievant_email": "<Grievant email>",
            "incident_date": "<yyyy-mm-dd>",
            "narrative": "<Notes>",
        },
        "templateDataFields": {
            "union_rep_email": "<Union representative email>",
            "to": "<To>",
            "request_date": "<yyyy-mm-dd>",
            "grievant_names": "<Grievant names>",
            "grievants_attending": "<Grievants attending>",
            "grievants_in_attendance": "<Grievants in attendance>",
            "date_grievance_occurred": "<yyyy-mm-dd>",
            "issue_contract_section": "<Article or section>",
            "informal_meeting_date": "<yyyy-mm-dd>",
            "meeting_requested_date": "<yyyy-mm-dd>",
            "meeting_requested_time": "<Meeting time>",
            "meeting_requested_place": "<Meeting place>",
            "union_rep_attending": "<Union representative attending>",
            "union_reps_in_attendance": "<Union reps in attendance>",
            "company_reps_in_attendance": "<Company reps in attendance>",
            "additional_info": "<Additional info>",
            "reply_to_name_1": "<Reply-to name 1>",
            "reply_to_name_2": "<Reply-to name 2>",
            "reply_to_address_1": "<Reply-to address 1>",
            "reply_to_address_2": "<Reply-to address 2>",
        },
    },
    {
        "key": "mobility_meeting_request",
        "title": "Mobility Meeting Request",
        "routeType": "intake",
        "endpointPath": "/intake",
        "documentCommand": "mobility_meeting_request",
        "topLevelFields": {
            "grievance_id": "<Existing grievance id>",
            "contract": "AT&T Mobility",
            "grievant_firstname": "<Grievant first name>",
            "grievant_lastname": "<Grievant last name>",
            "grievant_email": "<Grievant email>",
            "incident_date": "<yyyy-mm-dd>",
            "narrative": "<Notes>",
        },
        "templateDataFields": {
            "union_rep_email": "<Union representative email>",
            "to": "<To>",
            "request_date": "<yyyy-mm-dd>",
            "grievant_names": "<Grievant names>",
            "grievants_attending": "<Grievants attending>",
            "grievants_in_attendance": "<Grievants in attendance>",
            "date_grievance_occurred": "<yyyy-mm-dd>",
            "issue_contract_section": "<Article or section>",
            "informal_meeting_date": "<yyyy-mm-dd>",
            "meeting_requested_date": "<yyyy-mm-dd>",
            "meeting_requested_time": "<Meeting time>",
            "meeting_requested_place": "<Meeting place>",
            "union_rep_attending": "<Union representative attending>",
            "union_reps_in_attendance": "<Union reps in attendance>",
            "company_reps_in_attendance": "<Company reps in attendance>",
            "additional_info": "<Additional info>",
            "reply_to_name_1": "<Reply-to name 1>",
            "reply_to_name_2": "<Reply-to name 2>",
            "reply_to_address_1": "<Reply-to address 1>",
            "reply_to_address_2": "<Reply-to address 2>",
        },
    },
    {
        "key": "grievance_data_request",
        "title": "Grievance Data Request",
        "routeType": "intake",
        "endpointPath": "/intake",
        "documentCommand": "grievance_data_request",
        "topLevelFields": {
            "grievance_id": "<Existing grievance id>",
            "contract": "<Contract>",
            "grievant_firstname": "<Grievant first name>",
            "grievant_lastname": "<Grievant last name>",
            "grievant_email": "<Grievant email>",
            "narrative": "Data request generated from intake",
        },
        "templateDataFields": {
            "articles": "<Articles requested>",
            "company_name": "<Company name>",
            "company_rep_name": "<Company representative name>",
            "company_rep_title": "<Company representative title>",
            "due_date": "<yyyy-mm-dd>",
            "grievant_name": "<Grievant display name>",
            "today_date": "<yyyy-mm-dd>",
            "union_phone": "<Union phone>",
            "union_rep_name": "<Union representative name>",
            "union_rep_title": "<Union representative title>",
        },
    },
    {
        "key": "data_request_letterhead",
        "title": "Data Request Letterhead",
        "routeType": "intake",
        "endpointPath": "/intake",
        "documentCommand": "data_request_letterhead",
        "topLevelFields": {
            "grievance_id": "<Existing grievance id>",
            "contract": "<Contract>",
            "grievant_firstname": "<Grievant first name>",
            "grievant_lastname": "<Grievant last name>",
            "grievant_email": "<Grievant email>",
            "narrative": "Data request cover letter generated from intake",
        },
        "templateDataFields": {
            "grievance_number": "<Existing grievance id>",
            "grievant_name": "<Grievant display name>",
            "today_date": "<yyyy-mm-dd>",
            "company_rep_name": "<Company representative name>",
            "company_rep_title": "<Company representative title>",
            "company_rep_email": "<Company representative email>",
            "data_requested": "<Requested information list>",
            "preferred_format": "<Preferred delivery format>",
            "steward_name": "<Union representative name>",
            "steward_email": "<Union representative email>",
            "approver_block": "<Optional approval block>",
        },
    },
    {
        "key": "true_intent_brief",
        "title": "True Intent Grievance Brief",
        "routeType": "intake",
        "endpointPath": "/intake",
        "documentCommand": "true_intent_brief",
        "topLevelFields": {
            "contract": "CWA",
            "grievant_firstname": "<Grievant first name>",
            "grievant_lastname": "<Grievant last name>",
            "grievant_email": "<Grievant email>",
            "narrative": "True intent grievance brief",
        },
        "templateDataFields": {
            "analysis": "<Analysis>",
            "appealed_to_state_date": "<yyyy-mm-dd>",
            "argument": "<Argument>",
            "articles": "<Articles>",
            "attachment_1": "",
            "attachment_2": "",
            "attachment_3": "",
            "attachment_4": "",
            "attachment_5": "",
            "attachment_6": "",
            "attachment_7": "",
            "attachment_8": "",
            "attachment_9": "",
            "attachment_10": "",
            "company_name": "<Company name>",
            "company_position": "<Company position>",
            "company_proposed_settlement": "<Company proposed settlement>",
            "company_strengths": "<Company strengths>",
            "company_weaknesses": "<Company weaknesses>",
            "date_grievance_occurred": "<yyyy-mm-dd>",
            "department": "<Department>",
            "grievance_type": "<Grievance type>",
            "grievant_city": "<City>",
            "grievant_name": "<Full name>",
            "grievant_phone": "<Phone>",
            "grievant_state": "<State>",
            "grievant_street": "<Street>",
            "grievant_zip": "<Zip>",
            "issue_involved": "<Issue involved>",
            "local_city": "<Local city>",
            "local_number": "<Local number>",
            "local_phone": "<Local phone>",
            "local_state": "<Local state>",
            "local_street": "<Local street>",
            "local_zip": "<Local zip>",
            "management_structure": "<Management structure>",
            "seniority_date": "<yyyy-mm-dd>",
            "step1_informal_date": "<yyyy-mm-dd>",
            "step2_formal_date": "<yyyy-mm-dd>",
            "timeline": "<Timeline>",
            "title": "<Title>",
            "union_position": "<Union position>",
            "union_proposed_settlement": "<Union proposed settlement>",
            "union_strengths": "<Union strengths>",
            "union_weaknesses": "<Union weaknesses>",
            "signer_email": "<Optional signer email override>",
        },
    },
    {
        "key": "non_discipline_brief",
        "title": "Non-Discipline Grievance Brief",
        "routeType": "intake",
        "endpointPath": "/intake",
        "documentCommand": "non_discipline_brief",
        "topLevelFields": {
            "contract": "CWA",
            "grievant_firstname": "<Grievant first name>",
            "grievant_lastname": "<Grievant last name>",
            "grievant_email": "<Grievant email>",
            "narrative": "Non-discipline grievance brief",
        },
        "templateDataFields": {
            "analysis_of_grievance": "<Analysis of grievance>",
            "attachment_1": "",
            "attachment_10": "",
            "attachment_2": "",
            "attachment_3": "",
            "attachment_4": "",
            "attachment_5": "",
            "attachment_6": "",
            "attachment_7": "",
            "attachment_8": "",
            "attachment_9": "",
            "action_taken": "<Action taken>",
            "chronology_of_facts": "<Chronology of facts>",
            "company_position": "<Company position>",
            "current_status": "<Current status>",
            "date_grievance_appealed_to_executive_level": "<yyyy-mm-dd>",
            "date_grievance_filed": "<yyyy-mm-dd>",
            "date_grievance_occurred": "<yyyy-mm-dd>",
            "grievant_home_address": "<Grievant home address>",
            "grievant_name": "<Full name>",
            "grievant_or_work_group": "<Grievant(s) or work group>",
            "issue_or_condition_involved": "<Issue or condition involved>",
            "local_grievance_number": "<Local grievance number>",
            "local_number": "<Local number>",
            "location": "<Location>",
            "potential_witnesses": "<Potential witnesses>",
            "recommendation": "<Recommendation>",
            "signer_email": "<Optional signer email override>",
            "union_position": "<Union position>",
        },
    },
    {
        "key": "disciplinary_brief",
        "title": "Disciplinary Grievance Brief",
        "routeType": "intake",
        "endpointPath": "/intake",
        "documentCommand": "disciplinary_brief",
        "topLevelFields": {
            "contract": "CWA",
            "grievant_firstname": "<Grievant first name>",
            "grievant_lastname": "<Grievant last name>",
            "grievant_email": "<Grievant email>",
            "narrative": "Disciplinary grievance brief",
        },
        "templateDataFields": {
            "appealed_to_state_date": "<yyyy-mm-dd>",
            "articles": "<Articles>",
            "attachment_1_name": "",
            "attachment_1_date": "",
            "attachment_2_name": "",
            "attachment_2_date": "",
            "attachment_3_name": "",
            "attachment_3_date": "",
            "attachment_4_name": "",
            "attachment_4_date": "",
            "attachment_5_name": "",
            "attachment_5_date": "",
            "attachment_6_name": "",
            "attachment_6_date": "",
            "attachment_7_name": "",
            "attachment_7_date": "",
            "attachment_8_name": "",
            "attachment_8_date": "",
            "attachment_9_name": "",
            "attachment_9_date": "",
            "attachment_10_name": "",
            "attachment_10_date": "",
            "attachment_11_name": "",
            "attachment_11_date": "",
            "attachment_12_name": "",
            "attachment_12_date": "",
            "attachment_13_name": "",
            "attachment_13_date": "",
            "attachment_14_name": "",
            "attachment_14_date": "",
            "attachment_15_name": "",
            "attachment_15_date": "",
            "attachment_16_name": "",
            "attachment_16_date": "",
            "attachment_17_name": "",
            "attachment_17_date": "",
            "attachment_18_name": "",
            "attachment_18_date": "",
            "company_argument": "<Company argument>",
            "company_facts": "<Company facts>",
            "company_name": "<Company name>",
            "company_proposed_settlement": "<Company proposed settlement>",
            "current_status": "<Current status>",
            "date_discipline_grieved": "<yyyy-mm-dd>",
            "date_grievance_occurred": "<yyyy-mm-dd>",
            "department": "<Department>",
            "disparate_treatment": "<Disparate treatment>",
            "grievance_type": "<Grievance type>",
            "grievant_city": "<City>",
            "grievant_name": "<Full name>",
            "grievant_phone": "<Phone>",
            "grievant_state": "<State>",
            "grievant_street": "<Street>",
            "grievant_zip": "<Zip>",
            "local_city": "<Local city>",
            "local_number": "<Local number>",
            "local_phone": "<Local phone>",
            "local_state": "<Local state>",
            "local_street": "<Local street>",
            "local_zip": "<Local zip>",
            "management_structure": "<Management structure>",
            "other_related_grievances": "<Other related grievances>",
            "outside_remedies": "<Outside remedies>",
            "seniority_date": "<yyyy-mm-dd>",
            "step1_informal_date": "<yyyy-mm-dd>",
            "step2_formal_date": "<yyyy-mm-dd>",
            "title": "<Title>",
            "union_argument": "<Union argument>",
            "union_facts": "<Union facts>",
            "union_proposed_settlement": "<Union proposed settlement>",
            "union_representation": "<Union representation>",
            "signer_email": "<Optional signer email override>",
        },
    },
    {
        "key": "settlement_form",
        "title": "Settlement Form 3106",
        "routeType": "intake",
        "endpointPath": "/intake",
        "documentCommand": "settlement_form",
        "topLevelFields": {
            "grievance_id": "<Existing grievance id>",
            "contract": "CWA",
            "grievant_firstname": "<Grievant first name>",
            "grievant_lastname": "<Grievant last name>",
            "grievant_email": "<Grievant email>",
            "narrative": "<Short summary copied from issue_text>",
        },
        "documents": [
            {
                "doc_type": "settlement_form_3106",
                "template_key": "settlement_form_3106",
                "requires_signature": True,
                "signers": [
                    "<Manager email>",
                    "<Steward email>",
                ],
            }
        ],
        "templateDataFields": {
            "grievance_number": "<Same value as grievance_id>",
            "informal_meeting_date": "<yyyy-mm-dd>",
            "company_rep_attending": "<Company representative attending>",
            "union_rep_attending": "<Union representative attending>",
            "issue_article": "<Issue article>",
            "issue_text": "<Issue details>",
            "settlement_text": "<Settlement terms>",
            "issue_line_wrap_width": 95,
            "settlement_line_wrap_width": 95,
        },
    },
    {
        "key": "mobility_record_of_grievance",
        "title": "Mobility Record of Grievance",
        "routeType": "intake",
        "endpointPath": "/intake",
        "documentCommand": "mobility_record_of_grievance",
        "topLevelFields": {
            "grievance_id": "<Existing grievance id>",
            "contract": "AT&T Mobility",
            "grievant_firstname": "<Grievant first name>",
            "grievant_lastname": "<Grievant last name>",
            "grievant_email": "<Grievant email>",
            "incident_date": "<yyyy-mm-dd>",
            "narrative": "<Initial union statement>",
        },
        "documents": [
            {
                "doc_type": "mobility_record_of_grievance",
                "template_key": "mobility_record_of_grievance",
                "requires_signature": True,
                "signers": [
                    "<Union stage 1 email>",
                    "<Company stage 2 email>",
                    "<Union stage 3 email>",
                ],
            }
        ],
        "templateDataFields": {
            "district_grievance_number": "<District grievance number>",
            "date_grievance_occurred": "<yyyy-mm-dd>",
            "department": "<Department>",
            "specific_location_state": "<Specific location and state>",
            "local_number": "3106",
            "employee_work_group_name": "<Employee or work group name>",
            "job_title": "<Job title>",
            "ncs_date": "<yyyy-mm-dd>",
            "union_statement": "<Initial union statement>",
            "contract_articles": "<Contract articles>",
            "date_informal": "<yyyy-mm-dd>",
            "date_first_step_requested": "<yyyy-mm-dd>",
            "date_first_step_held": "<yyyy-mm-dd>",
        },
    },
    {
        "key": "bst_grievance_form_3g3a",
        "title": "BST Grievance Form 3G3A",
        "routeType": "intake",
        "endpointPath": "/intake",
        "documentCommand": "bst_grievance_form_3g3a",
        "topLevelFields": {
            "grievance_id": "<Existing grievance id>",
            "contract": "<BST or Utilities>",
            "narrative": "<Request summary>",
        },
        "documents": [
            {
                "doc_type": "bst_grievance_form_3g3a",
                "template_key": "bst_grievance_form_3g3a",
                "requires_signature": True,
                "signers": [
                    "<Union stage 1 email>",
                    "<Manager stage 2 email>",
                    "<Union stage 3 email>",
                ],
            }
        ],
        "templateDataFields": {
            "q1_occurred_date": "<yyyy-mm-dd>",
            "q1_city_state": "<City, State>",
            "q2_employee_name": "<Employee or work group name>",
            "q2_employee_attuid": "<ATTUID>",
            "q2_department": "<Department>",
            "q2_job_title": "<Job title>",
            "q2_payroll_id": "<Payroll ID>",
            "q2_seniority_date": "<yyyy-mm-dd>",
            "q2a_job_title_requested": "<Job title involved or requested>",
            "q2a_requisition_number": "<Requisition number>",
            "q2a_other_department": "<Other department>",
            "q3_union_statement": "<Union statement>",
            "q4_contract_basis": "<Contract basis>",
            "q5_informal_meeting_date": "<yyyy-mm-dd>",
            "q5_3g3r_issued_date": "<yyyy-mm-dd>",
            "q5_union_rep_name_attuid": "<Union representative name and ATTUID>",
        },
    },
    {
        "key": "att_mobility_bargaining_suggestion",
        "title": "AT&T Mobility Bargaining Suggestion",
        "routeType": "standalone",
        "endpointPath": "/standalone/forms/att_mobility_bargaining_suggestion/submissions",
        "formKey": "att_mobility_bargaining_suggestion",
        "topLevelFields": {
            "local_president_signer_email": "<Optional signer override>",
        },
        "templateDataFields": {
            "local_number": "<Local number>",
            "demand_from_local": "<Demand from local>",
            "submitting_member_title": "<Submitting member title>",
            "submitting_member_name": "<Submitting member name>",
            "demand_text": "<Demand>",
            "reason_text": "<Reason>",
            "specific_examples_text": "<Specific examples>",
            "work_phone": "<Work phone>",
            "home_phone": "<Home phone>",
            "non_work_email": "<Non-work email>",
        },
    },
)


def _full_name(values: dict[str, str]) -> str:
    return " ".join(part for part in (values.get("grievant_firstname", ""), values.get("grievant_lastname", "")) if part).strip()


def _today_iso(_values: dict[str, str]) -> str:
    return date.today().isoformat()


def _value_from(values: dict[str, str], key: str) -> str:
    return str(values.get(key, "") or "").strip()


def _settlement_narrative(values: dict[str, str]) -> str:
    return _value_from(values, "issue_text")


def _same_grievance_id(values: dict[str, str]) -> str:
    return _value_from(values, "grievance_id")


def _mobility_record_incident_date(values: dict[str, str]) -> str:
    return _value_from(values, "date_grievance_occurred")


def _mobility_record_narrative(values: dict[str, str]) -> str:
    return _value_from(values, "union_statement")


_FORM_OVERRIDES: dict[str, dict[str, object]] = {
    "statement_of_occurrence": {
        "description": "Submit a hosted Statement of Occurrence intake directly into the grievance workflow.",
        "optional_fields": {
            "seniority_date",
            "ncs_date",
            "personal_email",
            "grievants uid",
        },
        "field_options": {
            "contract": _STATEMENT_OF_OCCURRENCE_CONTRACT_OPTIONS,
        },
    },
    "bellsouth_meeting_request": {
        "description": "Submit a BellSouth meeting request against an existing grievance folder.",
        "hidden_template_keys": {"request_date"},
        "derived_template_values": {"request_date": _today_iso},
    },
    "mobility_meeting_request": {
        "description": "Submit an AT&T Mobility meeting request against an existing grievance folder.",
        "hidden_template_keys": {"request_date"},
        "derived_template_values": {"request_date": _today_iso},
    },
    "grievance_data_request": {
        "description": "Submit a grievance data request against an existing grievance number and store the generated file in that grievance folder.",
        "hidden_template_keys": {"grievant_name", "today_date"},
        "derived_template_values": {
            "grievant_name": _full_name,
            "today_date": _today_iso,
        },
    },
    "data_request_letterhead": {
        "description": "Submit a data request cover letter against an existing grievance folder.",
        "hidden_template_keys": {"grievance_number", "grievant_name", "today_date"},
        "derived_template_values": {
            "grievance_number": _same_grievance_id,
            "grievant_name": _full_name,
            "today_date": _today_iso,
        },
        "optional_fields": {
            "company_rep_email",
            "company_rep_title",
            "approver_block",
        },
    },
    "true_intent_brief": {
        "description": "Submit a hosted True Intent grievance brief into the standard intake workflow.",
        "hidden_template_keys": {"grievant_name"},
        "derived_template_values": {"grievant_name": _full_name},
        "optional_fields": {
            "appealed_to_state_date",
            "management_structure",
            "seniority_date",
            "step1_informal_date",
            "step2_formal_date",
            "signer_email",
        },
    },
    "non_discipline_brief": {
        "description": "Submit the hosted Non-Discipline grievance brief using the first-class intake workflow.",
        "hidden_template_keys": {"grievant_name"},
        "derived_template_values": {"grievant_name": _full_name},
        "optional_fields": {
            "local_grievance_number",
            "date_grievance_appealed_to_executive_level",
            "potential_witnesses",
            "signer_email",
        },
    },
    "disciplinary_brief": {
        "description": "Submit a hosted disciplinary grievance brief into the standard intake workflow.",
        "hidden_template_keys": {"grievant_name"},
        "derived_template_values": {"grievant_name": _full_name},
        "optional_fields": {
            "appealed_to_state_date",
            "date_discipline_grieved",
            "disparate_treatment",
            "management_structure",
            "other_related_grievances",
            "outside_remedies",
            "seniority_date",
            "step1_informal_date",
            "step2_formal_date",
            "signer_email",
        },
    },
    "settlement_form": {
        "description": "Submit a hosted Settlement Form 3106 intake for an existing grievance.",
        "hidden_top_level_keys": {"narrative"},
        "derived_top_level_values": {"narrative": _settlement_narrative},
        "hidden_template_keys": {"grievance_number"},
        "derived_template_values": {"grievance_number": _same_grievance_id},
        "fixed_template_values": {
            "issue_line_wrap_width": 95,
            "settlement_line_wrap_width": 95,
        },
        "optional_fields": {
            "informal_meeting_date",
            "company_rep_attending",
            "union_rep_attending",
            "issue_article",
        },
        "signer_fields": (
            HostedFormField(
                name="manager_signer_email",
                label="Company representative or manager signer email",
                source_scope="document_signer",
                source_key="0",
                type="email",
                required=True,
                hint="Signer 1 for the settlement form signature routing.",
            ),
            HostedFormField(
                name="steward_signer_email",
                label="Steward signer email",
                source_scope="document_signer",
                source_key="1",
                type="email",
                required=True,
                hint="Signer 2 for the settlement form signature routing.",
            ),
        ),
    },
    "mobility_record_of_grievance": {
        "description": "Submit a hosted Mobility Record of Grievance intake for an existing grievance folder.",
        "hidden_top_level_keys": {"incident_date", "narrative"},
        "derived_top_level_values": {
            "incident_date": _mobility_record_incident_date,
            "narrative": _mobility_record_narrative,
        },
        "hidden_template_keys": {"local_number"},
        "fixed_template_values": {"local_number": "3106"},
        "optional_fields": {
            "district_grievance_number",
            "department",
            "specific_location_state",
            "job_title",
            "ncs_date",
            "contract_articles",
            "date_informal",
            "date_first_step_requested",
            "date_first_step_held",
        },
        "signer_fields": (
            HostedFormField(
                name="union_stage_1_email",
                label="Union stage 1 signer email",
                source_scope="document_signer",
                source_key="0",
                type="email",
                required=True,
            ),
            HostedFormField(
                name="company_stage_2_email",
                label="Company stage 2 signer email",
                source_scope="document_signer",
                source_key="1",
                type="email",
                required=True,
            ),
            HostedFormField(
                name="union_stage_3_email",
                label="Union stage 3 signer email",
                source_scope="document_signer",
                source_key="2",
                type="email",
                required=True,
            ),
        ),
    },
    "bst_grievance_form_3g3a": {
        "description": "Submit a hosted BST 3G3A grievance intake for the automated Q1-Q10 workflow.",
        "hidden_template_keys": {"local_grievance_number", "local_number"},
        "derived_template_values": {"local_grievance_number": _same_grievance_id},
        "fixed_template_values": {"local_number": "3106"},
        "optional_fields": {
            "q2_employee_attuid",
            "q2_department",
            "q2_job_title",
            "q2_payroll_id",
            "q2_seniority_date",
            "q2a_job_title_requested",
            "q2a_requisition_number",
            "q2a_other_department",
            "q5_informal_meeting_date",
            "q5_3g3r_issued_date",
        },
        "field_options": {"contract": ("BST", "Utilities")},
        "signer_fields": (
            HostedFormField(
                name="union_stage_1_email",
                label="Union stage 1 signer email",
                source_scope="document_signer",
                source_key="0",
                type="email",
                required=True,
            ),
            HostedFormField(
                name="manager_stage_2_email",
                label="Manager stage 2 signer email",
                source_scope="document_signer",
                source_key="1",
                type="email",
                required=True,
            ),
            HostedFormField(
                name="union_stage_3_email",
                label="Union stage 3 signer email",
                source_scope="document_signer",
                source_key="2",
                type="email",
                required=True,
            ),
        ),
    },
    "att_mobility_bargaining_suggestion": {
        "description": "Submit an AT&T Mobility bargaining suggestion into the standalone signing workflow.",
        "optional_fields": {
            "local_president_signer_email",
            "submitting_member_title",
            "specific_examples_text",
            "work_phone",
            "home_phone",
            "non_work_email",
        },
    },
}


def _placeholder_text(value: object) -> str:
    raw = str(value or "").strip()
    match = _PLACEHOLDER.match(raw)
    if not match:
        return ""
    return match.group(1).strip()


def _is_placeholder(value: object) -> bool:
    return bool(_placeholder_text(value))


def _safe_name(value: str) -> str:
    return _SAFE_FIELD_NAME.sub("_", str(value or "").strip()).strip("_").lower()


def _humanize_key(value: str) -> str:
    text = str(value or "").replace("_", " ").replace(".", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return "Field"
    tokens = text.split(" ")
    out: list[str] = []
    for token in tokens:
        if token.lower() in {"id", "uid", "attuid", "bst", "cwa", "att"}:
            out.append(token.upper())
            continue
        if re.fullmatch(r"q\d+[a-z]?", token.lower()):
            out.append(token.upper())
            continue
        out.append(token.capitalize())
    return " ".join(out)


def _field_type_for(actual_key: str, label: str, placeholder: str, options: tuple[str, ...]) -> str:
    lowered_key = actual_key.lower()
    lowered_label = label.lower()
    lowered_placeholder = placeholder.lower()
    if options:
        return "select"
    if "email" in lowered_key or "email" in lowered_label or "email" in lowered_placeholder:
        return "email"
    if lowered_placeholder == "yyyy-mm-dd" or " date" in lowered_label or lowered_key.endswith("_date") or "_date_" in lowered_key:
        return "date"
    for hint in _LONG_TEXT_KEY_HINTS:
        if hint in lowered_key or hint in lowered_label:
            return "textarea"
    return "text"


def _field_label(actual_key: str, raw_value: object) -> str:
    placeholder = _placeholder_text(raw_value)
    if placeholder and placeholder.lower() != "yyyy-mm-dd":
        return placeholder
    return _humanize_key(actual_key)


def _field_placeholder(actual_key: str, raw_value: object) -> str:
    placeholder = _placeholder_text(raw_value)
    if placeholder.lower() == "yyyy-mm-dd":
        return "YYYY-MM-DD"
    if placeholder and placeholder.lower() != _field_label(actual_key, raw_value).lower():
        return placeholder
    return ""


def _form_description(form_key: str, title: str) -> str:
    override = str(_FORM_OVERRIDES.get(form_key, {}).get("description", "") or "").strip()
    if override:
        return override
    return f"Complete the hosted {title} form and submit it into the grievance workflow."


def _optional_fields(form_key: str) -> set[str]:
    return set(_FORM_OVERRIDES.get(form_key, {}).get("optional_fields", set()))


def _hidden_keys(form_key: str, scope: str) -> set[str]:
    key = "hidden_top_level_keys" if scope == "top_level" else "hidden_template_keys"
    return set(_FORM_OVERRIDES.get(form_key, {}).get(key, set()))


def _derived_values(form_key: str, scope: str) -> dict[str, Callable[[dict[str, str]], object]]:
    key = "derived_top_level_values" if scope == "top_level" else "derived_template_values"
    return dict(_FORM_OVERRIDES.get(form_key, {}).get(key, {}))


def _fixed_values(form_key: str, scope: str) -> dict[str, object]:
    key = "fixed_top_level_values" if scope == "top_level" else "fixed_template_values"
    return dict(_FORM_OVERRIDES.get(form_key, {}).get(key, {}))


def _field_options(form_key: str, actual_key: str) -> tuple[str, ...]:
    mapping = dict(_FORM_OVERRIDES.get(form_key, {}).get("field_options", {}))
    options = mapping.get(actual_key)
    if not options:
        return ()
    return tuple(str(item).strip() for item in options if str(item).strip())


def _build_catalog_field(
    *,
    form_key: str,
    actual_key: str,
    raw_value: object,
    scope: str,
) -> HostedFormField:
    label = _field_label(actual_key, raw_value)
    options = _field_options(form_key, actual_key)
    field_type = _field_type_for(actual_key, label, _placeholder_text(raw_value), options)
    required = _is_placeholder(raw_value) and actual_key not in _optional_fields(form_key)
    return HostedFormField(
        name=_safe_name(actual_key),
        label=label,
        source_scope=scope,
        source_key=actual_key,
        type=field_type,
        required=required,
        placeholder=_field_placeholder(actual_key, raw_value),
        options=options,
    )


def _validate_cleaned_value(field: HostedFormField, raw_value: object) -> str:
    value = str(raw_value or "").strip()
    if field.required and not value:
        raise ValueError(f"{field.label} is required")
    if value and field.type == "email" and "@" not in value:
        raise ValueError(f"{field.label} must be a valid email address")
    return value


def _field_value_map(fields: tuple[HostedFormField, ...], raw_values: dict[str, object]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    seen: set[str] = set()
    for field in fields:
        if field.name in seen:
            raise RuntimeError(f"duplicate hosted form field name: {field.name}")
        seen.add(field.name)
        cleaned[field.name] = _validate_cleaned_value(field, raw_values.get(field.name))
    return cleaned


def _ordered_fields(form_key: str, fields: tuple[HostedFormField, ...]) -> tuple[HostedFormField, ...]:
    wanted = _FORM_FIELD_ORDERS.get(form_key, ())
    if not wanted:
        return fields
    rank = {name: index for index, name in enumerate(wanted)}
    trailing_rank = len(rank)
    ordered = sorted(
        enumerate(fields),
        key=lambda item: (rank.get(item[1].name, trailing_rank + item[0]), item[0]),
    )
    return tuple(field for _index, field in ordered)


def _build_intake_payload_from_catalog(catalog: dict[str, object], cleaned_values: dict[str, str]) -> dict[str, object]:
    form_key = str(catalog["key"])
    top_fields = dict(catalog.get("topLevelFields", {}))
    template_fields = dict(catalog.get("templateDataFields", {}))
    derived_top = _derived_values(form_key, "top_level")
    derived_template = _derived_values(form_key, "template_data")
    fixed_top = _fixed_values(form_key, "top_level")
    fixed_template = _fixed_values(form_key, "template_data")
    hidden_top = _hidden_keys(form_key, "top_level")
    hidden_template = _hidden_keys(form_key, "template_data")
    payload: dict[str, object] = {
        "request_id": cleaned_values.get("request_id") or f"forms-hosted-{form_key}-{uuid4().hex}",
        "document_command": str(catalog.get("documentCommand", "") or "").strip(),
    }

    for actual_key, raw_value in top_fields.items():
        if actual_key in hidden_top or actual_key in derived_top or actual_key in fixed_top:
            continue
        if _is_placeholder(raw_value) or raw_value == "":
            payload[actual_key] = cleaned_values.get(_safe_name(actual_key), "")
        else:
            payload[actual_key] = raw_value
    for actual_key, raw_value in fixed_top.items():
        payload[actual_key] = raw_value
    for actual_key, func in derived_top.items():
        payload[actual_key] = func(cleaned_values)

    template_data: dict[str, object] = {}
    for actual_key, raw_value in template_fields.items():
        if actual_key in hidden_template or actual_key in derived_template or actual_key in fixed_template:
            continue
        if _is_placeholder(raw_value) or raw_value == "":
            template_data[actual_key] = cleaned_values.get(_safe_name(actual_key), "")
        else:
            template_data[actual_key] = raw_value
    template_data.update(fixed_template)
    for actual_key, func in derived_template.items():
        template_data[actual_key] = func(cleaned_values)
    payload["template_data"] = template_data

    document_specs = list(catalog.get("documents", []) or [])
    signer_fields = tuple(_FORM_OVERRIDES.get(form_key, {}).get("signer_fields", ()))
    if document_specs:
        documents: list[dict[str, object]] = []
        for index, doc_spec in enumerate(document_specs):
            signers: list[str] = []
            for signer_field in signer_fields:
                signers.append(cleaned_values.get(signer_field.name, ""))
            documents.append(
                {
                    "doc_type": str(doc_spec.get("doc_type", "") or "").strip(),
                    "template_key": str(doc_spec.get("template_key", "") or "").strip() or None,
                    "requires_signature": bool(doc_spec.get("requires_signature", False)),
                    "signers": signers,
                }
            )
        payload["documents"] = documents
    return payload


def _build_standalone_payload_from_catalog(catalog: dict[str, object], cleaned_values: dict[str, str]) -> dict[str, object]:
    form_key = str(catalog["key"])
    top_fields = dict(catalog.get("topLevelFields", {}))
    template_fields = dict(catalog.get("templateDataFields", {}))
    fixed_top = _fixed_values(form_key, "top_level")
    fixed_template = _fixed_values(form_key, "template_data")
    hidden_top = _hidden_keys(form_key, "top_level")
    hidden_template = _hidden_keys(form_key, "template_data")
    derived_top = _derived_values(form_key, "top_level")
    derived_template = _derived_values(form_key, "template_data")
    payload: dict[str, object] = {
        "request_id": cleaned_values.get("request_id") or f"forms-hosted-{form_key}-{uuid4().hex}",
        "form_key": str(catalog.get("formKey", form_key) or form_key),
    }
    for actual_key, raw_value in top_fields.items():
        if actual_key in hidden_top or actual_key in derived_top or actual_key in fixed_top:
            continue
        if _is_placeholder(raw_value) or raw_value == "":
            payload[actual_key] = cleaned_values.get(_safe_name(actual_key), "")
        else:
            payload[actual_key] = raw_value
    for actual_key, raw_value in fixed_top.items():
        payload[actual_key] = raw_value
    for actual_key, func in derived_top.items():
        payload[actual_key] = func(cleaned_values)

    template_data: dict[str, object] = {}
    for actual_key, raw_value in template_fields.items():
        if actual_key in hidden_template or actual_key in derived_template or actual_key in fixed_template:
            continue
        if _is_placeholder(raw_value) or raw_value == "":
            template_data[actual_key] = cleaned_values.get(_safe_name(actual_key), "")
        else:
            template_data[actual_key] = raw_value
    template_data.update(fixed_template)
    for actual_key, func in derived_template.items():
        template_data[actual_key] = func(cleaned_values)
    payload["template_data"] = template_data
    return payload


def _form_metadata(catalog: dict[str, object]) -> tuple[tuple[str, str], ...]:
    form_key = str(catalog["key"])
    top_fields = dict(catalog.get("topLevelFields", {}))
    metadata: list[tuple[str, str]] = [("Route type", str(catalog["routeType"]))]
    if catalog.get("documentCommand"):
        metadata.append(("Document command", str(catalog["documentCommand"])))
    if catalog.get("formKey"):
        metadata.append(("Standalone form key", str(catalog["formKey"])))
    for actual_key, value in top_fields.items():
        if actual_key == "contract" and not _is_placeholder(value):
            metadata.append(("Contract", str(value)))
    metadata.append(("Backend path", str(catalog["endpointPath"])))
    return tuple(metadata)


def _catalog_fields(catalog: dict[str, object]) -> tuple[HostedFormField, ...]:
    form_key = str(catalog["key"])
    fields: list[HostedFormField] = []
    for actual_key, raw_value in dict(catalog.get("topLevelFields", {})).items():
        if actual_key in _hidden_keys(form_key, "top_level") or actual_key in _derived_values(form_key, "top_level"):
            continue
        if actual_key in _fixed_values(form_key, "top_level") or (not _is_placeholder(raw_value) and raw_value != ""):
            continue
        fields.append(_build_catalog_field(form_key=form_key, actual_key=actual_key, raw_value=raw_value, scope="top_level"))
    for actual_key, raw_value in dict(catalog.get("templateDataFields", {})).items():
        if actual_key in _hidden_keys(form_key, "template_data") or actual_key in _derived_values(form_key, "template_data"):
            continue
        if actual_key in _fixed_values(form_key, "template_data") or (not _is_placeholder(raw_value) and raw_value != ""):
            continue
        fields.append(
            _build_catalog_field(
                form_key=form_key,
                actual_key=actual_key,
                raw_value=raw_value,
                scope="template_data",
            )
        )
    for signer_field in tuple(_FORM_OVERRIDES.get(form_key, {}).get("signer_fields", ())):
        fields.append(signer_field)
    return tuple(fields)


def _build_definition(catalog: dict[str, object]) -> HostedFormDefinition:
    route_type = str(catalog["routeType"])
    form_key = str(catalog["key"])
    fields = _ordered_fields(form_key, _catalog_fields(catalog))
    if route_type == "standalone":
        payload_builder = lambda cleaned_values, _catalog=catalog: _build_standalone_payload_from_catalog(_catalog, cleaned_values)
    else:
        payload_builder = lambda cleaned_values, _catalog=catalog: _build_intake_payload_from_catalog(_catalog, cleaned_values)
    return HostedFormDefinition(
        form_key=form_key,
        title=str(catalog["title"]),
        description=_form_description(form_key, str(catalog["title"])),
        route_type=route_type,
        target_path=str(catalog["endpointPath"]),
        fields=fields,
        metadata=_form_metadata(catalog),
        default_visibility="public",
        default_enabled=True,
        build_payload=lambda values, _fields=fields, _payload_builder=payload_builder: _payload_builder(
            {
                **_field_value_map(_fields, values),
                "request_id": str(values.get("request_id", "") or "").strip(),
            }
        ),
    )


_HOSTED_FORMS: tuple[HostedFormDefinition, ...] = tuple(_build_definition(catalog) for catalog in _FORM_CATALOG)
_HOSTED_FORM_BY_KEY = {definition.form_key: definition for definition in _HOSTED_FORMS}


def hosted_form_keys() -> tuple[str, ...]:
    return tuple(definition.form_key for definition in _HOSTED_FORMS)


def list_hosted_form_definitions() -> tuple[HostedFormDefinition, ...]:
    return _HOSTED_FORMS


def get_hosted_form_definition(form_key: str) -> HostedFormDefinition | None:
    wanted = str(form_key or "").strip()
    if not wanted:
        return None
    direct = _HOSTED_FORM_BY_KEY.get(wanted)
    if direct:
        return direct
    return _HOSTED_FORM_BY_KEY.get(wanted.lower())
