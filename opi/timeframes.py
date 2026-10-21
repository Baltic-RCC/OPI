import calendar
import logging
import pandas as pd
import isodate
from datetime import datetime, timedelta, time, timezone
from typing import List
from opi.outages import query_elk_uap

logger = logging.getLogger(__name__)


def get_run_time(set_run_time: str, iso_offset: str) -> datetime:
    """Run time: RUN_TIME when set (rerun of old data), otherwise now; DATE_OFFSET added to both."""
    run_time = datetime.fromisoformat(set_run_time.strip()) if set_run_time.strip() else datetime.now()
    if run_time.tzinfo is not None:
        run_time = run_time.astimezone(timezone.utc).replace(tzinfo=None)

    return run_time + isodate.parse_duration(iso_offset)


def get_uap_time(run_time: datetime, time_horizon: str, first_timestamp: datetime) -> datetime:
    """Time the UAP batch is taken at: the run time, or the start of the assessed period when that is earlier, so a
    rerun of old data assesses the plan of that period, not the latest batch (which covers the coming period)."""
    if time_horizon == 'YR':
        period_start = datetime(first_timestamp.year, 1, 1)
    else:
        period_start = datetime.combine(first_timestamp.date(), time())

    return min(run_time, period_start)


def calculate_critical_timestamps(uap_df: pd.DataFrame, candidates: List[pd.Timestamp],
                                  properties: dict) -> List[pd.Timestamp]:
    """Candidates whose active outages reach a limit; an outage set already checked in the same season is skipped."""

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
        if outage_set in season_sets:
            continue
        season_sets.add(outage_set)

        # Elements counted once by EIC, also when reported in several rows
        area_tieline_count = date_outages[date_outages['asset_type'].isin(['TIE'])].groupby('Area')['eic'].nunique().max()
        area_line_count = date_outages[date_outages['asset_type'].isin(['LINE'])].groupby('Area')['eic'].nunique().max()
        region_total_line_count = date_outages[date_outages['asset_type'].isin(['TIE', 'LINE'])]['eic'].nunique()
        area_outage_count = date_outages.groupby('Area')['eic'].nunique().max()
        region_total_outage_count = date_outages['eic'].nunique()
        generation = date_outages[date_outages['asset_type'].isin(['GEN', 'PROD'])]
        unavailable_generation = pd.to_numeric(generation['Unavailable_capacity'], errors='coerce').groupby(
            generation['eic']).max().sum()

        # Check if new outage set can be considered critical timestamp
        if (area_tieline_count >= int(properties['Area_tieline_limit'])
                or area_line_count >= int(properties['Area_line_limit'])
                or region_total_line_count >= int(properties['Region_total_line_limit'])
                or area_outage_count >= int(properties['Area_outages_limit'])
                or region_total_outage_count >= int(properties['Region_total_outages_limit'])
                or unavailable_generation >= float(properties['Unavailable_generation_limit'])):
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


def get_opi_timestamps(run_time: datetime, timestamps_per_day: list, time_horizon: str, set_timestamps=None,
                       uap_query_offset: str = None) -> List[pd.Timestamp]:
    # TODO update logic to be fool proof
    if set_timestamps:
        critical_timestamps = parse_set_timestamps(set_timestamps, timestamps_per_day)
    elif time_horizon == 'WK':
        critical_timestamps = get_opi_1w_timestamps(run_time, timestamps_per_day)
    elif time_horizon == 'MO':
        critical_timestamps = get_opi_1m_timestamps(run_time, timestamps_per_day)
    elif time_horizon == 'YR':
        critical_timestamps = get_opi_yr_timestamps(run_time, timestamps_per_day, uap_query_offset)
    else:
        critical_timestamps = None

    return critical_timestamps


def get_opi_1w_timestamps(run_time: datetime, timestamps_per_day: list):

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


def get_opi_1m_timestamps(run_time: datetime, hours_per_day: list):

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


def get_opi_yr_timestamps(run_time: datetime, timestamps_per_day: list, uap_query_offset: str = None) -> List[pd.Timestamp]:
    """Y-1 timestamps: critical timestamps of the target year from the latest Y-1 UAP."""
    # Imported on first Y-1 use: its config echo then reaches opi-logs
    from config.opi.opi_yr_criteria import opi_yr_ct_properties

    year = get_opi_yr_year(run_time)
    logger.info(f"OPI Y-1 year: {year}")
    set_hours = [time(hour=int(hour.split(':')[0]), minute=int(hour.split(':')[1])) for hour in timestamps_per_day]
    candidates = sorted(pd.Timestamp(datetime.combine(day, h))
                        for day in pd.date_range(f'{year}-01-01', f'{year}-12-31', freq='D').date for h in set_hours)

    uap_df = query_elk_uap(time_horizon='YR', uap_time=get_uap_time(run_time, 'YR', datetime(year, 1, 1)),
                           uap_query_offset=uap_query_offset or get_uap_offset_time(None, 'YR'))
    if uap_df is None:
        logger.error("No Y-1 UAP batch found, timestamps not calculated")
        return []

    return calculate_critical_timestamps(uap_df, candidates, opi_yr_ct_properties)


def get_input_dir(time_horizon: str):

    mapping_dict = {
        "WK" : "OPI/inputs/W-1",
        "MO" : "OPI/inputs/M-1",
        "YR" : "OPI/inputs/Y-1",
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

def get_opi_yr_year(run_time: datetime) -> int:
    """Y-1 target year: OPI_YEAR when set, otherwise the year after the run time."""
    from config.opi.opi_yr_criteria import opi_yr_ct_properties

    if str(opi_yr_ct_properties['OPI_Year']).strip():
        return int(opi_yr_ct_properties['OPI_Year'])
    return run_time.year + 1


def get_yr_season(timestamp: datetime) -> tuple:
    """Y-1 season of a timestamp as (year, first month, last month)."""
    from config.opi.opi_yr_criteria import seasons

    first, last = next(season for season in seasons if season[0] <= timestamp.month <= season[1])
    return timestamp.year, first, last


def select_yr_models(models: List[dict], scenario_timestamp: str) -> List[dict]:
    """Y-1 models in the timestamp's season, only those at the scenario time nearest to the timestamp."""
    timestamp = pd.Timestamp(scenario_timestamp)
    season = get_yr_season(timestamp)
    in_season = []
    for model in models:
        model_time = pd.Timestamp(model['@scenario_timestamp'])
        if model_time.tzinfo is not None:
            model_time = model_time.tz_convert(None)
        if get_yr_season(model_time) == season:
            in_season.append((model_time, model))
    if not in_season:
        logger.error(f"No Y-1 model in months {season[1]}-{season[2]} of {season[0]} for {scenario_timestamp}")
        return []
    nearest = min((model_time for model_time, _ in in_season), key=lambda model_time: abs(model_time - timestamp))

    return [model for model_time, model in in_season if model_time == nearest]


def get_opi_period(run_time: datetime, time_horizon: str) -> tuple:
    """First and last day of the assessed period: next OPC week, next month or the Y-1 target year."""
    if time_horizon == 'YR':
        year = get_opi_yr_year(run_time)
        return pd.Timestamp(f'{year}-01-01'), pd.Timestamp(f'{year}-12-31')
    days = get_opi_timestamps(run_time=run_time, timestamps_per_day=['00:00'], time_horizon=time_horizon)

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
