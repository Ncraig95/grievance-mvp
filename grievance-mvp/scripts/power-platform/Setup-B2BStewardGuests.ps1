[CmdletBinding()]
param(
    [string]$TenantId = "",
    [string[]]$GuestEmails = @(),
    [string]$InviteRedirectUrl = "https://grievance.cwa3106.org/steward",
    [switch]$ResendInvitation,
    [switch]$DoNotSendEmail,
    [switch]$EnableGuestInvitesIfBlocked
)

<#
Invites outside stewards as B2B guest users in the current Microsoft Entra tenant.

What this script does:
- connects to Microsoft Graph in the current workforce tenant
- creates or re-sends B2B guest invitations for the provided emails
- prints the invited or existing guest users
- prints the next app-side steps needed in /officers

What this script does not do:
- it does not create an External ID customer tenant
- it does not change app registration settings
- it does not allowlist guests in the app database
- it does not assign guest users to cases
- it does not restart the API

The grievance app now uses B2B guest sign-in for /steward when
external_steward_auth.reuse_officer_auth_app is true.
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

function Normalize-Email {
    param([Parameter(Mandatory = $true)][string]$Email)

    $text = $Email.Trim().ToLowerInvariant()
    if (-not $text.Contains("@")) {
        throw "Invalid email address: $Email"
    }
    return $text
}

function Resolve-ExistingGuest {
    param([Parameter(Mandatory = $true)][string]$Email)

    $normalized = Normalize-Email -Email $Email
    $escapedEmail = [System.Uri]::EscapeDataString($normalized.Replace("'", "''"))
    $select = [System.Uri]::EscapeDataString("id,displayName,mail,userPrincipalName,userType,otherMails,externalUserState")

    $directResponse = Invoke-GraphJson -Method GET -Uri "https://graph.microsoft.com/v1.0/users?`$filter=mail eq '$escapedEmail'&`$select=$select"
    $directRows = @()
    if ($directResponse -and $null -ne $directResponse.value) {
        $directRows = @($directResponse.value)
    }
    $direct = $directRows | Where-Object { [string]$_.userType -eq "Guest" } | Select-Object -First 1
    if ($direct) {
        return [pscustomobject]@{
            Id                = [string]$direct.id
            DisplayName       = [string]$direct.displayName
            Mail              = [string]$direct.mail
            UserPrincipalName = [string]$direct.userPrincipalName
            ExternalUserState = [string]$direct.externalUserState
        }
    }

    $fallbackResponse = Invoke-GraphJson -Method GET -Uri "https://graph.microsoft.com/v1.0/users?`$select=$select&`$top=999"
    $fallbackRows = @()
    if ($fallbackResponse -and $null -ne $fallbackResponse.value) {
        $fallbackRows = @($fallbackResponse.value)
    }
    $fallback = $fallbackRows |
        Where-Object {
            [string]$_.userType -eq "Guest" -and (
                [string]$_.mail -eq $normalized -or
                (@($_.otherMails) -contains $normalized)
            )
        } |
        Select-Object -First 1
    if (-not $fallback) {
        return $null
    }

    return [pscustomobject]@{
        Id                = [string]$fallback.id
        DisplayName       = [string]$fallback.displayName
        Mail              = [string]$fallback.mail
        UserPrincipalName = [string]$fallback.userPrincipalName
        ExternalUserState = [string]$fallback.externalUserState
    }
}

function Get-GuestInvitePolicyValue {
    $policy = Invoke-GraphJson -Method GET -Uri "https://graph.microsoft.com/v1.0/policies/authorizationPolicy"
    return [string]$policy.allowInvitesFrom
}

function Invite-GuestUser {
    param([Parameter(Mandatory = $true)][string]$Email)

    $normalized = Normalize-Email -Email $Email
    $existing = Resolve-ExistingGuest -Email $normalized
    if ($existing -and -not $ResendInvitation) {
        Write-Host "Guest already exists: $normalized"
        return [pscustomobject]@{
            Email             = $normalized
            Status            = "existing"
            UserId            = [string]$existing.Id
            DisplayName       = [string]$existing.DisplayName
            UserPrincipalName = [string]$existing.UserPrincipalName
            ExternalState     = [string]$existing.ExternalUserState
        }
    }

    try {
        $invitation = Invoke-GraphJson `
            -Method POST `
            -Uri "https://graph.microsoft.com/v1.0/invitations" `
            -Body @{
                invitedUserEmailAddress = $normalized
                inviteRedirectUrl = $InviteRedirectUrl
                sendInvitationMessage = (-not $DoNotSendEmail)
            }
    }
    catch {
        $message = [string]$_.Exception.Message
        if ($message -like "*Guest invitations not allowed for your company*") {
            if ($EnableGuestInvitesIfBlocked) {
                Enable-GuestInvites
                try {
                    $invitation = Invoke-GraphJson `
                        -Method POST `
                        -Uri "https://graph.microsoft.com/v1.0/invitations" `
                        -Body @{
                            invitedUserEmailAddress = $normalized
                            inviteRedirectUrl = $InviteRedirectUrl
                            sendInvitationMessage = (-not $DoNotSendEmail)
                        }
                }
                catch {
                    $retryMessage = [string]$_.Exception.Message
                    throw @"
Guest invitations are still blocked after attempting to update the tenant policy.

Verify all of the following:
1. The External collaboration settings change was saved
2. Your signed-in user is a Member, not a Guest
3. Your signed-in user has a role such as Global Administrator, User Administrator, or Guest Inviter
4. The invited email domain is not blocked under Collaboration restrictions

Current allowInvitesFrom policy value:
$(Get-GuestInvitePolicyValue)

Original retry error:
$retryMessage
"@
                }
            }
            else {
            throw @"
Guest invitations are currently blocked in this tenant.

Fix this in Microsoft Entra admin center:
1. Go to Entra ID > External Identities > External collaboration settings
2. Under Guest invite settings, choose one of:
   - Member users and users assigned to specific admin roles can invite guest users
   - Only users assigned to specific admin roles can invite guest users
3. If you choose the admin-only option, make sure your account has one of:
   - Global Administrator
   - User Administrator
   - Guest Inviter
4. Check Collaboration restrictions and make sure the guest domain is not blocked

After that, rerun this script.

Original error:
$message
"@
            }
        }
        else {
            throw
        }
    }

    $invitedUser = $null
    if ($invitation -and $null -ne $invitation.invitedUser) {
        $invitedUser = $invitation.invitedUser
    }
    if (-not $invitedUser) {
        $invitedUser = Resolve-ExistingGuest -Email $normalized
    }
    if (-not $invitedUser) {
        return [pscustomobject]@{
            Email             = $normalized
            Status            = $(if ($existing) { "resent" } else { "invited" })
            UserId            = ""
            DisplayName       = ""
            UserPrincipalName = ""
            ExternalState     = ""
        }
    }
    Write-Host "Invitation created: $normalized"
    return [pscustomobject]@{
        Email             = $normalized
        Status            = $(if ($existing) { "resent" } else { "invited" })
        UserId            = [string]$invitedUser.Id
        DisplayName       = [string]$invitedUser.DisplayName
        UserPrincipalName = [string]$invitedUser.UserPrincipalName
        ExternalState     = [string]$invitedUser.ExternalUserState
    }
}

function Enable-GuestInvites {
    $currentValue = Get-GuestInvitePolicyValue
    if ($currentValue -eq "adminsAndGuestInviters" -or $currentValue -eq "adminsGuestInvitersAndAllMembers" -or $currentValue -eq "everyone") {
        Write-Host "Guest invite policy already allows invitations: $currentValue"
        return
    }

    try {
        Invoke-GraphJson `
            -Method PATCH `
            -Uri "https://graph.microsoft.com/v1.0/policies/authorizationPolicy" `
            -Body @{ allowInvitesFrom = "adminsAndGuestInviters" } | Out-Null
        Write-Host "Updated tenant guest invite policy to adminsAndGuestInviters."
    }
    catch {
        $message = [string]$_.Exception.Message
        throw @"
Failed to update the tenant guest invite policy.

Your signed-in user likely needs a higher Entra role, and the Graph consent must include Policy.ReadWrite.Authorization.
Recommended roles:
- Global Administrator
- Privileged Role Administrator
- User Administrator

Original error:
$message
"@
    }
}

Ensure-GraphModule -Name "Microsoft.Graph.Authentication"

$scopes = @("User.Invite.All", "User.Read.All", "Organization.Read.All")
if ($EnableGuestInvitesIfBlocked) {
    $scopes += "Policy.ReadWrite.Authorization"
}
if ([string]::IsNullOrWhiteSpace($TenantId)) {
    Connect-MgGraph -Scopes $scopes | Out-Null
}
else {
    Connect-MgGraph -TenantId $TenantId -Scopes $scopes | Out-Null
}

$orgResponse = Invoke-GraphJson -Method GET -Uri "https://graph.microsoft.com/v1.0/organization?`$select=id,displayName"
$org = @($orgResponse.value | Select-Object -First 1)[0]
Write-Host "Connected to tenant: $($org.id)"
Write-Host "Organization: $($org.displayName)"
Write-Host "Current allowInvitesFrom policy: $(Get-GuestInvitePolicyValue)"

if (-not $GuestEmails -or $GuestEmails.Count -eq 0) {
    throw @"
No guest emails were supplied.

Example:
.\Setup-B2BStewardGuests.ps1 `
  -TenantId "$($org.id)" `
  -GuestEmails "outside1@example.com","outside2@example.com"
"@
}

$results = New-Object System.Collections.Generic.List[object]
foreach ($email in $GuestEmails) {
    if ([string]::IsNullOrWhiteSpace($email)) {
        continue
    }
    $result = Invite-GuestUser -Email $email
    [void]$results.Add($result)
}

Write-Host ""
Write-Host "Guest invitation results:"
$results | Format-Table Email, Status, UserId, DisplayName, UserPrincipalName, ExternalState -AutoSize

Write-Host ""
Write-Host "Next steps:"
Write-Host "1. Sign in as an admin at https://grievance.cwa3106.org/officers"
Write-Host "2. Add each guest email in the External Stewards panel"
Write-Host "3. Assign cases to those external stewards"
Write-Host "4. Send them to https://grievance.cwa3106.org/steward"
Write-Host ""
Write-Host "If the app is already restarted with external_steward_auth enabled, no separate External ID tenant setup is needed."
