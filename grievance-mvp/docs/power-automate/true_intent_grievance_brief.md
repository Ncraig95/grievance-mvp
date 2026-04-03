# True Intent Grievance Brief Form + Power Automate Handoff

## Use This Guide For

- Building the Microsoft Form for the True Intent Grievance Brief.
- Building the Power Automate flow that posts the response to the intake API.
- Understanding which values come from Forms, which values are fixed in the flow, and which values should be composed inside Power Automate.

## Workflow Summary

- Preferred document command: `true_intent_brief`
- Full key also works: `true_intent_grievance_brief`
- API endpoint: `POST /intake`
- Contract: `CWA`
- Request id pattern: `forms-<Response Id>`
- Default signer behavior:
  `template_data.signer_email` when explicitly supplied, otherwise `grievant_email`
- Default narrative:
  `True intent grievance brief`
- `grievance_id` is not required for this workflow.
- `documents` is not required for the standard setup of this brief.
- The repo already supports the template and intake side. The remaining work is the Microsoft Form, the Power Automate flow, and the real published Form URL.

## Who Should Submit This Form

- Use this form when a steward, officer, or local representative needs to prepare a True Intent grievance brief for intake into the grievance system.
- After submission, the API creates the case intake, renders the document, routes the signing workflow, and continues the normal filing flow used by the app.

## Form Metadata

- Form title:
  `True Intent Grievance Brief`
- Form description:
  `Use this form to prepare and submit a True Intent grievance brief for intake into the grievance system. After submission, the brief is rendered into the app workflow and routed for signature using the configured signer rules.`

## Do Not Add These As Forms Questions

- `request_id`
  Build this in Power Automate from the Forms Response Id.
- `document_command`
  Keep this fixed as `true_intent_brief` in the flow.
- `contract`
  Keep this fixed as `CWA` in the flow.
- `narrative`
  Keep this fixed as `True intent grievance brief` unless you intentionally want a different fixed summary.
- `template_data.grievant_name`
  Compose this in the flow from first name and last name.
- DocuSeal signature anchors
  Do not create Forms questions for template signature fields.
- Attachment uploads
  `attachment_1` through `attachment_10` are text labels or exhibit names, not Microsoft Forms file-upload controls.

## Forms Build Sheet

Use the generated pack in `scripts/power-platform/output/true_intent_brief/` if you want the CSV and JSON versions of this mapping.

### Grievant And Local Questions

| Question text | Type | Required | Payload key | Notes |
| --- | --- | --- | --- | --- |
| Grievant first name | Text | Yes | `grievant_firstname` | Core intake field. |
| Grievant last name | Text | Yes | `grievant_lastname` | Core intake field. |
| Grievant email | Email | Yes | `grievant_email` | Core intake field and default signer fallback. |
| Grievant phone | Text | Yes | `template_data.grievant_phone` | Use a normal phone text field. |
| Grievant street | Text | Yes | `template_data.grievant_street` | Street address only. |
| Grievant city | Text | Yes | `template_data.grievant_city` |  |
| Grievant state | Text | Yes | `template_data.grievant_state` | Use the format your operators expect, for example `FL`. |
| Grievant zip | Text | Yes | `template_data.grievant_zip` | Keep this as text, not numeric. |
| Grievant title | Text | Yes | `template_data.title` | Job title or position title. |
| Department | Text | Yes | `template_data.department` |  |
| Seniority date | Date | No | `template_data.seniority_date` |  |
| Local number | Text | Yes | `template_data.local_number` | For example `3106`. |
| Local phone | Text | Yes | `template_data.local_phone` |  |
| Local street | Text | Yes | `template_data.local_street` |  |
| Local city | Text | Yes | `template_data.local_city` |  |
| Local state | Text | Yes | `template_data.local_state` |  |
| Local zip | Text | Yes | `template_data.local_zip` | Keep this as text, not numeric. |

### Grievance Questions

| Question text | Type | Required | Payload key | Notes |
| --- | --- | --- | --- | --- |
| Date grievance occurred | Date | Yes | `template_data.date_grievance_occurred` |  |
| Grievance type | Text | Yes | `template_data.grievance_type` | Short classification or category. |
| Issue involved | Long text | Yes | `template_data.issue_involved` | Main issue summary. |
| Articles involved | Long text | Yes | `template_data.articles` | Contract articles, sections, or clauses. |
| Management structure | Long text | No | `template_data.management_structure` | Optional organizational context. |
| Step 1 informal date | Date | No | `template_data.step1_informal_date` |  |
| Step 2 formal date | Date | No | `template_data.step2_formal_date` |  |
| Appealed to state date | Date | No | `template_data.appealed_to_state_date` |  |
| Timeline | Long text | Yes | `template_data.timeline` | Sequence of events or key dates. |

### Position And Analysis Questions

| Question text | Type | Required | Payload key | Notes |
| --- | --- | --- | --- | --- |
| Union argument | Long text | Yes | `template_data.argument` | Main union argument for the brief. |
| Analysis | Long text | Yes | `template_data.analysis` | Overall analysis of the case. |
| Company name | Text | Yes | `template_data.company_name` | For example `AT&T`. |
| Company position | Long text | Yes | `template_data.company_position` | Company's stated position. |
| Company strengths | Long text | No | `template_data.company_strengths` | Optional. |
| Company weaknesses | Long text | No | `template_data.company_weaknesses` | Optional. |
| Company proposed settlement | Long text | No | `template_data.company_proposed_settlement` | Optional. |
| Union position | Long text | Yes | `template_data.union_position` | Union's stated position. |
| Union strengths | Long text | No | `template_data.union_strengths` | Optional. |
| Union weaknesses | Long text | No | `template_data.union_weaknesses` | Optional. |
| Union proposed settlement | Long text | No | `template_data.union_proposed_settlement` | Optional. |

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
  Set this to `true_intent_brief`.
- `contract`
  Set this to `CWA`.
- `narrative`
  Set this to `True intent grievance brief`.
- `template_data.grievant_name`
  Compose this as first name plus last name.
- API URL
  Use `https://api.cwa3106.org/intake`.
- Do not send `grievance_id` for the standard True Intent Brief flow.
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
  "document_command": "true_intent_brief",
  "contract": "CWA",
  "grievant_firstname": "<Grievant first name>",
  "grievant_lastname": "<Grievant last name>",
  "grievant_email": "<Grievant email>",
  "narrative": "True intent grievance brief",
  "template_data": {
    "grievant_name": "<Compose first name + last name>",
    "date_grievance_occurred": "<Date grievance occurred>",
    "grievant_phone": "<Grievant phone>",
    "grievant_street": "<Grievant street>",
    "grievant_city": "<Grievant city>",
    "grievant_state": "<Grievant state>",
    "grievant_zip": "<Grievant zip>",
    "title": "<Grievant title>",
    "department": "<Department>",
    "seniority_date": "<Seniority date>",
    "local_number": "<Local number>",
    "local_phone": "<Local phone>",
    "local_street": "<Local street>",
    "local_city": "<Local city>",
    "local_state": "<Local state>",
    "local_zip": "<Local zip>",
    "grievance_type": "<Grievance type>",
    "issue_involved": "<Issue involved>",
    "articles": "<Articles involved>",
    "management_structure": "<Management structure>",
    "step1_informal_date": "<Step 1 informal date>",
    "step2_formal_date": "<Step 2 formal date>",
    "appealed_to_state_date": "<Appealed to state date>",
    "timeline": "<Timeline>",
    "argument": "<Union argument>",
    "analysis": "<Analysis>",
    "company_name": "<Company name>",
    "company_position": "<Company position>",
    "company_strengths": "<Company strengths>",
    "company_weaknesses": "<Company weaknesses>",
    "company_proposed_settlement": "<Company proposed settlement>",
    "union_position": "<Union position>",
    "union_strengths": "<Union strengths>",
    "union_weaknesses": "<Union weaknesses>",
    "union_proposed_settlement": "<Union proposed settlement>",
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
  `scripts/power-platform/output/true_intent_brief/`
