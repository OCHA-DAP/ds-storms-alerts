"""Create per-country listmonk mailing lists for all countries ever in storm monitoring.

Also creates two aggregate lists:
- "Storm Alerts - All Countries": receives every alert regardless of which countries
- "Storm Alerts - LAC": receives alerts that include at least one LAC country

Idempotent — safe to re-run. Creates lists only for entries that don't already exist.
All lists are tagged 'ds-storms-alerts' so the pipeline can fetch them in one query.

Usage:
    uv run python pipelines/setup_country_lists.py [--dry-run]
"""

import argparse
import os

import ocha_stratus as stratus
from ocha_relay.listmonk import ListmonkClient

from src.constants import COUNTRY_LIST_NAME_PREFIX, COUNTRY_LIST_TAG
from src.data import fetch_all_monitored_countries, load_adm1_boundaries


def _admin_client() -> ListmonkClient:
    return ListmonkClient(
        base_url=os.environ["DSCI_LISTMONK_BASE_URL"].rstrip("/"),
        username=os.environ["DSCI_LISTMONK_ADMIN_API_USERNAME"],
        password=os.environ["DSCI_LISTMONK_ADMIN_API_KEY"],
    )

_AGGREGATE_LISTS = [
    {
        "name": f"{COUNTRY_LIST_NAME_PREFIX} - All Countries",
        "tag": "aggregate:all",
        "description": "Receives every storm alert regardless of which countries are included.",
    },
    {
        "name": f"{COUNTRY_LIST_NAME_PREFIX} - LAC",
        "tag": "aggregate:lac",
        "description": (
            "Receives alerts that include at least one Caribbean, "
            "Central American, or South/North American country (excl. USA and Canada)."
        ),
    },
]


def main(dry_run: bool = False) -> None:
    engine = stratus.get_engine(stage="dev")
    client = _admin_client()

    # --- Aggregate lists ---
    existing_all = client.fetch_all_lists(tag=COUNTRY_LIST_TAG)
    existing_agg_tags = {
        tag
        for lst in existing_all
        for tag in lst.get("tags", [])
        if tag.startswith("aggregate:")
    }

    print("Aggregate lists:")
    for agg in _AGGREGATE_LISTS:
        if agg["tag"] in existing_agg_tags:
            print(f"  SKIP  {agg['tag']} (already exists)")
        elif dry_run:
            print(f"  DRY   would create {agg['name']!r} [{agg['tag']}]")
        else:
            list_id = client.create_list(
                name=agg["name"],
                tags=[COUNTRY_LIST_TAG, agg["tag"]],
            )
            print(f"  CREATE list {list_id}: {agg['name']!r} [{agg['tag']}]")

    # --- Per-country lists ---
    iso3s = fetch_all_monitored_countries(engine)
    print(f"\nPer-country lists ({len(iso3s)} countries):")

    existing_iso3s: dict[str, int] = {}
    for lst in existing_all:
        for tag in lst.get("tags", []):
            if tag.startswith("iso3:"):
                existing_iso3s[tag[5:]] = lst["id"]

    adm1 = load_adm1_boundaries(iso3s)

    def _mode_name(x):
        # All-NaN group → empty value_counts → None (avoids IndexError); callers
        # fall back to the iso3 code.
        vc = x.value_counts()
        return vc.index[0] if len(vc) else None

    iso3_to_name: dict[str, str] = {
        k: v
        for k, v in adm1.groupby("iso_3")["adm0_name"].agg(_mode_name).to_dict().items()
        if v is not None
    }

    created = 0
    skipped = 0
    for iso3 in iso3s:
        if iso3 in existing_iso3s:
            print(f"  SKIP  {iso3:3s} → list {existing_iso3s[iso3]}")
            skipped += 1
            continue
        country_name = iso3_to_name.get(iso3, iso3)
        list_name = f"{COUNTRY_LIST_NAME_PREFIX} - {country_name}"
        if dry_run:
            print(f"  DRY   {iso3:3s} → would create {list_name!r}")
        else:
            list_id = client.create_list(
                name=list_name,
                tags=[COUNTRY_LIST_TAG, f"iso3:{iso3}"],
            )
            print(f"  CREATE {iso3:3s} → list {list_id}: {list_name!r}")
        created += 1

    noun = "would create" if dry_run else "created"
    print(f"\nDone. {noun} {created} new country lists, skipped {skipped} existing.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be created without making any API calls.",
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run)
