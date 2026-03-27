# Settlement Form 3106 Deployment Notes

## Form name

- `CWA 3106 - Settlement Form 3106 Intake`

## Form description

- `Capture informal grievance settlement data. The issue details answer renders as an auto-expanding multiline block in the Settlement Form 3106 document.`

## Recommended Forms questions (with subtext)

Set this as a fixed flow value (not a Forms question):
- `contract` (example: `CWA`)

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
Subtext: `Email for signer2 (steward signature line).`
Payload key: `documents[0].signers[1]`

4. Question: `Grievant First Name`
Type: `Text`
Subtext: `First name for document header and routing.`
Payload key: `grievant_firstname`

5. Question: `Grievant Last Name`
Type: `Text`
Subtext: `Last name for document header and routing.`
Payload key: `grievant_lastname`

6. Question: `Date of Informal Meeting with Management`
Type: `Date`
Subtext: `Date shown in the meeting date line.`
Payload key: `template_data.informal_meeting_date`

7. Question: `Company Representative in Attendance`
Type: `Text`
Subtext: `Full name of company representative.`
Payload key: `template_data.company_rep_attending`

8. Question: `Union Representative in Attendance`
Type: `Text`
Subtext: `Full name of union representative.`
Payload key: `template_data.union_rep_attending`

9. Question: `Issue Article Number`
Type: `Text`
Subtext: `Article number referenced in the issue section.`
Payload key: `template_data.issue_article`

10. Question: `Issue Details`
Type: `Long text`
Subtext: `Main issue text. Auto-expands in the DOCX issue block.`
Payload key: `template_data.issue_text`

No separate question for display grievance number:
- Set `template_data.grievance_number` from `grievance_id` in flow.
No separate question for intake narrative:
- Set `narrative` in flow from `template_data.issue_text` or another short summary value.

## API command

- Preferred: `document_command = settlement_form`
- Full key: `document_command = settlement_form_3106`

## Signer fields wired in template

- `{{Sig_es_:signer1:signature}}` for Company Representative Signature
- `{{Sig_es_:signer2:signature}}` for Steward Signature
- Send signer emails via `documents[0].signers` in this order: signer1, signer2.
- This template now uses two signer slots only.
- On completion, each signer receives the completion/copy notification email.
- On completion, signer emails include the signed PDF attachment when file size is within `email.max_attachment_bytes`.
- Optional: keep `email.allow_signer_copy_link: true` if you also want a link in the signer email body.

## Detailed integration guide

- `grievance-mvp/docs/power-automate/settlement_form_3106.md`
