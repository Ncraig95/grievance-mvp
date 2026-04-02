[CmdletBinding()]
param(
    [switch]$SkipModuleInstall,
    [switch]$SkipPacCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-CommandAvailable {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    return $null -ne (Get-Command -Name $Name -ErrorAction SilentlyContinue)
}

function Ensure-ModuleInstalled {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    if (Get-Module -ListAvailable -Name $Name) {
        Write-Host "Module already installed: $Name"
        return
    }

    Write-Host "Installing module: $Name"
    Install-Module -Name $Name -Scope CurrentUser -Force -AllowClobber
}

if (-not $SkipModuleInstall) {
    Ensure-ModuleInstalled -Name "Microsoft.PowerApps.Administration.PowerShell"
    Ensure-ModuleInstalled -Name "Microsoft.PowerApps.PowerShell"
} else {
    Write-Host "Skipping PowerShell module install."
}

if (-not $SkipPacCheck) {
    if (Test-CommandAvailable -Name "pac") {
        Write-Host "Found pac CLI."
        & pac --version
    } else {
        Write-Warning "Power Platform CLI (pac) was not found on PATH."
        Write-Warning "Install it from Microsoft Learn before using Import-GrievanceFlowSolution.ps1."
    }
} else {
    Write-Host "Skipping pac CLI check."
}

Write-Host ""
Write-Host "Next steps:"
Write-Host "1. Open PowerShell and run: Add-PowerAppsAccount"
Write-Host "2. Copy forms.local.example.json to forms.local.json and fill in real values."
Write-Host "3. Generate a payload template with New-GrievancePayloadTemplate.ps1."
