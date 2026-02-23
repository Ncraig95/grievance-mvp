# True Intent Grievance Brief Setup

## Document command

- Preferred alias: `true_intent_brief`
- Full key also works: `true_intent_grievance_brief`

## Recommended Forms questions and key mapping

Core fields:
- `request_id`, `contract`, `grievant_firstname`, `grievant_lastname`, `grievant_email`, `narrative`

Template-specific (`template_data`):
- `analysis`
- `appealed_to_state_date` (Date)
- `argument`
- `articles`
- `attachment_1` ... `attachment_10`
- `company_name`
- `company_position`
- `company_proposed_settlement`
- `company_strengths`
- `company_weaknesses`
- `date_grievance_occurred` (Date)
- `department`
- `grievance_type`
- `grievant_city`
- `grievant_name`
- `grievant_phone`
- `grievant_state`
- `grievant_street`
- `grievant_zip`
- `issue_involved`
- `local_city`
- `local_number`
- `local_phone`
- `local_state`
- `local_street`
- `local_zip`
- `management_structure`
- `seniority_date` (Date)
- `step1_informal_date` (Date)
- `step2_formal_date` (Date)
- `timeline`
- `title`
- `union_position`
- `union_proposed_settlement`
- `union_strengths`
- `union_weaknesses`
- optional signer override: `signer_email`

## HTTP body skeleton

```json
{
  "request_id": "forms-<responseId>",
  "document_command": "true_intent_brief",
  "contract": "CWA",
  "grievant_firstname": "<first>",
  "grievant_lastname": "<last>",
  "grievant_email": "<email>",
  "narrative": "True intent grievance brief",
  "template_data": {
    "grievant_name": "<full name>",
    "date_grievance_occurred": "<yyyy-mm-dd>",
    "articles": "<articles>",
    "argument": "<argument>",
    "analysis": "<analysis>",
    "company_position": "<company position>",
    "union_position": "<union position>",
    "signer_email": "<optional signer email>"
  }
}
```

Add all remaining fields from the list above as needed by your form.
