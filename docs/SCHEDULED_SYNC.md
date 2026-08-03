# Scheduled auto-sync

How the Google Sheets auto-sync is actually driven, and why it does not run inside the dashboard process.

## The problem this replaces

app/sync/scheduler.py contains auto_sync_loop, which does await asyncio.sleep(3600) BEFORE its first due-check.
The dashboard is published as a Replit Autoscale deployment, so its container is spun down whenever there is no traffic.
Every cold start restarts that hour from zero, so in practice the hour never elapses and no automatic sync ever fires.
Every sync in the history was a manual one.

The loop is still in the code and is harmless, but it is not what keeps sources current. Treat it as dead weight.

## The arrangement

One GitHub repo, two Replit Apps:

| App | Replit name | Deployment | Purpose |
| --- | --- | --- | --- |
| 1 | SRIMacroreports | Autoscale, sri-macroreports.replit.app | the dashboard itself |
| 2 | SRIMacroreports-1 | Scheduled, no URL | runs the sync on a cron |

App 2 is a job, not a web app. It has no domain. It wakes on the cron, runs one pass, and exits.
It writes to the SAME production database as app 1.

Entry point: scripts/run_due_syncs.py (committed on main).

## Runner configuration (app 2 only)

The .replit deployment block in app 2:

    [deployment]
    deploymentTarget = "scheduled"
    run = ["python3", "-m", "scripts.run_due_syncs"]

Cron 0 */6 * * * , timezone EST, job timeout 10 minutes, geography North America, 1 vCPU / 2 GiB.

Workspace Secrets (Tools then Secrets), which are inherited into the deployment on every publish:

- SYNC_DATABASE_URL   connection string for the app 1 production database
- GOOGLE_SERVICE_ACCOUNT_JSON   same service account as app 1
- SESSION_SECRET and ADMIN_INITIAL_PASSWORD   unused by the job, inherited anyway

## Rules and traps

1. Never push app 2 .replit to main. App 2 carries deploymentTarget = scheduled. Pushing that over main would turn the live dashboard into a cron job. App 2 sits deliberately ahead of origin/main and must stay unpushed. Do not press Sync Changes in app 2.

2. App 2 also carries an Agent-written rewrite of _run_migrations() in app/main.py that stamps alembic to head when tables exist without a version row. It was never reviewed and must not reach main.

3. Cadence must be shorter than auto_sync_days. sources_due compares last_synced_at against now minus interval_days. A once-a-day schedule at a fixed clock time finds the previous run fractionally too recent and syncs nothing, forever. Six-hourly gives four chances a day for the one-day interval to clear.

4. Call run_due_syncs from app.sync.scheduler, not run_sync from app.sync.engine. The scheduler wrapper is what applies the auto_sync_days setting, readiness and upload-source filtering, oldest-first ordering, per-source transactions, and the auto-sync audit actor label.

5. SYNC_DATABASE_URL exists because Replit force-injects DATABASE_URL into any app that owns a production database. The script overrides DATABASE_URL with SYNC_DATABASE_URL at import time so the job always writes to the real dashboard database.

6. Run it as a module. python3 scripts/run_due_syncs.py fails with ModuleNotFoundError No module named app, because that puts scripts/ on sys.path instead of the project root. Use python3 -m scripts.run_due_syncs.

## Replit UI mechanics worth knowing

- Deployment type, cron and deployment secrets live inside the Adjust settings panel and are only committed by the Publish button INSIDE that panel. The top-right Republish button and the Agent chat Publish button both discard the panel and republish the last saved config. Two publishes were lost to this, each one going out as a public Autoscale web app instead of a job.
- Secrets set under Tools then Secrets persist across publishes and are inherited into the deployment. Values typed into the deployment-secrets accordion are not persisted.
- One deployment per Replit App. Changing type is Publishing then Manage then Change deployment type, which unpublishes and republishes.
- The Replit Agent rewrites .replit run commands on its own initiative. It replaced the job command with a uvicorn server once. Check .replit after any Agent activity.

## Verifying

- Publishing then Schedule shows run history and a Run now button.
- A healthy run logs: Database ready, then Auto-sync check: interval=1 day(s), N source(s) due to sync, then Scheduled auto-sync pass complete: N source(s) synced.
- Cross-check in the dashboard at /admin/sources under Recent sync runs.
- First green run: 2026-08-01 23:45 EST, 38.2s, 0 sources due (Q3 had been synced manually 90 minutes earlier).

## Open items

- App 2 still has an orphaned production database attached from the two accidental Autoscale publishes. It is unused and should be deleted from Database then Settings so it is not billed.
- App 2 was briefly public at sri-macroreports-1.replit.app during those two publishes. It is unpublished now and the URL is free again.
- auto_sync_loop in app/sync/scheduler.py is now redundant. Removing it would avoid a double-sync if an Autoscale container ever stays warm for over an hour. Low risk, not done.
- .replit.bak in app 2 is a backup of the pre-patch .replit. Safe to delete once the schedule has been stable for a while.


See also: docs/PROJECT_CONTEXT.md - full background, the two-app / two-branch
arrangement, traps already hit, and the current verified state.

## Correction - 2026-08-03

The sections above say the in-process loop never fires. It does. Whenever an Autoscale container stays warm for longer than an hour the loop wakes and syncs. On 2026-08-03 it synced Q3 itself between 02:39 and 02:42 UTC, 78 minutes before the 04:00 UTC cron pass, which then correctly reported 0 sources due.

As of this commit the loop is gated behind ENABLE_INPROCESS_AUTOSYNC and is off by default, so this scheduled job is the only thing that drives auto-sync.

A healthy pass now logs, in order: Database ready, Sync target, Data sources visible in this database, Google service account credential present, Auto-sync check, and Scheduled auto-sync pass complete. If Sync target reads injected DATABASE_URL, or the visible source count is 0, the job is pointed at the wrong database.

Verified 2026-08-03: the deployed job reads the production database, proved by the auto_sync_days default of 0 against the logged interval=1; the Google service account authenticates and can list the Q3 tabs; and Scheduled Job Notifications are now enabled, so a failed pass emails the owner.
