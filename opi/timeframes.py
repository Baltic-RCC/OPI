import calendar
import logging
import pandas as pd
import isodate
import config
from datetime import datetime, timedelta, time
from functools import cache
from typing import List
from opi.common import parse_app_properties
from opi.outages import query_elk_uap
from opi.integrations import INPUT_FOLDER

logger = logging.getLogger(__name__)


@cache
def get_yr_config() -> dict:
    """yr.properties, parsed on first Y-1 use so its config echo reaches opi-logs.
    YR_SEASONS as a list of (name, first day, last day)."""
    yr_config = {}
    parse_app_properties(caller_globals=yr_config, path=config.paths.opi.yr)
    seasons = []
    for season in yr_config['YR_SEASONS'].split(','):
        name, dates = season.split(':')
        first, last = dates.split('/')
        seasons.append((name.strip(), pd.Timestamp(first.strip()), pd.Timestamp(last.strip())))
    yr_config['YR_SEASONS'] = seasons
    return yr_config


def calculate_critical_timestamps(uap_df: pd.DataFrame, candidates: List[pd.Timestamp]) -> List[pd.Timestamp]:
    """Candidates whose set of active outages is new in their season; no active outages is skipped."""

    logger.info("Calculating critical timestamps")
    # TODO check if this statement still needed or remove it
    pd.set_option('future.no_silent_downcasting', True)

    critical_timestamps = []
    if uap_df.empty:
        return critical_timestamps

    # Outage times compared as UTC, as in the crosscheck
    start = pd.to_datetime(uap_df['start_date'], utc=True)
    end = pd.to_datetime(uap_df['end_date'], utc=True)
    checked_sets = {}
    for timestamp in candidates:
        utc_timestamp = timestamp.tz_localize('UTC')
        date_outages = uap_df[(start <= utc_timestamp) & (end >= utc_timestamp)]

        # Same elements out in the same season (same model) give the same result
        season_sets = checked_sets.setdefault(get_yr_season(timestamp), set())
        outage_set = frozenset(date_outages['eic'].dropna())
        if not outage_set or outage_set in season_sets:
            continue
        season_sets.add(outage_set)
        critical_timestamps.append(timestamp)

    logger.info(f"OPI Y-1 critical timestamps ({len(critical_timestamps)}): {[str(ts) for ts in critical_timestamps]}")

    return critical_timestamps


def parse_set_timestamps(timestamp_str: str, timestamps_per_day: list):

    timestamps = []
    seen = set()

    def parse_hour(h):
        parts = h.strip().split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        return time(hour, minute)

    for r in timestamp_str.split(','):
        r = r.strip()
        if not r:
            continue
        if '-' in r:
            from_str, to_str = r.strip().split('-')
        else:
            from_str = to_str = r

        from_date = datetime.strptime(from_str.strip(), "%Y_%m_%d").date()
        to_date = datetime.strptime(to_str.strip(), "%Y_%m_%d").date()

        for day in pd.date_range(start=from_date, end=to_date, freq='D'):
            day_date = day.date()
            for hour in timestamps_per_day:
                hr = parse_hour(hour)
                timestamp = datetime.combine(day_date, hr)
                if day not in seen:
                    timestamps.append(timestamp)
                    seen.add(timestamp)

    return [pd.Timestamp(dt) for dt in timestamps]


def get_opi_timestamps(iso_offset: str, timestamps_per_day: list, time_horizon: str, set_timestamps=None,
                       uap_query_offset: str = None) -> List[pd.Timestamp]:
    # TODO update logic to be fool proof
    if set_timestamps:
        critical_timestamps = parse_set_timestamps(set_timestamps, timestamps_per_day)
    elif time_horizon == 'WK':
        critical_timestamps = get_opi_1w_timestamps(iso_offset, timestamps_per_day)
    elif time_horizon == 'MO':
        critical_timestamps = get_opi_1m_timestamps(iso_offset, timestamps_per_day)
    elif time_horizon == 'YR':
        critical_timestamps = get_opi_yr_timestamps(iso_offset, timestamps_per_day, uap_query_offset)
    else:
        critical_timestamps = None

    return critical_timestamps


def get_opi_1w_timestamps(iso_duration: str, timestamps_per_day: list):

    # Get run week
    timehorizon_offset = isodate.parse_duration(iso_duration)
    now = datetime.now()

    # OLD Added 7 day time delta to not change existing code that calculated for run day for WK
    run_time = now + timehorizon_offset

    # Find upcoming Saturday
    days_until_saturday = (5 - run_time.weekday() + 7) % 7
    if days_until_saturday == 0:
        days_until_saturday = 7
    next_saturday = run_time + timedelta(days=days_until_saturday)

    # Generate dates from next Saturday to the following Friday
    next_week_dates = [datetime.combine(next_saturday + timedelta(days=i), time(hour=0, minute=0)) for i in range(7)]

    set_hours = [time(hour=int(hour.split(':')[0]), minute=int(hour.split(':')[1])) for hour in timestamps_per_day]
    timestamps = [pd.Timestamp(datetime.combine(dt, h)) for h in set_hours for dt in next_week_dates]
    logger.info(f"OPI W-1 timestamps: {str(next_week_dates[0].date())} - {str(next_week_dates[-1].date())}")

    return timestamps


def get_opi_1m_timestamps(iso_duration: str, hours_per_day: list):

    # Get current date
    timehorizon_offset = isodate.parse_duration(iso_duration)
    now = datetime.now()
    run_time = now + timehorizon_offset

    # Find first day of upcoming month
    next_month = run_time.month % 12 + 1
    year = run_time.year + (1 if next_month == 1 else 0)
    first_day_next_month = datetime(year, next_month, 1)

    # Find last day of upcoming month
    _, last_day = calendar.monthrange(year, next_month)
    last_day_next_month =  datetime(year, next_month, last_day)

    timestamps = []
    current_day = first_day_next_month
    while current_day <= last_day_next_month:
        timestamps.append(current_day)
        current_day += timedelta(days=1)

    set_hours = [time(hour=int(hour.split(':')[0]), minute=int(hour.split(':')[1])) for hour in hours_per_day]
    timestamps = [pd.Timestamp(datetime.combine(dt, h)) for h in set_hours for dt in timestamps]

    return timestamps


def get_opi_yr_timestamps(iso_offset: str, timestamps_per_day: list, uap_query_offset: str = None) -> List[pd.Timestamp]:
    """Y-1 timestamps of the target year: one per set of active outages new in its season, from the latest Y-1 UAP."""
    year = get_opi_yr_year(iso_offset)
    logger.info(f"OPI Y-1 year: {year}")
    set_hours = [time(hour=int(hour.split(':')[0]), minute=int(hour.split(':')[1])) for hour in timestamps_per_day]
    candidates = sorted(pd.Timestamp(datetime.combine(day, h))
                        for day in pd.date_range(f'{year}-01-01', f'{year}-12-31', freq='D').date for h in set_hours)
    if any(get_yr_season(candidate) is None for candidate in candidates):
        logger.error(f"YR_SEASONS does not cover the whole Y-1 year {year}, timestamps not calculated")
        return []

    uap_df = query_elk_uap(time_horizon='YR', run_time_offset=iso_offset,
                           uap_query_offset=uap_query_offset or get_uap_offset_time(None, 'YR'))
    if uap_df is None:
        logger.error("No Y-1 UAP batch found, timestamps not calculated")
        return []

    return calculate_critical_timestamps(uap_df, candidates)


def get_input_dir(time_horizon: str):

    mapping_dict = {
        "WK" : f"{INPUT_FOLDER}/W-1",
        "MO" : f"{INPUT_FOLDER}/M-1",
        "YR" : f"{INPUT_FOLDER}/Y-1",
    }
    return mapping_dict.get(time_horizon)


def get_uap_offset_time(time_offset: str, time_horizon: str):
    mapping_dict = {
        "WK": "-P2W",
        "MO": "-P2M",
        "YR": "-P6M",
    }
    if time_offset:
        uap_offset_time = time_offset
    else:
        uap_offset_time = mapping_dict.get(time_horizon)

    return uap_offset_time


def get_opi_year(timestamps: list):

    opi_year = str(min(ts.year for ts in timestamps))

    return opi_year

def get_opi_yr_year(iso_offset: str) -> int:
    """Y-1 target year: YR_TARGET_YEAR when set, otherwise the year after the run time (now + RUN_TIME_OFFSET)."""
    opi_year = get_yr_config()['YR_TARGET_YEAR']
    if str(opi_year).strip():
        return int(opi_year)
    return (datetime.now() + isodate.parse_duration(iso_offset)).year + 1


def get_yr_season(timestamp: datetime) -> str | None:
    """Name of the Y-1 season of a timestamp, None when no season in YR_SEASONS covers it."""
    day = pd.Timestamp(timestamp).normalize()
    return next((name for name, first, last in get_yr_config()['YR_SEASONS'] if first <= day <= last), None)


def select_yr_models(models: List[dict], scenario_timestamp: str) -> List[dict]:
    """Y-1 models in the timestamp's season, only those at the scenario time nearest to the timestamp."""
    timestamp = pd.Timestamp(scenario_timestamp)
    season = get_yr_season(timestamp)
    if season is None:
        logger.error(f"No Y-1 season in YR_SEASONS covers {scenario_timestamp}")
        return []
    in_season = []
    for model in models:
        model_time = pd.Timestamp(model['@scenario_timestamp'])
        if model_time.tzinfo is not None:
            model_time = model_time.tz_convert(None)
        if get_yr_season(model_time) == season:
            in_season.append((model_time, model))
    if not in_season:
        logger.error(f"No Y-1 model in season {season} for {scenario_timestamp}")
        return []
    nearest = min((model_time for model_time, _ in in_season), key=lambda model_time: abs(model_time - timestamp))

    return [model for model_time, model in in_season if model_time == nearest]


def get_opi_period(iso_offset: str, time_horizon: str) -> tuple:
    """First and last day of the assessed period: next OPC week, next month or the Y-1 target year."""
    if time_horizon == 'YR':
        year = get_opi_yr_year(iso_offset)
        return pd.Timestamp(f'{year}-01-01'), pd.Timestamp(f'{year}-12-31')
    days = get_opi_timestamps(iso_offset=iso_offset, timestamps_per_day=['00:00'], time_horizon=time_horizon)

    return min(days), max(days)


def get_opi_week_number(timestamp: pd.Timestamp) -> str:
    if timestamp.weekday() == 4:
        last_friday = timestamp - pd.Timedelta(days=7)
    else:
        days_ago = (timestamp.weekday() - 4) % 7
        last_friday = timestamp - pd.Timedelta(days=days_ago)
    week_number = last_friday.isocalendar().week

    return str(week_number)


def get_result_opi_year(timestamp: datetime, time_horizon: str, task_opi_year: str) -> str:
    """opi_year for results and logs; W-1 weeks 52/53 dated in January stay in the previous year."""
    if time_horizon == "WK":
        if get_opi_week_number(timestamp) in ['52', '53'] and timestamp.month == 1:
            return str(timestamp.year - 1)
        return str(timestamp.year)
    return task_opi_year
