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
    from datetime import datetime, timedelta
    from sqlalchemy import text
    from pipelines.run_alert import generate_alert_html, send_test_alert
    return datetime, generate_alert_html, mo, send_test_alert, stratus, text, timedelta


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
def _storm_selector(mo, storm_options):
    storm = mo.ui.dropdown(options=storm_options, label="Storm", searchable=True)
    storm


@app.cell
def _time_selector(mo, storm, engine, text, timedelta):
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
def _preview(mo, generate_btn, issued_time, engine, generate_alert_html, datetime):
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
def _send(mo, send_btn, issued_time, engine, send_test_alert, datetime):
    mo.stop(not send_btn.value)
    mo.stop(issued_time.value is None)
    _issued_time_dt = datetime.strptime(issued_time.value, "%Y-%m-%dT%H")
    try:
        _status = send_test_alert(engine, _issued_time_dt)
        mo.callout(mo.md(f"**Sent.** {_status}"), kind="success")
    except Exception as _e:
        mo.callout(mo.md(f"**Error:** {_e}"), kind="danger")


if __name__ == "__main__":
    app.run()
