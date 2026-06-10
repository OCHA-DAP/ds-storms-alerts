import marimo

__generated_with = "0.13.11"
app = marimo.App(width="full")


@app.cell
def _imports():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parents[1]))

    import marimo as mo
    import ocha_stratus as stratus
    import pandas as pd
    from datetime import datetime, timedelta
    from sqlalchemy import text
    from pipelines.run_alert import generate_alert_html, send_test_alert
    return (
        datetime, generate_alert_html, mo, pd,
        send_test_alert, stratus, text, timedelta,
    )


@app.cell
def _mode(mo):
    """Top-level mode toggle.

    - **General**: original app (NHC storm picker, advisory-time, email
      preview, send test).
    - **Admin 1 comparison**: broader storm dropdown (every GDACS event
      with non-zero exposure) and the FM↔GDACS matching table — for
      verifying the canonical lookup against the full historical record.
    """
    mode = mo.ui.dropdown(
        options=["General", "Admin 1 comparison"],
        value="General",
        label="Mode",
    )
    mode


@app.cell
def _db(stratus, text):
    engine = stratus.get_engine(stage="dev")
    with engine.connect() as _conn:
        _rows = _conn.execute(text(
            "SELECT DISTINCT e.atcf_id, "
            "  COALESCE(NULLIF(s.name, 'NaN'), ib.name) AS name, "
            "  COALESCE(s.season, ib.season) AS season "
            "FROM storms.nhc_tracks_fcastonly_exposure e "
            "LEFT JOIN storms.nhc_storms s ON s.atcf_id = e.atcf_id "
            "LEFT JOIN storms.ibtracs_storms ib ON ib.atcf_id = e.atcf_id "
            "ORDER BY COALESCE(s.season, ib.season) DESC NULLS LAST, e.atcf_id DESC"
        )).fetchall()
    storm_options = {
        (
            f"{r[1].strip().title()} {int(r[2])} ({r[0]})"
            if r[1] and r[2] else r[0]
        ): r[0]
        for r in _rows
    }
    return engine, storm_options


@app.cell
def _storm_selector(mo, mode, storm_options):
    mo.stop(mode.value != "General")
    storm = mo.ui.dropdown(options=storm_options, label="Storm", searchable=True)
    storm


@app.cell
def _time_selector(mo, mode, storm, engine, text, timedelta):
    mo.stop(mode.value != "General")
    _rows = []
    if storm.value is not None:
        with engine.connect() as _conn:
            _rows = _conn.execute(text(
                "SELECT DISTINCT issued_time "
                "FROM storms.nhc_tracks_fcastonly_exposure "
                "WHERE atcf_id = :aid "
                "UNION "
                "SELECT DISTINCT issued_time "
                "FROM storms.nhc_wsp_fcastonly_exposure "
                "WHERE atcf_id = :aid "
                "ORDER BY issued_time DESC"
            ), {"aid": storm.value}).fetchall()
    _times = [r[0] for r in _rows]
    # Build options dict: label -> value string
    _options = {t.strftime("%Y-%m-%dT%H"): t.strftime("%Y-%m-%dT%H") for t in _times}
    # Prepend synthetic "final advisory" = last advisory + 6h, if not already present
    if _times:
        _final_dt = max(_times) + timedelta(hours=6)
        _final_key = _final_dt.strftime("%Y-%m-%dT%H")
        if _final_key not in _options:
            _options = {f"{_final_key} (final advisory)": _final_key, **_options}
    issued_time = mo.ui.dropdown(options=_options, label="Issued time")
    generate_btn = mo.ui.run_button(label="Generate")
    send_btn = mo.ui.run_button(label="Send test email")
    mo.hstack([issued_time, generate_btn, send_btn], gap=2)


@app.cell
def _preview(mo, mode, generate_btn, issued_time, engine, generate_alert_html, datetime):
    mo.stop(mode.value != "General")
    mo.stop(not generate_btn.value)
    mo.stop(issued_time.value is None)
    _issued_time_dt = datetime.strptime(issued_time.value, "%Y-%m-%dT%H")
    _body = generate_alert_html(engine, _issued_time_dt)
    (
        mo.md("**No storms with forecasted or final-update exposure for this issued time.**")
        if _body is None
        else mo.Html(f"<div style='font-family:sans-serif;max-width:900px;margin:auto'>{_body}</div>")
    )


@app.cell
def _send(mo, mode, send_btn, issued_time, engine, send_test_alert, datetime):
    mo.stop(mode.value != "General")
    mo.stop(not send_btn.value)
    mo.stop(issued_time.value is None)
    _issued_time_dt = datetime.strptime(issued_time.value, "%Y-%m-%dT%H")
    try:
        _status = send_test_alert(engine, _issued_time_dt)
        mo.callout(mo.md(f"**Sent.** {_status}"), kind="success")
    except Exception as _e:
        mo.callout(mo.md(f"**Error:** {_e}"), kind="danger")


# ─── Admin 1 comparison mode ─────────────────────────────────────────
# Verifies the canonical FM↔GDACS lookup against every GDACS event we
# have on file. Each row = one GDACS admin row + the FM unit it joined
# to. Orphans (no match) and multi-attach cases (one GDACS row → many
# FM rows, e.g. CUB pre-split) are flagged. NHC numbers intentionally
# absent — this view exists to verify name matching, not to compare
# sources.


@app.cell
def _cmp_storm_options(mo, mode, engine, text):
    mo.stop(mode.value != "Admin 1 comparison")
    # Every GDACS event with at least one non-zero exposure row.
    # Includes pre-2025 events (single final snapshot per event) and
    # 2025+ events (multiple episodes per event).
    with engine.connect() as _conn:
        _rows = _conn.execute(text(
            "SELECT g.gdacs_eventid, lk.atcf_id, "
            "  COALESCE(NULLIF(s.name, 'NaN'), ib.name) AS name, "
            "  COALESCE(s.season, ib.season) AS season "
            "FROM (SELECT DISTINCT gdacs_eventid FROM storms.gdacs_exposure "
            "      WHERE COALESCE(pop_exposed, 0) > 0) g "
            "LEFT JOIN storms.storm_id_lookup lk "
            "  ON lk.gdacs_eventid = g.gdacs_eventid "
            "LEFT JOIN storms.nhc_storms s ON s.atcf_id = lk.atcf_id "
            "LEFT JOIN storms.ibtracs_storms ib ON ib.atcf_id = lk.atcf_id "
            "ORDER BY COALESCE(s.season, ib.season) DESC NULLS LAST, "
            "         name NULLS LAST, g.gdacs_eventid DESC"
        )).fetchall()
    cmp_storm_options = {}
    for _eid, _aid, _name, _season in _rows:
        if _name and _season:
            _stem = f"{_name.strip().title()} {int(_season)}"
            _suffix = f"({_aid})" if _aid else f"(GDACS {_eid})"
            _lbl = f"{_stem} {_suffix}"
        else:
            _lbl = f"GDACS {_eid}"
        cmp_storm_options[_lbl] = int(_eid)
    return (cmp_storm_options,)


@app.cell
def _cmp_storm_selector(mo, mode, cmp_storm_options):
    mo.stop(mode.value != "Admin 1 comparison")
    cmp_storm = mo.ui.dropdown(
        options=cmp_storm_options,
        label="Storm (GDACS event)",
        searchable=True,
    )
    cmp_storm


@app.cell
def _gdacs_lookup_source(mo, mode):
    """GDACS lookup source toggle. Defaults to Dev DB
    (`storms.gdacs_fm_lookup`) so colleagues can use this app without
    needing the local ds-storms-pipeline build output. The Local CSV
    option is a dev workflow for testing humrev-build changes before
    pushing them to the DB.
    """
    mo.stop(mode.value != "Admin 1 comparison")
    gdacs_source = mo.ui.radio(
        options=[
            "Dev DB (storms.gdacs_fm_lookup)",
            "Local CSV (dev workflow — requires ds-storms-pipeline build)",
        ],
        value="Dev DB (storms.gdacs_fm_lookup)",
        label="GDACS lookup source",
    )
    gdacs_source
    return (gdacs_source,)


@app.cell
def _gdacs_lookup_table(mo, mode, gdacs_source, stratus, pd):
    """Resolve the SQL table name the matching demo joins against.

    - Dev DB: returns `storms.gdacs_fm_lookup` directly. No DB writes,
      no local files needed.
    - Local CSV: reads `data/gdacs_fm_lookup.csv` from the sibling
      ds-storms-pipeline repo and pushes it to
      `storms.gdacs_fm_lookup_test` so the downstream SQL has
      something to join. Surfaces a legible error if the CSV is
      missing rather than failing the matching demo silently.
    """
    from pathlib import Path as _Path
    mo.stop(mode.value != "Admin 1 comparison")
    if gdacs_source.value.startswith("Local"):
        _csv = (
            _Path(__file__).resolve().parents[2]
            / "ds-storms-pipeline" / "data" / "gdacs_fm_lookup.csv"
        )
        if not _csv.exists():
            gdacs_lookup_table = None
            _status = mo.callout(
                mo.md(
                    f"**Local CSV not found** at `{_csv}`.\n\n"
                    f"To use this option, build the lookup locally:\n\n"
                    f"```\n"
                    f"cd ds-storms-pipeline\n"
                    f"uv run python scripts/build_gdacs_fm_lookup_v2.py\n"
                    f"```\n\n"
                    f"Or switch the radio above to **Dev DB** to use "
                    f"the production `storms.gdacs_fm_lookup` table."
                ),
                kind="warn",
            )
        else:
            # utf-8-sig: transparently strips BOM if present (the
            # lookup CSV is written with BOM so Excel handles it).
            _df = pd.read_csv(_csv, encoding="utf-8-sig")
            _write_engine = stratus.get_engine(stage="dev", write=True)
            _df.to_sql(
                "gdacs_fm_lookup_test", _write_engine,
                schema="storms", if_exists="replace", index=False,
            )
            gdacs_lookup_table = "storms.gdacs_fm_lookup_test"
            _status = mo.md(
                f"**Using local CSV** — `{_csv.name}` → "
                f"`storms.gdacs_fm_lookup_test` ({len(_df)} rows)"
            )
    else:
        gdacs_lookup_table = "storms.gdacs_fm_lookup"
        _status = mo.md(
            "**Using production** `storms.gdacs_fm_lookup`."
        )
    _status
    return (gdacs_lookup_table,)


@app.cell
def _matching_demo(mo, mode, cmp_storm, engine, pd, text, gdacs_lookup_table):
    """FM-centric multi-source matching table.

    Architecture: three small per-source queries, each returning an
    FM-keyed snap (admin_level, iso3, fm_pcode, wind_speed_kt). Outer-
    merged in pandas. The GDACS query also surfaces orphan rows
    (gdacs admin with no FM lookup match) as fm_pcode=NULL entries.

    Row status (derived from which sources reported):
      ❌ orphan           — GDACS admin with no FM match in lookup
      ⚠️ aggregated       — N>1 GDACS admins → 1 FM (e.g. PRI's 8
                            senatorial districts → 1 FM polygon)
      ⚠️ caveat           — GDACS row with a caveat from gdacs_fm_lookup
      ⚠️ ADAM-only        — ADAM has pop but GDACS and NHC don't —
                            surfaces ADAM coverage that GDACS missed
      ✅ clean 1:1         — clean FM↔source match, no caveat
    """
    mo.stop(mode.value != "Admin 1 comparison")
    mo.stop(cmp_storm.value is None)
    # gdacs_lookup_table is None when the user picked Local CSV but
    # the file isn't on disk — the source cell already showed the
    # error; just skip this cell instead of building broken SQL.
    mo.stop(gdacs_lookup_table is None)

    _eid = cmp_storm.value

    # ── GDACS snap: matched rows aggregated to FM + orphan rows ────
    _gdacs_sql = text(
        "WITH event_rows AS ("
        "  SELECT DISTINCT ON (admin_level, gdacs_admin_code, wind_speed_kt) "
        "    admin_level, iso3, gdacs_admin_code, "
        "    admin_name AS gdacs_admin_name, "
        "    wind_speed_kt, pop_exposed "
        "  FROM storms.gdacs_exposure ge "
        "  WHERE gdacs_eventid = :eid "
        "    AND NOT EXISTS ("
        f"      SELECT 1 FROM {gdacs_lookup_table} x "
        "      WHERE x.iso3 = ge.iso3 "
        "        AND x.admin_level = ge.admin_level "
        "        AND x.gmi_admin IS NULL"
        "    )"
        "  ORDER BY admin_level, gdacs_admin_code, wind_speed_kt, "
        "           valid_time DESC"
        ") "
        # Matched: aggregate to FM
        "SELECT e.admin_level, e.iso3, lk.fm_pcode, e.wind_speed_kt, "
        "  SUM(e.pop_exposed) AS gdacs_pop, "
        "  string_agg(e.gdacs_admin_code, ', ' "
        "    ORDER BY e.gdacs_admin_code) AS gdacs_admins, "
        "  COUNT(DISTINCT e.gdacs_admin_code) AS n_gdacs_admins, "
        "  MAX(lk.caveat_note) AS gdacs_caveat_note "
        "FROM event_rows e "
        f"JOIN {gdacs_lookup_table} lk "
        "  ON lk.iso3 = e.iso3 "
        "  AND lk.admin_level = e.admin_level "
        "  AND lk.gmi_admin = e.gdacs_admin_code "
        "GROUP BY e.admin_level, e.iso3, lk.fm_pcode, e.wind_speed_kt "
        "UNION ALL "
        # Orphans: GDACS admin with no FM match
        "SELECT e.admin_level, e.iso3, "
        "  NULL::text AS fm_pcode, e.wind_speed_kt, "
        "  e.pop_exposed AS gdacs_pop, "
        "  e.gdacs_admin_code AS gdacs_admins, "
        "  1 AS n_gdacs_admins, "
        "  NULL::text AS gdacs_caveat_note "
        "FROM event_rows e "
        f"LEFT JOIN {gdacs_lookup_table} lk "
        "  ON lk.iso3 = e.iso3 "
        "  AND lk.admin_level = e.admin_level "
        "  AND lk.gmi_admin = e.gdacs_admin_code "
        "WHERE lk.fm_pcode IS NULL"
    )

    # ── NHC snap: last observed valid_time per (iso3, wind_speed_kt) ──
    _nhc_sql = text(
        "WITH event_atcf AS ("
        "  SELECT atcf_id FROM storms.storm_id_lookup "
        "  WHERE gdacs_eventid = :eid AND atcf_id IS NOT NULL"
        "), latest_obsv AS ("
        "  SELECT DISTINCT ON (n.atcf_id, n.iso3, n.wind_speed_kt) "
        "    n.atcf_id, n.iso3, n.wind_speed_kt, n.valid_time "
        "  FROM storms.nhc_tracks_obsv_exposure n "
        "  JOIN event_atcf ea ON ea.atcf_id = n.atcf_id "
        "  WHERE n.admin_level = 0 "
        "  ORDER BY n.atcf_id, n.iso3, n.wind_speed_kt, "
        "           n.valid_time DESC"
        ") "
        "SELECT n.iso3, n.pcode AS fm_pcode, n.admin_level, "
        "  n.wind_speed_kt, MAX(n.pop_exposed) AS nhc_pop "
        "FROM storms.nhc_tracks_obsv_exposure n "
        "JOIN latest_obsv p "
        "  ON p.atcf_id = n.atcf_id "
        "  AND p.iso3 = n.iso3 "
        "  AND p.wind_speed_kt = n.wind_speed_kt "
        "  AND p.valid_time = n.valid_time "
        "GROUP BY n.iso3, n.pcode, n.admin_level, n.wind_speed_kt"
    )

    # ── ADAM snap: aggregated to FM via the (toggle-driven) lookup ──
    _adam_sql = text(
        "WITH adam_event_rows AS ("
        "  SELECT DISTINCT ON "
        "    (ae.admin_level, ae.iso3, lower(ae.admin_name), ae.wind_speed_kt) "
        "    ae.admin_level, ae.iso3, ae.admin_name, "
        "    lower(ae.admin_name) AS admin_name_lc, "
        "    ae.wind_speed_kt, ae.pop_exposed "
        "  FROM storms.adam_exposure ae "
        "  JOIN storms.storm_id_lookup sl "
        "    ON sl.adam_eventid = ae.adam_eventid "
        "  WHERE sl.gdacs_eventid = :eid "
        "    AND ae.admin_level <= 1 "
        "  ORDER BY ae.admin_level, ae.iso3, lower(ae.admin_name), "
        "           ae.wind_speed_kt, ae.valid_time DESC"
        ") "
        "SELECT aer.admin_level, aer.iso3, lk.fm_pcode, "
        "  aer.wind_speed_kt, "
        "  SUM(aer.pop_exposed) AS adam_pop, "
        "  string_agg(aer.admin_name, ', ' ORDER BY aer.admin_name) "
        "    AS adam_admins, "
        "  COUNT(DISTINCT aer.admin_name) AS n_adam_admins, "
        "  string_agg(DISTINCT lk.caveat_note, ' | ' "
        "    ORDER BY lk.caveat_note) AS adam_caveat_note "
        "FROM adam_event_rows aer "
        "JOIN storms.adam_fm_lookup lk "
        "  ON lk.iso3 = aer.iso3 "
        "  AND lk.admin_level = aer.admin_level "
        "  AND lower(lk.adam_admin_name) = aer.admin_name_lc "
        "WHERE lk.fm_pcode IS NOT NULL "
        "GROUP BY aer.admin_level, aer.iso3, lk.fm_pcode, "
        "         aer.wind_speed_kt"
    )

    # ── FM dim: pcode → name from the canonical FM lookup ──────────
    _fm_dim_sql = text(
        "SELECT DISTINCT admin_level, fm_pcode, fm_name "
        f"FROM {gdacs_lookup_table}"
    )

    _g = pd.read_sql(_gdacs_sql, engine, params={"eid": _eid})
    _n = pd.read_sql(_nhc_sql, engine, params={"eid": _eid})
    _a = pd.read_sql(_adam_sql, engine, params={"eid": _eid})
    _fm_dim = pd.read_sql(_fm_dim_sql, engine)

    if _g.empty and _n.empty and _a.empty:
        _out = mo.md("_no exposure rows from GDACS / NHC / ADAM for this event_")
    else:
        _key = ["admin_level", "iso3", "fm_pcode", "wind_speed_kt"]

        # Outer merge: every FM key from any source surfaces as a row.
        # Orphan rows (fm_pcode IS NULL) don't merge to NHC / ADAM —
        # they stay as standalone rows on the GDACS-only side.
        _df = (
            _g.merge(_n, on=_key, how="outer")
              .merge(_a, on=_key, how="outer")
              .merge(_fm_dim, on=["admin_level", "fm_pcode"], how="left")
        )

        def _row_status(r):
            has_g = pd.notna(r.get("gdacs_pop")) or (
                pd.notna(r.get("n_gdacs_admins"))
                and r.get("n_gdacs_admins", 0) > 0
            )
            has_n = pd.notna(r.get("nhc_pop"))
            has_a = pd.notna(r.get("adam_pop"))
            if pd.isna(r["fm_pcode"]):
                return "❌ orphan"
            if has_g and r.get("n_gdacs_admins", 0) > 1:
                return (
                    f"⚠️ aggregated "
                    f"({int(r['n_gdacs_admins'])} GDACS→1 FM)"
                )
            if has_g and pd.notna(r.get("gdacs_caveat_note")):
                return "⚠️ caveat"
            if has_g:
                return "✅ clean 1:1"
            if has_a and not has_n:
                return "⚠️ ADAM-only"
            if has_n:
                return "✅ clean 1:1"
            return "❓"

        _df["status"] = _df.apply(_row_status, axis=1)

        _out_cols = [
            "status", "admin_level", "iso3", "fm_pcode", "fm_name",
            "n_gdacs_admins", "gdacs_admins", "wind_speed_kt",
            "gdacs_pop", "nhc_pop", "adam_pop",
            "n_adam_admins", "adam_admins",
            "gdacs_caveat_note", "adam_caveat_note",
        ]
        _df = _df.reindex(columns=_out_cols)

        _df = _df.sort_values(
            ["iso3", "admin_level", "fm_pcode", "wind_speed_kt"],
            na_position="last",
        ).reset_index(drop=True)

        _n_orphan = int((_df["status"] == "❌ orphan").sum())
        _n_aggregated = int(
            _df["status"].str.startswith("⚠️ aggregated").sum()
        )
        _n_caveat = int((_df["status"] == "⚠️ caveat").sum())
        _n_adam_only = int((_df["status"] == "⚠️ ADAM-only").sum())
        _n_clean = int((_df["status"] == "✅ clean 1:1").sum())
        _n_adam_attached = int(_df["adam_pop"].notna().sum())

        _out = mo.vstack([
            mo.md(
                f"### FM ↔ multi-source matching — GDACS event "
                f"`{cmp_storm.value}`\n\n"
                f"**{_n_orphan}** orphan (GDACS admin, no FM match) · "
                f"**{_n_aggregated}** aggregated (N GDACS→1 FM) · "
                f"**{_n_caveat}** with GDACS caveat · "
                f"**{_n_adam_only}** ADAM-only (no GDACS, no NHC) · "
                f"**{_n_clean}** clean · "
                f"**{_n_adam_attached}** rows with ADAM pop · "
                f"{len(_df)} rows total\n\n"
                f"_View is **FM-centric**, built by outer-merging the "
                f"GDACS, NHC, and ADAM per-source snapshots on "
                f"`(admin_level, iso3, fm_pcode, wind_speed_kt)`. "
                f"Orphans (`fm_pcode = NULL`) are GDACS exposures with "
                f"no FM lookup match. ADAM-only rows surface FM units "
                f"where only ADAM reported — useful for validating "
                f"the adam_fm_lookup caveats._"
            ),
            mo.ui.table(_df, page_size=80, selection=None),
        ])
    _out


if __name__ == "__main__":
    app.run()
