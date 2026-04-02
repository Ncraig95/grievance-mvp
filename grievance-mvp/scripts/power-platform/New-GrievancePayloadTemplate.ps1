[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$FormKey,

    [string]$CatalogPath = (Join-Path $PSScriptRoot "forms.catalog.json"),

    [string]$OutputPath = (Join-Path $PSScriptRoot "output\$FormKey.payload.json"),

    [switch]$Overwrite
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Convert-JsonNodeToOrderedHashtable {
    param(
        [Parameter(Mandatory = $false)]
        [object]$Node
    )

    if ($null -eq $Node) {
        return $null
    }

    if ($Node -is [string] -or $Node -is [ValueType]) {
        return $Node
    }

    if ($Node -is [System.Collections.IDictionary]) {
        $table = [ordered]@{}
        foreach ($key in $Node.Keys) {
            $table[$key] = Convert-JsonNodeToOrderedHashtable -Node $Node[$key]
        }
        return $table
    }

    if ($Node -is [System.Collections.IEnumerable] -and -not ($Node -is [string])) {
        $items = @()
        foreach ($item in $Node) {
            $items += ,(Convert-JsonNodeToOrderedHashtable -Node $item)
        }
        return $items
    }

    if ($Node.PSObject -and $Node.PSObject.Properties.Count -gt 0) {
        $table = [ordered]@{}
        foreach ($prop in $Node.PSObject.Properties) {
            $table[$prop.Name] = Convert-JsonNodeToOrderedHashtable -Node $prop.Value
        }
        return $table
    }

    return $Node
}

if (-not (Test-Path -LiteralPath $CatalogPath)) {
    throw "Catalog file not found: $CatalogPath"
}

$catalog = Get-Content -LiteralPath $CatalogPath -Raw | ConvertFrom-Json
$form = $catalog.forms | Where-Object { $_.key -eq $FormKey } | Select-Object -First 1

if ($null -eq $form) {
    $supported = ($catalog.forms | ForEach-Object { $_.key }) -join ", "
    throw "Unknown form key '$FormKey'. Supported keys: $supported"
}

$payload = [ordered]@{
    request_id = $form.requestIdPattern
}

if ($form.documentCommand) {
    $payload["document_command"] = $form.documentCommand
}

if ($form.formKey) {
    $payload["form_key"] = $form.formKey
}

if ($form.topLevelFields) {
    foreach ($prop in $form.topLevelFields.PSObject.Properties) {
        $payload[$prop.Name] = Convert-JsonNodeToOrderedHashtable -Node $prop.Value
    }
}

if ($form.documents) {
    $payload["documents"] = Convert-JsonNodeToOrderedHashtable -Node $form.documents
}

if ($form.templateDataFields) {
    $payload["template_data"] = Convert-JsonNodeToOrderedHashtable -Node $form.templateDataFields
}

$outDir = Split-Path -Parent $OutputPath
if (-not [string]::IsNullOrWhiteSpace($outDir) -and -not (Test-Path -LiteralPath $outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

if ((Test-Path -LiteralPath $OutputPath) -and -not $Overwrite) {
    throw "Output file already exists. Re-run with -Overwrite: $OutputPath"
}

$json = $payload | ConvertTo-Json -Depth 20
Set-Content -LiteralPath $OutputPath -Value $json -Encoding UTF8

Write-Host "Wrote payload template:"
Write-Host $OutputPath
Write-Host ""
Write-Host "Endpoint:"
Write-Host "$($catalog.apiBaseUrl)$($form.endpointPath)"
