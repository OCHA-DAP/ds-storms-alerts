"""Basin profiles: what differs between the NHC alert and the South Pacific one.

pipelines/run_alert.py renders one email layout for both. A profile carries
the data module it reads (src.data for NHC, src.data_sp for RSMC Nadi), the
time zone and wording of the forecast centre, which parts exist for that
basin, and the Listmonk lists it mails.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import ModuleType
from typing import Callable
from zoneinfo import ZoneInfo

from src import data as _data_nhc
from src import data_sp as _data_sp


def _time_formatter(tz: ZoneInfo) -> Callable[[datetime], str]:
    def fmt(dt: datetime) -> str:
        """Naive = UTC, shown in the centre's local time, e.g. 'Jun. 6, 11am'."""
        aware = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
        local = aware.astimezone(tz)
        hour = local.strftime("%I").lstrip("0") or "12"
        ampm = local.strftime("%p").lower()
        return f"{local.strftime('%b')}. {local.day}, {hour}{ampm}"
    return fmt


def saffir_simpson(wind_kt: float) -> str:
    """Saffir-Simpson label for a 1-minute wind (NHC)."""
    from src.landfall import saffir_simpson as _ss
    return _ss(wind_kt)


def australian_category(wind_kt: float) -> str:
    """Australian tropical cyclone category for a 10-minute mean wind, the
    scale FMS uses (gales 34-47 kt = Category 1 ... >= 108 kt = Category 5)."""
    for lo, cat in ((108, 5), (86, 4), (64, 3), (48, 2), (34, 1)):
        if wind_kt >= lo:
            return f"Category {cat}"
    return ""


_NHC_BASINS = {"AL": "Atlantic", "EP": "Eastern Pacific", "CP": "Central Pacific"}


@dataclass(frozen=True)
class BasinProfile:
    key: str
    data: ModuleType
    provider: str                 # in running text: "NHC has issued ..."
    tz: ZoneInfo
    tz_label: str                 # after a time in prose: "(NY time)"
    tz_abbr: str                  # after a time in the landfall panel: "ET"
    wind_note: str                # what the knots mean
    campaign_slug: str            # Listmonk campaign name prefix
    category: Callable[[float], str]
    basin_name: Callable[[str], str]
    has_wsp: bool
    has_gdacs_adam: bool
    has_scenarios: bool
    first_season: int = 2002
    season_of: Callable[[datetime], int] = field(default=lambda t: t.year)
    aggregate_tags: tuple = ("aggregate:all",)

    @property
    def format_time(self) -> Callable[[datetime], str]:
        return _time_formatter(self.tz)

    def n_seasons(self, t: datetime) -> int:
        return self.season_of(t) - self.first_season + 1

    def default_issued_time(self, engine) -> datetime:
        if self.key == "nhc":
            return self.data.most_recent_advisory_time()
        return self.data.most_recent_advisory_time(engine)


NHC = BasinProfile(
    key="nhc",
    data=_data_nhc,
    provider="NHC",
    tz=ZoneInfo("America/New_York"),
    tz_label="NY time",
    tz_abbr="ET",
    wind_note="1-minute sustained winds (NHC)",
    campaign_slug="ds-storms-alerts",
    category=saffir_simpson,
    basin_name=lambda aid: _NHC_BASINS.get(str(aid)[:2].upper(), ""),
    has_wsp=True,
    has_gdacs_adam=True,
    has_scenarios=False,
    aggregate_tags=("aggregate:all", "aggregate:lac"),
)

SP = BasinProfile(
    key="sp",
    data=_data_sp,
    provider="RSMC Nadi (Fiji Meteorological Service)",
    tz=ZoneInfo("Pacific/Fiji"),
    tz_label="Fiji time",
    tz_abbr="Fiji time",
    wind_note="10-minute mean winds (FMS)",
    campaign_slug="ds-storms-alerts-sp",
    category=australian_category,
    basin_name=lambda aid: "South Pacific",
    has_wsp=False,
    has_gdacs_adam=False,
    has_scenarios=True,
    season_of=_data_sp.season_of,
    aggregate_tags=("aggregate:sp",),
)

BASINS = {"nhc": NHC, "sp": SP}
