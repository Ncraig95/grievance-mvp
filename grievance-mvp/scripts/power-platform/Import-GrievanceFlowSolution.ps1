[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SolutionZipPath,

    [string]$EnvironmentUrl,

    [string]$DeploymentSettingsPath,

    [switch]$PublishChanges,

    [switch]$ActivatePlugins,

    [switch]$ForceOverwrite,

    [switch]$ImportAsHolding,

    [switch]$StageAndUpgrade,

    [switch]$SkipDependencyCheck,

    [switch]$SkipLowerVersion,

    [switch]$Async,

    [int]$MaxAsyncWaitTimeMinutes = 60,

    [switch]$NoImport
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Get-Command -Name "pac" -ErrorAction SilentlyContinue)) {
    throw "The pac CLI was not found on PATH."
}

$resolvedSolutionZip = (Resolve-Path -LiteralPath $SolutionZipPath).Path
$args = @(
    "solution"
    "import"
    "--path"
    $resolvedSolutionZip
)

if ($EnvironmentUrl) {
    $args += @("--environment", $EnvironmentUrl)
}

if ($DeploymentSettingsPath) {
    $resolvedSettings = (Resolve-Path -LiteralPath $DeploymentSettingsPath).Path
    $args += @("--settings-file", $resolvedSettings)
}

if ($PublishChanges) {
    $args += "--publish-changes"
}
if ($ActivatePlugins) {
    $args += "--activate-plugins"
}
if ($ForceOverwrite) {
    $args += "--force-overwrite"
}
if ($ImportAsHolding) {
    $args += "--import-as-holding"
}
if ($StageAndUpgrade) {
    $args += "--stage-and-upgrade"
}
if ($SkipDependencyCheck) {
    $args += "--skip-dependency-check"
}
if ($SkipLowerVersion) {
    $args += "--skip-lower-version"
}
if ($Async) {
    $args += "--async"
}
if ($MaxAsyncWaitTimeMinutes -gt 0) {
    $args += @("--max-async-wait-time", [string]$MaxAsyncWaitTimeMinutes)
}

$displayCommand = "pac " + (($args | ForEach-Object {
            if ($_ -match "\s") { '"' + $_ + '"' } else { $_ }
        }) -join " ")

Write-Host "Prepared command:"
Write-Host $displayCommand

if ($NoImport) {
    Write-Host "NoImport specified. Exiting without running pac."
    return
}

& pac @args

if ($LASTEXITCODE -ne 0) {
    throw "pac solution import failed with exit code $LASTEXITCODE"
}

Write-Host "Solution import completed."
