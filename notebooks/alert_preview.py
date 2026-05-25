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
def _matching_demo(mo, mode, cmp_storm, engine, pd, text):
    mo.stop(mode.value != "Admin 1 comparison")
    mo.stop(cmp_storm.value is None)

    # Three CTEs:
    #  1. event_rows  — latest GDACS snapshot per (admin_level,
    #     gdacs_admin_code, wind_speed_kt) for this event. Works for
    #     both single-snapshot (pre-2025) and multi-episode (2025+)
    #     events.
    #  2. event_atcf  — the ATCF id this GDACS event resolves to (if
    #     any) via storm_id_lookup. Used to pull NHC numbers below.
    #  3. nhc_max     — max-across-advisories NHC fcast exposure per
    #     (pcode, admin_level, wind_speed_kt) for this storm.
    # Then LEFT JOIN the lookup (orphans surface as fm_pcode NULL) and
    # LEFT JOIN nhc_max on the FM pcode (NHC's pcode is FM-keyed).
    # GDACS pop is null on many rows — that's a data quirk (admin in
    # buffer, no pop attributed), not a matching failure.
    _sql = text(
        "WITH event_rows AS ("
        "  SELECT DISTINCT ON (admin_level, gdacs_admin_code, wind_speed_kt) "
        "    admin_level, iso3, gdacs_admin_code, "
        "    admin_name AS gdacs_admin_name, "
        "    wind_speed_kt, pop_exposed "
        "  FROM storms.gdacs_exposure "
        "  WHERE gdacs_eventid = :eid "
        "  ORDER BY admin_level, gdacs_admin_code, wind_speed_kt, "
        "           valid_time DESC"
        "), event_atcf AS ("
        "  SELECT atcf_id FROM storms.storm_id_lookup "
        "  WHERE gdacs_eventid = :eid AND atcf_id IS NOT NULL"
        "), nhc_max AS ("
        "  SELECT n.pcode, n.admin_level, n.wind_speed_kt, "
        "    MAX(n.pop_exposed) AS nhc_pop "
        "  FROM storms.nhc_tracks_fcast_exposure n "
        "  JOIN event_atcf ea ON ea.atcf_id = n.atcf_id "
        "  GROUP BY n.pcode, n.admin_level, n.wind_speed_kt"
        ") "
        "SELECT e.admin_level, e.iso3, e.gdacs_admin_code, "
        "  e.gdacs_admin_name, e.wind_speed_kt, "
        "  e.pop_exposed AS gdacs_pop, "
        "  lk.fm_pcode, lk.fm_name, lk.caveat_note, "
        "  nm.nhc_pop "
        "FROM event_rows e "
        "LEFT JOIN storms.gdacs_fm_lookup lk "
        "  ON lk.iso3 = e.iso3 "
        "  AND lk.admin_level = e.admin_level "
        "  AND lk.gmi_admin = e.gdacs_admin_code "
        "LEFT JOIN nhc_max nm "
        "  ON nm.pcode = lk.fm_pcode "
        "  AND nm.admin_level = e.admin_level "
        "  AND nm.wind_speed_kt = e.wind_speed_kt "
        "ORDER BY e.admin_level, e.iso3, e.gdacs_admin_code"
    )
    _df = pd.read_sql(_sql, engine, params={"eid": cmp_storm.value})

    if _df.empty:
        _out = mo.md("_no GDACS exposure rows for this event_")
    else:
        # FM-CENTRIC VIEW: one row per (admin_level, iso3, fm_pcode,
        # wind_speed_kt). gdacs_pop is SUMMED across all GDACS admins
        # that aggregate to the same FM unit (e.g. PRI: 8 senatorial
        # districts → single FM Puerto Rico polygon).
        #
        # This is the question consumers of the canonical lookup
        # actually ask at runtime: "for FM unit X, what's the total
        # exposure?" The GDACS-side detail is preserved as
        # `n_gdacs_admins` + `gdacs_admins` (comma-listed codes).
        #
        # Orphans (no fm_pcode match) can't be aggregated — kept as
        # one row per gdacs_admin_code in a separate orphan section.

        _orphan = _df[_df["fm_pcode"].isna()].copy()
        _matched = _df[_df["fm_pcode"].notna()].copy()

        if not _matched.empty:
            _matched_agg = (
                _matched.groupby(
                    ["admin_level", "iso3", "fm_pcode", "fm_name",
                     "wind_speed_kt"],
                    dropna=False,
                ).agg(
                    n_gdacs_admins=("gdacs_admin_code", "nunique"),
                    gdacs_admins=("gdacs_admin_code",
                                  lambda s: ", ".join(sorted(s.unique()))),
                    gdacs_pop=("gdacs_pop", "sum"),
                    nhc_pop=("nhc_pop", "first"),
                    caveat_note=(
                        "caveat_note",
                        lambda s: s.dropna().iloc[0]
                        if s.notna().any() else None,
                    ),
                ).reset_index()
            )
            # ``sum`` on an all-NaN series gives 0; surface as NaN so
            # the UI shows "no pop attributed" instead of a fake zero.
            _all_null_mask = (
                _matched.groupby(
                    ["admin_level", "iso3", "fm_pcode", "fm_name",
                     "wind_speed_kt"], dropna=False,
                )["gdacs_pop"].apply(lambda s: s.isna().all())
                .reset_index(name="_all_null")
            )
            _matched_agg = _matched_agg.merge(
                _all_null_mask,
                on=["admin_level", "iso3", "fm_pcode", "fm_name",
                    "wind_speed_kt"],
                how="left",
            )
            _matched_agg.loc[
                _matched_agg["_all_null"], "gdacs_pop"
            ] = pd.NA
            _matched_agg = _matched_agg.drop(columns="_all_null")

            def _status_matched(n, caveat):
                if n > 1:
                    return f"⚠️ aggregated ({n} GDACS→1 FM)"
                if pd.notna(caveat):
                    return "⚠️ pre-split caveat"
                return "✅ clean 1:1"

            _matched_agg["status"] = [
                _status_matched(n, cv) for n, cv in zip(
                    _matched_agg["n_gdacs_admins"],
                    _matched_agg["caveat_note"],
                )
            ]
        else:
            _matched_agg = pd.DataFrame(columns=[
                "admin_level", "iso3", "fm_pcode", "fm_name",
                "wind_speed_kt", "n_gdacs_admins", "gdacs_admins",
                "gdacs_pop", "nhc_pop", "caveat_note", "status",
            ])

        # Orphans: keep one row per (gdacs_admin_code, wind_speed_kt).
        # No FM match → no NHC join possible.
        if not _orphan.empty:
            _orphan = _orphan.assign(
                fm_pcode=pd.NA,
                fm_name=pd.NA,
                n_gdacs_admins=1,
                gdacs_admins=_orphan["gdacs_admin_code"],
                nhc_pop=pd.NA,
                status="❌ orphan",
            )[[
                "admin_level", "iso3", "fm_pcode", "fm_name",
                "wind_speed_kt", "n_gdacs_admins", "gdacs_admins",
                "gdacs_pop", "nhc_pop", "caveat_note", "status",
            ]]

        _df = pd.concat([_orphan, _matched_agg], ignore_index=True)

        _out_cols = [
            "status", "admin_level", "iso3", "fm_pcode", "fm_name",
            "n_gdacs_admins", "gdacs_admins", "wind_speed_kt",
            "gdacs_pop", "nhc_pop", "caveat_note",
        ]
        _df = _df[_out_cols]

        # Orphans first, then aggregated/caveat (most interesting for
        # review), then clean 1:1.
        _rank = _df["status"].apply(lambda s: (
            0 if s.startswith("❌") else
            1 if s.startswith("⚠️") else 2
        ))
        _df = (
            _df.assign(_rank=_rank)
               .sort_values(["_rank", "admin_level", "iso3",
                             "fm_pcode", "wind_speed_kt"])
               .drop(columns="_rank")
               .reset_index(drop=True)
        )

        # Summary: unique FM-unit count per status bucket. For orphans,
        # count gdacs_admins (each is its own unmatched item).
        _matched_units = _df[~_df["status"].str.startswith("❌")][
            ["admin_level", "iso3", "fm_pcode", "status"]
        ].drop_duplicates()
        _n_o = int(_df["status"].str.startswith("❌").sum())
        _n_c = int(
            _matched_units["status"].str.startswith("⚠️").sum()
        )
        _n_k = int(
            (_matched_units["status"] == "✅ clean 1:1").sum()
        )

        _out = mo.vstack([
            mo.md(
                f"### FM ↔ GDACS matching — GDACS event "
                f"`{cmp_storm.value}`\n\n"
                f"**{_n_o}** orphan (GDACS admin with no FM match)  ·  "
                f"**{_n_c}** with caveat (aggregated or pre-split)  ·  "
                f"**{_n_k}** clean 1:1  ·  {len(_df)} rows total\n\n"
                f"_View is **FM-centric**: one row per "
                f"`(admin_level, fm_pcode, wind_speed_kt)`. When "
                f"multiple GDACS admins map to the same FM unit "
                f"(`n_gdacs_admins > 1`), their pop_exposed values are "
                f"**summed** into `gdacs_pop` and the contributing "
                f"GDACS codes are listed in `gdacs_admins`. `gdacs_pop` "
                f"may be NULL when GDACS includes admins in the buffer "
                f"but attributes no population — data quirk, not a "
                f"matching failure. `nhc_pop` is NULL when our NHC "
                f"pipeline has no rows at this fm_pcode (older storm, "
                f"non-Atlantic basin, or no NHC adm1 coverage)._"
            ),
            mo.ui.table(_df, page_size=80, selection=None),
        ])
    _out


if __name__ == "__main__":
    app.run()
