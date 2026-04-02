# Power Automate + Forms Integration Guides

This folder contains per-document setup guides for the intake API.
The AT&T Mobility bargaining suggestion guide is the combined operator handoff for both the Microsoft Form build and the Power Automate flow.

Supported guides:

- `statement_of_occurrence.md`
- `bellsouth_meeting_request.md`
- `mobility_meeting_request.md`
- `grievance_data_request.md`
- `true_intent_grievance_brief.md`
- `disciplinary_grievance_brief.md`
- `settlement_form_3106.md`
- `mobility_record_of_grievance.md`
- `bst_grievance_form_3g3a.md` (Q1-Q10 staging guide)
- `att_mobility_bargaining_suggestion.md` (combined Forms + Power Automate handoff for the standalone workflow)

Automation toolkit:

- `scripts/power-platform/README.md`
- `scripts/power-platform/FORM_SETUP_GUIDE.md`
- `scripts/power-platform/forms.catalog.json`

## Common flow pattern (all docs)

1. Microsoft Forms trigger: `When a new response is submitted`
2. `Get response details`
3. `HTTP` action:
   - Method: `POST`
   - URL: `https://api.cwa3106.org/intake`
   - Headers:
     - `Content-Type: application/json`
     - intake auth headers if enabled in your config
4. Parse JSON response
5. Store `case_id`, `grievance_id`, and optional `documents[0].signing_link`

Standalone exception:

- `att_mobility_bargaining_suggestion.md` uses the standalone endpoint instead of `/intake`.
- Its SharePoint filing happens only after DocuSeal signature completion.

## Global intake fields required in every payload

- `request_id` (unique per run, idempotency key)
- `document_command`
- `contract`
- `grievant_firstname`
- `grievant_lastname`
- `grievant_email`
- `narrative`

Use `template_data` for document-specific placeholders.

## Notes

- Do not send `grievance_id` when system is in auto ID mode, except for docs that intentionally target an existing case folder (BellSouth/Mobility meeting requests).
- Signature tags in DOCX (for example `Sig_es_:signer1:signature`) are template anchors; do not create Forms questions for those.
- For 3-step sequential signature flows (like 3G3A or `mobility_record_of_grievance`), pass explicit signer order in `documents[0].signers`.
- For signed documents, completion emails to signers include the signed PDF attachment when the file is within `email.max_attachment_bytes`.
- If you retry the same Forms response, keep the same `request_id` to avoid duplicates.
