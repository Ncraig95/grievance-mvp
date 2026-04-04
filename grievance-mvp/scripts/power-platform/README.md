# Power Platform Form Toolkit

This folder is the operator toolkit for the grievance Microsoft Forms and Power Automate setup.

What it does:

- installs the supported Power Platform PowerShell modules
- checks for the `pac` CLI used for solution imports
- provides an Entra group setup entry point for officer/admin/chief steward sign-in
- generates starter request payloads for any form in the catalog
- runs direct API smoke tests against the grievance app
- imports a Power Platform solution zip when you have one
- exports a personalized markdown guide from the shared form catalog

What it does not do:

- it does not create Microsoft Forms for you, because Microsoft does not expose supported form-creation automation that fits this repo workflow
- it does not create a Power Automate cloud flow definition from scratch unless you already have a solution zip to import

## Files

- `forms.catalog.json`
  Shared metadata for every supported form in this repo.
- `forms.local.example.json`
  Copy this to `forms.local.json` and fill in your real Form IDs, published URLs, flow names, and environment values.
- `Install-PowerPlatformPrereqs.ps1`
  Installs the Power Platform PowerShell modules and checks for `pac`.
- `New-GrievancePayloadTemplate.ps1`
  Generates a starter JSON payload for a selected form key.
- `New-TrueIntentBriefPowerAutomatePack.ps1`
  Generates the True Intent Brief Forms question map, HTTP body template, and flow runbook.
- `New-NonDisciplineBriefPowerAutomatePack.ps1`
  Generates the Non-Discipline Brief Forms question map, HTTP body template, and flow runbook.
- `Invoke-GrievanceApiSmokeTest.ps1`
  Posts a payload JSON file to the repo API endpoint for smoke testing.
- `Import-GrievanceFlowSolution.ps1`
  Imports a Dataverse solution zip with `pac solution import`.
- `Export-GrievanceFormOperatorGuide.ps1`
  Builds a local markdown guide from the catalog and optional local config.
- `Setup-EntraOfficerGroups.ps1`
  Creates the Entra officer/admin/chief steward groups and seeds the current members from this folder.
- `Setup-B2BStewardGuests.ps1`
  Invites outside stewards as B2B guest users in the current Entra tenant and prints the next app-side steps.
- `Setup-ExternalStewardAuth.ps1`
  Creates or reuses the free multitenant Microsoft sign-in app used by the external steward portal and prints the `external_steward_auth` config block.
- `FORM_SETUP_GUIDE.md`
  Human-readable checklist for each supported form.

## Recommended order

1. Fill out `forms.local.json` from `forms.local.example.json`.
2. Run `Install-PowerPlatformPrereqs.ps1`.
3. Generate a starter payload:
   `.\New-GrievancePayloadTemplate.ps1 -FormKey true_intent_brief`
4. Build the Form manually using the matching doc in `grievance-mvp/docs/power-automate/`.
5. Build or import the Power Automate flow.
6. Run `Setup-EntraOfficerGroups.ps1` to create the officer/admin Entra groups and seed members.
7. Run `Invoke-GrievanceApiSmokeTest.ps1` with a real or sample payload.
8. Replace placeholder Form URLs in repo docs after the published links are known.

## Examples

Install the Power Platform modules and check `pac`:

```powershell
.\Install-PowerPlatformPrereqs.ps1
```

Generate a payload skeleton for the mobility record:

```powershell
.\New-GrievancePayloadTemplate.ps1 `
  -FormKey mobility_record_of_grievance `
  -OutputPath .\output\mobility_record_of_grievance.payload.json `
  -Overwrite
```

Generate the True Intent Brief Forms + flow pack:

```powershell
.\New-TrueIntentBriefPowerAutomatePack.ps1 -Overwrite
```

Generate the Non-Discipline Brief Forms + flow pack:

```powershell
.\New-NonDisciplineBriefPowerAutomatePack.ps1 -Overwrite
```

Dry-run an API smoke test without submitting:

```powershell
.\Invoke-GrievanceApiSmokeTest.ps1 `
  -FormKey disciplinary_brief `
  -PayloadPath .\output\disciplinary_brief.payload.json `
  -NoSubmit
```

Import a managed solution zip into a Power Platform environment:

```powershell
.\Import-GrievanceFlowSolution.ps1 `
  -SolutionZipPath C:\Deploy\GrievanceFlows_managed.zip `
  -EnvironmentUrl https://org123.crm.dynamics.com `
  -DeploymentSettingsPath C:\Deploy\deployment-settings.json `
  -PublishChanges `
  -ActivatePlugins
```

Export a personalized operator guide after filling in `forms.local.json`:

```powershell
.\Export-GrievanceFormOperatorGuide.ps1 `
  -LocalConfigPath .\forms.local.json `
  -OutputPath .\output\FORM_SETUP_GUIDE.generated.md `
  -Overwrite
```

Create the Entra groups used by `/officers` sign-in:

```powershell
.\Setup-EntraOfficerGroups.ps1
```

Create the free Microsoft sign-in app for external stewards:

```powershell
.\Setup-ExternalStewardAuth.ps1 `
  -TenantId 46216f1a-070c-4b4f-8aa7-7178fce8c32c
```
