[CmdletBinding()]
param(
    [string]$CatalogPath = (Join-Path $PSScriptRoot "forms.catalog.json"),
    [string]$LocalConfigPath = (Join-Path $PSScriptRoot "forms.local.json"),
    [string]$OutputDir = (Join-Path (Join-Path $PSScriptRoot "output") "true_intent_brief"),
    [string]$ApiBaseUrl = "",
    [switch]$Overwrite
)

<#
Builds the operator pack for the True Intent Grievance Brief Microsoft Form and Power Automate flow.

What this script does:
- reads the shared form catalog and optional local config
- writes a Forms question map CSV for the true intent brief
- writes a ready-to-edit HTTP body JSON template for the /intake call
- writes a short runbook that tells the operator how to wire the flow

What this script does not do:
- it does not create the Microsoft Form in Microsoft 365
- it does not create the Power Automate cloud flow in the tenant
- it does not publish the Form or replace the repo placeholder URL

This script is meant to remove the manual copy/paste work once the Form is being built.
#>

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

function Get-TrueIntentRows {
    return @(
        (New-FieldRow -PayloadPath "request_id" -SourceType "Compose" -Section "Submission" -ValueSource "Trigger Response Id" -ValueTemplate "forms-<Response Id>" -ExampleValue "forms-123" -Notes "Build this from the Forms trigger Response Id so retries stay idempotent.")
        (New-FieldRow -PayloadPath "document_command" -SourceType "Fixed" -Section "Submission" -ValueSource "Fixed string" -ValueTemplate "true_intent_brief" -ExampleValue "true_intent_brief")
        (New-FieldRow -PayloadPath "contract" -SourceType "Fixed" -Section "Submission" -ValueSource "Fixed string" -ValueTemplate "CWA" -ExampleValue "CWA")
        (New-FieldRow -PayloadPath "grievant_firstname" -SourceType "Form" -Section "Grievant" -QuestionTitle "Grievant first name" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Grievant first name" -ValueTemplate "<Map from Forms: Grievant first name>" -ExampleValue "Taylor")
        (New-FieldRow -PayloadPath "grievant_lastname" -SourceType "Form" -Section "Grievant" -QuestionTitle "Grievant last name" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Grievant last name" -ValueTemplate "<Map from Forms: Grievant last name>" -ExampleValue "Jones")
        (New-FieldRow -PayloadPath "grievant_email" -SourceType "Form" -Section "Grievant" -QuestionTitle "Grievant email" -QuestionType "Email" -RequiredByDefault "Yes" -ValueSource "Get response details -> Grievant email" -ValueTemplate "<Map from Forms: Grievant email>" -ExampleValue "taylor.jones@example.com")
        (New-FieldRow -PayloadPath "narrative" -SourceType "Fixed" -Section "Submission" -ValueSource "Fixed string" -ValueTemplate "True intent grievance brief" -ExampleValue "True intent grievance brief" -Notes "Keep this fixed unless you intentionally want a separate summary question in the Form.")
        (New-FieldRow -PayloadPath "template_data.grievant_name" -SourceType "Compose" -Section "Grievant" -ValueSource "Compose from grievant_firstname + grievant_lastname" -ValueTemplate "<Compose from first and last name>" -ExampleValue "Taylor Jones" -Notes "Do not add a duplicate full-name question unless you want the operator to type it twice.")
        (New-FieldRow -PayloadPath "template_data.date_grievance_occurred" -SourceType "Form" -Section "Grievance" -QuestionTitle "Date grievance occurred" -QuestionType "Date" -RequiredByDefault "Yes" -ValueSource "Get response details -> Date grievance occurred" -ValueTemplate "<Map from Forms: Date grievance occurred>" -ExampleValue "2026-04-02")
        (New-FieldRow -PayloadPath "template_data.grievant_phone" -SourceType "Form" -Section "Grievant" -QuestionTitle "Grievant phone" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Grievant phone" -ValueTemplate "<Map from Forms: Grievant phone>" -ExampleValue "904-555-0100")
        (New-FieldRow -PayloadPath "template_data.grievant_street" -SourceType "Form" -Section "Grievant" -QuestionTitle "Grievant street" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Grievant street" -ValueTemplate "<Map from Forms: Grievant street>" -ExampleValue "123 Main St")
        (New-FieldRow -PayloadPath "template_data.grievant_city" -SourceType "Form" -Section "Grievant" -QuestionTitle "Grievant city" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Grievant city" -ValueTemplate "<Map from Forms: Grievant city>" -ExampleValue "Jacksonville")
        (New-FieldRow -PayloadPath "template_data.grievant_state" -SourceType "Form" -Section "Grievant" -QuestionTitle "Grievant state" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Grievant state" -ValueTemplate "<Map from Forms: Grievant state>" -ExampleValue "FL")
        (New-FieldRow -PayloadPath "template_data.grievant_zip" -SourceType "Form" -Section "Grievant" -QuestionTitle "Grievant zip" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Grievant zip" -ValueTemplate "<Map from Forms: Grievant zip>" -ExampleValue "32202")
        (New-FieldRow -PayloadPath "template_data.title" -SourceType "Form" -Section "Grievant" -QuestionTitle "Grievant title" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Grievant title" -ValueTemplate "<Map from Forms: Grievant title>" -ExampleValue "Customer Service Representative")
        (New-FieldRow -PayloadPath "template_data.department" -SourceType "Form" -Section "Grievant" -QuestionTitle "Department" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Department" -ValueTemplate "<Map from Forms: Department>" -ExampleValue "Customer Care")
        (New-FieldRow -PayloadPath "template_data.seniority_date" -SourceType "Form" -Section "Grievant" -QuestionTitle "Seniority date" -QuestionType "Date" -RequiredByDefault "No" -ValueSource "Get response details -> Seniority date" -ValueTemplate "<Map from Forms: Seniority date>" -ExampleValue "2020-06-15")
        (New-FieldRow -PayloadPath "template_data.local_number" -SourceType "Form" -Section "Local" -QuestionTitle "Local number" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Local number" -ValueTemplate "<Map from Forms: Local number>" -ExampleValue "3106")
        (New-FieldRow -PayloadPath "template_data.local_phone" -SourceType "Form" -Section "Local" -QuestionTitle "Local phone" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Local phone" -ValueTemplate "<Map from Forms: Local phone>" -ExampleValue "904-555-0110")
        (New-FieldRow -PayloadPath "template_data.local_street" -SourceType "Form" -Section "Local" -QuestionTitle "Local street" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Local street" -ValueTemplate "<Map from Forms: Local street>" -ExampleValue "456 Union Hall Ave")
        (New-FieldRow -PayloadPath "template_data.local_city" -SourceType "Form" -Section "Local" -QuestionTitle "Local city" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Local city" -ValueTemplate "<Map from Forms: Local city>" -ExampleValue "Jacksonville")
        (New-FieldRow -PayloadPath "template_data.local_state" -SourceType "Form" -Section "Local" -QuestionTitle "Local state" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Local state" -ValueTemplate "<Map from Forms: Local state>" -ExampleValue "FL")
        (New-FieldRow -PayloadPath "template_data.local_zip" -SourceType "Form" -Section "Local" -QuestionTitle "Local zip" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Local zip" -ValueTemplate "<Map from Forms: Local zip>" -ExampleValue "32202")
        (New-FieldRow -PayloadPath "template_data.grievance_type" -SourceType "Form" -Section "Grievance" -QuestionTitle "Grievance type" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Grievance type" -ValueTemplate "<Map from Forms: Grievance type>" -ExampleValue "Contract interpretation")
        (New-FieldRow -PayloadPath "template_data.issue_involved" -SourceType "Form" -Section "Grievance" -QuestionTitle "Issue involved" -QuestionType "Long text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Issue involved" -ValueTemplate "<Map from Forms: Issue involved>" -ExampleValue "Management denied overtime rotation rights.")
        (New-FieldRow -PayloadPath "template_data.articles" -SourceType "Form" -Section "Grievance" -QuestionTitle "Articles involved" -QuestionType "Long text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Articles involved" -ValueTemplate "<Map from Forms: Articles involved>" -ExampleValue "Article 12, Section 3")
        (New-FieldRow -PayloadPath "template_data.management_structure" -SourceType "Form" -Section "Grievance" -QuestionTitle "Management structure" -QuestionType "Long text" -RequiredByDefault "No" -ValueSource "Get response details -> Management structure" -ValueTemplate "<Map from Forms: Management structure>" -ExampleValue "First-line manager reports to area director.")
        (New-FieldRow -PayloadPath "template_data.step1_informal_date" -SourceType "Form" -Section "Grievance" -QuestionTitle "Step 1 informal date" -QuestionType "Date" -RequiredByDefault "No" -ValueSource "Get response details -> Step 1 informal date" -ValueTemplate "<Map from Forms: Step 1 informal date>" -ExampleValue "2026-03-01")
        (New-FieldRow -PayloadPath "template_data.step2_formal_date" -SourceType "Form" -Section "Grievance" -QuestionTitle "Step 2 formal date" -QuestionType "Date" -RequiredByDefault "No" -ValueSource "Get response details -> Step 2 formal date" -ValueTemplate "<Map from Forms: Step 2 formal date>" -ExampleValue "2026-03-08")
        (New-FieldRow -PayloadPath "template_data.appealed_to_state_date" -SourceType "Form" -Section "Grievance" -QuestionTitle "Appealed to state date" -QuestionType "Date" -RequiredByDefault "No" -ValueSource "Get response details -> Appealed to state date" -ValueTemplate "<Map from Forms: Appealed to state date>" -ExampleValue "2026-03-15")
        (New-FieldRow -PayloadPath "template_data.timeline" -SourceType "Form" -Section "Grievance" -QuestionTitle "Timeline" -QuestionType "Long text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Timeline" -ValueTemplate "<Map from Forms: Timeline>" -ExampleValue "03/01 incident, 03/02 discussion, 03/03 written denial.")
        (New-FieldRow -PayloadPath "template_data.argument" -SourceType "Form" -Section "Positions" -QuestionTitle "Union argument" -QuestionType "Long text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Union argument" -ValueTemplate "<Map from Forms: Union argument>" -ExampleValue "The contract requires equal rotation based on seniority.")
        (New-FieldRow -PayloadPath "template_data.analysis" -SourceType "Form" -Section "Positions" -QuestionTitle "Analysis" -QuestionType "Long text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Analysis" -ValueTemplate "<Map from Forms: Analysis>" -ExampleValue "Company practice conflicts with the negotiated overtime language.")
        (New-FieldRow -PayloadPath "template_data.company_name" -SourceType "Form" -Section "Positions" -QuestionTitle "Company name" -QuestionType "Text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Company name" -ValueTemplate "<Map from Forms: Company name>" -ExampleValue "AT&T")
        (New-FieldRow -PayloadPath "template_data.company_position" -SourceType "Form" -Section "Positions" -QuestionTitle "Company position" -QuestionType "Long text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Company position" -ValueTemplate "<Map from Forms: Company position>" -ExampleValue "Management states overtime was assigned by operational need.")
        (New-FieldRow -PayloadPath "template_data.company_strengths" -SourceType "Form" -Section "Positions" -QuestionTitle "Company strengths" -QuestionType "Long text" -RequiredByDefault "No" -ValueSource "Get response details -> Company strengths" -ValueTemplate "<Map from Forms: Company strengths>" -ExampleValue "Supervisor testimony is consistent.")
        (New-FieldRow -PayloadPath "template_data.company_weaknesses" -SourceType "Form" -Section "Positions" -QuestionTitle "Company weaknesses" -QuestionType "Long text" -RequiredByDefault "No" -ValueSource "Get response details -> Company weaknesses" -ValueTemplate "<Map from Forms: Company weaknesses>" -ExampleValue "No written exception to the rotation rule exists.")
        (New-FieldRow -PayloadPath "template_data.company_proposed_settlement" -SourceType "Form" -Section "Positions" -QuestionTitle "Company proposed settlement" -QuestionType "Long text" -RequiredByDefault "No" -ValueSource "Get response details -> Company proposed settlement" -ValueTemplate "<Map from Forms: Company proposed settlement>" -ExampleValue "No remedy offered.")
        (New-FieldRow -PayloadPath "template_data.union_position" -SourceType "Form" -Section "Positions" -QuestionTitle "Union position" -QuestionType "Long text" -RequiredByDefault "Yes" -ValueSource "Get response details -> Union position" -ValueTemplate "<Map from Forms: Union position>" -ExampleValue "Union seeks restoration of proper rotation and make-whole relief.")
        (New-FieldRow -PayloadPath "template_data.union_strengths" -SourceType "Form" -Section "Positions" -QuestionTitle "Union strengths" -QuestionType "Long text" -RequiredByDefault "No" -ValueSource "Get response details -> Union strengths" -ValueTemplate "<Map from Forms: Union strengths>" -ExampleValue "Clear contract language and past practice support the claim.")
        (New-FieldRow -PayloadPath "template_data.union_weaknesses" -SourceType "Form" -Section "Positions" -QuestionTitle "Union weaknesses" -QuestionType "Long text" -RequiredByDefault "No" -ValueSource "Get response details -> Union weaknesses" -ValueTemplate "<Map from Forms: Union weaknesses>" -ExampleValue "One witness statement was prepared after the fact.")
        (New-FieldRow -PayloadPath "template_data.union_proposed_settlement" -SourceType "Form" -Section "Positions" -QuestionTitle "Union proposed settlement" -QuestionType "Long text" -RequiredByDefault "No" -ValueSource "Get response details -> Union proposed settlement" -ValueTemplate "<Map from Forms: Union proposed settlement>" -ExampleValue "Pay the missed overtime and restore correct assignment order.")
        (New-FieldRow -PayloadPath "template_data.attachment_1" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 1 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 1 label" -ValueTemplate "<Map from Forms: Attachment 1 label>" -ExampleValue "Exhibit A - Attendance log" -Notes "Use a short exhibit label or filename, not a file upload control.")
        (New-FieldRow -PayloadPath "template_data.attachment_2" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 2 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 2 label" -ValueTemplate "<Map from Forms: Attachment 2 label>" -ExampleValue "Exhibit B - Schedule")
        (New-FieldRow -PayloadPath "template_data.attachment_3" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 3 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 3 label" -ValueTemplate "<Map from Forms: Attachment 3 label>" -ExampleValue "")
        (New-FieldRow -PayloadPath "template_data.attachment_4" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 4 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 4 label" -ValueTemplate "<Map from Forms: Attachment 4 label>" -ExampleValue "")
        (New-FieldRow -PayloadPath "template_data.attachment_5" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 5 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 5 label" -ValueTemplate "<Map from Forms: Attachment 5 label>" -ExampleValue "")
        (New-FieldRow -PayloadPath "template_data.attachment_6" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 6 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 6 label" -ValueTemplate "<Map from Forms: Attachment 6 label>" -ExampleValue "")
        (New-FieldRow -PayloadPath "template_data.attachment_7" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 7 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 7 label" -ValueTemplate "<Map from Forms: Attachment 7 label>" -ExampleValue "")
        (New-FieldRow -PayloadPath "template_data.attachment_8" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 8 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 8 label" -ValueTemplate "<Map from Forms: Attachment 8 label>" -ExampleValue "")
        (New-FieldRow -PayloadPath "template_data.attachment_9" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 9 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 9 label" -ValueTemplate "<Map from Forms: Attachment 9 label>" -ExampleValue "")
        (New-FieldRow -PayloadPath "template_data.attachment_10" -SourceType "Form" -Section "Attachments" -QuestionTitle "Attachment 10 label" -QuestionType "Text" -RequiredByDefault "No" -ValueSource "Get response details -> Attachment 10 label" -ValueTemplate "<Map from Forms: Attachment 10 label>" -ExampleValue "")
        (New-FieldRow -PayloadPath "template_data.signer_email" -SourceType "Form" -Section "Routing" -QuestionTitle "Signer email override" -QuestionType "Email" -RequiredByDefault "No" -ValueSource "Get response details -> Signer email override" -ValueTemplate "<Optional Forms answer: Signer email override>" -ExampleValue "steward@example.com" -Notes "Leave blank when the default signer should be grievant_email.")
    )
}

$catalog = Get-JsonFile -Path $CatalogPath
$form = $catalog.forms | Where-Object { $_.key -eq "true_intent_brief" } | Select-Object -First 1
if ($null -eq $form) {
    throw "true_intent_brief was not found in the catalog: $CatalogPath"
}

$localConfig = Get-OptionalLocalConfig -Path $LocalConfigPath
$localForm = $null
if ($localConfig -and $localConfig.forms) {
    $localForm = $localConfig.forms.true_intent_brief
}

$resolvedApiBaseUrl = $ApiBaseUrl
if ([string]::IsNullOrWhiteSpace($resolvedApiBaseUrl) -and $localConfig -and $localConfig.apiBaseUrl) {
    $resolvedApiBaseUrl = [string]$localConfig.apiBaseUrl
}
if ([string]::IsNullOrWhiteSpace($resolvedApiBaseUrl)) {
    $resolvedApiBaseUrl = [string]$catalog.apiBaseUrl
}

$flowDisplayName = "CWA 3106 - True Intent Brief Intake"
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

$csvPath = Join-Path $OutputDir "true_intent_brief.forms-map.csv"
$jsonPath = Join-Path $OutputDir "true_intent_brief.http-body.json"
$runbookPath = Join-Path $OutputDir "true_intent_brief.runbook.md"

Assert-TargetPathsWritable -Paths @($csvPath, $jsonPath, $runbookPath) -AllowOverwrite:$Overwrite

$rows = Get-TrueIntentRows
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
# True Intent Brief Power Automate Pack

This pack is the build sheet for the true_intent_brief Microsoft Form and flow.

## Resolved values

- Flow display name: $flowDisplayName
- Endpoint: $resolvedApiBaseUrl$($form.endpointPath)
- Document command: $($form.documentCommand)
- Contract: CWA
$formIdLine
$publishedUrlLine

## Files in this pack

- true_intent_brief.forms-map.csv
  Use this to build the Microsoft Form and to map each answer in Get response details.
- true_intent_brief.http-body.json
  Paste this into the HTTP action body, then replace each placeholder with dynamic content or a Compose output.
- true_intent_brief.runbook.md
  This file.

## Recommended Form build

1. Create a Microsoft Form named True Intent Grievance Brief.
2. Add the rows marked SourceType = Form from true_intent_brief.forms-map.csv.
3. Use the CSV Section column to group the questions in the Form.
4. Keep the attachment rows as text questions for exhibit labels or filenames. Do not use Forms file-upload questions for those API fields.
5. Do not add request_id, document_command, contract, narrative, or template_data.grievant_name as Form questions. Those are fixed or composed inside the flow.

## Recommended flow build

1. Create an automated cloud flow named $flowDisplayName.
2. Trigger: When a new response is submitted.
3. Action: Get response details.
4. Optional Compose: build template_data.grievant_name from first + last name.
5. Optional Compose: build request_id as forms-<Response Id>.
6. Action: HTTP.
7. Method: POST.
8. URL: $resolvedApiBaseUrl$($form.endpointPath).
9. Headers:
   - Content-Type: application/json
   - intake auth headers if your environment requires them
10. Body: paste true_intent_brief.http-body.json and replace each placeholder with the matching Forms answer or Compose output.
11. Parse the JSON response and capture at least case_id, grievance_id, and documents[0].signing_link when present.

## Fixed values to keep

- document_command: true_intent_brief
- contract: CWA
- narrative: True intent grievance brief

## Important notes

- Leave template_data.signer_email empty unless you need to override the default signer. If you omit it, the app can fall back to grievant_email.
- Keep the same request_id when you intentionally replay a Forms submission, or the API may create duplicates.
- Do not add DocuSeal signature anchors as Form questions.
- After publish, copy the real Form URL into scripts/power-platform/forms.local.json and replace the placeholder URL in repo docs.
"@

Set-Content -LiteralPath $runbookPath -Value $runbook -Encoding UTF8

Write-Host "Wrote True Intent Brief pack:"
Write-Host "  $csvPath"
Write-Host "  $jsonPath"
Write-Host "  $runbookPath"
Write-Host ""
Write-Host "Next step:"
Write-Host "  Open the CSV first, then build the Form and flow from the runbook."
