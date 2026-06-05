# Databricks job — Storm Alert

Runs `pipelines/run_alert.py` as a scheduled Databricks job, as an alternative to
the GitHub Actions workflow (`.github/workflows/run_alert.yml`), which has been
firing ~1 hour late due to GHA scheduler latency. The GHA workflow is kept running
for now; this job is deployed alongside and validated in dry-run first.

## How it works

- `databricks.yml` defines a single job, `storm_alert`, with one `spark_python_task`.
- `source: GIT` clones this repo at `${var.git_branch}` (default `initial-pipeline`)
  at run time — deploying code = pushing the branch.
- The task runs `databricks/run_alert_job.py` (this dir), a thin wrapper that injects
  the listmonk creds from the `dsci` secret scope and shells out to
  `pipelines/run_alert.py`. The pipeline code itself is unchanged and DBX-agnostic.
- Compute: the existing cluster `${var.existing_cluster_id}`, which already carries
  the `DSCI_AZ_*` DB/blob env vars. Job parameters: `issued_time`, `test_email`,
  `dry_run`.

## One-time prerequisites

1. Workspace GitHub credentials for this private repo (same mechanism as
   `ds-storms-pipeline`).
2. Listmonk creds in the `dsci` secret scope:
   ```bash
   databricks secrets put-secret dsci DSCI_LISTMONK_API_USERNAME -p default
   databricks secrets put-secret dsci DSCI_LISTMONK_API_KEY      -p default
   ```

## Deploy & run

```bash
databricks bundle validate -t dev -p default
databricks bundle deploy   -t dev -p default

# Dry-run end-to-end (default params: dry_run=True → no send):
databricks bundle run storm_alert -t dev -p default

# Real test send to the Tristan-only test list:
databricks bundle run storm_alert -t dev -p default --params dry_run=False,test_email=True

# Backfill a specific advisory:
databricks bundle run storm_alert -t dev -p default --params issued_time=2025-10-24T18
```

Note: the `dev` target uses development mode, so the deployed job's schedule is
auto-paused and its name is prefixed `[dev <user>]`. To run live on the cron, deploy
the `prod` target (`-t prod`) or unpause the dev job in the UI. `run_alert.py` is
hardcoded to `stage="dev"`, so both targets read the dev database.
