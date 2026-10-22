"""Export the current season's per-storm exposure CSVs for HDX.

Writes one CSV per storm plus a manifest to the dev blob, where
OCHA-DAP/hdx-scraper-storms picks them up and publishes them to HDX. See
src/hdx_export.py for the layout and the meaning of is_final_alert.

    python pipelines/export_hdx.py --stage prod --dry-run
    python pipelines/export_hdx.py --stage prod --issued-time 2026-10-09T15 --test
"""

import argparse
import json
import logging
from datetime import datetime

import ocha_stratus as stratus

from src.data import most_recent_advisory_time
from src.hdx_export import BLOB_ROOT, BLOB_ROOT_TEST, export_season

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--issued-time",
        default=None,
        help=(
            "Current advisory (format YYYY-MM-DDTHH): storms are read up to it "
            "and is_final_alert is judged against it. Defaults to the most "
            "recent NHC advisory hour (03/09/15/21 UTC) on or before now."
        ),
    )
    parser.add_argument(
        "--stage",
        default="dev",
        choices=["dev", "prod"],
        help=(
            "ocha-stratus DB stage to read exposure from. The storms pipeline "
            "writes prod since 2026-09-22; the Databricks job passes prod."
        ),
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help=(
            f"Write under {BLOB_ROOT_TEST}/ instead of {BLOB_ROOT}/ "
            "(never published)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and log every CSV and the manifest, upload nothing.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.issued_time:
        as_of = datetime.strptime(args.issued_time, "%Y-%m-%dT%H")
    else:
        as_of = most_recent_advisory_time()
    logger.info(
        f"Starting HDX export: as_of={as_of.isoformat()} stage={args.stage} "
        f"test={args.test} dry_run={args.dry_run}"
    )
    engine = stratus.get_engine(stage=args.stage)
    manifest = export_season(
        engine, as_of, db_stage=args.stage, test=args.test, dry_run=args.dry_run
    )
    logger.info("Manifest:\n%s", json.dumps(manifest, indent=1))
