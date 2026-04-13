# Grievance Data Request Form Setup

## Document command

- Preferred alias: `grievance_data_request`
- Full key also works: `grievance_data_request_form`

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
- Articles requested -> `articles`
- Company name -> `company_name`
- Company representative name -> `company_rep_name`
- Company representative title -> `company_rep_title`
- Due date (Date) -> `due_date`
- Grievant display name -> `grievant_name`
- Request date (Date) -> `today_date`
- Union phone -> `union_phone`
- Union representative name -> `union_rep_name`
- Union representative title -> `union_rep_title`

## HTTP body skeleton

```json
{
  "request_id": "forms-<responseId>",
  "document_command": "grievance_data_request",
  "grievance_id": "<existing grievance id>",
  "contract": "<contract>",
  "grievant_firstname": "<first>",
  "grievant_lastname": "<last>",
  "grievant_email": "<email>",
  "narrative": "Data request generated from intake",
  "template_data": {
    "articles": "<articles>",
    "company_name": "<company>",
    "company_rep_name": "<rep name>",
    "company_rep_title": "<rep title>",
    "due_date": "<yyyy-mm-dd>",
    "grievant_name": "<full name>",
    "today_date": "<yyyy-mm-dd>",
    "union_phone": "<union phone>",
    "union_rep_name": "<union rep>",
    "union_rep_title": "<union rep title>"
  }
}
```

## Existing-folder behavior

- This document now targets an existing grievance folder.
- `grievance_id` is required.
- `grievance_number` and `grievance_id` are treated as the same identifier for this flow.

## Signer behavior

If this doc needs signature routing, include one of:
- `documents[].signers` (preferred explicit)
- `template_data.signer_email`
- fallback is `grievant_email`
