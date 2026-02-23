# Mobility Meeting Request Setup

## Document command

- Preferred alias: `mobility_meeting_request`
- Full key also works: `mobility_formal_grievance_meeting_request`

## Important behavior

Use existing grievance folder resolution same as BellSouth; include `grievance_id` of existing case folder.

## Recommended Forms questions and key mapping

Use the same question structure as BellSouth and map to the same keys:

Core fields:
- `request_id`, `grievance_id`, `contract`, `grievant_firstname`, `grievant_lastname`, `grievant_email`, `incident_date`, `narrative`

Template-specific (`template_data`):
- `to`
- `request_date`
- `grievant_names`
- `grievants_attending`
- `grievants_in_attendance`
- `date_grievance_occurred`
- `issue_contract_section`
- `informal_meeting_date`
- `meeting_requested_date`
- `meeting_requested_time`
- `meeting_requested_place`
- `union_rep_attending`
- `union_reps_in_attendance`
- `company_reps_in_attendance`
- `additional_info`
- `reply_to_name_1`
- `reply_to_name_2`
- `reply_to_address_1`
- `reply_to_address_2`
- `union_rep_email`

## HTTP body skeleton

```json
{
  "request_id": "forms-<responseId>",
  "document_command": "mobility_meeting_request",
  "grievance_id": "2026001",
  "contract": "Mobility",
  "grievant_firstname": "<first>",
  "grievant_lastname": "<last>",
  "grievant_email": "<email>",
  "incident_date": "<yyyy-mm-dd>",
  "narrative": "<notes>",
  "template_data": {
    "union_rep_email": "<rep email>",
    "to": "",
    "request_date": "",
    "grievant_names": "",
    "grievants_attending": "",
    "grievants_in_attendance": "",
    "date_grievance_occurred": "",
    "issue_contract_section": "",
    "informal_meeting_date": "",
    "meeting_requested_date": "",
    "meeting_requested_time": "",
    "meeting_requested_place": "",
    "union_rep_attending": "",
    "union_reps_in_attendance": "",
    "company_reps_in_attendance": "",
    "additional_info": "",
    "reply_to_name_1": "",
    "reply_to_name_2": "",
    "reply_to_address_1": "",
    "reply_to_address_2": ""
  }
}
```
