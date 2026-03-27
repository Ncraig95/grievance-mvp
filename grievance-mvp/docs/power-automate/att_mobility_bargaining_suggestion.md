# AT&T Mobility Bargaining Suggestion Form + Power Automate Handoff

## Use This Guide For

- Building the Microsoft Form for AT&T Mobility bargaining demands.
- Building the Power Automate flow that sends the response to the standalone signing workflow.
- Understanding which values come from Forms, which values come from `config.yaml`, and which values are completed by the Local President in DocuSeal.

## Workflow Summary

- Form key: `att_mobility_bargaining_suggestion`
- API endpoint: `POST /standalone/forms/att_mobility_bargaining_suggestion/submissions`
- Signer count: `1`
- Signer role: `Local President`
- Signer email source:
  `standalone_forms.att_mobility_bargaining_suggestion.default_signer_email` in `config.yaml`
- DocuSeal-only signer field:
  `article_affected`
- SharePoint filing rule:
  nothing is filed before signature completion
- Final SharePoint filing tree:
  `Mobility Demand Forms/<year>/Mobility Demand <n>/`
- Uploaded artifacts after completion:
  `Mobility Demand <n>.pdf`
  `Mobility Demand <n> Audit.<ext>`

## Who Should Submit This Form

- Use this form when a mobility member, steward, or local representative needs to submit a bargaining demand for AT&T Mobility.
- The form submitter is not the signer unless the Local President is also the person completing the form.
- After submission, Power Automate sends the payload to the standalone workflow, the document is rendered, DocuSeal emails the signing link to the configured Local President, the Local President fills `Article Affected` directly in DocuSeal, and SharePoint filing happens only after signature completion.

## Form Metadata

- Form title:
  `AT&T Mobility Bargaining Suggestion`
- Form description:
  `Use this form to submit a bargaining demand for AT&T Mobility. After submission, the demand is turned into a signable document for the Local President. The Local President completes the affected article and signature in DocuSeal. The signed demand is filed to SharePoint only after signature completion.`

## Do Not Add These As Forms Questions

- `Local President Signer Email`
  This now comes from `grievance-mvp/config/config.yaml` under `standalone_forms.att_mobility_bargaining_suggestion.default_signer_email`.
- `Article Affected`
  This is no longer collected in Microsoft Forms. It is a signer1 DocuSeal text field on the Local President signing link.

## Forms Build Sheet

### Question 1

- Question text:
  `Local Number`
- Help text / subtext:
  `Enter the local number submitting this bargaining demand, for example 3106.`
- Question type:
  `Text`
- Required:
  `Yes`
- Payload key:
  `template_data.local_number`

### Question 2

- Question text:
  `Demand From Local`
- Help text / subtext:
  `Enter the title of the bargaining demand as you want it to appear on the final document.`
- Question type:
  `Text`
- Required:
  `Yes`
- Payload key:
  `template_data.demand_from_local`

### Question 3

- Question text:
  `Title of Mobility Member Submitting Demand`
- Help text / subtext:
  `Enter the title of the member or representative submitting this demand, such as Steward or Member.`
- Question type:
  `Text`
- Required:
  `No`
- Payload key:
  `template_data.submitting_member_title`

### Question 4

- Question text:
  `Name of Mobility Member Submitting Demand`
- Help text / subtext:
  `Enter the full name of the mobility member or representative submitting this demand.`
- Question type:
  `Text`
- Required:
  `Yes`
- Payload key:
  `template_data.submitting_member_name`

### Question 5

- Question text:
  `Demand`
- Help text / subtext:
  `State the bargaining demand exactly as you want it submitted. Use full sentences if needed.`
- Question type:
  `Long text`
- Required:
  `Yes`
- Payload key:
  `template_data.demand_text`

### Question 6

- Question text:
  `Reason`
- Help text / subtext:
  `Explain why this demand is needed and what problem or condition it is meant to address.`
- Question type:
  `Long text`
- Required:
  `Yes`
- Payload key:
  `template_data.reason_text`

### Question 7

- Question text:
  `Specific Examples`
- Help text / subtext:
  `List specific examples, incidents, or recurring problems that support this bargaining demand.`
- Question type:
  `Long text`
- Required:
  `No`
- Payload key:
  `template_data.specific_examples_text`

### Question 8

- Question text:
  `Work Phone`
- Help text / subtext:
  `Enter a work phone number for follow-up if one is available.`
- Question type:
  `Text`
- Required:
  `No`
- Payload key:
  `template_data.work_phone`

### Question 9

- Question text:
  `Home Phone`
- Help text / subtext:
  `Enter a non-work or home phone number for follow-up if one is available.`
- Question type:
  `Text`
- Required:
  `No`
- Payload key:
  `template_data.home_phone`

### Question 10

- Question text:
  `Non-Work Email Address`
- Help text / subtext:
  `Enter a personal email address for follow-up if one is available.`
- Question type:
  `Text`
- Required:
  `No`
- Payload key:
  `template_data.non_work_email`

## Fixed Flow Values And Non-Question Values

- `request_id`
  Use `forms-<Response Id>`. The Response Id must stay stable if you replay the same submission.
- `form_key`
  Use the fixed value `att_mobility_bargaining_suggestion`.
- `local_president_signer_email`
  Do not ask this in Forms. By default the API reads it from `standalone_forms.att_mobility_bargaining_suggestion.default_signer_email` in `config.yaml`.
- `article_affected`
  Do not send this from Forms. The Local President completes it in DocuSeal on the signer link.
- API URL
  Use `https://api.cwa3106.org/standalone/forms/att_mobility_bargaining_suggestion/submissions`
- Do not send:
  `grievance_id`
  `grievance_number`
  `document_command`
  `documents`

## Required Config Before Go-Live

- In `grievance-mvp/config/config.yaml`, set:

```yaml
standalone_forms:
  att_mobility_bargaining_suggestion:
    default_signer_email: local.president@example.org
```

- Leave the field blank only in `config.example.yaml`.
- If `default_signer_email` is blank in the live config and the flow does not send an override, submission creation will fail with `400`.

## Power Automate Build

### Trigger And Response Lookup

1. Add trigger:
   `When a new response is submitted`
2. Add action:
   `Get response details`
3. Use the same Form Id from the trigger in `Get response details`.

### Compose Values

4. Add a `Compose` action for `request_id`.
   Build it as `forms-` plus the Microsoft Forms Response Id.
5. Add a `Compose` action for `form_key`.
   Set it to `att_mobility_bargaining_suggestion`.

### HTTP Action

6. Add an `HTTP` action.
- Method:
  `POST`
- URL:
  `https://api.cwa3106.org/standalone/forms/att_mobility_bargaining_suggestion/submissions`
- Headers:
  `Content-Type: application/json`
  Add your intake auth headers if they are enabled in `config.yaml`.

### HTTP Body

7. Use this JSON body shape and replace the placeholders with the matching Forms dynamic values:

```json
{
  "request_id": "forms-<Response Id>",
  "form_key": "att_mobility_bargaining_suggestion",
  "template_data": {
    "local_number": "<Local Number>",
    "demand_from_local": "<Demand From Local>",
    "submitting_member_title": "<Title of Mobility Member Submitting Demand>",
    "submitting_member_name": "<Name of Mobility Member Submitting Demand>",
    "demand_text": "<Demand>",
    "reason_text": "<Reason>",
    "specific_examples_text": "<Specific Examples>",
    "work_phone": "<Work Phone>",
    "home_phone": "<Home Phone>",
    "non_work_email": "<Non-Work Email Address>"
  }
}
```

### Optional Override

8. Only if you need to override the configured signer for a temporary run, add:

```json
"local_president_signer_email": "override@example.org"
```

### Response Handling

9. Parse the HTTP response if you need the returned metadata.
10. Capture:
- `submission_id`
- `status`
- `documents[0].signing_link`
- `documents[0].document_link`

### What To Expect

- Right after submission:
  `status` should be `awaiting_signature`
- Before the Local President signs:
  `documents[0].signing_link` should be present
  `documents[0].document_link` should usually be blank
- In DocuSeal:
  the Local President should see a required text field for `Article Affected` and the signature line
- After signature completion:
  `documents[0].document_link` should point to the signed SharePoint file
  SharePoint should contain the signed PDF and audit artifact under:
  `Mobility Demand Forms/<year>/Mobility Demand <n>/`

## Idempotency Rules

- The workflow treats `request_id` as the idempotency key.
- If the same form response is replayed, send the same `request_id`.
- Reusing the same `request_id` returns the existing standalone submission instead of creating a second one.

## Troubleshooting

### Missing Or Bad Local President Email

- Symptom:
  the request fails validation or no signature request is sent.
- Meaning:
  `standalone_forms.att_mobility_bargaining_suggestion.default_signer_email` is blank or wrong in `config.yaml`, and no override was sent in the request body.
- Fix:
  set the actual Local President signer email in `config.yaml`, reload the API container, and replay the same response with the same `request_id` only if the original submission was never created.

### Article Affected Was Expected In Forms

- Symptom:
  operators think the flow is missing a question.
- Meaning:
  this is intentional. `Article Affected` is now completed by signer1 in DocuSeal.
- Fix:
  do not add it back to Microsoft Forms. Confirm the template still contains `{{Txt_es_:signer1:article_affected}}`.

### Duplicate Request ID

- Symptom:
  you get an existing submission back instead of a new one.
- Meaning:
  the same `request_id` was already used.
- Fix:
  use the original response only once, or intentionally reuse the same `request_id` only when replaying the same submission.

### Signed Document Not In SharePoint Yet

- Symptom:
  the submission exists and has a signing link, but no SharePoint document link is returned yet.
- Meaning:
  this is expected until the Local President finishes both the `Article Affected` input and the signature in DocuSeal.
- Fix:
  wait for DocuSeal completion, then check the standalone submission again.

### Signature Completed But Nothing Was Filed

- Symptom:
  the Local President signed, but the SharePoint folder or files are missing.
- Meaning:
  filing happens in the DocuSeal completion webhook path.
- Fix:
  check DocuSeal webhook delivery, API logs, and SharePoint Graph credentials.

## Final Filing Behavior

- This workflow does not upload a generated draft to SharePoint at submission time.
- The filing number is assigned only after signature completion.
- The numbering resets each year.
- The final SharePoint folder name is:
  `Mobility Demand <n>`
- The final uploaded artifacts are:
  `Mobility Demand <n>.pdf`
  `Mobility Demand <n> Audit.<ext>`
