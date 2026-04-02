[CmdletBinding()]
param(
    [string]$CatalogPath = (Join-Path $PSScriptRoot "forms.catalog.json"),

    [string]$LocalConfigPath = (Join-Path $PSScriptRoot "forms.local.json"),

    [string]$OutputPath = (Join-Path $PSScriptRoot "output\FORM_SETUP_GUIDE.generated.md"),

    [switch]$Overwrite
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $CatalogPath)) {
    throw "Catalog file not found: $CatalogPath"
}

$catalog = Get-Content -LiteralPath $CatalogPath -Raw | ConvertFrom-Json
$local = $null
if (Test-Path -LiteralPath $LocalConfigPath) {
    $local = Get-Content -LiteralPath $LocalConfigPath -Raw | ConvertFrom-Json
}

if ((Test-Path -LiteralPath $OutputPath) -and -not $Overwrite) {
    throw "Output file already exists. Re-run with -Overwrite: $OutputPath"
}

$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("# Grievance Form Operator Guide")
$lines.Add("")
$lines.Add("Generated from ``forms.catalog.json``.")
$lines.Add("")

foreach ($form in $catalog.forms) {
    $localForm = $null
    if ($local -and $local.forms -and $local.forms.PSObject.Properties.Name -contains $form.key) {
        $localForm = $local.forms.($form.key)
    }

    $lines.Add("## $($form.title)")
    $lines.Add("")
    $lines.Add("- Form key: ``$($form.key)``")
    if ($form.documentCommand) {
        $lines.Add("- Document command: ``$($form.documentCommand)``")
    }
    if ($form.formKey) {
        $lines.Add("- Standalone form key: ``$($form.formKey)``")
    }
    $lines.Add("- Endpoint: ``POST $($catalog.apiBaseUrl)$($form.endpointPath)``")
    $lines.Add("- Detailed field guide: ``$($form.detailedGuide)``")
    if ($localForm -and $localForm.publishedFormUrl) {
        $lines.Add("- Published Form URL: ``$($localForm.publishedFormUrl)``")
    }
    if ($localForm -and $localForm.microsoftFormId) {
        $lines.Add("- Microsoft Form ID: ``$($localForm.microsoftFormId)``")
    }
    if ($localForm -and $localForm.flowDisplayName) {
        $lines.Add("- Flow display name: ``$($localForm.flowDisplayName)``")
    }
    $lines.Add("")
    $lines.Add("Generate starter payload:")
    $lines.Add("")
    $lines.Add('```powershell')
    $lines.Add(".\New-GrievancePayloadTemplate.ps1 -FormKey $($form.key) -OutputPath .\output\$($form.key).payload.json -Overwrite")
    $lines.Add('```')
    $lines.Add("")

    if ($form.notes -and $form.notes.Count -gt 0) {
        $lines.Add("Important notes:")
        foreach ($note in $form.notes) {
            $lines.Add("- $note")
        }
        $lines.Add("")
    }

    if ($form.topLevelFields) {
        $lines.Add("Top-level fields:")
        foreach ($prop in $form.topLevelFields.PSObject.Properties) {
            $lines.Add("- ``$($prop.Name)``")
        }
        $lines.Add("")
    }

    if ($form.documents) {
        $lines.Add("Documents array:")
        foreach ($doc in $form.documents) {
            $lines.Add("- doc_type: ``$($doc.doc_type)`` template_key: ``$($doc.template_key)``")
            if ($doc.signers) {
                foreach ($signer in $doc.signers) {
                    $lines.Add("  - signer placeholder: ``$signer``")
                }
            }
        }
        $lines.Add("")
    }

    if ($form.templateDataFields) {
        $lines.Add("Template data fields:")
        foreach ($prop in $form.templateDataFields.PSObject.Properties) {
            $lines.Add("- ``$($prop.Name)``")
        }
        $lines.Add("")
    }
}

$outDir = Split-Path -Parent $OutputPath
if (-not [string]::IsNullOrWhiteSpace($outDir) -and -not (Test-Path -LiteralPath $outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

Set-Content -LiteralPath $OutputPath -Value ($lines -join [Environment]::NewLine) -Encoding UTF8
Write-Host "Wrote operator guide:"
Write-Host $OutputPath
