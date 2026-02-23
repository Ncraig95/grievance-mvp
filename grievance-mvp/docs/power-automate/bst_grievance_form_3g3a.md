# BST 3G3A Setup (Questions 1-10 Only)

## Document command

- `bst_grievance_form_3g3a`

## Scope locked for this phase

- Automate up to Question 10 only.
- District/state sections after Question 10 remain manual.
- Staged signer order is fixed: signer1 (union) -> signer2 (manager) -> signer3 (union final).

## Microsoft Forms blueprint

Form name:
- `CWA 3106 - 3G3A Intake (Q1-Q10)`

Form description:
- `Capture 3G3A grievance intake data for Q1-Q10. This flow creates stage 1 immediately, then auto-advances to stage 2 and stage 3 after completion webhooks.`

### Section A - Request metadata

1. Question text: `Existing Grievance ID`
Question type: `Text`
Required: `Yes`
Helper text: `Existing folder/case grievance number, e.g. 2026001.`
Payload key: `grievance_id`

2. Question text: Contract
Question type: Choice
Required: Yes
Choices: BST, Utilities
Helper text: Select the contract bucket used for this grievance intake.
Payload key: contract

3. Question text: `Request Summary`
Question type: `Long text`
Required: `Yes`
Helper text: `Short internal summary used for intake tracking and routing.`
Payload key: `narrative`

4. Question text: `Local Grievance Number (display on form)`
Question type: `Text`
Required: `No`
Helper text: `Optional display number on the form. If blank, app falls back to grievance_id.`
Payload key: `template_data.local_grievance_number`
Status: `Skipped in Forms`
Skip reason: `Redundant with Existing Grievance ID.`

### Section B - Question 1

5. Question text: `Q1 Grievance Type`
Question type: `Choice`
Required: `Yes`
Choices: `BST`, `Billing`, `Utility Operations`, `Other`
Helper text: `Select the grievance type shown in Question 1 on the 3G3A document.`
Payload key: `template_data.q1_choice`
Status: `Skipped in Forms`
Skip reason: `Redundant for this intake path.`

6. Question text: `Q1 Date Occurred`
Question type: `Date`
Required: `Yes`
Helper text: `Date the incident or issue occurred.`
Payload key: `template_data.q1_occurred_date`

7. Question text: `Q1 City and State`
Question type: `Text`
Required: `Yes`
Helper text: `City and state where the issue occurred (example: Atlanta, GA).`
Payload key: `template_data.q1_city_state`

8. Question text: `Local Number`
Question type: `Text`
Required: `Yes`
Helper text: `Union local number shown on the form.`
Default value suggestion: `3106`
Payload key: `template_data.local_number`
Status: `Skipped in Forms`
Skip reason: `Set as a fixed value in payload/template defaults.`

### Section C - Question 2 and 2A

9. Question text: `Q2 Employee or Work Group Name`
Question type: `Text`
Required: `Yes`
Helper text: `Full name of employee or work group tied to the grievance.`
Payload key: `template_data.q2_employee_name`

10. Question text: `Q2 Employee ATTUID`
Question type: `Text`
Required: `No`
Helper text: `ATTUID for the employee if available.`
Payload key: `template_data.q2_employee_attuid`

11. Question text: `Q2 Department`
Question type: `Text`
Required: `No`
Helper text: `Department currently assigned to the employee/work group.`
Payload key: `template_data.q2_department`

12. Question text: `Q2 Job Title`
Question type: `Text`
Required: `No`
Helper text: `Current job title for the employee/work group.`
Payload key: `template_data.q2_job_title`

13. Question text: `Q2 Payroll ID (PERNR)`
Question type: `Text`
Required: `No`
Helper text: `Payroll ID/PERNR if known.`
Payload key: `template_data.q2_payroll_id`

14. Question text: `Q2 Seniority Date`
Question type: `Date`
Required: `No`
Helper text: `Employee seniority date, if relevant and available.`
Payload key: `template_data.q2_seniority_date`

15. Question text: `Q2A Job Title Involved or Requested`
Question type: `Text`
Required: `No`
Helper text: `Job title involved in dispute or requested outcome.`
Payload key: `template_data.q2a_job_title_requested`

16. Question text: `Q2A Requisition Number`
Question type: `Text`
Required: `No`
Helper text: `Requisition number tied to the requested or disputed position.`
Payload key: `template_data.q2a_requisition_number`

17. Question text: `Q2A Other Department Involved or Requested`
Question type: `Text`
Required: `No`
Helper text: `Any other department involved or requested as part of the grievance.`
Payload key: `template_data.q2a_other_department`

### Section D - Questions 3 and 4

18. Question text: `Q3 Union Statement of What Happened`
Question type: `Long text`
Required: `Yes`
Helper text: `Union narrative of facts/events. Include clear timeline and key details.`
Payload key: `template_data.q3_union_statement`

19. Question text: `Q4 Specific Basis or Contract Section`
Question type: `Long text`
Required: `Yes`
Helper text: `Contract language, policy, or basis supporting this grievance.`
Payload key: `template_data.q4_contract_basis`

### Section E - Question 5

20. Question text: `Q5 Informal Meeting Date`
Question type: `Date`
Required: `No`
Helper text: `Date of informal meeting, if already held.`
Payload key: `template_data.q5_informal_meeting_date`

21. Question text: `Q5 Date 3G3R Issued`
Question type: `Date`
Required: `No`
Helper text: `Date the 3G3R response was issued, if applicable.`
Payload key: `template_data.q5_3g3r_issued_date`

22. Question text: `Q5 Date 2nd Level Meeting Held`
Question type: `Date`
Required: `No`
Helper text: `Date second-level meeting occurred. Leave blank if not yet held.`
Payload key: `template_data.q5_second_level_meeting_date`
Status: `Skipped in Forms`
Skip reason: `Not available at intake; completed during later workflow stage.`

23. Question text: `Q5 Union Representative Name and ATTUID`
Question type: `Text`
Required: `Yes`
Helper text: `Union representative full name plus ATTUID.`
Payload key: `template_data.q5_union_rep_name_attuid`

### Section F - Stage routing emails

24. Question text: `Stage 1 Local Union Email`
Question type: `Text`
Required: `Yes`
Helper text: `Email for first signer (union stage 1).`
Payload key: `documents[0].signers[0]`

25. Question text: `Stage 2 Second Level Manager Email`
Question type: `Text`
Required: `Yes`
Helper text: `Email for second signer (manager stage 2).`
Payload key: `documents[0].signers[1]`

26. Question text: `Stage 3 Union Final Decision Email`
Question type: `Text`
Required: `Yes`
Helper text: `Email for third signer (union final decision stage).`
Payload key: `documents[0].signers[2]`

### Section G - True Intent selectors (checkbox marks)

27. Question text: `Q10 Company True Intent`
Question type: `Choice`
Required: `No`
Choices: `Yes`, `No`
Helper text: `Company true-intent selection.`
Payload key: `template_data.q10_company_true_intent_choice`
Status: `Skipped in Forms`
Skip reason: `Collected only at stage 3 / final decision step.`

28. Question text: `Q10 Union True Intent`
Question type: `Choice`
Required: `No`
Choices: `Yes`, `No`
Helper text: `Union true-intent selection.`
Payload key: `template_data.q10_union_true_intent_choice`
Status: `Skipped in Forms`
Skip reason: `Collected only at stage 3 / final decision step.`

## Do not collect these in Forms (DocuSeal handles during signing)

- Q6 company statement text
- Q7 second-level proposed disposition text
- Q7 company rep name/ATTUID text
- Q8 union disposition text
- Q8 union rep name/ATTUID text
- Q9 mediation fields (currently unused in your process)

## Word tagging standard (use this exactly)

Base fields (prefill):
- `{{ q1_occurred_date }}`
- `{{ q1_city_state }}`
- `{{ local_number }}`
- `{{ local_grievance_number }}`
- `{{ q2_employee_name }}`
- `{{ q2_employee_attuid }}`
- `{{ q2_department }}`
- `{{ q2_job_title }}`
- `{{ q2_payroll_id }}`
- `{{ q2_seniority_date }}`
- `{{ q2a_job_title_requested }}`
- `{{ q2a_requisition_number }}`
- `{{ q2a_other_department }}`
- `{{ q3_union_statement }}`
- `{{ q4_contract_basis }}`
- `{{ q5_informal_meeting_date }}`
- `{{ q5_3g3r_issued_date }}`
- `{{ q5_second_level_meeting_date }}`
- `{{ q5_union_rep_name_attuid }}`

Checkbox mark tags (`☒/☐` from backend):
- Q1: `{{ q1_is_bst_mark }}`, `{{ q1_is_billing_mark }}`, `{{ q1_is_utility_operations_mark }}`, `{{ q1_is_other_mark }}`
- Q8: `{{ q8_is_accepted_mark }}`, `{{ q8_is_rejected_mark }}`, `{{ q8_is_appealed_mark }}`, `{{ q8_is_requested_mediation_mark }}`
- Q10 company: `{{ q10_company_is_yes_mark }}`, `{{ q10_company_is_no_mark }}`
- Q10 union: `{{ q10_union_is_yes_mark }}`, `{{ q10_union_is_no_mark }}`

Signature/date/email tags:
- signer1: `{{Sig_es_:signer1:signature}}`, `{{Dte_es_:signer1:date}}`, `{{Eml_es_:signer1:email}}`
- signer2: `{{Sig_es_:signer2:signature}}`, `{{Dte_es_:signer2:date}}`, `{{Eml_es_:signer2:email}}`
- signer3: `{{Sig_es_:signer3:signature}}`, `{{Dte_es_:signer3:date}}`, `{{Eml_es_:signer3:email}}`

Optional true-intent extra signature blocks:
- signer2: `{{Sig_es_:signer2:signature_true_intent}}`, `{{Dte_es_:signer2:date_true_intent}}`, `{{Eml_es_:signer2:email_true_intent}}`
- signer3: `{{Sig_es_:signer3:signature_true_intent}}`, `{{Dte_es_:signer3:date_true_intent}}`, `{{Eml_es_:signer3:email_true_intent}}`

Signer-entered text anchors:
- stage 2: `{{Txt_es_:signer2:q6_company_statement}}`, `{{Txt_es_:signer2:q7_proposed_disposition_second_level}}`, `{{Txt_es_:signer2:q7_company_rep_name_attuid}}`
- stage 3: `{{Txt_es_:signer3:q8_union_disposition}}`, `{{Txt_es_:signer3:q8_union_rep_name_attuid}}`

## Power Automate HTTP setup

### Add approval gate before HTTP intake (required)

Use this sequence so Derek (or assigned reviewer) approves before any signature stage is created:

1. Trigger: `When a new response is submitted` (Microsoft Forms).
2. Action: `Get response details`.
3. Action: `Start and wait for an approval` (Approval type: `Approve/Reject - First to respond`).
4. In approval details, include key intake fields:
   - Grievance ID
   - Contract
   - Request Summary (reason for request)
   - Q1 Date Occurred
   - Q1 City and State
   - Q2 Employee/Work Group Name
5. Set `Assigned to` as reviewer email (Derek or other approver mailbox/user).
6. Add `Condition`:
   - If `Outcome` equals `Approve` -> continue to HTTP `POST /intake`.
   - If `Outcome` equals `Reject` -> end flow (or send rejection email to submitter/internal queue).
7. Optional but recommended: add a rejection notification email that includes approver comments.

Suggested approval title:
- `3G3A Intake Review - @{outputs('Get_response_details')?['body/ExistingGrievanceID']}`

Suggested approval details:
- `Please review this intake and reason before signature routing starts.`

HTTP action:
- Method: `POST`
- URL: `https://<your-api-host>/intake`
- Headers:
  - `Content-Type: application/json`
  - intake auth header(s) configured in your app

Body template:

```json
{
  "request_id": "forms-3g3a-@{triggerOutputs()?['body/resourceData/responseId']}",
  "document_command": "bst_grievance_form_3g3a",
  "grievance_id": "@{outputs('Get_response_details')?['body/rb36e6f94e7b1489ea4bfd66be0f074b0']}",
  "contract": "@{outputs('Get_response_details')?['body/r99a04cfe09ef4ce4b72d2d42c6940d1b']}",
  "grievant_firstname": "",
  "grievant_lastname": "",
  "grievant_email": "",
  "incident_date": "@{outputs('Get_response_details')?['body/r2f689856db2e41cb87410b39694830f5']}",
  "narrative": "@{outputs('Get_response_details')?['body/r6e05953fa1234adebca8294be5794b4c']}",
  "documents": [
    {
      "doc_type": "bst_grievance_form_3g3a",
      "template_key": "bst_grievance_form_3g3a",
      "requires_signature": true,
      "signers": [
        "@{outputs('Get_response_details')?['body/r6571dcabbbe343df87ef6b018b3eccfe']}",
        "@{outputs('Get_response_details')?['body/r3ac6985213584d22ad0da47381dae1bc']}",
        "@{outputs('Get_response_details')?['body/r88210dd0ee0442e18611641a883bf663']}"
      ]
    }
  ],
  "template_data": {
    "q1_occurred_date": "@{outputs('Get_response_details')?['body/r2f689856db2e41cb87410b39694830f5']}",
    "q1_city_state": "@{outputs('Get_response_details')?['body/rafdfad3ac3bc46a89ee72cb03fc129eb']}",
    "local_number": "3106",
    "q2_employee_name": "@{outputs('Get_response_details')?['body/r815f904f51f44f8899edd2558f8bdd86']}",
    "q2_employee_attuid": "@{outputs('Get_response_details')?['body/rfd1ab991be4b4e0db09500cc2733347a']}",
    "q2_department": "@{outputs('Get_response_details')?['body/r439e9d5fcd2c4e9fba0c2574f26ef74f']}",
    "q2_job_title": "@{outputs('Get_response_details')?['body/r505a7bfe183a4246b91f703a137f3c83']}",
    "q2_payroll_id": "@{outputs('Get_response_details')?['body/rfd1ee898e74e4dc1880dcf5e978b943a']}",
    "q2_seniority_date": "@{outputs('Get_response_details')?['body/r7dd7f1d6c4314b4889d03e1302adf876']}",
    "q2a_job_title_requested": "@{outputs('Get_response_details')?['body/ra77bdb133f15400f812f30c3b284b4ea']}",
    "q2a_requisition_number": "@{outputs('Get_response_details')?['body/ra77bdb133f15400f812f30c3b284b4ea']}",
    "q2a_other_department": "@{outputs('Get_response_details')?['body/rf1ef0fc959df4c83a69819c828e983ee']}",
    "q3_union_statement": "@{outputs('Get_response_details')?['body/rb6cf8cd1983340a392c711f14cc974d5']}",
    "q4_contract_basis": "@{outputs('Get_response_details')?['body/rd3917b69a4a341fab4c6fdb4e6148ae0']}",
    "q5_informal_meeting_date": "@{outputs('Get_response_details')?['body/raac48a46ff5d4e04927ed979cc88244c']}",
    "q5_3g3r_issued_date": "@{outputs('Get_response_details')?['body/redcd1d9aa90848109ffbd7cdb20e98cc']}",
    "q5_union_rep_name_attuid": "@{outputs('Get_response_details')?['body/r5a6b38bd7f444e359a4fc26e512dcd99']}"
  }
}
```

## Notes

- `grievance_id` is required for this doc type.
- `documents[0].signers` must contain exactly 3 emails in order.
- For Q1 display:
  - `q1_grievance_type` is now treated as `Other` free-text only.
  - For normal selections (`BST`, `Billing`, `Utility Operations`), leave `q1_grievance_type` blank.
  - Checkbox marks are rendered from `q1_choice` (or fallback mapping when present).
- Keep Q6/Q7/Q8 values out of intake payload; those are signed-stage text fields.
- The app already enforces wrapped/clamped render for long prefill fields (`q3`, `q4`).
- `request_id` is idempotent and must be globally unique across all intake flows. Reusing the same value returns the old case and does not resend stage 1.
- Keep the `forms-3g3a-` prefix in this flow to avoid collisions with other Forms/flows that may also produce `responseId=1`, `2`, etc.
