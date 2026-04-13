# Data Request Letterhead Setup

## Document command

- `data_request_letterhead`

## Recommended Forms questions and key mapping

Core fields:
- Request ID source -> `request_id`
- Existing grievance number/id -> `grievance_id`
- Contract -> `contract`
- Grievant first name -> `grievant_firstname`
- Grievant last name -> `grievant_lastname`
- Grievant email -> `grievant_email`
- Narrative -> `narrative`

Template-specific (`template_data`):
- Company representative name -> `company_rep_name`
- Company representative title -> `company_rep_title`
- Company representative email -> `company_rep_email`
- Requested information list -> `data_requested`
- Preferred delivery format -> `preferred_format`
- Union representative name -> `steward_name`
- Union representative email -> `steward_email`
- Optional approval block -> `approver_block`

Derived by the API:
- `template_data.grievance_number` from `grievance_id`
- `template_data.grievant_name` from first + last name
- `template_data.today_date` from the submission date

## HTTP body skeleton

```json
{
  "request_id": "forms-<responseId>",
  "document_command": "data_request_letterhead",
  "grievance_id": "<existing grievance id>",
  "contract": "<contract>",
  "grievant_firstname": "<first>",
  "grievant_lastname": "<last>",
  "grievant_email": "<email>",
  "narrative": "Data request cover letter generated from intake",
  "template_data": {
    "company_rep_name": "<rep name>",
    "company_rep_title": "<rep title>",
    "company_rep_email": "<rep email>",
    "data_requested": "<requested items>",
    "preferred_format": "<preferred delivery format>",
    "steward_name": "<union rep name>",
    "steward_email": "<union rep email>",
    "approver_block": "<optional approval block>"
  }
}
```

## Existing-folder behavior

- This document targets an existing grievance folder.
- `grievance_id` is required.
- `grievance_number` and `grievance_id` are treated as the same identifier for this flow.
- The template is an unsigned cover letter, so it should be stored alongside the matching grievance data request form.
