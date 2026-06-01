"""Generate docs/index.html — a static listmonk subscription form for GH Pages.

Queries listmonk for all lists tagged 'ds-storms-alerts', groups them by region,
and writes a self-contained HTML file.

Usage:
    uv run python pipelines/generate_signup_form.py
"""

from pathlib import Path

from ocha_relay.listmonk import ListmonkClient

from src.constants import COUNTRY_LIST_TAG

REGIONS: list[tuple[str, list[str]]] = [
    ("Caribbean", [
        "ABW", "AIA", "ATG", "BES", "BHS", "BLM", "BMU", "BRB",
        "CUB", "CUW", "CYM", "DMA", "DOM", "GLP", "GRD", "HTI",
        "JAM", "KNA", "LCA", "MAF", "MSR", "MTQ", "PRI", "SPM",
        "SXM", "TCA", "TTO", "VCT", "VGB", "VIR",
    ]),
    ("Central America", ["BLZ", "CRI", "GTM", "HND", "MEX", "NIC", "PAN", "SLV"]),
    ("South America",   ["COL", "GUF", "GUY", "SUR", "VEN"]),
    ("North America",   ["CAN", "USA"]),
    ("Africa & Atlantic Islands", ["CPV", "DZA", "ESH", "GMB", "MAR", "MRT", "SEN"]),
    ("Europe", [
        "BEL", "DEU", "DNK", "ESP", "FRA", "FRO", "GBR", "GGY",
        "GIB", "GRL", "IMN", "IRL", "ISL", "JEY", "LUX", "NLD", "NOR", "PRT",
    ]),
]

# Strip the "Storm Alerts - " prefix for display inside region fieldsets
_PREFIX = "Storm Alerts - "


def _short(name: str) -> str:
    return name[len(_PREFIX):] if name.startswith(_PREFIX) else name


def _checkbox(lst: dict, checked: bool = False) -> str:
    uid = lst["uuid"]
    short_id = uid[:5]
    chk = " checked" if checked else ""
    label = _short(lst["name"])
    return (
        f'<label class="cb">'
        f'<input type="checkbox" name="l" value="{uid}"{chk}> {label}'
        f'</label>'
    )


def _region_fieldset(region_name: str, iso3s: list[str], by_iso3: dict) -> str:
    items = [by_iso3[c] for c in iso3s if c in by_iso3]
    if not items:
        return ""
    rid = region_name.lower().replace(" ", "-").replace("&", "and")
    checkboxes = "\n      ".join(_checkbox(lst) for lst in items)
    return f"""
  <fieldset>
    <legend>
      {region_name}
      <button type="button" class="tog" data-region="{rid}" data-state="0">Select all</button>
    </legend>
    <div class="cb-grid" id="region-{rid}">
      {checkboxes}
    </div>
  </fieldset>"""


def generate(listmonk_url: str, all_lists: list[dict]) -> str:
    by_iso3: dict[str, dict] = {}
    aggregates: dict[str, dict] = {}

    for lst in all_lists:
        for tag in lst.get("tags", []):
            if tag.startswith("iso3:"):
                by_iso3[tag[5:]] = lst
            elif tag.startswith("aggregate:"):
                aggregates[tag[len("aggregate:"):]] = lst

    agg_all = aggregates.get("all")
    agg_lac = aggregates.get("lac")

    agg_html = ""
    if agg_all or agg_lac:
        rows = []
        if agg_all:
            rows.append(
                f'<div class="agg-row">{_checkbox(agg_all, checked=True)}'
                f'<span class="agg-desc">Receive every storm alert, regardless of region.</span></div>'
            )
        if agg_lac:
            rows.append(
                f'<div class="agg-row">{_checkbox(agg_lac)}'
                f'<span class="agg-desc">Receive alerts that include at least one Caribbean, '
                f'Central American, or South/North American country (excl. USA &amp; Canada).</span></div>'
            )
        agg_html = f"""
  <fieldset class="agg">
    <legend>Bundled subscriptions</legend>
    {"".join(rows)}
  </fieldset>"""

    region_sections = "".join(
        _region_fieldset(name, iso3s, by_iso3) for name, iso3s in REGIONS
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Storm Alerts — Subscribe</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: system-ui, -apple-system, sans-serif;
    font-size: 15px;
    color: #222;
    background: #f5f6f7;
    padding: 24px 16px 60px;
  }}
  .wrap {{
    max-width: 760px;
    margin: 0 auto;
    background: #fff;
    border-radius: 8px;
    padding: 32px 36px;
    box-shadow: 0 1px 4px rgba(0,0,0,.10);
  }}
  h1 {{ font-size: 1.5em; color: #007eb5; margin-bottom: 6px; }}
  .lead {{ color: #555; margin-bottom: 28px; line-height: 1.5; }}
  .field {{ margin-bottom: 14px; }}
  .field label {{ display: block; font-size: 0.82em; color: #666; margin-bottom: 3px; }}
  .field input {{
    width: 100%; padding: 8px 10px;
    border: 1px solid #ccc; border-radius: 4px;
    font-size: 0.95em;
  }}
  .field input:focus {{ outline: none; border-color: #007eb5; }}
  fieldset {{
    border: 1px solid #dde; border-radius: 6px;
    padding: 14px 18px; margin-bottom: 18px;
  }}
  fieldset.agg {{ background: #f0f7fb; border-color: #b3d4e8; }}
  legend {{
    font-weight: 600; font-size: 0.9em; color: #333;
    padding: 0 6px;
    display: flex; align-items: center; gap: 10px;
  }}
  .cb-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 4px 12px;
    margin-top: 10px;
  }}
  label.cb {{
    display: flex; align-items: baseline; gap: 6px;
    font-size: 0.88em; cursor: pointer; padding: 2px 0;
  }}
  label.cb input {{ width: auto; flex-shrink: 0; }}
  .agg-row {{ display: flex; align-items: baseline; gap: 10px; margin-bottom: 10px; }}
  .agg-row:last-child {{ margin-bottom: 0; }}
  .agg-row label.cb {{ font-size: 0.95em; font-weight: 500; white-space: nowrap; }}
  .agg-desc {{ font-size: 0.82em; color: #555; }}
  .tog {{
    font-size: 0.75em; font-weight: 400; color: #007eb5;
    background: none; border: 1px solid #b3d4e8;
    border-radius: 3px; padding: 1px 7px; cursor: pointer;
  }}
  .tog:hover {{ background: #e8f4fb; }}
  .submit-row {{ margin-top: 24px; }}
  button[type=submit] {{
    background: #007eb5; color: #fff;
    border: none; border-radius: 5px;
    padding: 10px 28px; font-size: 1em; cursor: pointer;
  }}
  button[type=submit]:hover {{ background: #005f8a; }}
  .note {{ margin-top: 20px; font-size: 0.78em; color: #888; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Storm Alerts</h1>
  <p class="lead">
    Subscribe to receive email alerts when tropical storms are forecast to affect
    specific countries. Select the regions or countries you want to hear about.
  </p>

  <form method="post" action="{listmonk_url}/subscription/form">
    <input type="hidden" name="nonce" />

    <div class="field">
      <label for="f-email">Email address *</label>
      <input id="f-email" type="email" name="email" required placeholder="you@example.com">
    </div>
    <div class="field">
      <label for="f-name">Name</label>
      <input id="f-name" type="text" name="name" placeholder="Optional">
    </div>
{agg_html}
{region_sections}
    <div class="submit-row">
      <button type="submit">Subscribe</button>
    </div>
    <p class="note">
      You can unsubscribe at any time using the link in any alert email.
      Alerts are sent by the
      <a href="https://centre.humdata.org" style="color:#007eb5">OCHA Centre for Humanitarian Data</a>.
    </p>
  </form>
</div>
<script>
  document.querySelectorAll('.tog').forEach(btn => {{
    btn.addEventListener('click', () => {{
      const on = btn.dataset.state === '0';
      document.querySelectorAll('#region-' + btn.dataset.region + ' input').forEach(cb => {{
        cb.checked = on;
      }});
      btn.dataset.state = on ? '1' : '0';
      btn.textContent = on ? 'Clear' : 'Select all';
    }});
  }});
</script>
</body>
</html>"""


def main() -> None:
    import os
    base_url = os.environ["DSCI_LISTMONK_BASE_URL"].rstrip("/")
    client = ListmonkClient.from_env()
    all_lists = client.fetch_all_lists(tag=COUNTRY_LIST_TAG)
    print(f"Fetched {len(all_lists)} lists tagged '{COUNTRY_LIST_TAG}'")

    html = generate(base_url, all_lists)

    out = Path(__file__).parents[1] / "docs" / "index.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Written: {out}")


if __name__ == "__main__":
    main()
