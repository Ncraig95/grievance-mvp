# Mobility Record of Grievance Setup

## Document command

- Preferred key: `mobility_record_of_grievance`
- API endpoint: `POST /intake`
- Template key: `mobility_record_of_grievance`

## Workflow shape

- This is a case-linked staged grievance document.
- `grievance_id` is required and must match an existing SharePoint case folder.
- `documents[0].signers` must contain exactly 3 emails in this order:
  1. union filing signer
  2. company responder
  3. union appeal or final signer
- Intake sends only stage 1.
- Webhook completion auto-advances stages 2 and 3.

## Do not build Forms questions for stage-owned DocuSeal fields

Stage 1 DocuSeal-owned:
- `union_position_first_level`

Stage 2 DocuSeal-owned:
- `company_statement_first_level`
- `proposed_disposition_first_level`
- `company_position_first_level`
- `proposed_disposition_second_level`
- `company_position_second_level`

Stage 3 DocuSeal-owned:
- `union_disposition_first_level`
- `union_disposition_second_level`
- `union_position_second_level`

Those values are completed on the signer links, not in Microsoft Forms.

## Recommended Forms questions and key mapping

Core intake fields:
- `request_id`
- `grievance_id`
- `contract`
- `grievant_firstname`
- `grievant_lastname`
- `grievant_email`
- `incident_date`
- `narrative`

Document-specific `template_data` fields:
- `district_grievance_number`
- `date_grievance_occurred`
- `department`
- `specific_location_state`
- `local_number`
- `employee_work_group_name`
- `job_title`
- `ncs_date`
- `union_statement`
- `contract_articles`
- `date_informal`
- `date_first_step_requested`
- `date_first_step_held`

## HTTP body skeleton

```json
{
  "request_id": "forms-mobility-record-<responseId>",
  "document_command": "mobility_record_of_grievance",
  "grievance_id": "2026001",
  "contract": "AT&T Mobility",
  "grievant_firstname": "<first>",
  "grievant_lastname": "<last>",
  "grievant_email": "<email>",
  "incident_date": "<yyyy-mm-dd>",
  "narrative": "<initial union statement>",
  "documents": [
    {
      "doc_type": "mobility_record_of_grievance",
      "template_key": "mobility_record_of_grievance",
      "requires_signature": true,
      "signers": [
        "<union_stage1_email>",
        "<company_stage2_email>",
        "<union_stage3_email>"
      ]
    }
  ],
  "template_data": {
    "district_grievance_number": "",
    "date_grievance_occurred": "<yyyy-mm-dd>",
    "department": "",
    "specific_location_state": "",
    "local_number": "3106",
    "employee_work_group_name": "<member or work group>",
    "job_title": "",
    "ncs_date": "<yyyy-mm-dd>",
    "union_statement": "<initial statement>",
    "contract_articles": "",
    "date_informal": "<yyyy-mm-dd>",
    "date_first_step_requested": "<yyyy-mm-dd>",
    "date_first_step_held": "<yyyy-mm-dd>"
  }
}
```

## Notes

- `cw_grievance_number` is filled by the app from `grievance_number` when present, otherwise from `grievance_id`.
- Keep stage 2 and stage 3 narrative fields out of the intake payload.
- Reuse the same `request_id` if you replay the same Forms response.
- This workflow uses `existing_exact_grievance_id` folder resolution, not grievance number matching.
