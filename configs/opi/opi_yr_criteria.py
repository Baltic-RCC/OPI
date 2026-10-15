from opi import configs
from opi.shared.config_parser import parse_app_properties
parse_app_properties(caller_globals=globals(), path=configs.paths.opi.opi_yr_critical_timestamp)

opi_yr_ct_properties = {
    "OPI_Year": OPI_YEAR,
    'Area_tieline_limit': AREA_TIELINE_LIMIT,
    'Area_line_limit': AREA_LINE_LIMIT,
    'Region_total_line_limit': REGION_TOTAL_LINE_LIMIT,
    'Area_outages_limit': AREA_OUTAGES_LIMIT,
    'Region_total_outages_limit': REGION_TOTAL_OUTAGES_LIMIT,
    'Unavailable_generation_limit': GENERATION_LIMIT,}

# Y-1 seasons as (first month, last month); a timestamp uses the Y-1 model of its season
seasons = [(1, 2), (3, 5), (6, 8), (9, 11), (12, 12)]
