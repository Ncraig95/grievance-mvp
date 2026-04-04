[CmdletBinding()]
param(
    [string]$CatalogPath = (Join-Path $PSScriptRoot "forms.catalog.json"),
    [string]$LocalConfigPath = (Join-Path $PSScriptRoot "forms.local.json"),
    [string]$OutputDir = (Join-Path (Join-Path $PSScriptRoot "output") "non_discipline_brief"),
    [string]$ApiBaseUrl = "",
    [switch]$Overwrite
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-JsonFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "JSON file not found: $Path"
    }

    return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json)
}

function Get-OptionalLocalConfig {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }

    return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json)
}

function New-FieldRow {
    param(
        [Parameter(Mandatory = $true)][string]$PayloadPath,
        [Parameter(Mandatory = $true)][string]$SourceType,
        [Parameter(Mandatory = $true)][string]$Section,
        [Parameter(Mandatory = $true)][string]$ValueSource,
        [Parameter(Mandatory = $true)][string]$ValueTemplate,
        [string]$QuestionTitle = "",
        [string]$QuestionType = "",
        [string]$RequiredByDefault = "",
        [string]$ExampleValue = "",
        [string]$Notes = ""
    )

    return [pscustomobject]([ordered]@{
            PayloadPath       = $PayloadPath
            SourceType        = $SourceType
            Section           = $Section
            FormQuestionTitle = $QuestionTitle
            QuestionType      = $QuestionType
            RequiredByDefault = $RequiredByDefault
            ValueSource       = $ValueSource
            ValueTemplate     = $ValueTemplate
            ExampleValue      = $ExampleValue
            Notes             = $Notes
        })
}

function Set-OrderedPathValue {
    param(
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$Target,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )

    $parts = $Path.Split(".")
    $node = $Target
    for ($index = 0; $index -lt ($parts.Length - 1); $index++) {
        $segment = $parts[$index]
        if (-not $node.Contains($segment)) {
            $node[$segment] = [ordered]@{}
        }
        $node = [System.Collections.IDictionary]$node[$segment]
    }
    $node[$parts[$parts.Length - 1]] = $Value
}

function Assert-TargetPathsWritable {
    param(
        [Parameter(Mandatory = $true)][string[]]$Paths,
        [switch]$AllowOverwrite
    )

    foreach ($path in $Paths) {
        if ((Test-Path -LiteralPath $path) -and -not $AllowOverwrite) {
            throw "Output file already exists. Re-run with -Overwrite: $path"
        }
    }
}

function Get-NonDisciplineRows {
    return @(
        (New-FieldRow -PayloadPath "request_id" -SourceType "Compose" -Section "Submission" -ValueSource "Trigger Response Id" -ValueTemplate "forms-<Response Id>" -ExampleValue "forms-123" -Notes "Build this from the Forms trigger Response Id so retries stay idempotent.")
        (New-FieldRow -PayloadPath "document_command" -SourceType "Fixed" -Section "Submission" -ValueSource "Fixed string" -ValueTemplate "non_discipline_brief" -ExampleValue "non_discipline_brief")
        (New-FieldRow -PayloadPath "contract" -SourceType "Fixed" -Section "Submission" -ValueSource "Fixed string" -ValueTemplate "CWA" -ExampleValue "CWA")
        (New-FieldRow -PayloadPath "grievant_firstname" -SourceType "Form" -Section "Grievant" -QuestionTitle "Grievant first name" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Grievant first name" -ValueTemplate "<Map from Forms: Grievant first name>" -ExampleValue "Taylor")
        (New-FieldRow -PayloadPath "grievant_lastname" -SourceType "Form" -Section "Grievant" -QuestionTitle "Grievant last name" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Grievant last name" -ValueTemplate "<Map from Forms: Grievant last name>" -ExampleValue "Jones")
        (New-FieldRow -PayloadPath "grievant_email" -SourceType "Form" -Section "Grievant" -QuestionTitle "Grievant email" -QuestionType "Email" -RequiredByDefault "Yes" -ValueSource "Get response details -> Grievant email" -ValueTemplate "<Map from Forms: Grievant email>" -ExampleValue "taylor.jones@example.com")
        (New-FieldRow -PayloadPath "narrative" -SourceType "Fixed" -Section "Submission" -ValueSource "Fixed string" -ValueTemplate "Non-discipline grievance brief" -ExampleValue "Non-discipline grievance brief")
        (New-FieldRow -PayloadPath "template_data.grievant_name" -SourceType "Compose" -Section "Grievant" -ValueSource "Compose from grievant_firstname + grievant_lastname" -ValueTemplate "<Compose from first and last name>" -ExampleValue "Taylor Jones")
        (New-FieldRow -PayloadPath "template_data.local_number" -SourceType "Form" -Section "Guide" -QuestionTitle "Local number" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Local number" -ValueTemplate "<Map from Forms: Local number>" -ExampleValue "3106")
        (New-FieldRow -PayloadPath "template_data.local_grievance_number" -SourceType "Form" -Section "Guide" -QuestionTitle "Local grievance number" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Local grievance number" -ValueTemplate "<Map from Forms: Local grievance number>" -ExampleValue "Local-26-001")
        (New-FieldRow -PayloadPath "template_data.location" -SourceType "Form" -Section "Guide" -QuestionTitle "Location" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Location" -ValueTemplate "<Map from Forms: Location>" -ExampleValue "Jacksonville, FL")
        (New-FieldRow -PayloadPath "template_data.grievant_or_work_group" -SourceType "Form" -Section "Guide" -QuestionTitle "Grievant(s) or work group" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Grievant(s) or work group" -ValueTemplate "<Map from Forms: Grievant(s) or work group>" -ExampleValue "Taylor Jones")
        (New-FieldRow -PayloadPath "template_data.grievant_home_address" -SourceType "Form" -Section "Guide" -QuestionTitle "Grievant home address" -QuestionType "Long text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Grievant home address" -ValueTemplate "<Map from Forms: Grievant home address>" -ExampleValue "123 Main St, Jacksonville, FL 32202")
        (New-FieldRow -PayloadPath "template_data.date_grievance_occurred" -SourceType "Form" -Section "Dates" -QuestionTitle "Date grievance occurred" -QuestionType "Date" -RequiredByDefault "Yes" -ValueSource "Get response details -> Date grievance occurred" -ValueTemplate "<Map from Forms: Date grievance occurred>" -ExampleValue "2026-04-02")
        (New-FieldRow -PayloadPath "template_data.date_grievance_filed" -SourceType "Form" -Section "Dates" -QuestionTitle "Date grievance filed" -QuestionType "Date" -RequiredByDefault "Yes" -ValueSource "Get response details -> Date grievance filed" -ValueTemplate "<Map from Forms: Date grievance filed>" -ExampleValue "2026-04-03")
        (New-FieldRow -PayloadPath "template_data.date_grievance_appealed_to_executive_level" -SourceType "Form" -Section "Dates" -QuestionTitle "Date grievance appealed to executive level" -QuestionType "Date" -RequiredByDefault "No" -ValueSource "Get response details -> Date grievance appealed to executive level" -ValueTemplate "<Map from Forms: Date grievance appealed to executive level>" -ExampleValue "2026-04-10")
        (New-FieldRow -PayloadPath "template_data.issue_or_condition_involved" -SourceType "Form" -Section "Guide" -QuestionTitle "Issue or condition involved" -QuestionType "Long text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Issue or condition involved" -ValueTemplate "<Map from Forms: Issue or condition involved>" -ExampleValue "Management denied agreed scheduling rights.")
        (New-FieldRow -PayloadPath "template_data.action_taken" -SourceType "Form" -Section "Guide" -QuestionTitle "Action taken" -QuestionType "Long text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Action taken" -ValueTemplate "<Map from Forms: Action taken>" -ExampleValue "Steward requested immediate correction and meeting.")
        (New-FieldRow -PayloadPath "template_data.chronology_of_facts" -SourceType "Form" -Section "Guide" -QuestionTitle "Chronology of facts pertaining to grievance" -QuestionType "Long text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Chronology of facts pertaining to grievance" -ValueTemplate "<Map from Forms: Chronology of facts pertaining to grievance>" -ExampleValue "04/02 event occurred, 04/03 grievance filed, 04/04 response issued.")
        (New-FieldRow -PayloadPath "template_data.analysis_of_grievance" -SourceType "Form" -Section "Guide" -QuestionTitle "Analysis of grievance" -QuestionType "Long text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Analysis of grievance" -ValueTemplate "<Map from Forms: Analysis of grievance>" -ExampleValue "The facts and contract language support the union position.")
        (New-FieldRow -PayloadPath "template_data.current_status" -SourceType "Form" -Section "Guide" -QuestionTitle "Current status of grievant or condition" -QuestionType "Long text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Current status of grievant or condition" -ValueTemplate "<Map from Forms: Current status of grievant or condition>" -ExampleValue "Condition remains unresolved and continues to affect the grievant.")
        (New-FieldRow -PayloadPath "template_data.union_position" -SourceType "Form" -Section "Guide" -QuestionTitle "Union position" -QuestionType "Long text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Union position" -ValueTemplate "<Map from Forms: Union position>" -ExampleValue "Union requests a full corrective remedy.")
        (New-FieldRow -PayloadPath "template_data.company_position" -SourceType "Form" -Section "Guide" -QuestionTitle "Company position" -QuestionType "Long text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Company position" -ValueTemplate "<Map from Forms: Company position>" -ExampleValue "Management claims the action was operationally necessary.")
        (New-FieldRow -PayloadPath "template_data.potential_witnesses" -SourceType "Form" -Section "Guide" -QuestionTitle "Potential witnesses" -QuestionType "Long text" -RequiredByDefault "No" -ValueSource "Get response details -> Potential witnesses" -ValueTemplate "<Map from Forms: Potential witnesses>" -ExampleValue "Taylor Jones, Chris Smith, Area Manager")
        (New-FieldRow -PayloadPath "template_data.recommendation" -SourceType "Form" -Section "Guide" -QuestionTitle "Recommendation" -QuestionType "Long text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Recommendation" -ValueTemplate "<Map from Forms: Recommendation>" -ExampleValue "Advance the grievance and seek full make-whole relief.")
        (New-FieldRow -PayloadPath "template_data.attachment_1" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 1 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 1 label" -ValueTemplate "<Map from Forms: Attachment 1 label>" -ExampleValue "Exhibit A - Timeline")
        (New-FieldRow -PayloadPath "template_data.attachment_2" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 2 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 2 label" -ValueTemplate "<Map from Forms: Attachment 2 label>" -ExampleValue "Exhibit B - Witness statement")
        (New-FieldRow -PayloadPath "template_data.attachment_3" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 3 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 3 label" -ValueTemplate "<Map from Forms: Attachment 3 label>" -ExampleValue "")
        (New-FieldRow -PayloadPath "template_data.attachment_4" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 4 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 4 label" -ValueTemplate "<Map from Forms: Attachment 4 label>" -ExampleValue "")
        (New-FieldRow -PayloadPath "template_data.attachment_5" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 5 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 5 label" -ValueTemplate "<Map from Forms: Attachment 5 label>" -ExampleValue "")
        (New-FieldRow -PayloadPath "template_data.attachment_6" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 6 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 6 label" -ValueTemplate "<Map from Forms: Attachment 6 label>" -ExampleValue "")
        (New-FieldRow -PayloadPath "template_data.attachment_7" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 7 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 7 label" -ValueTemplate "<Map from Forms: Attachment 7 label>" -ExampleValue "")
        (New-FieldRow -PayloadPath "template_data.attachment_8" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 8 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 8 label" -ValueTemplate "<Map from Forms: Attachment 8 label>" -ExampleValue "")
        (New-FieldRow -PayloadPath "template_data.attachment_9" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 9 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 9 label" -ValueTemplate "<Map from Forms: Attachment 9 label>" -ExampleValue "")
        (New-FieldRow -PayloadPath "template_data.attachment_10" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 10 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 10 label" -ValueTemplate "<Map from Forms: Attachment 10 label>" -ExampleValue "")
        (New-FieldRow -PayloadPath "template_data.signer_email" -SourceType "Form" -Section "Routing" -QuestionTitle "Signer email override" -QuestionType "Email" -RequiredByDefault "No" -ValueSource "Get response details -> Signer email override" -ValueTemplate "<Optional Forms answer: Signer email override>" -ExampleValue "steward@example.com")
    )
}

$catalog = Get-JsonFile -Path $CatalogPath
$form = $catalog.forms | Where-Object { $_.key -eq "non_discipline_brief" } | Select-Object -First 1
if ($null -eq $form) {
    throw "non_discipline_brief was not found in the catalog: $CatalogPath"
}

$localConfig = Get-OptionalLocalConfig -Path $LocalConfigPath
$localForm = $null
if ($localConfig -and $localConfig.forms) {
    $localForm = $localConfig.forms.non_discipline_brief
}

$resolvedApiBaseUrl = $ApiBaseUrl
if ([string]::IsNullOrWhiteSpace($resolvedApiBaseUrl) -and $localConfig -and $localConfig.apiBaseUrl) {
    $resolvedApiBaseUrl = [string]$localConfig.apiBaseUrl
}
if ([string]::IsNullOrWhiteSpace($resolvedApiBaseUrl)) {
    $resolvedApiBaseUrl = [string]$catalog.apiBaseUrl
}

$flowDisplayName = "CWA 3106 - Non-Discipline Brief Intake"
if ($localForm -and $localForm.flowDisplayName) {
    $flowDisplayName = [string]$localForm.flowDisplayName
}

$microsoftFormId = ""
if ($localForm -and $localForm.microsoftFormId) {
    $microsoftFormId = [string]$localForm.microsoftFormId
}

$publishedFormUrl = ""
if ($localForm -and $localForm.publishedFormUrl) {
    $publishedFormUrl = [string]$localForm.publishedFormUrl
}

if (-not (Test-Path -LiteralPath $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}

$csvPath = Join-Path $OutputDir "non_discipline_brief.forms-map.csv"
$jsonPath = Join-Path $OutputDir "non_discipline_brief.http-body.json"
$runbookPath = Join-Path $OutputDir "non_discipline_brief.runbook.md"

Assert-TargetPathsWritable -Paths @($csvPath, $jsonPath, $runbookPath) -AllowOverwrite:$Overwrite

$rows = Get-NonDisciplineRows
$rows | Export-Csv -LiteralPath $csvPath -NoTypeInformation -Encoding UTF8

$payload = [ordered]@{}
foreach ($row in $rows) {
    Set-OrderedPathValue -Target $payload -Path $row.PayloadPath -Value $row.ValueTemplate
}

$payloadJson = $payload | ConvertTo-Json -Depth 20
Set-Content -LiteralPath $jsonPath -Value $payloadJson -Encoding UTF8

$formIdLine = if ([string]::IsNullOrWhiteSpace($microsoftFormId)) {
    "- Microsoft Form Id: fill this in after the Form exists"
}
else {
    "- Microsoft Form Id: $microsoftFormId"
}

$publishedUrlLine = if ([string]::IsNullOrWhiteSpace($publishedFormUrl)) {
    "- Published Form URL: still blank in local config"
}
else {
    "- Published Form URL: $publishedFormUrl"
}

$runbook = @"
# Non-Discipline Brief Power Automate Pack

This pack is the build sheet for the `non_discipline_brief` Microsoft Form and flow.

## Resolved values

- Flow display name: `$flowDisplayName`
- Endpoint: `$resolvedApiBaseUrl$($form.endpointPath)`
- Document command: `$($form.documentCommand)`
- Contract: `CWA`
$formIdLine
$publishedUrlLine

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
"@

Set-Content -LiteralPath $runbookPath -Value $runbook.Trim() -Encoding UTF8

Write-Host "Wrote Non-Discipline Brief pack:"
Write-Host $csvPath
Write-Host $jsonPath
Write-Host $runbookPath
