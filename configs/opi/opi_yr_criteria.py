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

intervals = [
                (f'{OPI_YEAR}-01-01', f'{OPI_YEAR}-02-28', f'{OPI_YEAR}-01-20'),
                (f'{OPI_YEAR}-03-01', f'{OPI_YEAR}-05-31', f'{OPI_YEAR}-04-21'),
                (f'{OPI_YEAR}-06-01', f'{OPI_YEAR}-08-31', f'{OPI_YEAR}-07-21'),
                (f'{OPI_YEAR}-09-01', f'{OPI_YEAR}-11-30', f'{OPI_YEAR}-10-20'),
                (f'{OPI_YEAR}-12-01', f'{OPI_YEAR}-12-31', f'{OPI_YEAR}-12-15'),
            ]
