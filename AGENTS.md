# Codex Instructions

## Project

This repository contains the Grievance Automation MVP. The application code lives in
`grievance-mvp/`.

Treat this as a production operations system. It handles grievance intake,
document generation, DocuSeal signing, Microsoft Graph email, SharePoint filing,
and audit records.

## Safety Rules

- Do not commit secrets, `.env` files, certificates, private keys, tokens, or
  production config values.
- Do not disable SSH, Docker, webhook, DocuSeal, Graph, SharePoint, or TLS
  security controls.
- Do not deploy, restart production services, run migrations, or change live
  service state unless the user explicitly approves that action.
- Do not push to `main` directly. Work on a feature branch and show the diff
  before asking to merge.
- Preserve existing user changes. Do not revert unrelated work.

## Working Directory

Most commands should be run from:

```bash
cd grievance-mvp
```

The root-level branch currently used for Codex work is:

```text
codex/grievance-work-20260506
```

## Useful Commands

Inspect status:

```bash
git status --short --branch
git diff --stat
```

Run the app stack from `grievance-mvp/`:

```bash
make up
make ps
curl -sS http://127.0.0.1:8080/healthz
```

Run smoke checks from `grievance-mvp/` only when the user approves interaction
with local services:

```bash
make smoke
make smoke-signed
```

Run Python tests from the API app when changing backend code:

```bash
cd grievance-mvp/apps/api
python -m pytest
```

Use narrower tests when possible, for example:

```bash
cd grievance-mvp/apps/api
python -m pytest grievance_api/tests/test_referrals.py
```

## Review Expectations

Before finishing a code task:

- Show `git diff --stat`.
- Summarize the files changed.
- Report the exact tests or checks run.
- Note any checks that were skipped and why.
