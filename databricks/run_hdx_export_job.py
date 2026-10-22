"""DBX entry wrapper for the HDX export (pipelines/export_hdx.py).

Runs as the `hdx_export` task of the Storm Alert job, side by side with
`run_alert` on the same job cluster, with the job's own parameters passed
positionally:

    sys.argv[1] = issued_time   # YYYY-MM-DDTHH, or "" for realtime
    sys.argv[2] = test_email    # "True" -> write to the test prefix, never published
    sys.argv[3] = dry_run       # "True" -> build and log, upload nothing
    sys.argv[4] = stage         # "dev" | "prod" (ocha-stratus DB stage)

Reusing the alert's live/test switch means the dev target (test_email=True)
can never feed hdx-scraper-storms. No secrets beyond the cluster's DSCI_AZ_*
env vars: the export reads the DB and writes the dev blob, nothing else.

Same shell-out pattern as run_alert_job.py: PYTHONPATH set to the repo root
so ``from src ...`` resolves (the repo is cloned under source: GIT, not
pip-installed).
"""

import os
import subprocess
import sys


def _find_script_dir() -> str:
    """spark_python_task's exec context doesn't always define __file__."""
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        pass
    if sys.argv and sys.argv[0]:
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    return os.getcwd()


def _arg(i: int, default: str = "") -> str:
    return sys.argv[i] if len(sys.argv) > i else default


REPO_ROOT = os.path.abspath(os.path.join(_find_script_dir(), ".."))

ISSUED_TIME = _arg(1)
TEST = _arg(2, "True").strip().lower() == "true"
DRY_RUN = _arg(3, "True").strip().lower() == "true"
STAGE = _arg(4, "dev")

env = dict(os.environ)
env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")

cmd = [sys.executable, os.path.join(REPO_ROOT, "pipelines", "export_hdx.py"),
       "--stage", STAGE]
if ISSUED_TIME:
    cmd += ["--issued-time", ISSUED_TIME]
if TEST:
    cmd.append("--test")
if DRY_RUN:
    cmd.append("--dry-run")

if __name__ == "__main__":
    print(
        f"[run_hdx_export_job] repo_root={REPO_ROOT} STAGE={STAGE} TEST={TEST} "
        f"DRY_RUN={DRY_RUN} issued_time={ISSUED_TIME or '(realtime)'}"
    )
    rc = subprocess.run(cmd, cwd=REPO_ROOT, env=env, check=False).returncode
    # DBX treats a top-level sys.exit()/SystemExit (even code 0) as a task
    # failure. Raise only on non-zero; let success return naturally.
    if rc != 0:
        raise RuntimeError(f"export_hdx.py exited with code {rc}")
    print("[run_hdx_export_job] OK")
