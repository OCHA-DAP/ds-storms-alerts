# Databricks job — Storm Alert

`pipelines/run_alert.py` runs as a scheduled **Databricks job** (defined in the
repo-root `databricks.yml`). This replaced the GitHub Actions schedule, whose
trigger latency had degraded to ~1 hour late (vs ~10 min a year earlier); the GHA
`Run Storm Alert` workflow is now disabled.

## Architecture

- One job, `storm_alert`, a single `spark_python_task`.
- `source: GIT` clones this repo at `${var.git_branch}` **at run time**, so shipping
  a code change = pushing the branch (no redeploy needed). A `bundle deploy` is only
  needed when the *job config* (schedule, params, libraries, cluster) changes.
- The task runs the thin wrapper `databricks/run_alert_job.py`, which injects the
  listmonk creds from the `dsci` secret scope, sets the send-mode env vars, and shells
  out to `pipelines/run_alert.py`. Pipeline code (`pipelines/`, `src/`) stays pure
  Python and DBX-agnostic.
- Compute: the existing cluster `${var.existing_cluster_id}`, which already carries the
  `DSCI_AZ_*` DB/blob env vars. `run_alert.py` is hardcoded to `stage="dev"`, so it
  reads the **dev** database regardless of target.

## Target model — one live job, dev on demand

**Don't run two standing jobs.** Two targets scheduled on the same branch just fire
twice per advisory and cause confusion.

- **`prod`** (default target) is the only normally-deployed job. It serves both the
  scheduled runs *and* ad-hoc manual runs (override params per run). Live params:
  `test_email=False`, `dry_run=False`.
- **`dev`** is deployed **on demand for feature work**, pointed at a feature branch.
  Development mode auto-pauses its schedule and defaults to the test list, so it never
  competes with prod. Destroy it when the feature merges.

## Common commands

```bash
# --- Live job (prod is the default target) ---
databricks bundle validate -p default
databricks bundle deploy   -p default                 # deploy/update the live job
databricks bundle run storm_alert -p default          # manual run — SENDS FOR REAL
databricks bundle run storm_alert -p default --params issued_time=2025-10-24T18   # backfill
databricks bundle run storm_alert -p default --params test_email=True,dry_run=True # safe manual test

# --- Feature development (throwaway dev job from your branch) ---
databricks bundle deploy  -t dev -p default --var git_branch=my-feature
databricks bundle run     storm_alert -t dev -p default   # paused schedule, test list
databricks bundle destroy -t dev -p default               # when the feature merges
```

## Send-mode params (the GHA-vars analog)

`dry_run` and `test_email` are bundle **variables** wired into the job's parameter
defaults, so the mode flips at deploy time without editing files
(`--var test_email=False`). Per run, override with `--params`.

| | `dry_run` | `test_email` |
|---|---|---|
| `"True"` | generate but **skip** send/write | send to the **Tristan-only test list** |
| `"False"` | **actually send** | send to **real per-country subscriber lists** |

⚠️ The single job is the **live** one: a bare `bundle run storm_alert` **sends for
real**. For a safe manual check pass `--params test_email=True,dry_run=True`, or use a
`dev` deploy.

## Prerequisites (one-time)

1. Workspace GitHub credentials for this private repo (same mechanism as
   `ds-storms-pipeline`).
2. Listmonk creds in the `dsci` secret scope:
   ```bash
   databricks secrets put-secret dsci DSCI_LISTMONK_API_USERNAME -p default
   databricks secrets put-secret dsci DSCI_LISTMONK_API_KEY      -p default
   ```

## Gotchas / best practices (learned the hard way)

- **Don't set `schedule.pause_status: UNPAUSED` in the resource.** It overrides
  development mode's auto-pause and makes the `dev` job fire on the cron too. Leave it
  unset: production mode keeps prod running; development mode pauses dev.
- **The wrapper must not call `sys.exit()` at the top level.** `spark_python_task`
  treats a top-level `SystemExit` — *even code 0* — as a task failure (`INTERNAL_ERROR`).
  Raise an exception only on a non-zero child exit; let success return naturally.
- **Exclude big files from the deploy sync** (`sync.exclude: data/**`). The job pulls
  everything from `source: GIT`, so uploading the large `data/*.parquet` boundary files
  at deploy is pointless and was timing out (HTTP 408).
- **Verify runs from the task logs, not the CLI exit code.** `databricks bundle run`
  can report exit 0 while the task itself failed. Check the run output for
  `Sent campaign …` / `DRY_RUN=True — skipping email`, or
  `databricks jobs get-run-output <task_run_id>`.
- **`source: GIT` means runtime = branch HEAD.** Pushing the branch updates the next
  run automatically; only config changes need a `bundle deploy`.
- **`matplotlib` needs a writable config dir** on the cluster (`MPLCONFIGDIR=/tmp/...`),
  since the cloned repo lives on the read-only workspace FUSE mount.

## Rollback

Re-enable the GitHub Actions schedule if needed:
```bash
gh workflow enable "Run Storm Alert"
```
Revert the DBX job to the test list: `databricks bundle deploy -p default --var test_email=True`.
Pause it entirely: pause the job in the workspace UI.
