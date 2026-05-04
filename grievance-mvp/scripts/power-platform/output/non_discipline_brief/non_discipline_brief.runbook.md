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
3. `Local number`
4. `Local grievance number`
5. `Location`
6. `Grievant(s) or work group`
7. `Grievant home address`
8. `Date grievance occurred`
9. `Date grievance filed`
10. `Date grievance appealed to executive level`
11. `Issue or condition involved`
12. `Action taken`
13. `Chronology of facts pertaining to grievance`
14. `Analysis of grievance`
15. `Current status of grievant or condition`
16. `Union position`
17. `Company position`
18. `Potential witnesses`
19. `Recommendation`
20. `Attachment 1 label`
21. `Attachment 2 label`
22. `Attachment 3 label`
23. `Attachment 4 label`
24. `Attachment 5 label`
25. `Attachment 6 label`
26. `Attachment 7 label`
27. `Attachment 8 label`
28. `Attachment 9 label`
29. `Attachment 10 label`

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
   - `documents[0].status`

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

- Brief submissions do not collect grievant email and do not route signatures.
- Keep the same `request_id` when intentionally replaying the same Forms submission.
- Do not add email or signature routing fields as Form questions.
- The attachment fields are labels or exhibit names, not uploaded files.
