# BST 3G3A Setup (Questions 1-10 Only)

## Document command

- `bst_grievance_form_3g3a`

## Microsoft Forms blueprint

Form name:
- `CWA 3106 - 3G3A Intake (Q1-Q10)`

Form description:
- `Use this form to capture Record of Grievance information for Questions 1-10 only. District/State sections are not included in this phase. Submission creates stage 1, then stage 2 and stage 3 route automatically in sequence.`

### Section A - Request metadata

1. Question text: `Existing Grievance ID`
Question type: `Text`
Required: `Yes`
Helper text: `Enter the existing grievance number exactly as used in the case folder, for example 2026001.`
Payload key: `grievance_id`

2. Question text: `Contract`
Question type: `Choice`
Required: `Yes`
Choices: `BellSouth`
Helper text: `This should remain BellSouth for this form.`
Payload key: `contract`

3. Question text: `Request Summary`
Question type: `Long text`
Required: `Yes`
Helper text: `Internal summary for tracking. This is not the full union statement field.`
Payload key: `narrative`

### Section B - Question 1

4. Question text: `Q1 Grievance Type`
Question type: `Choice`
Required: `Yes`
Choices: `BST`, `Billing`, `Utility Operations`, `Other`
Helper text: `Select the grievance type shown in Question 1.`
Payload key: `template_data.q1_grievance_type`

5. Question text: `Q1 Date Occurred`
Question type: `Date`
Required: `Yes`
Helper text: `Date grievance occurred for Question 1.`
Payload key: `template_data.q1_occurred_date`

6. Question text: `Q1 City and State`
Question type: `Text`
Required: `Yes`
Helper text: `City and state where grievance occurred.`
Payload key: `template_data.q1_city_state`

7. Question text: `Local Number`
Question type: `Text`
Required: `Yes`
Default value suggestion: `3106`
Helper text: `Local union number shown on the 3G3A form.`
Payload key: `template_data.local_number`

### Section C - Question 2 and 2A

8. Question text: `Q2 Employee or Work Group Name`
Question type: `Text`
Required: `Yes`
Helper text: `Full name or work group name.`
Payload key: `template_data.q2_employee_name`

9. Question text: `Q2 Employee ATTUID`
Question type: `Text`
Required: `No`
Helper text: `ATTUID if available.`
Payload key: `template_data.q2_employee_attuid`

10. Question text: `Q2 Department`
Question type: `Text`
Required: `No`
Helper text: `Department of grieving employee/work group.`
Payload key: `template_data.q2_department`

11. Question text: `Q2 Job Title`
Question type: `Text`
Required: `No`
Helper text: `Current job title.`
Payload key: `template_data.q2_job_title`

12. Question text: `Q2 Payroll ID (PERNR)`
Question type: `Text`
Required: `No`
Helper text: `Payroll ID/PERNR if known.`
Payload key: `template_data.q2_payroll_id`

13. Question text: `Q2 Seniority Date`
Question type: `Date`
Required: `No`
Helper text: `Employee seniority date.`
Payload key: `template_data.q2_seniority_date`

14. Question text: `Q2A Job Title Involved or Requested`
Question type: `Text`
Required: `No`
Helper text: `Selection grievances only.`
Payload key: `template_data.q2a_job_title_requested`

15. Question text: `Q2A Requisition Number`
Question type: `Text`
Required: `No`
Helper text: `Selection grievances only.`
Payload key: `template_data.q2a_requisition_number`

16. Question text: `Q2A Other Department Involved or Requested`
Question type: `Text`
Required: `No`
Helper text: `Selection grievances only.`
Payload key: `template_data.q2a_other_department`

### Section D - Questions 3 and 4

17. Question text: `Q3 Union Statement of What Happened`
Question type: `Long text`
Required: `Yes`
Helper text: `Complete union statement for Question 3.`
Payload key: `template_data.q3_union_statement`

18. Question text: `Q4 Specific Basis or Contract Section`
Question type: `Long text`
Required: `Yes`
Helper text: `List article/section and basis of grievance for Question 4.`
Payload key: `template_data.q4_contract_basis`

### Section E - Question 5

19. Question text: `Q5 Informal Meeting Date`
Question type: `Date`
Required: `No`
Helper text: `Date informal meeting held.`
Payload key: `template_data.q5_informal_meeting_date`

20. Question text: `Q5 Date 3G3R Issued`
Question type: `Date`
Required: `No`
Helper text: `Date 3G3R issued.`
Payload key: `template_data.q5_3g3r_issued_date`

21. Question text: `Q5 Date 2nd Level Meeting Held`
Question type: `Date`
Required: `No`
Helper text: `Date second-level meeting held.`
Payload key: `template_data.q5_second_level_meeting_date`

22. Question text: `Q5 Union Representative Name and ATTUID`
Question type: `Text`
Required: `Yes`
Helper text: `Print name and ATTUID if applicable.`
Payload key: `template_data.q5_union_rep_name_attuid`

### Section F - Stage routing emails

23. Question text: `Stage 1 Local Union Email`
Question type: `Text`
Required: `Yes`
Helper text: `Email for first signature step.`
Payload key: `documents[0].signers[0]`

24. Question text: `Stage 2 Second Level Manager Email`
Question type: `Text`
Required: `Yes`
Helper text: `Email for second signature step.`
Payload key: `documents[0].signers[1]`

25. Question text: `Stage 3 Union Final Decision Email`
Question type: `Text`
Required: `Yes`
Helper text: `Email for final union signature/disposition step.`
Payload key: `documents[0].signers[2]`

### Optional capture for later stages (recommended now)

26. Question text: `Q6 Company Statement (optional pre-capture)`
Question type: `Long text`
Required: `No`
Helper text: `Optional now; can be completed by manager in stage 2 if blank.`
Payload key: `template_data.q6_company_statement`

27. Question text: `Q7 Proposed Disposition - Second Level (optional pre-capture)`
Question type: `Long text`
Required: `No`
Helper text: `Optional now; can be completed by manager in stage 2 if blank.`
Payload key: `template_data.q7_proposed_disposition_second_level`

28. Question text: `Q7 Company Representative Name and ATTUID (optional pre-capture)`
Question type: `Text`
Required: `No`
Helper text: `Optional now; can be completed by manager in stage 2 if blank.`
Payload key: `template_data.q7_company_rep_name_attuid`

29. Question text: `Q8 Union Disposition (optional pre-capture)`
Question type: `Choice`
Required: `No`
Choices: `Accepted`, `Rejected`, `Appealed`, `Requested Mediation`
Helper text: `Optional now; can be completed in stage 3 if blank.`
Payload key: `template_data.q8_union_disposition`

30. Question text: `Q8 Union Representative Name and ATTUID (optional pre-capture)`
Question type: `Text`
Required: `No`
Helper text: `Optional now; can be completed in stage 3 if blank.`
Payload key: `template_data.q8_union_rep_name_attuid`

31. Question text: `Q9 Mediation Requested Date (optional pre-capture)`
Question type: `Date`
Required: `No`
Helper text: `Optional now; can be completed in stage 3 if blank.`
Payload key: `template_data.q9_mediation_requested_date`

32. Question text: `Q9 Mediation Held Date (optional pre-capture)`
Question type: `Date`
Required: `No`
Helper text: `Optional now; can be completed in stage 3 if blank.`
Payload key: `template_data.q9_mediation_held_date`

33. Question text: `Q9 Mediator Name (optional pre-capture)`
Question type: `Text`
Required: `No`
Helper text: `Optional now; can be completed in stage 3 if blank.`
Payload key: `template_data.q9_mediator_name`

34. Question text: `Q10 True Intent Question Exists`
Question type: `Choice`
Required: `Yes`
Choices: `Yes`, `No`
Helper text: `Question 10 true intent indicator.`
Payload key: `template_data.q10_true_intent_exists`

## Scope locked for this phase

- Only tag/fill content up to Question 10.
- Do not automate district/state sections after Question 10 yet.
- Keep those later sections printable/manual.

## Required workflow sequence (signing order)

1. Local union signs first (`signer1`).
2. 2nd level manager signs second (`signer2`).
3. Union signs decision third (`signer3`).

Use explicit signer order in payload:
- `documents[0].signers = [local_union_email, second_level_manager_email, union_decision_email]`

## Word tagging standard (use this exactly)

Use plain Jinja tags for text fields:
- `{{ q1_grievance_type }}`
- `{{ q1_occurred_date }}`
- `{{ q1_city_state }}`
- `{{ local_number }}`
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
- `{{ q6_company_statement }}`
- `{{ q7_proposed_disposition_second_level }}`
- `{{ q7_company_rep_name_attuid }}`
- `{{ q8_union_disposition }}`
- `{{ q8_union_rep_name_attuid }}`
- `{{ q9_mediation_requested_date }}`
- `{{ q9_mediation_held_date }}`
- `{{ q9_mediator_name }}`
- `{{ q10_true_intent_exists }}`

Use DocuSeal anchor tags for signatures/dates/emails:
- Step 1 union:
  - `{{Sig_es_:signer1:signature}}`
  - `{{Dte_es_:signer1:date}}`
  - `{{Eml_es_:signer1:email}}`
- Step 2 manager:
  - `{{Sig_es_:signer2:signature}}`
  - `{{Dte_es_:signer2:date}}`
  - `{{Eml_es_:signer2:email}}`
- Step 3 union decision:
  - `{{Sig_es_:signer3:signature}}`
  - `{{Dte_es_:signer3:date}}`
  - `{{Eml_es_:signer3:email}}`

## Line-stability rules (to avoid visual break issues)

1. Replace old `FORMTEXT` controls with plain text runs before inserting `{{ }}` tags.
2. Put each tag in one run (do not split tag across formatting changes).
3. Keep underlines as paragraph/table borders, not underlined spaces.
4. Keep long narrative fields (`q3_union_statement`, `q6_company_statement`, `q7_proposed_disposition_second_level`) in dedicated multiline areas.
5. Keep date-like values as text tags, not Word date controls.

## Power Automate mapping (questions 1-10)

Map Forms answers into `template_data` keys above using exact key names.

Recommended dropdown/text values:
- `q8_union_disposition`: `Accepted`, `Rejected`, `Appealed`, `Requested Mediation`
- `q10_true_intent_exists`: `Yes` or `No`

## HTTP body skeleton

```json
{
  "request_id": "forms-<responseId>",
  "document_command": "bst_grievance_form_3g3a",
  "grievance_id": "<existing grievance id>",
  "contract": "BellSouth",
  "grievant_firstname": "<first>",
  "grievant_lastname": "<last>",
  "grievant_email": "",
  "incident_date": "<yyyy-mm-dd>",
  "narrative": "<summary for case record>",
  "documents": [
    {
      "doc_type": "bst_grievance_form_3g3a",
      "template_key": "bst_grievance_form_3g3a",
      "requires_signature": true,
      "signers": [
        "<local_union_email>",
        "<second_level_manager_email>",
        "<union_decision_email>"
      ]
    }
  ],
  "template_data": {
    "q1_grievance_type": "BST",
    "q1_occurred_date": "<yyyy-mm-dd>",
    "q1_city_state": "<city, state>",
    "local_number": "3106",
    "q2_employee_name": "<name>",
    "q2_employee_attuid": "<attuid>",
    "q2_department": "<department>",
    "q2_job_title": "<job title>",
    "q2_payroll_id": "<pernr>",
    "q2_seniority_date": "<yyyy-mm-dd>",
    "q2a_job_title_requested": "",
    "q2a_requisition_number": "",
    "q2a_other_department": "",
    "q3_union_statement": "<statement>",
    "q4_contract_basis": "<article/section>",
    "q5_informal_meeting_date": "<yyyy-mm-dd>",
    "q5_3g3r_issued_date": "<yyyy-mm-dd>",
    "q5_second_level_meeting_date": "<yyyy-mm-dd>",
    "q5_union_rep_name_attuid": "<name/attuid>",
    "q6_company_statement": "",
    "q7_proposed_disposition_second_level": "",
    "q7_company_rep_name_attuid": "<name/attuid>",
    "q8_union_disposition": "",
    "q8_union_rep_name_attuid": "<name/attuid>",
    "q9_mediation_requested_date": "",
    "q9_mediation_held_date": "",
    "q9_mediator_name": "",
    "q10_true_intent_exists": "No"
  }
}
```

## Notes for current pipeline behavior

- `grievance_id` must be provided for this form (existing case/folder workflow).
- This doc is not in auto grievance-id mode.
- `documents[0].signers` is required and must contain exactly three ordered emails.
- The app sends stage 1 at intake, then auto-advances stage 2 and stage 3 from DocuSeal completion webhooks.
- The app stores stage artifacts in both local data path and SharePoint using stage-specific filenames.
