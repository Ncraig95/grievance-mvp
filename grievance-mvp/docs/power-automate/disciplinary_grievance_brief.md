# Disciplinary Grievance Brief Setup

## Document command

- Preferred alias: `disciplinary_brief`
- Full key also works: `disciplinary_grievance_brief`

## Recommended Forms questions and key mapping

Core fields:
- `request_id`, `contract`, `grievant_firstname`, `grievant_lastname`, `grievant_email`, `narrative`

Template-specific (`template_data`):
- `appealed_to_state_date` (Date)
- `articles`
- `attachment_1_name` ... `attachment_18_name`
- `attachment_1_date` ... `attachment_18_date` (Date)
- `company_argument`
- `company_facts`
- `company_name`
- `company_proposed_settlement`
- `current_status`
- `date_discipline_grieved` (Date)
- `date_grievance_occurred` (Date)
- `department`
- `disparate_treatment`
- `grievance_type`
- `grievant_city`
- `grievant_name`
- `grievant_phone`
- `grievant_state`
- `grievant_street`
- `grievant_zip`
- `local_city`
- `local_number`
- `local_phone`
- `local_state`
- `local_street`
- `local_zip`
- `management_structure`
- `other_related_grievances`
- `outside_remedies`
- `seniority_date` (Date)
- `step1_informal_date` (Date)
- `step2_formal_date` (Date)
- `title`
- `union_argument`
- `union_facts`
- `union_proposed_settlement`
- `union_representation`
- optional signer override: `signer_email`

## HTTP body skeleton

```json
{
  "request_id": "forms-<responseId>",
  "document_command": "disciplinary_brief",
  "contract": "CWA",
  "grievant_firstname": "<first>",
  "grievant_lastname": "<last>",
  "grievant_email": "<email>",
  "narrative": "Disciplinary grievance brief",
  "template_data": {
    "grievant_name": "<full name>",
    "date_grievance_occurred": "<yyyy-mm-dd>",
    "date_discipline_grieved": "<yyyy-mm-dd>",
    "articles": "<articles>",
    "company_facts": "<company facts>",
    "union_facts": "<union facts>",
    "signer_email": "<optional signer email>"
  }
}
```

Add all remaining fields from the list above as needed by your form.
