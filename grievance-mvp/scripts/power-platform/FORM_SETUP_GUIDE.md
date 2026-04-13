# Form Setup Guide

Use this file as the operator checklist.
Use the detailed field-map docs under `grievance-mvp/docs/power-automate/` when you are actually building the Microsoft Form and the Power Automate flow.

## Before you start

1. Copy `forms.local.example.json` to `forms.local.json`.
2. Fill in the real Microsoft Form IDs, published URLs, and the intended flow display names.
3. Run `.\Install-PowerPlatformPrereqs.ps1`.
4. Generate a starter payload for the form you are building with `.\New-GrievancePayloadTemplate.ps1`.

## Statement of Occurrence

- Detailed doc: `docs/power-automate/statement_of_occurrence.md`
- Endpoint: `POST /intake`
- Document command: `statement_of_occurrence`
- Build the Form manually from the detailed doc.
- Include the statement-specific top-level fields like `grievant_phone`, `work_location`, `supervisor`, and `incident_date`.
- Keep `template_data.personal_email` if you want the signer to route to a personal email; otherwise the app falls back to `grievant_email`.
- Generate a starter payload with:
  `.\New-GrievancePayloadTemplate.ps1 -FormKey statement_of_occurrence -OutputPath .\output\statement_of_occurrence.payload.json -Overwrite`

## BellSouth Meeting Request

- Detailed doc: `docs/power-automate/bellsouth_meeting_request.md`
- Endpoint: `POST /intake`
- Document command: `bellsouth_meeting_request`
- This form requires an existing `grievance_id`.
- If the SharePoint folder does not exist, the API returns `422`.
- Use `template_data.union_rep_email` when you know the signer email. Otherwise the app falls back to `grievant_email`.
- Generate a starter payload with:
  `.\New-GrievancePayloadTemplate.ps1 -FormKey bellsouth_meeting_request -OutputPath .\output\bellsouth_meeting_request.payload.json -Overwrite`

## Mobility Meeting Request

- Detailed doc: `docs/power-automate/mobility_meeting_request.md`
- Endpoint: `POST /intake`
- Document command: `mobility_meeting_request`
- This form also requires an existing `grievance_id`.
- If you use one Form for BellSouth and Mobility, switch `document_command` in Power Automate.
- Use the same question structure as BellSouth and only change the fixed contract bucket and `document_command`.
- Generate a starter payload with:
  `.\New-GrievancePayloadTemplate.ps1 -FormKey mobility_meeting_request -OutputPath .\output\mobility_meeting_request.payload.json -Overwrite`

## Grievance Data Request

- Detailed doc: `docs/power-automate/grievance_data_request.md`
- Endpoint: `POST /intake`
- Document command: `grievance_data_request`
- This form requires an existing `grievance_id`.
- `grievance_number` and `grievance_id` are the same identifier for this flow.
- This form is generated as a regular file and does not use signature routing.
- Generate a starter payload with:
  `.\New-GrievancePayloadTemplate.ps1 -FormKey grievance_data_request -OutputPath .\output\grievance_data_request.payload.json -Overwrite`

## Data Request Letterhead

- Detailed doc: `docs/power-automate/data_request_letterhead.md`
- Endpoint: `POST /intake`
- Document command: `data_request_letterhead`
- This form requires an existing `grievance_id`.
- `grievance_number` and `grievance_id` are the same identifier for this flow.
- Use this for the unsigned cover letter that pairs with the signed grievance data request.
- Generate a starter payload with:
  `.\New-GrievancePayloadTemplate.ps1 -FormKey data_request_letterhead -OutputPath .\output\data_request_letterhead.payload.json -Overwrite`

## True Intent Grievance Brief

- Detailed doc: `docs/power-automate/true_intent_grievance_brief.md`
- Endpoint: `POST /intake`
- Document command: `true_intent_brief`
- The repo already supports the template and intake side.
- Your remaining work is the Microsoft Form, the Power Automate flow, and the real published Form URL.
- Prefer `template_data.signer_email` only when you need to override the default signer behavior.
- Use `.\New-TrueIntentBriefPowerAutomatePack.ps1 -Overwrite` when you want the ready-to-wire Forms question sheet and flow body template in `scripts/power-platform/output/true_intent_brief/`.
- Generate a starter payload with:
  `.\New-GrievancePayloadTemplate.ps1 -FormKey true_intent_brief -OutputPath .\output\true_intent_brief.payload.json -Overwrite`

## Non-Discipline Grievance Brief

- Detailed doc: `docs/power-automate/non_discipline_grievance_brief.md`
- Compatibility pointer: `docs/power-automate/non_discipline_brief_README.md`
- Endpoint: `POST /intake`
- Document command: `non_discipline_brief`
- Full command names that also work: `non_discipline_grievance_brief`, `non_disciplinary_brief`, `non_disciplinary_grievance_brief`
- This is the first-class intake implementation of the 2010 non-discipline staff guide.
- The repo supports this as a dedicated option and also documents how to branch to it from a shared True Intent + Non-Discipline Form/Flow.
- Prefer `template_data.signer_email` only when you need to override the default signer behavior.
- Use `.\New-NonDisciplineBriefPowerAutomatePack.ps1 -Overwrite` when you want the ready-to-wire Forms question sheet and flow body template in `scripts/power-platform/output/non_discipline_brief/`.
- Generate a starter payload with:
  `.\New-GrievancePayloadTemplate.ps1 -FormKey non_discipline_brief -OutputPath .\output\non_discipline_brief.payload.json -Overwrite`

## Disciplinary Grievance Brief

- Detailed doc: `docs/power-automate/disciplinary_grievance_brief.md`
- Endpoint: `POST /intake`
- Document command: `disciplinary_brief`
- The repo already supports the template and intake side.
- Live config already has `disciplinary_grievance_brief` email test mode disabled.
- Your remaining work is the Microsoft Form, the Power Automate flow, the real published Form URL, and one real end-to-end tenant submission.
- Generate a starter payload with:
  `.\New-GrievancePayloadTemplate.ps1 -FormKey disciplinary_brief -OutputPath .\output\disciplinary_brief.payload.json -Overwrite`

## Settlement Form 3106

- Detailed doc: `docs/power-automate/settlement_form_3106.md`
- Endpoint: `POST /intake`
- Document command: `settlement_form`
- This form requires an existing `grievance_id`.
- `documents[0].signers` must contain exactly two emails: manager first, steward second.
- Set `narrative` in the flow from `template_data.issue_text` or another short summary.
- Set `template_data.grievance_number` from `grievance_id` in the flow.
- Generate a starter payload with:
  `.\New-GrievancePayloadTemplate.ps1 -FormKey settlement_form -OutputPath .\output\settlement_form.payload.json -Overwrite`

## Mobility Record of Grievance

- Detailed doc: `docs/power-automate/mobility_record_of_grievance.md`
- Endpoint: `POST /intake`
- Document command: `mobility_record_of_grievance`
- This form requires an existing `grievance_id`.
- `documents[0].signers` must contain exactly three emails in union -> company -> union order.
- Do not add stage-owned DocuSeal fields to the Microsoft Form.
- The repo now contains the staged template, routing logic, and tests for this form.
- Generate a starter payload with:
  `.\New-GrievancePayloadTemplate.ps1 -FormKey mobility_record_of_grievance -OutputPath .\output\mobility_record_of_grievance.payload.json -Overwrite`

## BST Grievance Form 3G3A

- Detailed doc: `docs/power-automate/bst_grievance_form_3g3a.md`
- Endpoint: `POST /intake`
- Document command: `bst_grievance_form_3g3a`
- This repo only automates Questions 1 through 10 for this workflow.
- `documents[0].signers` must contain exactly three emails in union -> manager -> union order.
- Skip the late-stage fields that the detailed doc already marks as DocuSeal-owned or manual.
- Generate a starter payload with:
  `.\New-GrievancePayloadTemplate.ps1 -FormKey bst_grievance_form_3g3a -OutputPath .\output\bst_grievance_form_3g3a.payload.json -Overwrite`

## AT&T Mobility Bargaining Suggestion

- Detailed doc: `docs/power-automate/att_mobility_bargaining_suggestion.md`
- Endpoint: `POST /standalone/forms/att_mobility_bargaining_suggestion/submissions`
- Standalone form key: `att_mobility_bargaining_suggestion`
- This does not use `/intake`.
- Do not add `article_affected` as a Forms question.
- The default signer comes from `config.yaml` unless `local_president_signer_email` is explicitly supplied.
- Generate a starter payload with:
  `.\New-GrievancePayloadTemplate.ps1 -FormKey att_mobility_bargaining_suggestion -OutputPath .\output\att_mobility_bargaining_suggestion.payload.json -Overwrite`

## After each form is built

1. Publish the Microsoft Form.
2. Copy the real published URL into `forms.local.json`.
3. Replace the placeholder URL in repo docs.
4. Run `.\Invoke-GrievanceApiSmokeTest.ps1 -FormKey <key> -PayloadPath .\output\<key>.payload.json -NoSubmit` to confirm the target endpoint and payload shape.
5. Run one real submission from the Form in the tenant.
6. Verify the API response, signing link behavior, and final SharePoint filing.
