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
Helper text: Select the local contract bucket for this 3G3A intake.
Payload key: contract

3. Question text: `Request Summary`
Question type: `Long text`
Required: `Yes`
Helper text: `Internal summary for the intake record.`
Payload key: `narrative`

4. Question text: `Local Grievance Number (display on form)`
Question type: `Text`
Required: `No`
Helper text: `Optional display number on the form. If blank, app falls back to grievance_id.`
Payload key: `template_data.local_grievance_number`

### Section B - Question 1

5. Question text: `Q1 Grievance Type`
Question type: `Choice`
Required: `Yes`
Choices: `BST`, `Billing`, `Utility Operations`, `Other`
Payload key: `template_data.q1_choice`

6. Question text: `Q1 Date Occurred`
Question type: `Date`
Required: `Yes`
Payload key: `template_data.q1_occurred_date`

7. Question text: `Q1 City and State`
Question type: `Text`
Required: `Yes`
Payload key: `template_data.q1_city_state`

8. Question text: `Local Number`
Question type: `Text`
Required: `Yes`
Default value suggestion: `3106`
Payload key: `template_data.local_number`

### Section C - Question 2 and 2A

9. Question text: `Q2 Employee or Work Group Name`
Question type: `Text`
Required: `Yes`
Payload key: `template_data.q2_employee_name`

10. Question text: `Q2 Employee ATTUID`
Question type: `Text`
Required: `No`
Payload key: `template_data.q2_employee_attuid`

11. Question text: `Q2 Department`
Question type: `Text`
Required: `No`
Payload key: `template_data.q2_department`

12. Question text: `Q2 Job Title`
Question type: `Text`
Required: `No`
Payload key: `template_data.q2_job_title`

13. Question text: `Q2 Payroll ID (PERNR)`
Question type: `Text`
Required: `No`
Payload key: `template_data.q2_payroll_id`

14. Question text: `Q2 Seniority Date`
Question type: `Date`
Required: `No`
Payload key: `template_data.q2_seniority_date`

15. Question text: `Q2A Job Title Involved or Requested`
Question type: `Text`
Required: `No`
Payload key: `template_data.q2a_job_title_requested`

16. Question text: `Q2A Requisition Number`
Question type: `Text`
Required: `No`
Payload key: `template_data.q2a_requisition_number`

17. Question text: `Q2A Other Department Involved or Requested`
Question type: `Text`
Required: `No`
Payload key: `template_data.q2a_other_department`

### Section D - Questions 3 and 4

18. Question text: `Q3 Union Statement of What Happened`
Question type: `Long text`
Required: `Yes`
Payload key: `template_data.q3_union_statement`

19. Question text: `Q4 Specific Basis or Contract Section`
Question type: `Long text`
Required: `Yes`
Payload key: `template_data.q4_contract_basis`

### Section E - Question 5

20. Question text: `Q5 Informal Meeting Date`
Question type: `Date`
Required: `No`
Payload key: `template_data.q5_informal_meeting_date`

21. Question text: `Q5 Date 3G3R Issued`
Question type: `Date`
Required: `No`
Payload key: `template_data.q5_3g3r_issued_date`

22. Question text: `Q5 Date 2nd Level Meeting Held`
Question type: `Date`
Required: `No`
Payload key: `template_data.q5_second_level_meeting_date`

23. Question text: `Q5 Union Representative Name and ATTUID`
Question type: `Text`
Required: `Yes`
Payload key: `template_data.q5_union_rep_name_attuid`

### Section F - Stage routing emails

24. Question text: `Stage 1 Local Union Email`
Question type: `Text`
Required: `Yes`
Payload key: `documents[0].signers[0]`

25. Question text: `Stage 2 Second Level Manager Email`
Question type: `Text`
Required: `Yes`
Payload key: `documents[0].signers[1]`

26. Question text: `Stage 3 Union Final Decision Email`
Question type: `Text`
Required: `Yes`
Payload key: `documents[0].signers[2]`

### Section G - True Intent selectors (checkbox marks)

27. Question text: `Q10 Company True Intent`
Question type: `Choice`
Required: `No`
Choices: `Yes`, `No`
Payload key: `template_data.q10_company_true_intent_choice`

28. Question text: `Q10 Union True Intent`
Question type: `Choice`
Required: `No`
Choices: `Yes`, `No`
Payload key: `template_data.q10_union_true_intent_choice`

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

HTTP action:
- Method: `POST`
- URL: `https://<your-api-host>/intake`
- Headers:
  - `Content-Type: application/json`
  - intake auth header(s) configured in your app

Body template:

```json
{
  "request_id": "forms-@{triggerOutputs()?['body/responseId']}",
  "document_command": "bst_grievance_form_3g3a",
  "grievance_id": "@{outputs('Get_response_details')?['body/ExistingGrievanceID']}",
  "contract": "BellSouth",
  "grievant_firstname": "@{outputs('Get_response_details')?['body/GrievantFirstName']}",
  "grievant_lastname": "@{outputs('Get_response_details')?['body/GrievantLastName']}",
  "grievant_email": "",
  "incident_date": "@{outputs('Get_response_details')?['body/Q1DateOccurred']}",
  "narrative": "@{outputs('Get_response_details')?['body/RequestSummary']}",
  "documents": [
    {
      "doc_type": "bst_grievance_form_3g3a",
      "template_key": "bst_grievance_form_3g3a",
      "requires_signature": true,
      "signers": [
        "@{outputs('Get_response_details')?['body/Stage1LocalUnionEmail']}",
        "@{outputs('Get_response_details')?['body/Stage2SecondLevelManagerEmail']}",
        "@{outputs('Get_response_details')?['body/Stage3UnionFinalDecisionEmail']}"
      ]
    }
  ],
  "template_data": {
    "local_grievance_number": "@{outputs('Get_response_details')?['body/LocalGrievanceNumberdisplayonform']}",
    "q1_choice": "@{outputs('Get_response_details')?['body/Q1GrievanceType']}",
    "q1_occurred_date": "@{outputs('Get_response_details')?['body/Q1DateOccurred']}",
    "q1_city_state": "@{outputs('Get_response_details')?['body/Q1CityandState']}",
    "local_number": "@{outputs('Get_response_details')?['body/LocalNumber']}",
    "q2_employee_name": "@{outputs('Get_response_details')?['body/Q2EmployeeorWorkGroupName']}",
    "q2_employee_attuid": "@{outputs('Get_response_details')?['body/Q2EmployeeATTUID']}",
    "q2_department": "@{outputs('Get_response_details')?['body/Q2Department']}",
    "q2_job_title": "@{outputs('Get_response_details')?['body/Q2JobTitle']}",
    "q2_payroll_id": "@{outputs('Get_response_details')?['body/Q2PayrollIDPERNR']}",
    "q2_seniority_date": "@{outputs('Get_response_details')?['body/Q2SeniorityDate']}",
    "q2a_job_title_requested": "@{outputs('Get_response_details')?['body/Q2AJobTitleInvolvedorRequested']}",
    "q2a_requisition_number": "@{outputs('Get_response_details')?['body/Q2ARequisitionNumber']}",
    "q2a_other_department": "@{outputs('Get_response_details')?['body/Q2AOtherDepartmentInvolvedorRequested']}",
    "q3_union_statement": "@{outputs('Get_response_details')?['body/Q3UnionStatementofWhatHappened']}",
    "q4_contract_basis": "@{outputs('Get_response_details')?['body/Q4SpecificBasisorContractSection']}",
    "q5_informal_meeting_date": "@{outputs('Get_response_details')?['body/Q5InformalMeetingDate']}",
    "q5_3g3r_issued_date": "@{outputs('Get_response_details')?['body/Q5Date3G3RIssued']}",
    "q5_second_level_meeting_date": "@{outputs('Get_response_details')?['body/Q5Date2ndLevelMeetingHeld']}",
    "q5_union_rep_name_attuid": "@{outputs('Get_response_details')?['body/Q5UnionRepresentativeNameandATTUID']}",
    "q10_company_true_intent_choice": "@{outputs('Get_response_details')?['body/Q10CompanyTrueIntent']}",
    "q10_union_true_intent_choice": "@{outputs('Get_response_details')?['body/Q10UnionTrueIntent']}"
  }
}
```

## Notes

- `grievance_id` is required for this doc type.
- `documents[0].signers` must contain exactly 3 emails in order.
- Keep Q6/Q7/Q8 values out of intake payload; those are signed-stage text fields.
- The app already enforces wrapped/clamped render for long prefill fields (`q3`, `q4`).
