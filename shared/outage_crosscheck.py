import pandas as pd
import pypowsybl as pp
import datetime
import logging
from pathlib import Path
from typing import Optional, List
from loadflow_engine.helper import get_network_elements_by_ids

logger = logging.getLogger(__name__)

# Remove BRELL lines from UAP
BRELL_LINES = ['10T-LT-RU-00001W', '10T-LT-RU-00002U', '10T-LT-RU-00003S', '10T-LV-RU-00001A',
               '10T-LV-RU-00001A', '10T-BY-LT-000053', '10T-BY-LT-00001B', '10T-BY-LT-000029',
               '10T-EE-RU-00001M', '10T-EE-RU-00002K', '10T-EE-RU-00003I', '10T-BY-LT-000045']

def run_outage_crosscheck(network: pp.network,
                          element_df: pd.DataFrame,
                          contingency_list : list,# Original element DF from Excel
                          outages: pd.DataFrame,  # Parsed outages DF
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
    outages_df = pd.DataFrame([vars(out) for out in outages])
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
    element_df_filtered = element_df[element_df['secured_for_region'] != 'PL'].copy()

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
    mapped_uap_outage_ids = get_outage_grid_id(relevant_outages_df, element_df_filtered, contingency_list)

    # Pass the filtered element list and mapped IDs to the assessment function
    result_assessment_df = outage_assessment(network, element_df_filtered, mapped_uap_outage_ids)
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


def get_outage_grid_id(relevant_outages_df: pd.DataFrame, element_df: pd.DataFrame, contingency_list: list) -> pd.Series:
    """Maps relevant outages EICs to grid element IDs using the element DataFrame."""

    # TODO hack solution until Litgrid fixes eic in OPC tool
    #  this should not cause issues even after eic change
    relevant_outages_df['eic'] = relevant_outages_df['eic'].replace('41TKRUONIOHAE32Q', '41T9472798283LN-')
    relevant_outages_df = relevant_outages_df[~relevant_outages_df['name'].str.contains("LitPol Link BTB")]

    if relevant_outages_df is None or relevant_outages_df.empty:
        logger.info("No relevant outages to map.")
        return pd.Series(dtype=str) # Return empty Series

    # Input Validation
    if not isinstance(relevant_outages_df, pd.DataFrame):
        logger.error("relevant_outages_df is not a DataFrame.")
        return pd.Series(dtype=str)
    if not isinstance(element_df, pd.DataFrame):
        logger.error("element_df is not a DataFrame.")
        return pd.Series(dtype=str)

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
        # Filter BRELL
        outage_df_filtered = relevant_outages_df[~relevant_outages_df['eic'].isin(BRELL_LINES)].copy()
        logger.debug(f"Filtered out {len(relevant_outages_df) - len(outage_df_filtered)} BRELL lines.")

        # --- Merge ---
        # Select only necessary columns from element_df for the merge
        eic_map = [{'eic': item.eq_eic, 'grid_element_id': item.equipment[0]} for item in contingency_list]
        element_map_df = element_df[['eic', 'grid_element_id']].drop_duplicates()
        element_map_df = pd.concat([element_map_df, pd.DataFrame(eic_map)]).drop_duplicates()
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
        # Ensure IDs are strings and unique
        unique_grid_ids = grid_id_series.astype(str).drop_duplicates()

        return unique_grid_ids.reset_index(drop=True)

    except Exception as e:
        logger.error(f"Error during outage grid ID mapping: {e}", exc_info=True)
        return pd.Series(dtype=str) # Return empty on error


def outage_assessment(network: pp.network,
                      element_df_view: pd.DataFrame,
                      uap_outage_ids: pd.Series
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
    # Get unique, non-null IDs from the current view being assessed
    all_ids_in_view = assessment_df['grid_element_id'].dropna().unique().tolist()
    for item in uap_outage_ids:
        if item not in all_ids_in_view and item:
            all_ids_in_view.append(item)

    if not all_ids_in_view:
         logger.warning("No valid grid element IDs found in element_df view for checking model outages.")
    else:
        try:
            # Use the helper function to get network element details
            elements = get_network_elements_by_ids(network, all_ids_in_view)

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
            # Log the full exception
            logger.error(f"Error getting/processing network elements for CGM check: {e}", exc_info=True)
            # Keep model_outage_id as empty Series

    # TODO rework for more permanent fix
    additional_elements = elements[(elements.index.isin(uap_outage_ids)) & (elements['type'] == 'GENERATOR')].reset_index()
    additional_elements = additional_elements.rename(columns={'id': 'grid_element_id'})[['grid_element_id', 'name']]
    additional_elements['outage_in_uap'] = True

    assessment_df = pd.concat([assessment_df, additional_elements])

    # Add CGM outage flag
    assessment_df['outage_in_cgm'] = assessment_df['grid_element_id'].isin(model_outage_id)
    # Calculate inconsistency
    assessment_df['inconsistency_detected'] = assessment_df['outage_in_uap'] != assessment_df['outage_in_cgm']

    return assessment_df
