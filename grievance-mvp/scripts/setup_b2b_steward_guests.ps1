$repoScript = Join-Path $PSScriptRoot "power-platform\Setup-B2BStewardGuests.ps1"
$resolvedScript = [System.IO.Path]::GetFullPath($repoScript)

if (-not (Test-Path $resolvedScript)) {
    throw "Missing target script: $resolvedScript"
}

& $resolvedScript @args
