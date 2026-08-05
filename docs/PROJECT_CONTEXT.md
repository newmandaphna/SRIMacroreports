# Project Context - SRI Practice Dashboard

Last updated: 2026-08-05

Purpose: a durable handover file. Any new assistant session (Claude in the
browser, Claude Code, or a human picking this up cold) should read this file
FIRST, together with docs/SCHEDULED_SYNC.md. No credentials are recorded here.

---

## 1. The two Replit Apps

There are TWO Replit Apps pointing at this ONE GitHub repo
(github.com/newmandaphna/SRIMacroreports). They are not duplicates. They have
different jobs and they MUST stay on different branches.

### App 1 - SRIMacroreports  (the dashboard)

- Replit: replit.com/@newmandaphna/SRIMacroreports
- Live URL: https://sri-macroreports.replit.app
- Deployment type: Autoscale, Published, Public
- Git branch: main
- .replit deployment block:
    deploymentTarget = "autoscale"
    run = "uvicorn run:app --host 0.0.0.0 --port 5000"
- Owns the PRODUCTION database (the real one, with PHI).
- This is the app real users log into. Do not change its deployment type.

### App 2 - SRIMacroreports-1  (the scheduled runner)

- Replit: replit.com/@newmandaphna/SRIMacroreports-1
- No public URL. It is a job, not a website.
- Deployment type: Scheduled. Cron 0 */6 * * * , timezone EST.
- 1 vCPU / 2 GiB, job timeout 10 min, North America.
- Git branch: scheduled-runner   (NOT main - see section 4)
- .replit deployment block:
    deploymentTarget = "scheduled"
    run = ["python3", "-m", "scripts.run_due_syncs"]
- Workspace Secrets it needs: SYNC_DATABASE_URL, GOOGLE_SERVICE_ACCOUNT_JSON,
  SESSION_SECRET, ADMIN_INITIAL_PASSWORD

---

## 2. The original problem and why App 2 exists

Q3 2026 was not auto-syncing. The only syncs happening were manual ones.

Root cause: .replit on App 1 sets deploymentTarget = "autoscale". The in-process
scheduler in app/sync/scheduler.py (auto_sync_loop) does
await asyncio.sleep(3600) BEFORE its first check. Autoscale spins containers
down when idle, so the one-hour countdown restarts from zero on every cold
start and never actually elapses. The loop therefore never fires.

Fix chosen: do not keep a process alive (that would mean a Reserved VM, more
money). Instead drive the sync from OUTSIDE, on a Replit Scheduled Deployment,
which wakes up on a cron, runs one script, and exits. That is App 2.

Approved budget: roughly $1 to $3 per month. Do not exceed without asking.

---

## 3. How the runner works

Entry point: scripts/run_due_syncs.py

It calls run_due_syncs() from app.sync.scheduler. It must NOT call run_sync()
from app.sync.engine directly. Going through the scheduler is what preserves:

- the auto_sync_days interval per source
- readiness and upload-source filtering
- oldest-first ordering
- per-source transactions (one bad source does not poison the others)
- the actor_label="auto-sync" audit label, so the audit trail stays honest

It MUST be invoked as a module:
    python3 -m scripts.run_due_syncs        <- correct
    python3 scripts/run_due_syncs.py        <- ModuleNotFoundError: No module named app

DATABASE_URL override shim: Replit force-injects DATABASE_URL and the PG*
variables into any app that owns a production database, and that injection wins
over a manually set secret. So the script reads SYNC_DATABASE_URL at import
time (before load_settings() runs inside main()) and copies it over
DATABASE_URL. That is how App 2 reaches App 1 production database.

CRON CADENCE RULE: the schedule interval must be SHORTER than auto_sync_days
(currently 1 day). sources_due compares last_synced_at against
now - interval_days. A once-daily job at a fixed clock time will always find
the previous run fractionally too recent and will sync nothing, forever.
Hence 0 */6 * * * (every 6 hours). If auto_sync_days ever changes, re-check
this.

---

## 4. THE BRANCH RULE (read this before touching git)

App 1 and App 2 share one repo but need DIFFERENT .replit files. If both sit on
main, every mutual sync overwrites the other. This already happened once:
App 2 pushed its scheduled .replit to main, App 1 pulled it, and App 1
deployment config was silently switched to a scheduled job. Repaired with
git checkout <good-sha> -- .replit app/main.py and a follow-up commit.

The permanent arrangement:

  App 1  ->  branch main               (autoscale .replit)
  App 2  ->  branch scheduled-runner   (scheduled .replit)

Rules:

1. Never press Sync Changes in App 2 while it is on main.
2. Application code changes are made on main in App 1, then App 2 pulls them
   selectively:  git checkout origin/main -- app/ migrations/ scripts/
   Note the deliberate absence of .replit from that list.
3. Never let .replit or .replit.bak travel between the two apps.
4. .replit.bak should not exist in the repo at all. Delete it if it reappears
   (the Replit Agent likes to create it).

---

## 5. Traps already hit - do not repeat these

Replit publish mechanics:

- Deployment type, cron schedule and deployment secrets live INSIDE the
  "Adjust settings" form, and are only committed by the Publish button that
  belongs to that same form. Any other Publish button (workspace top-right, or
  the one in the Agent chat pane) silently discards the form and republishes
  with the LAST SAVED config. Two publishes were lost this way, and both went
  out as public Autoscale web apps - i.e. a second public copy of a dashboard
  holding PHI. Watch for this.

- Workspace Secrets (Tools > Secrets) PERSIST across publish cycles and are
  inherited into deployment secrets. Secrets typed into the deployment form do
  NOT persist. Always put secrets in workspace Secrets.

- One deployment per Replit App. Changing type means Manage >
  "Change deployment type" > unpublish > republish.

- git push from the Replit shell fails with "remote: Invalid username or
  token." Use the Git pane Push / Sync Changes buttons instead. The first push
  of a session raises a "Pass GitHub Credentials" dialog - the human must click
  Confirm, an assistant must not.

- The Replit shell often ignores typed commands unless the prompt line is
  clicked first. Heredocs and long commands with nested escaped quotes are
  unreliable; build files with repeated printf appends instead.

- The Replit Agent bills separately and has repeatedly rewritten .replit and
  app/main.py without being asked. Prefer the shell. If the Agent has been
  active, always git diff before committing.

- raw.githubusercontent.com caches aggressively. To verify a file on GitHub
  right after a push, use the blob view with ?plain=1 instead.

---

## 6. Safety boundaries agreed with the owner

- This app holds PHI. Treat every public exposure as a real incident.
- Never read, print, paste or echo secret values. Verification code prints
  booleans, lengths and field names only.
- Never click credential-authorization dialogs; hand them to the owner.
- Never delete a database. Surface it and let the owner press Delete.
- Ask before any unpublish, any destructive git operation, and before running
  a sync against production.
- Do not exceed roughly $1 to $3 per month without fresh approval.

---

## 7. Verified state as of 2026-08-02

App 1:
- Live, Autoscale, Published, healthy. /status OK.
- main == origin/main, working tree clean.
- .replit = autoscale + uvicorn run:app --host 0.0.0.0 --port 5000
- 12,210 session rows. Q3 2026 active, last synced 2026-08-02 02:07,
  2,838 rows. Q2 2026 inactive.

App 2:
- Published as Scheduled, EST, 0 */6 * * *.
- Branch scheduled-runner, clean, verified importable (JOB_IMPORT_OK).
- Run history: 23:45 Done (38.2s), 23:40 Failed, 23:35 Cancelled. The two bad
  runs were the wrong run-command and the module-path bug, both now fixed.

Also landed on main: an idempotent rewrite of the sync_runs migration
(20260731_1720_...) that uses ALTER TABLE ... ADD COLUMN IF NOT EXISTS for
error_kind and friends. Production already had those columns but Alembic did
not know it, so every boot crashed trying to add them again. Tests run on
PostgreSQL (see tests/conftest.py), so IF NOT EXISTS is safe.

---

## 8. Open items

1. App 2 still has its own orphaned Production Database attached (~30MB).
   It was inspected table by table: 16 tables, all empty except
   alembic_version 1, audit_log 1, module_grants 4, users 1. No PHI.
   It is unused - the job talks to App 1 database via SYNC_DATABASE_URL.
   Deleting it removes a small monthly cost. Owner deletes it, not the
   assistant. Do NOT republish App 2 after deleting.
2. Branch scheduled-runner exists locally in App 2 and may not be pushed to
   origin yet. Pushing it is optional but nice for backup.
3. App 1 in-process auto_sync_loop is now redundant. It is harmless (it never
   fires on autoscale) but could be removed for clarity one day.

---

## 9. Health check - how to tell it is still working

Fast version, no login needed for the first one:

1. https://sri-macroreports.replit.app/status  -> should return healthy.
2. /admin/sources -> Q3 2026 "Last synced" should never be older than about
   6-7 hours. If it is, the job stopped.
3. App 2 > Tools > Publishing > Schedule -> run history should show a Done
   entry roughly every 6 hours, each taking ~30-40 seconds.

If a run fails, open the run in App 2 and read the logs there. The usual
suspects, in order: a missing or rotated secret, the run command having been
rewritten by the Agent, or .replit having been clobbered by a branch mix-up.

---

## 10. Where else to look

- docs/SCHEDULED_SYNC.md - the operational runbook for the scheduled job.
- scripts/run_due_syncs.py - the job entry point.
- app/sync/scheduler.py - run_due_syncs(), sources_due(), the dead
  auto_sync_loop.
- app/sync/engine.py - run_sync(), the actual sheet-to-database work.
- .replit - deployment config. Differs per branch ON PURPOSE.


## 11. Correction and verification - 2026-08-03

Sections 2, 3 and 8 above claim that auto_sync_loop never fires. That is WRONG. The loop fires whenever an Autoscale container happens to stay warm for longer than an hour, and on 2026-08-03 it did the real work that the scheduled runner was expected to do.

Evidence, read from the live systems:

- App 1 deployment log, 2026-08-03T02:39:28Z: Auto-sync check: interval=1 day(s), 1 source(s) due to sync
- App 1 deployment log, 2026-08-03T02:42:37Z: Auto-sync wake complete: 1 source(s) synced this hour
- Audit log, 2026-08-03 02:42:36 UTC: actor auto-sync, sync_run success, data_source #3, auto=true, no IP
- The same hourly checks appear all through 2026-08-02, and an earlier one landed on 2026-07-29 02:00:25 UTC against data_source #1
- The 04:00 UTC cron pass that morning therefore found 0 sources due, correctly, 78 minutes after the loop had already synced Q3

App 2 is nevertheless wired correctly. Verified 2026-08-03 from the App 2 Shell, names lengths and booleans only, no secret values printed:

- SYNC_DATABASE_URL, GOOGLE_SERVICE_ACCOUNT_JSON, SESSION_SECRET and ADMIN_INITIAL_PASSWORD are all present in the environment. The Secrets pane can render completely empty even when they are set. Trust the environment, not the pane.
- Through SYNC_DATABASE_URL: 2 data sources, newest last_synced_at 2026-08-03 02:42:36 UTC, which matches the dashboard exactly.
- Through the injected DATABASE_URL: 0 data sources. That is the orphaned Development Database, 30.13MB, still attached to App 2.
- Proof that the DEPLOYED job reads production and not the orphan: the default for auto_sync_days is 0 (app/config_store.py) and the scheduler logs Auto-sync check: disabled (auto_sync_days=0) in that case. The orphan holds no config rows, so a job pointed at it would log disabled. Every deployed pass logged interval=1 day(s), and 1 is the production value set on 2026-07-31.
- The Google credential works: building GoogleSheetsClient with the App 2 secret and calling list_tabs on the Q3 spreadsheet returned 13 tabs, including the configured tab. Metadata only, no writes.
- Branch scheduled-runner is pushed: HEAD b5a3cb4 equals origin/scheduled-runner, so the open item in section 8 is closed. No .replit.bak exists in App 2.

Changes landed on main in this session:

- app/sync/scheduler.py: auto_sync_loop now returns immediately unless ENABLE_INPROCESS_AUTOSYNC is set to 1, true, yes or on. Default is off, so App 2 is the single source of truth and the race is gone. Setting the variable in App 1 Secrets restores the old behaviour with no code change, for example on an always-on Reserved VM.
- scripts/run_due_syncs.py: every pass now logs which variable supplied the database URL, how many data sources are visible (total and active), and a boolean for whether the Google credential is present. A pass against the wrong database is now obvious from the log instead of looking like a quiet success. The block is wrapped in try and except so logging can never break a sync.
- App 2 Scheduled Job Notifications switched from Disabled to Enabled under Publishing then Manage, so a failed pass emails the owner. That toggle needed no republish.

Open after this session:

- The runner has still never performed a live sync. With the loop off, the first pass after Q3 goes a full day stale should log 1 source due and write. Q3 was last synced 2026-08-03 02:42 UTC, so watch the 04:00 UTC pass on 2026-08-04.
- One row dated 2026-07-22 was imported earlier but was absent from the sheet at the 2026-08-03 sync. Nothing was deleted, so the stored row still counts and the figures may overcount until somebody decides whether it was voided on purpose.
- Q3 has 2 open rejected rows in the review queue.
- The orphaned Development Database on App 2 is still attached and still billed. The owner deletes it, not an assistant. Do not republish App 2 afterwards.


## 12. Verification and republish - 2026-08-05

Section 11 was correct as written. This records what happened when App 2 was actually moved onto commit 18bf014 and republished.

Shell verification, App 2, branch scheduled-runner, before touching anything: HEAD b5a3cb4, working tree clean, .replit sha256 c24155a1d52c73e640611fb7a33b67f3743969c540e7aa0bbfdda94f891d5d9b, git blob 7b9891a9c47f3d5f8db8928d68a16c92ba4d1500, no .replit.bak. origin/main's actual tip is acd24c0, an empty "Published your App" marker one commit above 18bf014 with 0 files changed, so the tree checked out is identical to 18bf014's.

The command run was git fetch origin followed by git checkout origin/main for the app, migrations and scripts directories only. The diff touched only app/sync/scheduler.py, 20 lines added, and scripts/run_due_syncs.py, 26 lines added, 46 insertions total and 0 deletions. app/main.py was already identical to origin/main, so the unreviewed Agent rewrite of the migrations function that SCHEDULED_SYNC.md describes was not present to drop. Commit b5a3cb4's own message says it moved the sync runner onto its own branch and dropped the unreviewed migration hack, so that had already happened before this session, and the warning in SCHEDULED_SYNC.md is now stale. After the checkout, the dotreplit file hash and blob were unchanged, byte for byte, and git status showed only the two intended files modified, never dotreplit.

Republish happened from inside Adjust settings only, with the owner's explicit go ahead. The Overview afterward showed Type Scheduled, EST, every six hours, Production database connected. The Publish button itself created a local commit, b98d1af, Published your App, containing exactly those same two files, 46 insertions and 0 deletions, and not dotreplit. That commit sits on scheduled runner only. The branch is ahead of origin scheduled runner by one commit and has not been pushed.

What the cron passes actually did, in order. At 2026-08-04 04:00 UTC the job ran on the OLD code, still b5a3cb4, before the republish. This was the runner's first genuine live sync: Q3 rows went from 2,838 to 3,300, and the dashboard Recent sync runs list shows 2026-08-04 04:00 live and success. It logged only the old style lines, Database ready, Auto-sync check, complete, with none of the new evidence, because the new code was not deployed yet. At 16:00 and 22:00 UTC that day, the first two passes ran on the new code, and both correctly found 0 sources due, since Q3 had just synced. At 2026-08-05 04:00 UTC the job found 1 source due, Q3, interval 1 day, last synced the previous 04:01, and it crashed before completing. The log has no Database ready line at all. It fails inside SQLAlchemy's connection pool, engine dot raw underscore connection calling pool dot connect calling underscore checkout calling underscore do underscore get, while opening the very first database connection. The traceback is genuine, read directly in the Replit log viewer. The final exception line never appeared there, cut off by the log viewer itself before any connection string could surface, so it was not chased further. That run was marked Failed at 96.7 seconds. At 2026-08-05 10:00 UTC the job succeeded and completed the delayed sync, logging the full evidence chain designed in 18bf014 in order: Database ready, Sync target is the SYNC_DATABASE_URL override, data sources visible in this database is 2 total and 1 active, Google service account credential present is True, Auto-sync check interval is 1 day with 1 source due to sync, and Scheduled auto-sync pass complete with 1 source synced. The dashboard cross-check matched: Q3 last sync 2026-08-05 10:01, a new Recent sync runs row reading 3,416 rows read, 117 added, 519 changed, 2 rejected, result success, and Q2 2026 untouched at 2026-07-28 18:38, still inactive.

Diagnosis, what is working and what is not. The branch safety mechanics held throughout: dotreplit never moved, verified byte identical before the checkout, after the checkout, and after the publish, across both apps and one full publish cycle. The new self-evidencing logging, sync target, source counts, credential boolean, works exactly as designed, but it only reaches the log once the initial database bootstrap succeeds. That bootstrap sits upstream of the try and except described in section 11 and is not itself wrapped, so a connectivity blip there produces a bare unhandled traceback with none of the new evidence, which from outside looks indistinguishable from a genuinely broken SYNC_DATABASE_URL secret. The only things that told the two failure modes apart here were the missing Database ready line and the fact that the very next scheduled pass, six hours later, connected and worked with no human action. The six hourly cadence is holding to the cron exactly, with runs observed at 04:00, 10:00, 16:00 and 22:00 UTC. Not yet done: nobody has established why the 2026-08-05 04:00 UTC connection attempt failed, and it has not recurred as of the 10:00 UTC pass and later. Scheduled Job Notifications, enabled per section 11, would have emailed the owner about this Failed run. Unchanged and untouched this session: the orphaned Development Database on App 2, the 2026-07-22 row absent from the sheet, and Q3's 2 rejected rows.
