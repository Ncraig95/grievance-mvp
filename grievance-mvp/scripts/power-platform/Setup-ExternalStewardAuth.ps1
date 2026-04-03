[CmdletBinding()]
param(
    [string]$TenantId = "",
    [string]$AppDisplayName = "Grievance External Steward Sign-In",
    [string]$RedirectUri = "https://grievance.cwa3106.org/auth/steward/callback",
    [string]$PostLogoutRedirectUri = "https://grievance.cwa3106.org/",
    [int]$SecretMonthsValid = 12
)

<#
Creates or reuses the free Microsoft sign-in app used by the external steward portal.

What this script does:
- connects to Microsoft Graph in the current tenant
- creates or reuses a multitenant + personal Microsoft account app registration
- ensures the steward callback redirect URI is present
- creates a new client secret
- prints the exact external_steward_auth block to paste into config.yaml

What this script does not do:
- it does not invite B2B guests
- it does not allowlist users in the app database
- it does not assign cases
- it does not restart the API container

This path is free and does not require an External ID customer tenant.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Ensure-GraphModule {
    param([Parameter(Mandatory = $true)][string]$Name)

    if (-not (Get-Module -ListAvailable -Name $Name)) {
        Write-Host "Installing missing PowerShell module: $Name"
        Install-Module $Name -Scope CurrentUser -Force -AllowClobber
    }
    Import-Module $Name -ErrorAction Stop
}

function ConvertTo-JsonCompact {
    param([Parameter(Mandatory = $true)]$Value)
    return ($Value | ConvertTo-Json -Depth 20 -Compress)
}

function Invoke-GraphJson {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Uri,
        $Body = $null
    )

    if ($null -eq $Body) {
        return Invoke-MgGraphRequest -Method $Method -Uri $Uri
    }

    return Invoke-MgGraphRequest `
        -Method $Method `
        -Uri $Uri `
        -Body (ConvertTo-JsonCompact -Value $Body) `
        -ContentType "application/json"
}

function Get-OrganizationInfo {
    $response = Invoke-GraphJson -Method GET -Uri "https://graph.microsoft.com/v1.0/organization?`$select=id,displayName,verifiedDomains"
    $org = @($response.value | Select-Object -First 1)[0]
    if (-not $org) {
        throw "Unable to read organization details from Microsoft Graph."
    }

    $initialDomain = @($org.verifiedDomains | Where-Object { $_.isInitial -eq $true } | Select-Object -First 1)[0].name
    return [pscustomobject]@{
        Id            = [string]$org.id
        DisplayName   = [string]$org.displayName
        InitialDomain = [string]$initialDomain
    }
}

function Resolve-ExistingApplication {
    param([Parameter(Mandatory = $true)][string]$DisplayName)

    $escaped = $DisplayName.Replace("'", "''")
    $response = Invoke-GraphJson `
        -Method GET `
        -Uri "https://graph.microsoft.com/v1.0/applications?`$filter=displayName eq '$escaped'&`$select=id,appId,displayName,signInAudience,web"
    $rows = @()
    if ($response -and $null -ne $response.value) {
        $rows = @($response.value)
    }
    return ($rows | Select-Object -First 1)
}

function Ensure-Application {
    param(
        [Parameter(Mandatory = $true)][string]$DisplayName,
        [Parameter(Mandatory = $true)][string]$RedirectUriValue,
        [Parameter(Mandatory = $true)][string]$PostLogoutValue
    )

    $existing = Resolve-ExistingApplication -DisplayName $DisplayName
    if (-not $existing) {
        $created = Invoke-GraphJson `
            -Method POST `
            -Uri "https://graph.microsoft.com/v1.0/applications" `
            -Body @{
                displayName = $DisplayName
                signInAudience = "AzureADandPersonalMicrosoftAccount"
                web = @{
                    redirectUris = @($RedirectUriValue)
                    logoutUrl = $PostLogoutValue
                }
            }
        Write-Host "Created app registration: $DisplayName"
        return $created
    }

    $redirectUris = @()
    if ($existing.web -and $null -ne $existing.web.redirectUris) {
        $redirectUris = @($existing.web.redirectUris)
    }
    if ($redirectUris -notcontains $RedirectUriValue -or [string]$existing.signInAudience -ne "AzureADandPersonalMicrosoftAccount" -or [string]$existing.web.logoutUrl -ne $PostLogoutValue) {
        $updatedRedirectUris = @($redirectUris + @($RedirectUriValue) | Select-Object -Unique)
        Invoke-GraphJson `
            -Method PATCH `
            -Uri "https://graph.microsoft.com/v1.0/applications/$($existing.id)" `
            -Body @{
                signInAudience = "AzureADandPersonalMicrosoftAccount"
                web = @{
                    redirectUris = $updatedRedirectUris
                    logoutUrl = $PostLogoutValue
                }
            } | Out-Null
        Write-Host "Updated app registration: $DisplayName"
        $existing = Resolve-ExistingApplication -DisplayName $DisplayName
    }
    else {
        Write-Host "App registration already exists: $DisplayName"
    }

    return $existing
}

function New-ApplicationSecret {
    param(
        [Parameter(Mandatory = $true)][string]$ApplicationObjectId,
        [Parameter(Mandatory = $true)][int]$MonthsValid
    )

    $endDate = (Get-Date).ToUniversalTime().AddMonths([Math]::Max(1, $MonthsValid)).ToString("o")
    $result = Invoke-GraphJson `
        -Method POST `
        -Uri "https://graph.microsoft.com/v1.0/applications/$ApplicationObjectId/addPassword" `
        -Body @{
            passwordCredential = @{
                displayName = "grievance-external-steward-login"
                endDateTime = $endDate
            }
        }
    if (-not $result.secretText) {
        throw "Microsoft Graph did not return a client secret value."
    }
    return $result
}

Ensure-GraphModule -Name "Microsoft.Graph.Authentication"

$scopes = @("Application.ReadWrite.All", "Directory.Read.All")
if ([string]::IsNullOrWhiteSpace($TenantId)) {
    Connect-MgGraph -Scopes $scopes | Out-Null
}
else {
    Connect-MgGraph -TenantId $TenantId -Scopes $scopes | Out-Null
}

$org = Get-OrganizationInfo
Write-Host "Connected to tenant: $($org.Id)"
Write-Host "Organization: $($org.DisplayName) ($($org.InitialDomain))"

$application = Ensure-Application `
    -DisplayName $AppDisplayName `
    -RedirectUriValue $RedirectUri `
    -PostLogoutValue $PostLogoutRedirectUri

$secret = New-ApplicationSecret -ApplicationObjectId ([string]$application.id) -MonthsValid $SecretMonthsValid

Write-Host ""
Write-Host "Paste this block into grievance-mvp/config/config.yaml:"
Write-Host "external_steward_auth:"
Write-Host "  enabled: true"
Write-Host "  tenant_id: common"
Write-Host "  reuse_officer_auth_app: false"
Write-Host "  authority: \"\""
Write-Host "  discovery_url: \"\""
Write-Host "  client_id: $([string]$application.appId)"
Write-Host "  client_secret: $([string]$secret.secretText)"
Write-Host "  redirect_uri: $RedirectUri"
Write-Host "  post_logout_redirect_uri: $PostLogoutRedirectUri"
Write-Host ""
Write-Host "Supported account type is set to:"
Write-Host "  Accounts in any organizational directory and personal Microsoft accounts"
Write-Host ""
Write-Host "Next steps:"
Write-Host "1. Paste the config block into config.yaml"
Write-Host "2. Rebuild/restart the API"
Write-Host "3. Allowlist steward emails in /officers"
Write-Host "4. Assign cases"
Write-Host "5. Send stewards to https://grievance.cwa3106.org/steward"
