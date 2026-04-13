# Statement of Occurrence Setup

## Document command

- `statement_of_occurrence`

## Recommended Forms questions and key mapping

Create these questions in Forms and map to these payload keys.

Core fields:
- Request ID source: use Forms response id -> `request_id` (`forms-<responseId>`)
- Contract (choice) -> `contract`
- Grievant first name -> `grievant_firstname`
- Grievant last name -> `grievant_lastname`
- Grievant email -> `grievant_email`
- Grievant phone -> `grievant_phone`
- Work location/address -> `work_location`
- Supervisor name -> `supervisor`
- Incident date (Date) -> `incident_date`
- Narrative/statement -> `narrative`

Template-specific (`template_data`):
- Home address -> `home_address`
- Seniority date (Date) -> `seniority_date`
- NCS date (Date) -> `ncs_date`
- Personal cell -> `personal_cell`
- Personal email (signer target) -> `personal_email`
- Department -> `department`
- Title -> `title`
- Supervisor phone -> `supervisor_phone`
- Supervisor email -> `supervisor_email`
- Grievant UID -> `grievants uid`
- Article violated -> `article`
- Witness 1 name/title/phone -> `witness_1_name`, `witness_1_title`, `witness_1_phone`
- Witness 2 name/title/phone -> `witness_2_name`, `witness_2_title`, `witness_2_phone`
- Witness 3 name/title/phone -> `witness_3_name`, `witness_3_title`, `witness_3_phone`

## HTTP body skeleton

```json
{
  "request_id": "forms-<responseId>",
  "document_command": "statement_of_occurrence",
  "contract": "<contract choice>",
  "grievant_firstname": "<first>",
  "grievant_lastname": "<last>",
  "grievant_email": "<email>",
  "grievant_phone": "<phone>",
  "work_location": "<work location>",
  "supervisor": "<supervisor>",
  "incident_date": "<yyyy-mm-dd>",
  "narrative": "<statement text>",
  "template_data": {
    "home_address": "<home address>",
    "seniority_date": "<yyyy-mm-dd>",
    "ncs_date": "<yyyy-mm-dd>",
    "personal_cell": "<cell>",
    "personal_email": "<signer email>",
    "department": "<department>",
    "title": "<title>",
    "supervisor_phone": "<supervisor phone>",
    "supervisor_email": "<supervisor email>",
    "grievants uid": "<uid>",
    "article": "<article>",
    "witness_1_name": "",
    "witness_1_title": "",
    "witness_1_phone": "",
    "witness_2_name": "",
    "witness_2_title": "",
    "witness_2_phone": "",
    "witness_3_name": "",
    "witness_3_title": "",
    "witness_3_phone": ""
  }
}
```

## Signer behavior

Default signer resolution for this doc:
1. `template_data.personal_email`
2. `grievant_email`
