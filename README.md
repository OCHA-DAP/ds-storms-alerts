# ds-storms-alerts

Automated email alerts for NHC tropical-cyclone forecasts. For each advisory the
pipeline estimates population exposure per country (consolidating **OCHA CHD**, **ADAM**,
and **GDACS** estimates), renders storm maps and per-country exposure charts, and emails
a single consolidated alert to subscribers via Listmonk.

## For subscribers

- **Subscribe / unsubscribe:** https://ocha-dap.github.io/ds-storms-alerts/
- **About & documentation:** https://ocha-dap.github.io/ds-storms-alerts/guide.html

## How it runs

The alert runs as a scheduled **Databricks job** (`pipelines/run_alert.py`), four times
a day at the NHC advisory hours (03:30 / 09:30 / 15:30 / 21:30 UTC). See
**[`databricks/README.md`](databricks/README.md)** for the asset bundle, deploy/run
commands, send-mode switches, and operational notes. (A GitHub Actions schedule
previously ran it but is disabled — Databricks is the runner now.)

## Repository layout

| Path | What |
|------|------|
| `pipelines/run_alert.py` | The alert pipeline: fetch exposure → render maps/charts → email. |
| `pipelines/setup_country_lists.py` | Provisions the per-country Listmonk subscriber lists. |
| `src/data.py` | Data access (DB + blob) via `ocha-stratus`. |
| `src/plots.py` | Strip charts and storm maps (matplotlib / geopandas). |
| `databricks/` | Databricks Asset Bundle + the thin job wrapper, and its README. |
| `notebooks/alert_preview.py` | marimo app to preview an advisory and send a test email. |
| `docs/` | GitHub Pages site — the subscribe form and about page. |

## Local development

```bash
uv sync

# Preview an advisory in the browser (generates HTML, sends nothing):
uv run python pipelines/run_alert.py --issued-time 2025-10-24T18 --preview

# Or the interactive preview/test-send app:
uv run marimo edit notebooks/alert_preview.py
```

The exposure tables, tracks, WSP polygons, and boundaries are read from the OCHA
**dev** database / blob storage via `ocha-stratus`; the upstream
[`ds-storms-pipeline`](https://github.com/OCHA-DAP/ds-storms-pipeline) repo produces
that data.
