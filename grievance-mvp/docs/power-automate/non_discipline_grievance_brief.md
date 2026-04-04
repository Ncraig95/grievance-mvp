# Non-Discipline Grievance Brief Form + Power Automate Handoff

## Use This Guide For

- Building the Microsoft Form for the Non-Discipline Grievance Brief.
- Building the Power Automate flow that posts the response to the intake API.
- Implementing the 2010 staff guide as a first-class workflow option instead of folding it into another brief type.

## Workflow Summary

- Preferred document command: `non_discipline_brief`
- Full key also works: `non_discipline_grievance_brief`
- Compatibility aliases also work: `non_disciplinary_brief`, `non_disciplinary_grievance_brief`
- API endpoint: `POST /intake`
- Contract: `CWA`
- Request id pattern: `forms-<Response Id>`
- Default signer behavior:
  `template_data.signer_email` when explicitly supplied, otherwise `grievant_email`
- Default narrative:
  `Non-discipline grievance brief`
- `grievance_id` is not required for this workflow.
- `documents` is not required for the standard setup of this brief.
- Canonical source template:
  `Docx Files Template/Non Discipline guide to staff 2010.docx`

## Who Should Submit This Form

- Use this form when a steward, officer, or local representative needs to prepare the non-discipline grievance guide as a formal intake document.
- After submission, the API creates the case, renders the non-discipline brief, routes signature collection, and continues the normal filing flow used by the app.

## Form Metadata

- Form title:
  `Non-Discipline Grievance Brief`
- Form description:
  `Use this form to prepare and submit a non-discipline grievance brief for intake into the grievance system. After submission, the brief is rendered into the app workflow and routed for signature using the configured signer rules.`

## Do Not Add These As Forms Questions

- `request_id`
  Build this in Power Automate from the Forms Response Id.
- `document_command`
  Keep this fixed as `non_discipline_brief` in the flow.
- `contract`
  Keep this fixed as `CWA` in the flow.
- `narrative`
  Keep this fixed as `Non-discipline grievance brief` unless you intentionally want a different fixed summary.
- `template_data.grievant_name`
  Compose this in the flow from first name and last name.
- DocuSeal signature anchors
  Do not create Forms questions for template signature fields.
- Attachment uploads
  `attachment_1` through `attachment_10` are text labels or exhibit names, not Microsoft Forms file-upload controls.

## Forms Build Sheet

Use the generated pack in `scripts/power-platform/output/non_discipline_brief/` if you want the CSV and JSON versions of this mapping.

### Core Intake Questions

| Question text | Type | Required | Payload key | Notes |
| --- | --- | --- | --- | --- |
| Grievant first name | Text | Yes | `grievant_firstname` | Core intake field. |
| Grievant last name | Text | Yes | `grievant_lastname` | Core intake field. |
| Grievant email | Email | Yes | `grievant_email` | Core intake field and default signer fallback. |
| Local number | Text | Yes | `template_data.local_number` | For example `3106`. |
| Local grievance number | Text | No | `template_data.local_grievance_number` | Local-facing display number. |
| Location | Text | Yes | `template_data.location` | For example `Jacksonville, FL`. |
| Grievant(s) or work group | Text | Yes | `template_data.grievant_or_work_group` | Guide wording from the 2010 document. |
| Grievant home address | Long text | Yes | `template_data.grievant_home_address` | Full address block. |

### Date Questions

| Question text | Type | Required | Payload key | Notes |
| --- | --- | --- | --- | --- |
| Date grievance occurred | Date | Yes | `template_data.date_grievance_occurred` |  |
| Date grievance filed | Date | Yes | `template_data.date_grievance_filed` |  |
| Date grievance appealed to executive level | Date | No | `template_data.date_grievance_appealed_to_executive_level` |  |

### Guide Section Questions

| Question text | Type | Required | Payload key | Notes |
| --- | --- | --- | --- | --- |
| Issue or condition involved | Long text | Yes | `template_data.issue_or_condition_involved` | Section I. |
| Action taken | Long text | Yes | `template_data.action_taken` | Section II. |
| Chronology of facts pertaining to grievance | Long text | Yes | `template_data.chronology_of_facts` | Section III. |
| Analysis of grievance | Long text | Yes | `template_data.analysis_of_grievance` | Section IV. |
| Current status of grievant or condition | Long text | Yes | `template_data.current_status` | Section V. |
| Union position | Long text | Yes | `template_data.union_position` | Section VI. |
| Company position | Long text | Yes | `template_data.company_position` | Section VII. |
| Potential witnesses | Long text | No | `template_data.potential_witnesses` | Section VIII. |
| Recommendation | Long text | Yes | `template_data.recommendation` | Section IX. |

### Attachment And Routing Questions

| Question text | Type | Required | Payload key | Notes |
| --- | --- | --- | --- | --- |
| Attachment 1 label | Text | No | `template_data.attachment_1` | Exhibit name, description, or filename only. |
| Attachment 2 label | Text | No | `template_data.attachment_2` | Exhibit name, description, or filename only. |
| Attachment 3 label | Text | No | `template_data.attachment_3` | Exhibit name, description, or filename only. |
| Attachment 4 label | Text | No | `template_data.attachment_4` | Exhibit name, description, or filename only. |
| Attachment 5 label | Text | No | `template_data.attachment_5` | Exhibit name, description, or filename only. |
| Attachment 6 label | Text | No | `template_data.attachment_6` | Exhibit name, description, or filename only. |
| Attachment 7 label | Text | No | `template_data.attachment_7` | Exhibit name, description, or filename only. |
| Attachment 8 label | Text | No | `template_data.attachment_8` | Exhibit name, description, or filename only. |
| Attachment 9 label | Text | No | `template_data.attachment_9` | Exhibit name, description, or filename only. |
| Attachment 10 label | Text | No | `template_data.attachment_10` | Exhibit name, description, or filename only. |
| Signer email override | Email | No | `template_data.signer_email` | Optional. Leave blank unless you need to override the default signer routing. |

## Fixed Flow Values And Non-Question Values

- `request_id`
  Build this as `forms-<Response Id>`.
- `document_command`
  Set this to `non_discipline_brief`.
- `contract`
  Set this to `CWA`.
- `narrative`
  Set this to `Non-discipline grievance brief`.
- `template_data.grievant_name`
  Compose this as first name plus last name.
- API URL
  Use `https://api.cwa3106.org/intake`.
- Do not send `grievance_id` for the standard Non-Discipline Brief flow.
- Do not send `documents` unless you intentionally want explicit signer control beyond the normal signer fallback logic.

## Power Automate Build

### Trigger And Response Lookup

1. Add trigger:
   `When a new response is submitted`
2. Add action:
   `Get response details`
3. Use the same Form Id from the trigger in `Get response details`.

### Compose Values

4. Add a `Compose` action for `request_id`.
   Build it as `forms-` plus the Microsoft Forms Response Id.
5. Add a `Compose` action for `template_data.grievant_name`.
   Build it as first name plus a space plus last name.

### HTTP Action

6. Add an `HTTP` action.
- Method:
  `POST`
- URL:
  `https://api.cwa3106.org/intake`
- Headers:
  `Content-Type: application/json`
  Add intake auth headers if they are enabled in `config.yaml`.

### HTTP Body

7. Use this JSON body shape and replace the placeholders with the matching Forms dynamic values or Compose outputs:

```json
{
  "request_id": "forms-<Response Id>",
  "document_command": "non_discipline_brief",
  "contract": "CWA",
  "grievant_firstname": "<Grievant first name>",
  "grievant_lastname": "<Grievant last name>",
  "grievant_email": "<Grievant email>",
  "narrative": "Non-discipline grievance brief",
  "template_data": {
    "grievant_name": "<Compose first name + last name>",
    "local_number": "<Local number>",
    "local_grievance_number": "<Local grievance number>",
    "location": "<Location>",
    "grievant_or_work_group": "<Grievant(s) or work group>",
    "grievant_home_address": "<Grievant home address>",
    "date_grievance_occurred": "<Date grievance occurred>",
    "date_grievance_filed": "<Date grievance filed>",
    "date_grievance_appealed_to_executive_level": "<Date grievance appealed to executive level>",
    "issue_or_condition_involved": "<Issue or condition involved>",
    "action_taken": "<Action taken>",
    "chronology_of_facts": "<Chronology of facts pertaining to grievance>",
    "analysis_of_grievance": "<Analysis of grievance>",
    "current_status": "<Current status of grievant or condition>",
    "union_position": "<Union position>",
    "company_position": "<Company position>",
    "potential_witnesses": "<Potential witnesses>",
    "recommendation": "<Recommendation>",
    "attachment_1": "<Attachment 1 label>",
    "attachment_2": "<Attachment 2 label>",
    "attachment_3": "<Attachment 3 label>",
    "attachment_4": "<Attachment 4 label>",
    "attachment_5": "<Attachment 5 label>",
    "attachment_6": "<Attachment 6 label>",
    "attachment_7": "<Attachment 7 label>",
    "attachment_8": "<Attachment 8 label>",
    "attachment_9": "<Attachment 9 label>",
    "attachment_10": "<Attachment 10 label>",
    "signer_email": "<Signer email override>"
  }
}
```

## Shared Form / Shared Flow Option

If you want one Microsoft Form and one Power Automate flow for both brief types:

1. Add a required `Brief type` question to the Form.
2. Use choices:
   `True Intent Brief`
   `Non-Discipline Brief`
3. Use Microsoft Forms branching so the submitter sees only the section set for the chosen brief.
4. In Power Automate, add a `Switch` on `Brief type`.
5. For the Non-Discipline branch, keep:
   `document_command = non_discipline_brief`
   `narrative = Non-discipline grievance brief`
6. For the True Intent branch, send the payload documented in `true_intent_grievance_brief.md`.

## Response Handling

8. Parse the JSON response from the HTTP action.
9. Store or log at least:
   `case_id`
   `grievance_id`
   `documents[0].signing_link` when present

## Go-Live Notes

- If you do not need a signer override, either leave `template_data.signer_email` blank in the Form or remove that property from the JSON body.
- Reuse the same `request_id` when replaying the same Microsoft Forms response to avoid duplicate submissions.
- Replace placeholder published Form URLs in repo docs after the Form is published.
- Run one real tenant submission after building the flow and verify case creation, rendered document output, signing-link delivery, and SharePoint filing.
- The generated operator pack for this form is in:
  `scripts/power-platform/output/non_discipline_brief/`
