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
| `pipelines/generate_showcase.py` | Regenerates the historical example alerts under `docs/alerts/`. |
| `pipelines/setup_country_lists.py` | Provisions the per-country Listmonk subscriber lists. |
| `src/data.py` | Data access (DB + blob) via `ocha-stratus`. |
| `src/plots.py` | Strip charts and storm maps (matplotlib / geopandas / contextily). |
| `src/preview.py` | Renders a body through the real Listmonk template, no send. |
| `databricks/` | Databricks Asset Bundle + the thin job wrapper, and its README. |
| `docs/` | GitHub Pages site — the subscribe form and about page. |

## Local development

```bash
uv sync

# Preview an advisory in the browser, wrapped in the real Listmonk email
# template (OCHA header bar, test banner, footer). Sends nothing.
uv run python pipelines/run_alert.py --issued-time 2025-10-24T18 --preview

# The email is the CONDENSED layout (summary table + one combined map per
# storm). Add --full for the full-detail layout (both maps + all strip charts)
# that the online example pages use:
uv run python pipelines/run_alert.py --issued-time 2025-10-24T18 --preview --full

# Regenerate the published example alerts (docs/alerts/) after layout changes:
uv run python pipelines/generate_showcase.py

# Iterating on the layout: a fixed --out path means you just hit reload.
uv run python pipelines/run_alert.py --issued-time 2025-10-24T18 \
    --preview --out /tmp/alert.html --no-open

# Skip the Listmonk round-trip (bare body, works offline):
uv run python pipelines/run_alert.py --issued-time 2025-10-24T18 --preview --raw

# Actually mail it — to the internal test list only:
uv run python pipelines/run_alert.py --issued-time 2025-10-24T18 --send-test
```

`--preview` needs `DSCI_LISTMONK_BASE_URL` / `DSCI_LISTMONK_API_USERNAME` /
`DSCI_LISTMONK_API_KEY`. It reuses a single parked draft campaign
(`[test] ds-storms-alerts preview scratch - do not send`) and pushes each body
through that campaign's preview endpoint — no new campaigns, no sends. Without
those credentials it falls back to `--raw` with a warning.

The exposure tables, tracks, WSP polygons, and boundaries are read from the OCHA
**dev** database / blob storage via `ocha-stratus`; the upstream
[`ds-storms-pipeline`](https://github.com/OCHA-DAP/ds-storms-pipeline) repo produces
that data.

## Chart and map styling

Colours come from the [HDX v2 design tokens](https://github.com/OCHA-DAP/ds-knowledge-base/blob/main/methods/style-guide.md),
lifted into `src/plots.py` as hex constants (matplotlib can't import the CSS
bundle). Wind thresholds use the status ramp (amber → deep red); wind-speed
probability uses the primary blue ramp, pale to deep.

The maps draw a **CartoDB Positron** tile basemap via `contextily`, which is the
one runtime network call in the pipeline. It is not load-bearing: on any tile
failure the first attempt latches and every map falls back to the packaged
Natural Earth boundary layer under `data/`, with a warning in the log.
