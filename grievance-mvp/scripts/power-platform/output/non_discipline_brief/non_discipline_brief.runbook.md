# Non-Discipline Brief Power Automate Pack

This pack is the build sheet for the `non_discipline_brief` Microsoft Form and flow.

## Resolved values

- Flow display name: `CWA 3106 - Non-Discipline Brief Intake`
- Endpoint: `https://api.cwa3106.org/intake`
- Document command: `non_discipline_brief`
- Contract: `CWA`

## Files in this pack

- `non_discipline_brief.forms-map.csv`
  Use this to build the Microsoft Form and map every answer from `Get response details`.
- `non_discipline_brief.http-body.json`
  Paste this into the `HTTP` action body, then replace each placeholder with dynamic content or a `Compose` output.
- `non_discipline_brief.runbook.md`
  This file.

## Form question order

Add these questions in this order:

1. `Grievant first name`
2. `Grievant last name`
3. `Grievant email`
4. `Local number`
5. `Local grievance number`
6. `Location`
7. `Grievant(s) or work group`
8. `Grievant home address`
9. `Date grievance occurred`
10. `Date grievance filed`
11. `Date grievance appealed to executive level`
12. `Issue or condition involved`
13. `Action taken`
14. `Chronology of facts pertaining to grievance`
15. `Analysis of grievance`
16. `Current status of grievant or condition`
17. `Union position`
18. `Company position`
19. `Potential witnesses`
20. `Recommendation`
21. `Attachment 1 label`
22. `Attachment 2 label`
23. `Attachment 3 label`
24. `Attachment 4 label`
25. `Attachment 5 label`
26. `Attachment 6 label`
27. `Attachment 7 label`
28. `Attachment 8 label`
29. `Attachment 9 label`
30. `Attachment 10 label`
31. `Signer email override`

Use the CSV `Section` column to group them in Forms if you want separate pages or branches.

## Fields that are not Form questions

Do not add these to the Form:

- `request_id`
- `document_command`
- `contract`
- `narrative`
- `template_data.grievant_name`

Those are fixed or composed in the flow.

## Power Automate flow

1. Create an automated cloud flow named `CWA 3106 - Non-Discipline Brief Intake`.
2. Trigger: `When a new response is submitted`.
3. Action: `Get response details`.
4. Add a `Compose` action named `Compose Request Id` with:

```text
concat('forms-', <Response Id dynamic content>)
```

5. Add a `Compose` action named `Compose Grievant Name` with:

```text
concat(<Grievant first name dynamic content>, ' ', <Grievant last name dynamic content>)
```

6. Add an `HTTP` action.
7. Method: `POST`
8. URL: `https://api.cwa3106.org/intake`
9. Headers:
   - `Content-Type: application/json`
   - intake auth headers if enabled in your environment
10. Body: paste `non_discipline_brief.http-body.json`.
11. Replace each placeholder with the matching Forms answer or one of the two `Compose` outputs.
12. Parse the JSON response and capture at least:
   - `case_id`
   - `grievance_id`
   - `documents[0].signing_link` when present

## Shared Form and flow option

If you are using one shared Form/Flow for both brief types:

- Add a required `Brief type` choice question.
- Choices: `True Intent Brief`, `Non-Discipline Brief`
- Branch the Form so each brief shows the right section set.
- In Power Automate, use a `Switch` on `Brief type` and send:
  - `true_intent_brief` for the True Intent branch
  - `non_discipline_brief` for the Non-Discipline branch

## Fixed values to keep

- `document_command`: `non_discipline_brief`
- `contract`: `CWA`
- `narrative`: `Non-discipline grievance brief`

## Notes

- Leave `template_data.signer_email` blank unless you need to override the default signer.
- Keep the same `request_id` when intentionally replaying the same Forms submission.
- Do not add DocuSeal signature anchors as Form questions.
- The attachment fields are labels or exhibit names, not uploaded files.
