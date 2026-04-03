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
3. `Grievant email`
4. `Grievant phone`
5. `Grievant street`
6. `Grievant city`
7. `Grievant state`
8. `Grievant zip`
9. `Grievant title`
10. `Department`
11. `Seniority date`
12. `Local number`
13. `Local phone`
14. `Local street`
15. `Local city`
16. `Local state`
17. `Local zip`
18. `Date grievance occurred`
19. `Grievance type`
20. `Issue involved`
21. `Articles involved`
22. `Management structure`
23. `Step 1 informal date`
24. `Step 2 formal date`
25. `Appealed to state date`
26. `Timeline`
27. `Union argument`
28. `Analysis`
29. `Company name`
30. `Company position`
31. `Company strengths`
32. `Company weaknesses`
33. `Company proposed settlement`
34. `Union position`
35. `Union strengths`
36. `Union weaknesses`
37. `Union proposed settlement`
38. `Attachment 1 label`
39. `Attachment 2 label`
40. `Attachment 3 label`
41. `Attachment 4 label`
42. `Attachment 5 label`
43. `Attachment 6 label`
44. `Attachment 7 label`
45. `Attachment 8 label`
46. `Attachment 9 label`
47. `Attachment 10 label`
48. `Signer email override`

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
   - `documents[0].signing_link` when present

## Fixed values to keep

- `document_command`: `true_intent_brief`
- `contract`: `CWA`
- `narrative`: `True intent grievance brief`

## Notes

- Leave `template_data.signer_email` blank unless you need to override the default signer. If omitted, the app can fall back to `grievant_email`.
- Keep the same `request_id` when intentionally replaying the same Forms submission.
- Do not add DocuSeal signature anchors as Form questions.
- The attachment fields are labels or exhibit names, not uploaded files.
