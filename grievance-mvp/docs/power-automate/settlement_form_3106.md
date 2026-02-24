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
- Contract (choice) -> `contract`
- Grievant first name -> `grievant_firstname`
- Grievant last name -> `grievant_lastname`
- Grievant email -> `grievant_email`
- Narrative/intake summary (long text) -> `narrative`

Template-specific (`template_data`):
- Grievance number (display value on form) -> `grievance_number`
- Informal meeting date (Date) -> `informal_meeting_date`
- Company representative in attendance -> `company_rep_attending`
- Union representative in attendance -> `union_rep_attending`
- Issue article number -> `issue_article`
- Issue and article details (Long text, auto-expanding) -> `issue_text`
- Settlement terms (Long text, auto-expanding) -> `settlement_text`

Optional line-wrap controls:
- Issue wrap width (integer) -> `issue_line_wrap_width` (default `95`)
- Settlement wrap width (integer) -> `settlement_line_wrap_width` (default `95`)

## HTTP body skeleton

```json
{
  "request_id": "forms-<responseId>",
  "document_command": "settlement_form",
  "contract": "CWA",
  "grievant_firstname": "<first>",
  "grievant_lastname": "<last>",
  "grievant_email": "<email>",
  "narrative": "<short intake summary>",
  "template_data": {
    "grievance_number": "<display grievance number>",
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
- Do not create Forms questions for signatures. Pass signer emails with `documents[0].signers` in signer order `[signer1, signer2, signer3]`.
