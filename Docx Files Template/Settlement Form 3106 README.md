# Settlement Form 3106 Deployment Notes

## Form name

- `CWA 3106 - Settlement Form 3106 Intake`

## Form description

- `Capture informal grievance settlement data. Issue and Settlement answers render as auto-expanding multiline rows in the Settlement Form 3106 document.`

## Recommended Forms questions (with subtext)

Optional fixed flow value (not a Forms question):
- `contract` (example: `CWA`; not required for grievance_id-based folder routing)

1. Question: `Existing Grievance ID`
Type: `Text`
Subtext: `Required for auto routing to the correct case folder on completion. Example: 2026001.`
Payload key: `grievance_id`

2. Question: `Company Representative/Manager Signer Email`
Type: `Text`
Subtext: `Email for signer1 (company representative signature line).`
Payload key: `documents[0].signers[0]`

3. Question: `Steward Signer Email`
Type: `Text`
Subtext: `Email for signer2 (steward signature line). Grievant still signs as signer3 below.`
Payload key: `documents[0].signers[1]`

4. Question: `Grievant Signer Email`
Type: `Text`
Subtext: `Required signer email for signer3 (grievant signature line). Reuse this same response for top-level grievant_email.`
Payload key: `documents[0].signers[2]`

5. Question: `Grievant First Name`
Type: `Text`
Subtext: `First name for document header and routing.`
Payload key: `grievant_firstname`

6. Question: `Grievant Last Name`
Type: `Text`
Subtext: `Last name for document header and routing.`
Payload key: `grievant_lastname`

7. Question: `Narrative Summary`
Type: `Long text`
Subtext: `Short intake summary for case tracking.`
Payload key: `narrative`

8. Question: `Date of Informal Meeting with Management`
Type: `Date`
Subtext: `Date shown in the meeting date line.`
Payload key: `template_data.informal_meeting_date`

9. Question: `Company Representative in Attendance`
Type: `Text`
Subtext: `Full name of company representative.`
Payload key: `template_data.company_rep_attending`

10. Question: `Union Representative in Attendance`
Type: `Text`
Subtext: `Full name of union representative.`
Payload key: `template_data.union_rep_attending`

11. Question: `Issue Article Number`
Type: `Text`
Subtext: `Article number referenced in the issue section.`
Payload key: `template_data.issue_article`

12. Question: `Issue and Article Details`
Type: `Long text`
Subtext: `Main issue narrative. Auto-expands in the DOCX issue block.`
Payload key: `template_data.issue_text`

13. Question: `Settlement Terms`
Type: `Long text`
Subtext: `Main settlement narrative. Auto-expands in the DOCX settlement block.`
Payload key: `template_data.settlement_text`

No separate question for display grievance number:
- Set `template_data.grievance_number` from `grievance_id` in flow.

## API command

- Preferred: `document_command = settlement_form`
- Full key: `document_command = settlement_form_3106`

## Signer fields wired in template

- `{{Sig_es_:signer1:signature}}` for Company Representative Signature
- `{{Sig_es_:signer2:signature}}` for Steward Signature
- `{{Sig_es_:signer3:signature}}` for Grievant Signature
- Send signer emails via `documents[0].signers` in this order: signer1, signer2, signer3.
- All three signer emails are required. Grievant must be included as signer3.
- On completion, each signer receives the completion/copy notification email.
- On completion, signer emails include the signed PDF attachment when file size is within `email.max_attachment_bytes`.
- Optional: keep `email.allow_signer_copy_link: true` if you also want a link in the signer email body.

## Detailed integration guide

- `grievance-mvp/docs/power-automate/settlement_form_3106.md`
