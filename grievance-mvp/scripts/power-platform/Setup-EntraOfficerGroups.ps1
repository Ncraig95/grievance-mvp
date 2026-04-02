[CmdletBinding()]
param()

<#
Creates or reuses the Entra security groups used by the grievance officer login.

What this script does:
- creates `Grievance Admins`
- creates `Grievance Officers`
- creates `Grievance Chief Stewards`
- adds the current named users to those groups
- prints the three group object IDs you need to paste into config.yaml

What this script does not do:
- it does not enable officer auth in config.yaml for you
- it does not create chief steward scope assignments in the app database
- it does not restart the API container

Chief steward contract scopes are assigned later from the `/officers` admin UI after
an admin can sign in.
#>

function Ensure-GraphModule {
    param([Parameter(Mandatory = $true)][string]$Name)

    if (-not (Get-Module -ListAvailable -Name $Name)) {
        Write-Host "Installing missing PowerShell module: $Name"
        Install-Module $Name -Scope CurrentUser -Force -AllowClobber
    }
    Import-Module $Name -ErrorAction Stop
}

Ensure-GraphModule -Name "Microsoft.Graph.Authentication"
Ensure-GraphModule -Name "Microsoft.Graph.Groups"
Ensure-GraphModule -Name "Microsoft.Graph.Users"

Connect-MgGraph -Scopes "Group.ReadWrite.All", "User.Read.All"

function Ensure-Group {
    param(
        [Parameter(Mandatory = $true)][string]$DisplayName,
        [Parameter(Mandatory = $true)][string]$MailNickname
    )

    $escaped = $DisplayName.Replace("'", "''")
    $group = Get-MgGroup -Filter "displayName eq '$escaped'" | Select-Object -First 1
    if (-not $group) {
        $group = New-MgGroup `
            -DisplayName $DisplayName `
            -MailEnabled:$false `
            -MailNickname $MailNickname `
            -SecurityEnabled:$true
        Write-Host "Created group: $DisplayName"
    }
    else {
        Write-Host "Group already exists: $DisplayName"
    }
    return $group
}

function Resolve-User {
    param([Parameter(Mandatory = $true)][string]$Email)

    try {
        $user = Get-MgUser -UserId $Email -ErrorAction Stop
        if ($user) {
            return $user
        }
    }
    catch {
    }

    $escaped = $Email.Replace("'", "''")
    $user = Get-MgUser -Filter "mail eq '$escaped'" | Select-Object -First 1
    if (-not $user) {
        throw "User not found: $Email"
    }
    return $user
}

function Ensure-GroupMember {
    param(
        [Parameter(Mandatory = $true)]$Group,
        [Parameter(Mandatory = $true)][string]$Email
    )

    $user = Resolve-User -Email $Email
    $existing = Get-MgGroupMember -GroupId $Group.Id -All | Where-Object { $_.Id -eq $user.Id } | Select-Object -First 1
    if (-not $existing) {
        New-MgGroupMemberByRef -GroupId $Group.Id -BodyParameter @{
            "@odata.id" = "https://graph.microsoft.com/v1.0/directoryObjects/$($user.Id)"
        }
        Write-Host "Added $Email to $($Group.DisplayName)"
    }
    else {
        Write-Host "$Email already in $($Group.DisplayName)"
    }
}

$adminsGroup = Ensure-Group -DisplayName "Grievance Admins" -MailNickname "grievanceadmins"
$officersGroup = Ensure-Group -DisplayName "Grievance Officers" -MailNickname "grievanceofficers"
$chiefStewardsGroup = Ensure-Group -DisplayName "Grievance Chief Stewards" -MailNickname "grievancechiefstewards"

# Full-access officer manager/admin users.
$adminEmails = @(
    "ncraig@cwa3106.com",
    "kburton@cwa3106.com",
    "President@cwa3106.com",
    "Dwilliamson@cwa3106.com"
)

# Base sign-in users with read-only officer access.
$officerEmails = @(
    "CGaston@cwa3106.com"
)

# Chief stewards sign in through their own Entra group, then get contract scopes from the /officers admin UI.
$chiefStewardEmails = @(
    "JRice@cwa3106.com",
    "SGreen@cwa3106.com",
    "jmckinney@cwa3106.com",
    "SBrathwaite@cwa3106.com",
    "VGoll@cwa3106.com",
    "mshannon@cwa3106.com"
)

foreach ($email in $adminEmails) {
    Ensure-GroupMember -Group $adminsGroup -Email $email
}

foreach ($email in $officerEmails) {
    Ensure-GroupMember -Group $officersGroup -Email $email
}

foreach ($email in $chiefStewardEmails) {
    Ensure-GroupMember -Group $chiefStewardsGroup -Email $email
}

Write-Host ""
Write-Host "Paste these values into grievance-mvp/config/config.yaml:"
Write-Host "officer_group_ids: [$($officersGroup.Id)]"
Write-Host "admin_group_ids:   [$($adminsGroup.Id)]"
Write-Host "chief_steward_group_ids: [$($chiefStewardsGroup.Id)]"
Write-Host ""
Write-Host "After you enable officer_auth and restart the API, sign in as an admin and add these chief steward scope assignments in /officers:"
Write-Host "JRice@cwa3106.com -> wire_tech"
Write-Host "SGreen@cwa3106.com -> wire_tech"
Write-Host "jmckinney@cwa3106.com -> wire_tech"
Write-Host "jmckinney@cwa3106.com -> mobility"
Write-Host "jmckinney@cwa3106.com -> ihx"
Write-Host "SBrathwaite@cwa3106.com -> coj"
Write-Host "VGoll@cwa3106.com -> ihx"
Write-Host "mshannon@cwa3106.com -> utilities"
Write-Host ""
Write-Host "You can rerun this script later after adding more emails to the arrays above."
