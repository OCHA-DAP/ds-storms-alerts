import argparse
import logging
import os

from src.constants import PROD_LIST_IDS, TEST_LIST_IDS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def _parse_bool_env(name: str, default: bool) -> bool:
    val = os.environ.get(name, "")
    if val == "":
        return default
    return val.strip().lower() not in ("false", "0", "no")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--issued-time",
        required=True,
        help="Issued time of the forecast, format YYYY-MM-DDTHH (e.g. 2025-01-15T12)",
    )
    return parser.parse_args()


TEST_EMAIL = _parse_bool_env("TEST_EMAIL", default=True)
DRY_RUN = _parse_bool_env("DRY_RUN", default=True)

if __name__ == "__main__":
    args = parse_args()
    issued_time = args.issued_time

    logger.info(f"Starting alert pipeline: {issued_time=} {TEST_EMAIL=} {DRY_RUN=}")

    list_ids = TEST_LIST_IDS if TEST_EMAIL else PROD_LIST_IDS

    # TODO: fetch forecasts for issued_time
    logger.info("Fetching forecasts... (placeholder)")

    # TODO: generate plots
    logger.info("Generating plots... (placeholder)")

    body = f"<p>Storm alert pipeline ran for issued time: <strong>{issued_time}</strong></p>"
    subject = f"Storm alert: {issued_time}"
    campaign_name = f"ds-storms-alerts_{issued_time}"

    if DRY_RUN:
        logger.info(
            f"DRY_RUN=True — skipping email. Would have sent: {subject!r} to lists {list_ids}"
        )
    else:
        from ocha_relay.listmonk import ListmonkClient

        client = ListmonkClient.from_env()
        cid = client.create_campaign(
            name=campaign_name,
            subject=subject,
            body=body,
            list_ids=list_ids,
        )
        logger.info(f"Created campaign {cid}: {campaign_name!r}")
        client.send_campaign(cid, skip_confirmation=True)
        logger.info(f"Sent campaign {cid}")

    logger.info("Alert pipeline complete.")
