# True Intent Brief Power Automate Pack

This pack is the build sheet for the `true_intent_brief` Microsoft Form and flow.

## Resolved values

- Flow display name: `CWA 3106 - True Intent Brief Intake`
- Endpoint: `https://api.cwa3106.org/intake`
- Document command: `true_intent_brief`
- Contract: `CWA`

## Files in this pack

- `true_intent_brief.forms-map.csv`
  Use this to build the Microsoft Form and map every answer from `Get response details`.
- `true_intent_brief.http-body.json`
  Paste this into the `HTTP` action body, then replace each placeholder with dynamic content or a `Compose` output.
- `true_intent_brief.runbook.md`
  This file.

## Form question order

Add these questions in this order:

1. `Grievant first name`
2. `Grievant last name`
3. `Grievant phone`
4. `Grievant street`
5. `Grievant city`
6. `Grievant state`
7. `Grievant zip`
8. `Grievant title`
9. `Department`
10. `Seniority date`
11. `Local number`
12. `Local phone`
13. `Local street`
14. `Local city`
15. `Local state`
16. `Local zip`
17. `Date grievance occurred`
18. `Grievance type`
19. `Issue involved`
20. `Articles involved`
21. `Management structure`
22. `Step 1 informal date`
23. `Step 2 formal date`
24. `Appealed to state date`
25. `Timeline`
26. `Union argument`
27. `Analysis`
28. `Company name`
29. `Company position`
30. `Company strengths`
31. `Company weaknesses`
32. `Company proposed settlement`
33. `Union position`
34. `Union strengths`
35. `Union weaknesses`
36. `Union proposed settlement`
37. `Attachment 1 label`
38. `Attachment 2 label`
39. `Attachment 3 label`
40. `Attachment 4 label`
41. `Attachment 5 label`
42. `Attachment 6 label`
43. `Attachment 7 label`
44. `Attachment 8 label`
45. `Attachment 9 label`
46. `Attachment 10 label`

Use the CSV `Section` column to group them in Forms if you want separate pages/sections.

## Fields that are not Form questions

Do not add these to the Form:

- `request_id`
- `document_command`
- `contract`
- `narrative`
- `template_data.grievant_name`

Those are fixed or composed in the flow.

## Power Automate flow

1. Create an automated cloud flow named `CWA 3106 - True Intent Brief Intake`.
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
10. Body: paste `true_intent_brief.http-body.json`.
11. Replace each placeholder with the matching Forms answer or one of the two `Compose` outputs.
12. Parse the JSON response and capture at least:
   - `case_id`
   - `grievance_id`
   - `documents[0].status`

## Fixed values to keep

- `document_command`: `true_intent_brief`
- `contract`: `CWA`
- `narrative`: `True intent grievance brief`

## Notes

- Brief submissions do not collect grievant email and do not route signatures.
- Keep the same `request_id` when intentionally replaying the same Forms submission.
- Do not add email or signature routing fields as Form questions.
- The attachment fields are labels or exhibit names, not uploaded files.
