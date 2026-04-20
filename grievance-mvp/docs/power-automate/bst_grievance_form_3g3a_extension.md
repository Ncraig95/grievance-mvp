# 3G3A Extension Request Setup

## Document command

- `bst_grievance_form_3g3a_extension`

## Purpose

- This is a separate extension-only workflow.
- It does not change the existing `bst_grievance_form_3g3a` route.
- Signer order stays fixed: signer1 (union) -> signer2 (manager) -> signer3 (union final).

## Microsoft Forms blueprint

Form name:
- `CWA 3106 - 3G3A Extension Request`

Form description:
- `Submit a separate 3G3A extension request for grievance timeout handling. This route follows the same staged signer order as 3G3A without changing the current 3G3A intake.`

### Section A - Request metadata

1. Question text: `Existing Grievance ID`
Question type: `Text`
Required: `Yes`
Helper text: `Existing folder/case grievance number, e.g. 2026001.`
Payload key: `grievance_id`

2. Question text: `Contract`
Question type: `Choice`
Required: `Yes`
Choices: `BST`, `Utilities`
Helper text: `Select the contract bucket used for this extension request.`
Payload key: `contract`

3. Question text: `Request Summary`
Question type: `Long text`
Required: `Yes`
Helper text: `Short internal summary for routing and review.`
Payload key: `narrative`

### Section B - Question 1

4. Question text: `Q1 Date Occurred`
Question type: `Date`
Required: `Yes`
Helper text: `Date the original issue occurred.`
Payload key: `template_data.q1_occurred_date`

5. Question text: `Q1 City and State`
Question type: `Text`
Required: `Yes`
Helper text: `City and state where the issue occurred.`
Payload key: `template_data.q1_city_state`

### Section C - Question 2 and 2A

6. Question text: `Q2 Employee or Work Group Name`
Question type: `Text`
Required: `Yes`
Helper text: `Full name of employee or work group tied to the grievance.`
Payload key: `template_data.q2_employee_name`

7. Question text: `Q2 Employee ATTUID`
Question type: `Text`
Required: `No`
Payload key: `template_data.q2_employee_attuid`

8. Question text: `Q2 Department`
Question type: `Text`
Required: `No`
Payload key: `template_data.q2_department`

9. Question text: `Q2 Job Title`
Question type: `Text`
Required: `No`
Payload key: `template_data.q2_job_title`

10. Question text: `Q2 Payroll ID (PERNR)`
Question type: `Text`
Required: `No`
Payload key: `template_data.q2_payroll_id`

11. Question text: `Q2 Seniority Date`
Question type: `Date`
Required: `No`
Payload key: `template_data.q2_seniority_date`

12. Question text: `Q2A Job Title Involved or Requested`
Question type: `Text`
Required: `No`
Payload key: `template_data.q2a_job_title_requested`

13. Question text: `Q2A Requisition Number`
Question type: `Text`
Required: `No`
Payload key: `template_data.q2a_requisition_number`

Do not add this from the current 3G3A questionnaire:
- Question 17 / `template_data.q2a_other_department`

### Section C - Question 3

14. Question text: `Q3 Union Statement`
Question type: `Long text`
Required: `Yes`
Helper text: `Enter the union statement requesting the extension.`
Payload key: `template_data.q3_union_statement`

### Section D - Question 4

15. Question text: `Q4 Specific Basis or Contract Section`
Question type: `Long text`
Required: `Yes`
Helper text: `Contract language, policy, or basis supporting the extension request.`
Payload key: `template_data.q4_contract_basis`

### Section E - Question 5

15. Question text: `Q5 Informal Meeting Date`
Question type: `Date`
Required: `No`
Payload key: `template_data.q5_informal_meeting_date`

16. Question text: `Q5 Date 3G3R Issued`
Question type: `Date`
Required: `No`
Payload key: `template_data.q5_3g3r_issued_date`

17. Question text: `Q5 Union Representative Name and ATTUID`
Question type: `Text`
Required: `Yes`
Payload key: `template_data.q5_union_rep_name_attuid`

### Section F - Stage routing emails

18. Question text: `Stage 1 Local Union Email`
Question type: `Text`
Required: `Yes`
Payload key: `documents[0].signers[0]`

19. Question text: `Stage 2 Second Level Manager Email`
Question type: `Text`
Required: `Yes`
Payload key: `documents[0].signers[1]`

20. Question text: `Stage 3 Union Final Decision Email`
Question type: `Text`
Required: `Yes`
Payload key: `documents[0].signers[2]`

## Power Automate build

Recommended flow:

1. Trigger: `When a new response is submitted`
2. Action: `Get response details`
3. Optional approval gate: `Start and wait for an approval`
4. HTTP action:
   - Method: `POST`
   - URL: `https://<your-api-host>/intake`
   - Headers:
     - `Content-Type: application/json`
     - intake auth header(s) configured in your app

Body template:

```json
{
  "request_id": "forms-3g3a-extension-@{triggerOutputs()?['body/resourceData/responseId']}",
  "document_command": "bst_grievance_form_3g3a_extension",
  "grievance_id": "@{outputs('Get_response_details')?['body/<ExistingGrievanceIdQuestionId>']}",
  "contract": "@{outputs('Get_response_details')?['body/<ContractQuestionId>']}",
  "grievant_firstname": "",
  "grievant_lastname": "",
  "grievant_email": "",
  "incident_date": "@{outputs('Get_response_details')?['body/<Q1DateOccurredQuestionId>']}",
  "narrative": "@{outputs('Get_response_details')?['body/<RequestSummaryQuestionId>']}",
  "documents": [
    {
      "doc_type": "bst_grievance_form_3g3a_extension",
      "template_key": "bst_grievance_form_3g3a_extension",
      "requires_signature": true,
      "signers": [
        "@{outputs('Get_response_details')?['body/<UnionStage1EmailQuestionId>']}",
        "@{outputs('Get_response_details')?['body/<ManagerStage2EmailQuestionId>']}",
        "@{outputs('Get_response_details')?['body/<UnionStage3EmailQuestionId>']}"
      ]
    }
  ],
  "template_data": {
    "q1_occurred_date": "@{outputs('Get_response_details')?['body/<Q1DateOccurredQuestionId>']}",
    "q1_city_state": "@{outputs('Get_response_details')?['body/<Q1CityStateQuestionId>']}",
    "local_number": "3106",
    "q2_employee_name": "@{outputs('Get_response_details')?['body/<Q2EmployeeNameQuestionId>']}",
    "q2_employee_attuid": "@{outputs('Get_response_details')?['body/<Q2EmployeeAttuidQuestionId>']}",
    "q2_department": "@{outputs('Get_response_details')?['body/<Q2DepartmentQuestionId>']}",
    "q2_job_title": "@{outputs('Get_response_details')?['body/<Q2JobTitleQuestionId>']}",
    "q2_payroll_id": "@{outputs('Get_response_details')?['body/<Q2PayrollIdQuestionId>']}",
    "q2_seniority_date": "@{outputs('Get_response_details')?['body/<Q2SeniorityDateQuestionId>']}",
    "q2a_job_title_requested": "@{outputs('Get_response_details')?['body/<Q2AJobTitleRequestedQuestionId>']}",
    "q2a_requisition_number": "@{outputs('Get_response_details')?['body/<Q2ARequisitionNumberQuestionId>']}",
    "q3_union_statement": "@{outputs('Get_response_details')?['body/<Q3UnionStatementQuestionId>']}",
    "q4_contract_basis": "@{outputs('Get_response_details')?['body/<Q4ContractBasisQuestionId>']}",
    "q5_informal_meeting_date": "@{outputs('Get_response_details')?['body/<Q5InformalMeetingDateQuestionId>']}",
    "q5_3g3r_issued_date": "@{outputs('Get_response_details')?['body/<Q53G3RIssuedDateQuestionId>']}",
    "q5_union_rep_name_attuid": "@{outputs('Get_response_details')?['body/<Q5UnionRepQuestionId>']}"
  }
}
```

## Notes

- `grievance_id` is required.
- `documents[0].signers` must contain exactly 3 emails in union -> manager -> union order.
- Keep the `forms-3g3a-extension-` request id prefix so it does not collide with the current 3G3A flow.
- Do not collect Q6/Q7/Q8/Q9/Q10 stage-owned fields in Microsoft Forms.
- Do not add the removed question 17 from the current 3G3A questionnaire.
- Collect `q3_union_statement` as a user-entered long-text field.
