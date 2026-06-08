"""DBX entry wrapper for the storm-alert pipeline.

The bundle's ``spark_python_task`` passes the job parameters positionally:

    sys.argv[1] = issued_time   # YYYY-MM-DDTHH, or "" for realtime
    sys.argv[2] = test_email    # "True" | "False"
    sys.argv[3] = dry_run       # "True" | "False"
    sys.argv[4] = stage         # "dev" | "prod" (ocha-stratus DB/blob stage)

``pipelines/run_alert.py`` and ``src/`` stay pure Python — they don't know about
DBX (the GHA workflow runs the same script). This wrapper is the only DBX-specific
glue and does two things:

1. Inject the listmonk credentials. The reused cluster carries the ``DSCI_AZ_*``
   DB/blob env vars (used by ds-storms-pipeline) but NOT the listmonk ones, so we
   read those from the ``dsci`` secret scope and export them, alongside the
   run-mode env vars run_alert.py reads at import (``TEST_EMAIL`` / ``DRY_RUN``).

2. Shell out to ``pipelines/run_alert.py`` with ``PYTHONPATH`` set to the repo
   root so ``from src ...`` resolves — under ``source: GIT`` the repo is cloned
   but not pip-installed, and the script lives in ``pipelines/`` rather than at
   the root.
"""

import os
import subprocess
import sys


def _find_script_dir() -> str:
    """spark_python_task's exec context doesn't always define __file__."""
    try:
        return os.path.dirname(os.path.abspath(__file__))  # noqa: F821
    except NameError:
        pass
    if sys.argv and sys.argv[0]:
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    return os.getcwd()


def _arg(i: int, default: str = "") -> str:
    return sys.argv[i] if len(sys.argv) > i else default


REPO_ROOT = os.path.abspath(os.path.join(_find_script_dir(), ".."))

ISSUED_TIME = _arg(1)
TEST_EMAIL = _arg(2, "True")
DRY_RUN = _arg(3, "True")
STAGE = _arg(4, "dev")

# Listmonk config — absent from the reused cluster's env, pulled from the dsci scope
# (base URL + API creds, so dev/prod can't drift and repointing needs no code edit).
# Tolerated if missing so a dry-run (no send) still validates DB/blob/plotting;
# run_alert.py only builds the ListmonkClient when actually sending, and will then
# raise a clear missing-env error.
from databricks.sdk.runtime import dbutils  # noqa: E402

for _key in (
    "DSCI_LISTMONK_BASE_URL",
    "DSCI_LISTMONK_API_USERNAME",
    "DSCI_LISTMONK_API_KEY",
):
    try:
        os.environ[_key] = dbutils.secrets.get("dsci", _key)
    except Exception as exc:  # noqa: BLE001
        print(f"[run_alert_job] WARNING: dsci/{_key} unavailable ({exc}); "
              "real sends will fail until it is set.")

os.environ["TEST_EMAIL"] = TEST_EMAIL
os.environ["DRY_RUN"] = DRY_RUN

# Make `src` importable for the child process (repo isn't pip-installed here).
env = dict(os.environ)
env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
# Give matplotlib a writable local config/cache dir (the cloned repo lives on the
# read-only workspace FUSE mount, which it can't use).
env["MPLCONFIGDIR"] = "/tmp/mplconfig"

cmd = [sys.executable, os.path.join(REPO_ROOT, "pipelines", "run_alert.py"),
       "--stage", STAGE]
if ISSUED_TIME:
    cmd += ["--issued-time", ISSUED_TIME]

if __name__ == "__main__":
    print(
        f"[run_alert_job] repo_root={REPO_ROOT} STAGE={STAGE} "
        f"TEST_EMAIL={TEST_EMAIL} "
        f"DRY_RUN={DRY_RUN} issued_time={ISSUED_TIME or '(realtime)'}"
    )
    rc = subprocess.run(cmd, cwd=REPO_ROOT, env=env, check=False).returncode
    # DBX treats a top-level sys.exit()/SystemExit (even code 0) as a task
    # failure. Raise only on non-zero; let success return naturally.
    if rc != 0:
        raise RuntimeError(f"run_alert.py exited with code {rc}")
    print("[run_alert_job] OK")
