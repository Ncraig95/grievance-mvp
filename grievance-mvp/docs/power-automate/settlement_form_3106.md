# Settlement Form 3106 Setup

## Document command

- Preferred alias: `settlement_form`
- Full key also works: `settlement_form_3106`

## Microsoft Forms blueprint

Form name:
- `CWA 3106 - Settlement Form 3106 Intake`

Form description:
- `Capture informal grievance settlement data. Issue and Settlement answers render as auto-expanding multiline rows in the Settlement Form 3106 document.`

## Recommended Forms questions and key mapping

Core fields:
- Request ID source -> `request_id` (recommended: `forms-<responseId>`)
- Grievance ID source -> `grievance_id` (required for folder routing on this document flow)
- Grievant first name -> `grievant_firstname`
- Grievant last name -> `grievant_lastname`
- Grievant email -> `grievant_email` (use same response as `Grievant Signer Email`)
- Narrative/intake summary (long text) -> `narrative`

System-set field (not collected in Forms):
- Contract -> `contract` (set a fixed value in flow, for example `CWA`)

Template-specific (`template_data`):
- Grievance number (display value on form) -> `grievance_number` (set from `grievance_id` in flow; no separate Forms question)
- Informal meeting date (Date) -> `informal_meeting_date`
- Company representative in attendance -> `company_rep_attending`
- Union representative in attendance -> `union_rep_attending`
- Issue article number -> `issue_article`
- Issue and article details (Long text, auto-expanding) -> `issue_text`
- Settlement terms (Long text, auto-expanding) -> `settlement_text`

Optional line-wrap controls:
- Issue wrap width (integer) -> `issue_line_wrap_width` (default `95`)
- Settlement wrap width (integer) -> `settlement_line_wrap_width` (default `95`)

Signer emails (`documents[0].signers`, required for 3-signature flow):
- Company representative/manager signer email -> `documents[0].signers[0]`
- Steward signer email -> `documents[0].signers[1]`
- Grievant signer email -> `documents[0].signers[2]` (also map same value to top-level `grievant_email`)
- All 3 signer slots are required for this form. Grievant must be included as signer3.

## Forms questions and subtext (ready to build)

1. Question text: `Existing Grievance ID`
Question type: `Text`
Required: `Yes`
Subtext: `Required for folder routing. Use the existing grievance number (example: 2026001).`
Payload key: `grievance_id`

2. Question text: `Company Representative/Manager Signer Email`
Question type: `Text`
Required: `Yes`
Subtext: `Email for signer1 (company representative signature line).`
Payload key: `documents[0].signers[0]`

3. Question text: `Steward Signer Email`
Question type: `Text`
Required: `Yes`
Subtext: `Email for signer2 (steward signature line). Grievant still signs as signer3 below.`
Payload key: `documents[0].signers[1]`

4. Question text: `Grievant Signer Email`
Question type: `Text`
Required: `Yes`
Subtext: `Required signer email for signer3 (grievant signature line). Use this same response for top-level grievant_email.`
Payload key: `documents[0].signers[2]`

5. Question text: `Grievant First Name`
Question type: `Text`
Required: `Yes`
Subtext: `First name used for document merge fields and routing.`
Payload key: `grievant_firstname`

6. Question text: `Grievant Last Name`
Question type: `Text`
Required: `Yes`
Subtext: `Last name used for document merge fields and routing.`
Payload key: `grievant_lastname`

7. Question text: `Narrative Summary`
Question type: `Long text`
Required: `Yes`
Subtext: `Short intake summary for tracking and case context.`
Payload key: `narrative`

8. Question text: `Date of Informal Meeting with Management`
Question type: `Date`
Required: `No`
Subtext: `Meeting date shown in the form header section.`
Payload key: `template_data.informal_meeting_date`

9. Question text: `Company Representative in Attendance`
Question type: `Text`
Required: `No`
Subtext: `Name of the company representative present at the meeting.`
Payload key: `template_data.company_rep_attending`

10. Question text: `Union Representative in Attendance`
Question type: `Text`
Required: `No`
Subtext: `Name of the union representative present at the meeting.`
Payload key: `template_data.union_rep_attending`

11. Question text: `Issue Article Number`
Question type: `Text`
Required: `No`
Subtext: `Article number referenced in the issue section.`
Payload key: `template_data.issue_article`

12. Question text: `Issue and Article Details`
Question type: `Long text`
Required: `Yes`
Subtext: `Main issue narrative; this field auto-expands in the issue rows.`
Payload key: `template_data.issue_text`

13. Question text: `Settlement Terms`
Question type: `Long text`
Required: `Yes`
Subtext: `Main settlement narrative; this field auto-expands in the settlement rows.`
Payload key: `template_data.settlement_text`

## HTTP body skeleton

`contract` is still required by the intake API. Set it as a fixed value in the flow, not a Forms question.
For explicit 3-signer routing, send `documents[0].signers` as shown below.
Set `template_data.grievance_number` from `grievance_id` in flow.
Use the `Grievant Signer Email` response for both `documents[0].signers[2]` and top-level `grievant_email`.

```json
{
  "request_id": "forms-<responseId>",
  "grievance_id": "<existing grievance id>",
  "contract": "CWA",
  "grievant_firstname": "<first>",
  "grievant_lastname": "<last>",
  "grievant_email": "<same value as grievant signer email>",
  "narrative": "<short intake summary>",
  "documents": [
    {
      "doc_type": "settlement_form_3106",
      "template_key": "settlement_form_3106",
      "requires_signature": true,
      "signers": [
        "<manager email>",
        "<steward email>",
        "<grievant signer email>"
      ]
    }
  ],
  "template_data": {
    "grievance_number": "<same value as grievance_id>",
    "informal_meeting_date": "<yyyy-mm-dd>",
    "company_rep_attending": "<company rep>",
    "union_rep_attending": "<union rep>",
    "issue_article": "<article>",
    "issue_text": "<long issue narrative>",
    "settlement_text": "<long settlement narrative>",
    "issue_line_wrap_width": 95,
    "settlement_line_wrap_width": 95
  }
}
```

## Template behavior notes

- `issue_text` and `settlement_text` are rendered as dynamic row blocks (`issue_rows` and `settlement_rows`) so they can expand vertically without manual line edits.
- If `issue_article` is blank but `article` is provided, the template uses `article` as fallback.
- Signature fields are wired as DocuSeal tags.
- `{{Sig_es_:signer1:signature}}` -> Company Representative Signature.
- `{{Sig_es_:signer2:signature}}` -> Steward Signature.
- `{{Sig_es_:signer3:signature}}` -> Grievant Signature.
- Placement strategy for signature/date fields in `table_preferred` mode:
  - first: PDF table-cell tracing (`docuseal.signature_table_trace_enabled` / `signature_table_trace_by_form`)
  - second: per-form fixed map fallback (`docuseal.signature_table_maps.<form_key>.cells`)
  - third: generic placeholder-box fallback
- Settlement row mapping is fixed by row order in the signature table:
  - row 1 -> signer1 (company/manager)
  - row 2 -> signer2 (steward)
  - row 3 -> signer3 (grievant)
- Troubleshooting placement:
  - verify the submission used `template_key: settlement_form_3106`
  - check API logs for `docuseal_signature_placement_strategy` (`trace`, `map_fallback`, `generic_fallback`)
  - if tracing misses cells, tune `signature_table_maps.settlement_form_3106.cells.*` normalized `x/y/w/h`
- On completion, the system sends completion/copy email notifications to every signer in `documents[0].signers`.
- On completion, signer emails include the signed PDF attachment when file size is within `email.max_attachment_bytes`.
- Optional: keep `email.allow_signer_copy_link: true` if you also want a link in the signer email body.
