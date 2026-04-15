# Outreach Module Implementation Notes

Date: 2026-04-14

## Scope
This document summarizes the outreach mail work added to the existing `grievance-mvp` application. The implementation was done additively so the existing grievance intake, grievance notifications, officer pages, and current Microsoft Graph grievance mail path remain intact.

## High-Level Result
The repository now includes an outreach subsystem inside the existing FastAPI app with:

- outreach contact management
- reusable outreach templates
- visit/campaign stop scheduling
- test sending and one-off live sending
- scheduled due-send execution
- per-recipient send logging
- unsubscribe and suppression handling
- MIME-based outreach delivery with unsubscribe headers
- click tracking and estimated open tracking
- outreach analytics dashboard and CSV exports
- a simpler outreach UI with section navigation instead of one oversized admin page
- quick message testing aimed at an officer workflow

## Safety Approach
The work was kept separate from the grievance mail path.

- Existing grievance notification sending still uses the original Graph JSON path.
- Outreach mail uses a separate MIME send path added to the mailer.
- Outreach routes, models, service logic, and DB tables were added without replacing grievance workflows.
- New features were built inside the existing app and deployment model instead of creating a second service.

## Main Files Added
### New Python modules
- `apps/api/grievance_api/services/outreach_service.py`
- `apps/api/grievance_api/web/routes_outreach.py`
- `apps/api/grievance_api/web/outreach_models.py`
- `apps/api/grievance_api/scripts/run_outreach_due.py`
- `apps/api/grievance_api/tests/test_outreach_service.py`

### New helper shell script
- `scripts/run-outreach-due.sh`

## Main Files Updated
- `apps/api/grievance_api/core/config.py`
- `apps/api/grievance_api/db/schema.sql`
- `apps/api/grievance_api/db/migrate.py`
- `apps/api/grievance_api/main.py`
- `apps/api/grievance_api/services/graph_mail.py`
- `apps/api/grievance_api/web/routes_officers.py`
- `apps/api/requirements.txt`
- `config/config.example.yaml`
- `Makefile`
- `scripts/install-systemd-services.sh`

## Data Model Added
The outreach module introduced these database structures.

### Outreach core tables
- `outreach_contacts`
- `outreach_templates`
- `outreach_stops`
- `outreach_suppressions`
- `outreach_send_log`

### Analytics tables
- `outreach_tracked_links`
- `outreach_events`

### Outreach send log evolution
- added `open_token_hash` to `outreach_send_log`

## Config Support Added
A new `outreach` config block is now supported in app config parsing and documented in `config/config.example.yaml`.

Fields include:
- `enabled`
- `sender_user_id`
- `sender_display_name`
- `public_base_url`
- `reply_to_address`
- `reply_to_name`
- `timezone`
- `min_seconds_between_sends`
- `max_parallel_sends`
- `max_sends_per_run`

Note: the live `config/config.yaml` used on the machine is not tracked by git and is therefore not part of the commit history.

## Outreach Delivery Changes
### MIME mail path
The Graph mail service was extended with a separate MIME-based send path for outreach so the module can include headers such as:
- `List-Unsubscribe`
- `List-Unsubscribe-Post`
- `Reply-To`
- outreach metadata headers

### Existing grievance path preserved
The existing grievance sender path was left in place.

## Outreach Service Features Added
### Contacts
The outreach service supports:
- create/update/delete contact records
- CSV/XLSX import
- extra field storage for future merge values

### Templates
The outreach service supports:
- create/update/delete templates
- seeded notice and reminder templates
- preview rendering with merge fields

### Stops
The outreach service supports:
- create/update/delete campaign stops
- seeded 2026 draft visit schedule
- automatic default notice/reminder timing based on visit date and time

### Sending
The outreach service supports:
- preview render
- test send
- one-off live send for one recipient
- quick test message send without creating a saved template first
- due-send runner for scheduled notice/reminder sends
- suppression checks before send
- send log entries for sent, failed, and suppressed outcomes

### Merge fields
Supported placeholders include at least:
- `first_name`
- `last_name`
- `full_name`
- `email`
- `location`
- `campaign_location`
- `work_location`
- `work_group`
- `department`
- `bargaining_unit`
- `local_number`
- `steward_name`
- `rep_name`
- `visit_date`
- `visit_time`
- `subject`
- `sender_name`
- `reply_to`
- `unsubscribe_url`

Extra fields on contacts are also merged in when keys do not collide with built-in placeholders.

## Unsubscribe and Suppression
Public unsubscribe handling was added to the existing app.

Routes:
- `GET /unsubscribe/{token}`
- `POST /unsubscribe/{token}`

Behavior:
- tokenized unsubscribe
- suppression by normalized email
- preservation of send history
- unsubscribe event logging in analytics

## Analytics Added
### Tracking behavior
Tracked links and open pixels were added for outreach mail.

Routes:
- `GET /r/{token}`
- `GET /o/{token}.gif`

Behavior:
- tracked redirect logs click events and redirects to the true destination
- open pixel returns a tiny GIF and logs `estimated_open`
- no-store headers are returned on the pixel route

### Analytics metrics
The dashboard now reports:
- sent
- failed
- suppressed
- unsubscribe
- estimated open
- unique estimated open
- click
- unique click

### Analytics filtering
The dashboard supports filtering by:
- date range
- stop
- location
- template
- recipient
- work group

### Analytics displays
The UI now includes:
- topline metrics
- campaign engagement summary
- top clicked links
- recent event activity
- CSV exports for summary, clicks, send history, and suppressions

### Noise handling
Basic automation/prefetch noise handling was added.

- clearly flagged bot/prefetch activity is marked in event metadata
- clearly automated events are excluded from topline click/open metrics
- estimated opens are labeled as estimates, not human-read guarantees
- clicks are treated as more reliable than opens

## UI Changes
The outreach UI was reworked to be easier to use.

### Previous problem
The outreach screen had effectively become one long management page.

### Current structure
The page now has section navigation with these views:
- `Compose`
- `Overview`
- `Contacts`
- `Templates`
- `Stops`
- `Analytics`
- `History`

### Compose section
Compose now includes three distinct workflows:

#### Quick Test Message
Fastest path for an officer to type a subject/body and send a test message.
- default recipient: `ncraig@cwa3106.com`
- supports preview
- supports merge fields
- does not require CSV/XLSX import
- does not require creating a saved outreach template

#### Preview and Test Send
Existing saved-template path remains available.
- default recipient now prefilled as `ncraig@cwa3106.com`

#### One-Off Live Send
Allows a real one-recipient message without import first.
- can use a saved contact or manual person details
- still uses outreach tracking, unsubscribe, and send logging
- default recipient now prefilled as `ncraig@cwa3106.com`

## Officer Navigation
A link to the outreach module was added to the officer/admin pages in `routes_officers.py`.

## App Startup Integration
The app now initializes and mounts the outreach service and outreach routes in `main.py`.

## Scheduling / Operations
### Due-send execution
A runner script was added:
- `python -m grievance_api.scripts.run_outreach_due`

### Local helper
- `scripts/run-outreach-due.sh`

### Make target
A new make target was added for the due-send workflow.

### Systemd support
The systemd installer script was updated to support outreach due-send execution with a timer/service pattern.

## Dependencies Added
- `openpyxl` for `.xlsx` import support

## Tests Added
`apps/api/grievance_api/tests/test_outreach_service.py` covers:
- seeded data and preview rendering defaults
- due-send idempotency and suppression behavior
- link rewrite and analytics event recording
- prefetch click exclusion from topline metrics
- one-off send using manual contact context without saved-contact creation
- quick-message preview/test-send behavior using inline subject/body content

## Validation Performed
The following test command was run successfully before commit:

```bash
python -m unittest \
  grievance_api.tests.test_outreach_service \
  grievance_api.tests.test_notification_service_standalone \
  grievance_api.tests.test_hosted_forms \
  grievance_api.tests.test_officers
```

A container rebuild/restart and app health check also passed.

## Manual / External Setup Still Required
These items are outside repo code and still need to exist in Microsoft 365 / Cloudflare:

- `members.cwa3106.org` accepted mail domain / subdomain in Microsoft 365
- sender mailbox identity such as `organizing@members.cwa3106.org`
- DNS records for Microsoft 365 on the mail subdomain
- the public outreach/tracking hostname in Cloudflare Tunnel if using a dedicated host like `links.members.cwa3106.org`
- live values in non-tracked `config/config.yaml`

## Current Known Limits
- estimated opens are still estimates only
- automation and prefetch detection is heuristic, not perfect
- the live config file is not versioned in git
- the outreach UI is improved, but more workflow-specific simplification is still possible if desired

## Post-Launch Hardening (2026-04-15)
A follow-up fix was applied after validating the outreach compose screen against the live app.

### Problem observed
- the browser could reload and still post invalid or empty `stop_id` / `template_id` values
- that produced `404` responses from outreach preview/test-send routes even though the actual backend mailer path was functioning
- the quick-send and one-off flows were especially sensitive to stale page state after reloads

### Fixes applied
- added backend fallback resolution for stop/template selection in `outreach_service.py`
  - if the UI posts `0`, empty, or an invalid missing selection, outreach now falls back to the first available stop/template instead of failing immediately
- added `no-store` cache headers to the outreach page response in `routes_outreach.py`
  - this reduces stale browser page reuse and makes fresh compose code load more reliably after deploys
- added stronger client-side required-value checks in the compose UI
  - clearer errors now appear when a required stop/template selection is missing instead of silently posting invalid IDs
- improved select refresh logic in the outreach compose UI
  - current selections are re-applied when possible and valid defaults are chosen when not

### Operational takeaway
The outreach send path itself was verified server-side against the live config and sender mailbox. The remaining issue was browser form state and cache behavior, not Graph delivery.
