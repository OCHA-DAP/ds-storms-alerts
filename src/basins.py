"""Basin profiles: what differs between the NHC alert and the RSMC ones
(South Pacific, South-West Indian Ocean).

pipelines/run_alert.py renders one email layout for all of them. A profile
carries the data module it reads (src.data for NHC, src.data_sp for RSMC
Nadi, src.data_swio for RSMC La Réunion), the time zone its emails use, the
wording of the forecast centre, which parts exist for that basin, and the Listmonk lists it
mails.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import ModuleType
from typing import Callable
from zoneinfo import ZoneInfo

from src import data as _data_nhc
from src import data_sp as _data_sp
from src import data_swio as _data_swio


def _time_formatter(tz: ZoneInfo) -> Callable[[datetime], str]:
    def fmt(dt: datetime) -> str:
        """Naive = UTC, shown in the basin's display time zone, e.g. 'Jun. 6, 11am'."""
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


def mfr_category(wind_kt: float) -> str:
    """Météo-France's South-West Indian Ocean class for a 10-minute mean wind
    (moderate tropical storm 34-47 kt ... very intense tropical cyclone
    >= 116 kt); empty below gale force."""
    for lo, label in ((116, "very intense tropical cyclone"),
                      (90, "intense tropical cyclone"),
                      (64, "tropical cyclone"),
                      (48, "severe tropical storm"),
                      (34, "moderate tropical storm")):
        if wind_kt >= lo:
            return label
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
    # Land a map must show before it stops zooming out (plots._expand_to_land).
    # 2.5 deg2 (~3x Puerto Rico) suits the Atlantic; Pacific island groups are
    # far smaller (Samoa ~0.24, Tonga ~0.06), so at 2.5 the South Pacific maps
    # zoomed out to New Zealand and Hawaii.
    map_min_land_deg2: float = 2.5
    # Exposure map: widen the view to take in the shaded countries (see
    # plots._FRAME_EXPOSED). Off for NHC, whose maps stay as they are.
    map_frame_exposed: bool = False
    season_of: Callable[[datetime], int] = field(default=lambda t: t.year)
    aggregate_tags: tuple = ("aggregate:all",)
    # Wind thresholds of the exposure tables, lowest first.
    wind_speeds: tuple = (34, 50, 64)
    # Thresholds that get a return period; None = all of them.
    rp_wind_speeds: tuple | None = None
    # How the history's start is written in the email ("since 2002").
    history_since: str = "2002"
    # Storms have their own forecast times (RSMC basins): "current" is each
    # storm's latest forecast, emails are sent once per set of forecasts.
    per_storm_times: bool = False
    # When set, every email of this basin (alert or monitoring) goes to the
    # lists with these tags and nowhere else: no country lists, no shared
    # monitoring list. For a basin still in an internal trial.
    recipient_tags: tuple | None = None
    # RSMC wording (per_storm_times basins).
    region_name: str = ""          # "South Pacific"
    rsmc_name: str = ""            # "RSMC Nadi"
    rsmc_long: str = ""            # "RSMC Nadi (Fiji Meteorological Service)"
    centre_short: str = ""         # "FMS": "the FMS convention", "FMS gives ..."
    id_label: str = ""             # "the FMS disturbance id"
    intro_note: str = ""           # extra sentence(s) after the intro
    # Links to the public sign-up page, example alerts and methodology guide,
    # which cover NHC only so far. Off for a basin still in an internal trial.
    public_links: bool = True
    # Island countries drawn from their own boundaries (Natural Earth 1:110m
    # drops them) and named on the maps: ((iso3, name), ...). A tuple, not a
    # dict, so the frozen profile stays hashable.
    map_islands: tuple = ()

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
    map_min_land_deg2=0.1,
    map_frame_exposed=True,
    aggregate_tags=("aggregate:sp",),
    per_storm_times=True,
    region_name="South Pacific",
    rsmc_name="RSMC Nadi",
    rsmc_long="RSMC Nadi (Fiji Meteorological Service)",
    centre_short="FMS",
    id_label="the FMS disturbance id",
    map_islands=tuple((i, _data_sp.SP_COUNTRY_NAMES.get(i)) for i in _data_sp.SP_ISO3S),
)

SWIO = BasinProfile(
    key="swio",
    data=_data_swio,
    provider="RSMC La Réunion (Météo-France)",
    # Nairobi time (East Africa Time, UTC+3, no daylight saving), the time of
    # the OCHA regional office for Southern and Eastern Africa; also the local
    # time of Madagascar, Comoros and Mayotte. Mozambique is an hour behind,
    # Mauritius, Réunion and Seychelles an hour ahead. Workbooks and the
    # send-once campaign names stay in UTC.
    tz=ZoneInfo("Africa/Nairobi"),
    tz_label="Nairobi time, UTC+3",
    tz_abbr="Nairobi time",
    wind_note="10-minute mean winds (Météo-France)",
    campaign_slug="ds-storms-alerts-swio",
    category=mfr_category,
    basin_name=lambda aid: "South-West Indian Ocean",
    has_wsp=False,
    has_gdacs_adam=False,
    has_scenarios=True,
    season_of=_data_swio.season_of,
    # Return periods from 1998/99, at 48 kt only: before 2010/11 Météo-France
    # gave too few 34 kt radii, and the modelled ones understate the big
    # storms (ds-storms-pipeline swio_radii.py and its README).
    first_season=1999,
    history_since="1998/99",
    rp_wind_speeds=(48,),
    wind_speeds=_data_swio.WIND_SPEEDS_KT,
    map_min_land_deg2=0.1,
    map_frame_exposed=True,
    aggregate_tags=("aggregate:swio",),
    per_storm_times=True,
    recipient_tags=("aggregate:swio",),
    public_links=False,
    region_name="South-West Indian Ocean",
    rsmc_name="RSMC La Réunion",
    rsmc_long="RSMC La Réunion (Météo-France)",
    centre_short="Météo-France",
    id_label="the Météo-France disturbance id",
    intro_note=(
        "Météo-France gives wind radii at 34, 48 and 64 kt, so the middle "
        "threshold here is 48 kt. Return periods are given at 48 kt only, "
        "against storms since 1998/99. Before 2010/11 Météo-France recorded "
        "few 34 and 64 kt wind radii, so past exposure at those speeds is "
        "partly modelled; the similar storms and the past storms on the "
        "charts rest on it too."
    ),
    map_islands=tuple(_data_swio.SWIO_ISLAND_NAMES.items()),
)

BASINS = {"nhc": NHC, "sp": SP, "swio": SWIO}
