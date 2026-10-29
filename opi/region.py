import config
from opi.common import parse_app_properties

parse_app_properties(caller_globals=globals(), path=config.paths.opi.region)


def parse_list(value: str) -> list[str]:
    """Comma separated values, blanks dropped."""
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_map(value: str) -> dict[str, str]:
    """Comma separated key:value pairs."""
    pairs = (item.split(":", 1) for item in parse_list(value))
    return {key.strip(): mapped.strip() for key, mapped in pairs}


ASSESSED_AREAS = parse_list(ASSESSED_AREAS)
EXTERNAL_AREAS = parse_list(EXTERNAL_AREAS)
AREA_ALIASES = parse_map(AREA_ALIASES)
AREA_TSO_NAMES = parse_map(AREA_TSO_NAMES)
EXPORT_NAME_REPLACE = parse_map(EXPORT_NAME_REPLACE)
UAP_EXCLUDED_EICS = parse_list(UAP_EXCLUDED_EICS)
UAP_EXCLUDED_NAMES = parse_list(UAP_EXCLUDED_NAMES)
LINE_REPORT_THRESHOLD = float(LINE_REPORT_THRESHOLD)
TRANSFORMER_REPORT_THRESHOLD = float(TRANSFORMER_REPORT_THRESHOLD)


def normalize_area(name: str) -> str:
    """Area code of an area or region name in the model."""
    return AREA_ALIASES.get(name, name)
