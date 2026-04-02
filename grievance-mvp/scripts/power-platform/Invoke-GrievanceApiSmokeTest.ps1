[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$FormKey,

    [Parameter(Mandatory = $true)]
    [string]$PayloadPath,

    [string]$CatalogPath = (Join-Path $PSScriptRoot "forms.catalog.json"),

    [string]$LocalConfigPath = (Join-Path $PSScriptRoot "forms.local.json"),

    [string]$ApiBaseUrl,

    [string]$HeaderJsonPath,

    [string]$OutputPath,

    [switch]$NoSubmit
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Convert-JsonNodeToHashtable {
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
        $table = @{}
        foreach ($key in $Node.Keys) {
            $table[$key] = Convert-JsonNodeToHashtable -Node $Node[$key]
        }
        return $table
    }

    if ($Node -is [System.Collections.IEnumerable] -and -not ($Node -is [string])) {
        $items = @()
        foreach ($item in $Node) {
            $items += ,(Convert-JsonNodeToHashtable -Node $item)
        }
        return $items
    }

    if ($Node.PSObject -and $Node.PSObject.Properties.Count -gt 0) {
        $table = @{}
        foreach ($prop in $Node.PSObject.Properties) {
            $table[$prop.Name] = Convert-JsonNodeToHashtable -Node $prop.Value
        }
        return $table
    }

    return $Node
}

if (-not (Test-Path -LiteralPath $CatalogPath)) {
    throw "Catalog file not found: $CatalogPath"
}

if (-not (Test-Path -LiteralPath $PayloadPath)) {
    throw "Payload file not found: $PayloadPath"
}

$catalog = Get-Content -LiteralPath $CatalogPath -Raw | ConvertFrom-Json
$form = $catalog.forms | Where-Object { $_.key -eq $FormKey } | Select-Object -First 1
if ($null -eq $form) {
    throw "Unknown form key '$FormKey'."
}

if (-not $ApiBaseUrl -and (Test-Path -LiteralPath $LocalConfigPath)) {
    $local = Get-Content -LiteralPath $LocalConfigPath -Raw | ConvertFrom-Json
    if ($local.apiBaseUrl) {
        $ApiBaseUrl = [string]$local.apiBaseUrl
    }
}

if (-not $ApiBaseUrl) {
    $ApiBaseUrl = [string]$catalog.apiBaseUrl
}

$uri = $ApiBaseUrl.TrimEnd("/") + [string]$form.endpointPath
$body = Get-Content -LiteralPath $PayloadPath -Raw
$headers = @{}

if ($HeaderJsonPath) {
    if (-not (Test-Path -LiteralPath $HeaderJsonPath)) {
        throw "Header JSON file not found: $HeaderJsonPath"
    }

    $extraHeaders = Get-Content -LiteralPath $HeaderJsonPath -Raw | ConvertFrom-Json
    $extraHeadersTable = Convert-JsonNodeToHashtable -Node $extraHeaders
    foreach ($key in $extraHeadersTable.Keys) {
        if ($key -ieq "Content-Type") {
            continue
        }
        $headers[$key] = [string]$extraHeadersTable[$key]
    }
}

if ($NoSubmit) {
    Write-Host "Dry run only. No request submitted."
    Write-Host "POST $uri"
    Write-Host ""
    Write-Host $body
    return
}

$response = Invoke-RestMethod -Method Post -Uri $uri -Headers $headers -ContentType "application/json" -Body $body
$responseJson = $response | ConvertTo-Json -Depth 20

if ($OutputPath) {
    $outDir = Split-Path -Parent $OutputPath
    if (-not [string]::IsNullOrWhiteSpace($outDir) -and -not (Test-Path -LiteralPath $outDir)) {
        New-Item -ItemType Directory -Path $outDir -Force | Out-Null
    }
    Set-Content -LiteralPath $OutputPath -Value $responseJson -Encoding UTF8
    Write-Host "Wrote response JSON:"
    Write-Host $OutputPath
}

Write-Output $responseJson
