# BellSouth Meeting Request Setup

## Document command

- `bellsouth_meeting_request`

## Important behavior

- This flow is designed to use an existing grievance folder.
- Include `grievance_id` that already exists in SharePoint folder naming.
- If no folder matches, intake returns `422`.
- If multiple folders match, intake returns `409`.

## Recommended Forms questions and key mapping

Core fields:
- Request ID source -> `request_id`
- Grievance ID (existing) -> `grievance_id`
- Contract -> `contract`
- Grievant first/last/email -> `grievant_firstname`, `grievant_lastname`, `grievant_email`
- Incident date (Date) -> `incident_date`
- Narrative/additional notes -> `narrative`

Template-specific (`template_data`):
- To -> `to`
- Request date (Date) -> `request_date`
- Grievant names -> `grievant_names`
- Grievants attending -> `grievants_attending`
- Grievants in attendance -> `grievants_in_attendance`
- Date grievance occurred (Date) -> `date_grievance_occurred`
- Issue/contract section -> `issue_contract_section`
- Informal meeting date (Date) -> `informal_meeting_date`
- Meeting requested date (Date) -> `meeting_requested_date`
- Meeting requested time -> `meeting_requested_time`
- Meeting requested place -> `meeting_requested_place`
- Union rep attending -> `union_rep_attending`
- Union reps in attendance -> `union_reps_in_attendance`
- Company reps in attendance -> `company_reps_in_attendance`
- Additional info -> `additional_info`
- Reply-to name 1 -> `reply_to_name_1`
- Reply-to name 2 -> `reply_to_name_2`
- Reply-to address 1 -> `reply_to_address_1`
- Reply-to address 2 -> `reply_to_address_2`
- Union rep signer email -> `union_rep_email`

## HTTP body skeleton

```json
{
  "request_id": "forms-<responseId>",
  "document_command": "bellsouth_meeting_request",
  "grievance_id": "2026001",
  "contract": "BellSouth",
  "grievant_firstname": "<first>",
  "grievant_lastname": "<last>",
  "grievant_email": "<email>",
  "incident_date": "<yyyy-mm-dd>",
  "narrative": "<notes>",
  "template_data": {
    "union_rep_email": "<rep email>",
    "to": "<to>",
    "request_date": "<yyyy-mm-dd>",
    "grievant_names": "<name list>",
    "grievants_attending": "<names>",
    "grievants_in_attendance": "<names>",
    "date_grievance_occurred": "<yyyy-mm-dd>",
    "issue_contract_section": "<article/section>",
    "informal_meeting_date": "<yyyy-mm-dd>",
    "meeting_requested_date": "<yyyy-mm-dd>",
    "meeting_requested_time": "<time>",
    "meeting_requested_place": "<place>",
    "union_rep_attending": "<name>",
    "union_reps_in_attendance": "<names>",
    "company_reps_in_attendance": "<names>",
    "additional_info": "<details>",
    "reply_to_name_1": "<name>",
    "reply_to_name_2": "<name>",
    "reply_to_address_1": "<address>",
    "reply_to_address_2": "<address>"
  }
}
```

## Signer behavior

Default signer resolution:
1. `template_data.union_rep_email`
2. `grievant_email`
3. Explicit `documents[].signers` overrides both.
