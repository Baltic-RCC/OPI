import pandas as pd
import pypowsybl as pp
import datetime
import isodate
import logging
from pathlib import Path
from typing import Optional, List
from loadflow_engine.helper import get_network_elements_by_ids
from opi.integrations import get_docs_by_query, ELK_UAP_INDEX
from opi.region import EXTERNAL_AREAS, UAP_EXCLUDED_EICS, UAP_EXCLUDED_NAMES

logger = logging.getLogger(__name__)

# Mapped UAP outages: grid element ID with the UAP Area and outage name
UAP_ID_COLUMNS = ['grid_element_id', 'Area', 'name']
# UAP fields the assessment reads
UAP_COLUMNS = ['start_date', 'end_date', 'eic', 'mrid', 'name', 'Area', 'asset_type']

def run_outage_crosscheck(network: pp.network,
                          element_df: pd.DataFrame,
                          contingency_list : list,# Original element DF from Excel
                          outages: pd.DataFrame,  # UAP batch
                          set_timestamp: Optional[datetime.datetime] = None
                          ) -> pd.DataFrame:
    """
    Runs the outage crosscheck process.

    Args:
        network: The pypowsybl network object.
        element_df: DataFrame containing the list of elements to assess (from Excel).
        contingency_list: List of contingencies
        outages: containing parsed outage data (from UAP).
        set_timestamp: Optional specific timestamp for the assessment.

    Returns:
        DataFrame with assessment results including inconsistency flags.
    """
    outages_df = outages.copy() if outages is not None else pd.DataFrame()
    if not isinstance(element_df, pd.DataFrame) or element_df.empty:
         logger.error("Input element_df is invalid or empty.")
         return pd.DataFrame() # Return empty DataFrame

    # Ensure element_df has required columns
    required_element_cols = ['eic', 'grid_element_id', 'secured_for_region']
    if not all(col in element_df.columns for col in required_element_cols):
         missing = [col for col in required_element_cols if col not in element_df.columns]
         logger.error(f"Element DF missing required columns: {missing}")
         raise ValueError(f"Element DF missing required columns: {missing}")

    # Filter elements
    element_df_filtered = element_df[~element_df['secured_for_region'].isin(EXTERNAL_AREAS)].copy()

    # Determine Scenario Time
    if set_timestamp:

        model_scenario_time = pd.Timestamp(set_timestamp)
        # Ensure timezone (assuming network.case_date is UTC)
        if model_scenario_time.tzinfo is None:
            model_scenario_time = model_scenario_time.replace(tzinfo=datetime.timezone.utc)
    else:
        model_scenario_time = network.case_date # Assume this is timezone-aware UTC
        if model_scenario_time is None:
            logger.error("Network case_date is None and no set_timestamp provided.")
            raise ValueError("Cannot determine scenario time.")
        if model_scenario_time.tzinfo is None:
            # If case_date is naive, assume UTC based on typical usage
            logger.warning("Network case_date is timezone naive, assuming UTC.")
            model_scenario_time = model_scenario_time.replace(tzinfo=datetime.timezone.utc)

    logger.info("Crosschecking outages")

    # Get relevant outages for the specific time
    relevant_outages_df = get_relevant_outages(outages_df, model_scenario_time)

    # Map relevant outages to grid IDs using the filtered element list
    mapped_uap_outages = get_outage_grid_id(relevant_outages_df, element_df_filtered, contingency_list)
    # Elements of external areas stay as modelled by design
    external_ids = element_df.loc[element_df['secured_for_region'].isin(EXTERNAL_AREAS), 'grid_element_id'].astype(str)
    mapped_uap_outages = mapped_uap_outages[~mapped_uap_outages['grid_element_id'].isin(external_ids)]

    # Pass the filtered element list and mapped IDs to the assessment function
    result_assessment_df = outage_assessment(network, element_df_filtered, mapped_uap_outages['grid_element_id'],
                                             uap_outages=mapped_uap_outages)
    result_assessment_df = result_assessment_df.rename(columns={'secured_for_region': 'area', 'eq_type': 'type', 'grid_element_id': 'equipment'})

    return result_assessment_df


def get_relevant_outages(outages_df: pd.DataFrame, model_scenario_time: datetime.datetime) -> pd.DataFrame:

    if outages_df is None or outages_df.empty:
        logger.warning("Received empty or None outages DataFrame for relevance check.")
        return pd.DataFrame()

    if not isinstance(model_scenario_time, datetime.datetime):
        logger.error(f"Invalid model_scenario_time type: {type(model_scenario_time)}")
        return pd.DataFrame()

    if model_scenario_time.tzinfo is None:
        logger.warning("model_scenario_time is timezone naive, assuming UTC")
        model_scenario_time = model_scenario_time.replace(tzinfo=datetime.timezone.utc)
    elif model_scenario_time.tzinfo != datetime.timezone.utc:
        logger.warning(f"Converting model_scenario_time from {model_scenario_time.tzinfo} to UTC")
        model_scenario_time = model_scenario_time.astimezone(datetime.timezone.utc)

    # Check required columns
    required_cols = ['start_date', 'end_date']
    if not all(col in outages_df.columns for col in required_cols):
        missing_cols = [col for col in required_cols if col not in outages_df.columns]
        logger.error(f"Outage DataFrame missing required columns for relevance check: {missing_cols}")
        # Depending on strictness, either return empty or raise error
        raise ValueError(f"Missing required columns in outages_df: {missing_cols}")

    # Convert str dates to datetime format
    outages_df['start_date'] = pd.to_datetime(outages_df['start_date'], utc=True)
    outages_df['end_date'] = pd.to_datetime(outages_df['end_date'], utc=True)

    # Filtering
    relevant_mask = ((outages_df['start_date'] <= model_scenario_time) & (outages_df['end_date'] >= model_scenario_time))

    # Use .loc for boolean indexing and .copy() to avoid SettingWithCopyWarning later
    relevant_df = outages_df.loc[relevant_mask].copy()

    return relevant_df


def get_outage_grid_id(relevant_outages_df: pd.DataFrame, element_df: pd.DataFrame, contingency_list: list) -> pd.DataFrame:
    """Maps relevant outages EICs to grid element IDs using the element DataFrame; one row per grid ID with the
    outage's UAP Area and name."""

    if relevant_outages_df is None or relevant_outages_df.empty:
        logger.info("No relevant outages to map.")
        return pd.DataFrame(columns=UAP_ID_COLUMNS)

    # UAP outages ignored by name (region.properties)
    for name in UAP_EXCLUDED_NAMES:
        relevant_outages_df = relevant_outages_df[~relevant_outages_df['name'].str.contains(name, na=False, regex=False)]

    # Input Validation
    if not isinstance(relevant_outages_df, pd.DataFrame):
        logger.error("relevant_outages_df is not a DataFrame.")
        return pd.DataFrame(columns=UAP_ID_COLUMNS)
    if not isinstance(element_df, pd.DataFrame):
        logger.error("element_df is not a DataFrame.")
        return pd.DataFrame(columns=UAP_ID_COLUMNS)

    # Ensure required columns exist
    if 'eic' not in relevant_outages_df.columns:
         logger.error("Required 'eic' column missing in relevant_outages_df.")
         raise ValueError("Required 'eic' column missing in relevant_outages_df.")
    if 'eic' not in element_df.columns:
         logger.error("Required 'eic' column missing in element_df.")
         raise ValueError("Required 'eic' column missing in element_df.")
    if 'mrid' not in relevant_outages_df.columns:
         logger.error("Required 'eic' column missing in relevant_outages_df.")
         raise ValueError("Required 'eic' column missing in relevant_outages_df.")
    if 'grid_element_id' not in element_df.columns:
         logger.error("Required 'grid_element_id' column missing in element_df.")
         raise ValueError("Required 'grid_element_id' column missing in element_df.")

    try:
        # Filter ignored EICs (BRELL lines)
        outage_df_filtered = relevant_outages_df[~relevant_outages_df['eic'].isin(UAP_EXCLUDED_EICS)].copy()
        logger.debug(f"Filtered out {len(relevant_outages_df) - len(outage_df_filtered)} ignored EIC outages.")

        # --- Merge ---
        # Select only necessary columns from element_df for the merge
        eic_map = [{'eic': item.eq_eic, 'grid_element_id': item.equipment[0]} for item in contingency_list]
        element_map_df = element_df[['eic', 'grid_element_id']].drop_duplicates()
        element_map_df = pd.concat([element_map_df, pd.DataFrame(eic_map)]).drop_duplicates()
        # Null keys would match every outage without EIC
        element_map_df = element_map_df[element_map_df['eic'].notna() & (element_map_df['eic'] != '')
                                        & element_map_df['grid_element_id'].notna()]
        merged_df = outage_df_filtered.merge(
            element_map_df,
            on='eic',
            how='left',
            indicator=True
        )

        # Logging Missing
        merged_df = merged_df.replace(("None", "none", "nan", "NaN", ""), pd.NA)
        missing_outages_df = merged_df[(merged_df['_merge'] == 'left_only') & (merged_df["mrid"].isna())]
        if not missing_outages_df.empty:
             for _, row in missing_outages_df[['name']].iterrows():
                 logger.error(f"Failed to get outage mRID: {row.values.tolist()}")
        else:
             logger.info("All relevant outages mapped to grid id's")

        # Return IDs
        grid_id_series = merged_df.loc[merged_df['_merge'] == 'both', 'grid_element_id'].dropna()
        # Make sure mrid starts with '_'
        mask = (merged_df['mrid'].notna() &
                (merged_df['mrid'] != "") &
                ~merged_df['mrid'].astype(str).str.startswith("_").astype(bool))
        merged_df.loc[mask, "mrid"] = "_" + merged_df.loc[mask, "mrid"]

        grid_id_series = pd.concat([grid_id_series, merged_df['mrid'].dropna().astype(str)])
        # Unique string IDs with the Area and name of their UAP outage
        grid_ids = merged_df.loc[grid_id_series.index, ['Area', 'name']].assign(
            grid_element_id=grid_id_series.astype(str).values)

        return grid_ids[UAP_ID_COLUMNS].drop_duplicates('grid_element_id').reset_index(drop=True)

    except Exception as e:
        logger.error(f"Error during outage grid ID mapping: {e}")
        raise


def get_known_network_elements(network: pp.network, ids: List[str]) -> pd.DataFrame:
    """Network elements for ids. The lookup raises on an id missing from the model, so after a failure the ids
    are looked up one by one and missing ones skipped; fails only when no id can be looked up."""
    try:
        return get_network_elements_by_ids(network, ids)
    except Exception as error:
        logger.warning(f"Network element lookup failed ({error}), looking up ids one by one")

    found, missing = [], []
    for element_id in ids:
        try:
            found.append(get_network_elements_by_ids(network, [element_id]))
        except Exception:
            missing.append(element_id)
    if not found:
        raise RuntimeError(f"Network element lookup failed for all {len(ids)} id(s)")
    logger.error(f"{len(missing)} id(s) not found in the model, skipped: {missing}")

    return pd.concat(found)


def outage_assessment(network: pp.network,
                      element_df_view: pd.DataFrame,
                      uap_outage_ids: pd.Series,
                      uap_outages: Optional[pd.DataFrame] = None
                     ) -> pd.DataFrame:
    """Compares mapped UAP outages with CGM outages for the given elements."""

    # Input Validation
    if not isinstance(element_df_view, pd.DataFrame):
        logger.error("element_df_view is not a DataFrame for assessment.")
        # Depending on desired behavior, return original view or raise error
        return element_df_view
    if not isinstance(uap_outage_ids, pd.Series):
        logger.error("uap_outage_ids is not a Series for assessment.")
        uap_outage_ids = pd.Series(dtype=str) # Default to empty

    # Work on a copy to avoid modifying the DataFrame passed in
    assessment_df = element_df_view.copy()

    if 'grid_element_id' not in assessment_df.columns:
         logger.error("Missing 'grid_element_id' in element DataFrame for assessment.")
         raise ValueError("Missing 'grid_element_id' in element DataFrame for assessment.")

    # Ensure grid_element_id is string for consistent comparison
    assessment_df['grid_element_id'] = assessment_df['grid_element_id'].astype(str)
    uap_outage_ids = uap_outage_ids.astype(str) # Ensure IDs are strings

    # Add UAP outage flag
    assessment_df['outage_in_uap'] = assessment_df['grid_element_id'].isin(uap_outage_ids)

    # Get model outages (CGM)
    model_outage_id = pd.Series(dtype=str) # Default empty
    elements = pd.DataFrame()
    # Get unique, non-null IDs from the current view being assessed
    all_ids_in_view = assessment_df['grid_element_id'].dropna().unique().tolist()
    for item in uap_outage_ids:
        if item not in all_ids_in_view and item:
            all_ids_in_view.append(item)

    if not all_ids_in_view:
         logger.warning("No valid grid element IDs found in element_df view for checking model outages.")
    else:
        try:
            # Ids missing from the model are skipped, any other lookup failure fails the timestamp
            elements = get_known_network_elements(network, all_ids_in_view)

            if not elements.empty:
                # Find columns related to connection status (case-insensitive)
                connected_cols = elements.columns[elements.columns.str.contains('connected', case=False, na=False)]
                if not connected_cols.empty:
                     connected_flags = elements[connected_cols]
                     # Check if *any* connection status is False for a row
                     model_outages = connected_flags[connected_flags.eq(False).any(axis=1)]
                     if not model_outages.empty:
                          model_outage_id = model_outages.index.to_series().reset_index(drop=True).astype(str)
                          # logger.info(f"Found {len(model_outage_id)} elements disconnected in the CGM model.")
                     else:
                          logger.info("No elements found disconnected in the CGM model for the assessed set.")
                else:
                     logger.warning("Could not find 'connected' columns in network elements data.")
            else:
                 logger.warning("get_network_elements_by_ids returned empty DataFrame.")

        except Exception as e:
            raise RuntimeError(f"Error getting/processing network elements for CGM check: {e}") from e

    # UAP outages mapped to elements outside the assessed element list (contingency-only, mRID-only, generators)
    if not elements.empty:
        additional_elements = elements[elements.index.isin(uap_outage_ids)
                                       & ~elements.index.isin(assessment_df['grid_element_id'])].reset_index()
        additional_elements = additional_elements.rename(columns={'id': 'grid_element_id'})[['grid_element_id', 'name']]
        additional_elements['outage_in_uap'] = True
        # Area and description from the UAP outage; no type, so these are disconnected without switch handling
        if uap_outages is not None and not uap_outages.empty:
            uap_info = uap_outages.set_index('grid_element_id')
            additional_elements['secured_for_region'] = additional_elements['grid_element_id'].map(uap_info['Area'])
            additional_elements['description'] = additional_elements['grid_element_id'].map(uap_info['name'])
        if not additional_elements.empty:
            logger.info(f"Applying {len(additional_elements)} UAP outage(s) outside the assessed element list: "
                        f"{additional_elements['name'].tolist()}")
            assessment_df = pd.concat([assessment_df, additional_elements])

    # Add CGM outage flag
    assessment_df['outage_in_cgm'] = assessment_df['grid_element_id'].isin(model_outage_id)
    # Calculate inconsistency
    assessment_df['inconsistency_detected'] = assessment_df['outage_in_uap'] != assessment_df['outage_in_cgm']

    return assessment_df


def query_elk_uap(time_horizon: str, uap_time: datetime.datetime, uap_query_offset: str,
                  index: str = ELK_UAP_INDEX) -> pd.DataFrame | None:
    """Latest UAP batch of the horizon parsed by uap_time in the query window, OUT and SSS rows; None when no batch is found."""
    logger.info(f"Retrieving outages from ELK index: '{index}'")

    # uap_time is the run time, or the assessed period start for a rerun of old data
    filter_time = (uap_time + isodate.parse_duration(uap_query_offset)).strftime("%Y-%m-%dT%H:%M") + "Z"
    now = uap_time.strftime("%Y-%m-%dT%H:%M") + "Z"

    if time_horizon == 'WK':
        merge_type_list = ['week']
    elif time_horizon == 'MO':
        merge_type_list = ['week', 'month']
    elif time_horizon == 'YR':
        merge_type_list = ['year']

    query = {
        "bool": {
            "must": [
                {"exists": {"field": "name"}},
                {"terms": {"Merge": merge_type_list}},
            ],
            "filter": [{"range": {"reportParsedDate": {"lte": now, "gte": filter_time}}}],
        }
    }
    response = get_docs_by_query(index=index, query=query, size=10000, return_df=True)

    if response.empty:
        logger.warning(f"No UAP batch found in '{index}' for merge type {merge_type_list} between {filter_time} and {now}")
        return None

    # Get only latest report data
    response['reportParsedDate'] = pd.to_datetime(response['reportParsedDate'])
    response = response[response['reportParsedDate'] == response['reportParsedDate'].max()]
    logger.info(f"UAP batch {merge_type_list} parsed {response['reportParsedDate'].max()}, latest by {now}")

    return response[response['outage_type'].isin(['OUT', 'SSS'])]


def get_uap_outages(time_horizon: str, uap_time: datetime.datetime, uap_query_offset: str,
                    index: str = ELK_UAP_INDEX) -> pd.DataFrame:
    """Latest UAP batch without PROD rows; raises when no batch is found or a field the assessment reads is missing."""
    response = query_elk_uap(time_horizon=time_horizon, uap_time=uap_time,
                             uap_query_offset=uap_query_offset, index=index)
    if response is None:
        raise ValueError(f"No UAP batch found in '{index}'")
    missing = [column for column in UAP_COLUMNS if column not in response.columns]
    if missing:
        raise ValueError(f"UAP batch in '{index}' has no {missing} field(s)")

    return response[response['asset_type'] != 'PROD'].reset_index(drop=True)


def set_outage_corrections(outage_enable, outage_disable):

    outage_corrections = {
        "to_enable": {"equipment": [], "eic": [], "name": []},
        "to_disable": {"equipment": [], "eic": [], "name": []}
    }

    # Run if strings are not empty
    if not (not outage_enable.strip() and not outage_disable.strip()):

        # Sanitize inputs
        outage_enable = outage_enable.replace(" ", "")
        outage_enable = outage_enable.strip().rstrip(",")
        outage_enable = [item for item in outage_enable.split(",") if item]

        outage_disable = outage_disable.replace(" ", "")
        outage_disable = outage_disable.strip().rstrip(",")
        outage_disable = [item for item in outage_disable.split(",") if item]

        # Split outages either into mrid or name
        for outage in outage_enable:
            if outage.startswith("_"):
                outage_corrections["to_enable"]["equipment"].append(outage)
            elif outage.startswith("*"):
                outage = outage.lstrip("*")
                outage_corrections["to_enable"]["eic"].append(outage)
            else:
                outage_corrections["to_enable"]["name"].append(outage)

        for outage in outage_disable:
            if outage.startswith("_"):
                outage_corrections["to_disable"]["equipment"].append(outage)
            elif outage.startswith("*"):
                outage = outage.lstrip("*")
                outage_corrections["to_disable"]["eic"].append(outage)
            else:
                outage_corrections["to_disable"]["name"].append(outage)

    return outage_corrections
