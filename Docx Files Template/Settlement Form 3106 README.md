# Settlement Form 3106 Deployment Notes

## Form name

- `CWA 3106 - Settlement Form 3106 Intake`

## Form description

- `Capture informal grievance settlement data. Issue and Settlement answers render as auto-expanding multiline rows in the Settlement Form 3106 document.`

## Recommended Forms questions (with subtext)

1. Question: `Contract`
Type: `Choice`
Subtext: `Select the contract bucket for this settlement form.`
Payload key: `contract`

2. Question: `Grievant First Name`
Type: `Text`
Subtext: `First name for document header and routing.`
Payload key: `grievant_firstname`

3. Question: `Grievant Last Name`
Type: `Text`
Subtext: `Last name for document header and routing.`
Payload key: `grievant_lastname`

4. Question: `Grievant Email`
Type: `Text`
Subtext: `Work or preferred routing email.`
Payload key: `grievant_email`

5. Question: `Narrative Summary`
Type: `Long text`
Subtext: `Short intake summary for case tracking.`
Payload key: `narrative`

6. Question: `Grievance Number (display on form)`
Type: `Text`
Subtext: `Shown in the Grievance # field on the form.`
Payload key: `template_data.grievance_number`

7. Question: `Date of Informal Meeting with Management`
Type: `Date`
Subtext: `Date shown in the meeting date line.`
Payload key: `template_data.informal_meeting_date`

8. Question: `Company Representative in Attendance`
Type: `Text`
Subtext: `Full name of company representative.`
Payload key: `template_data.company_rep_attending`

9. Question: `Union Representative in Attendance`
Type: `Text`
Subtext: `Full name of union representative.`
Payload key: `template_data.union_rep_attending`

10. Question: `Issue Article Number`
Type: `Text`
Subtext: `Article number referenced in the issue section.`
Payload key: `template_data.issue_article`

11. Question: `Issue and Article Details`
Type: `Long text`
Subtext: `Main issue narrative. Auto-expands in the DOCX issue block.`
Payload key: `template_data.issue_text`

12. Question: `Settlement Terms`
Type: `Long text`
Subtext: `Main settlement narrative. Auto-expands in the DOCX settlement block.`
Payload key: `template_data.settlement_text`

## API command

- Preferred: `document_command = settlement_form`
- Full key: `document_command = settlement_form_3106`

## Signer fields wired in template

- `{{Sig_es_:signer1:signature}}` for Company Representative Signature
- `{{Sig_es_:signer2:signature}}` for Steward Signature
- `{{Sig_es_:signer3:signature}}` for Grievant Signature
- Send signer emails via `documents[0].signers` in this order: signer1, signer2, signer3.

## Detailed integration guide

- `grievance-mvp/docs/power-automate/settlement_form_3106.md`
