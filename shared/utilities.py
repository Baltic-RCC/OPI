import logging
import pandas as pd
from inspect import ismethod
from pydantic import BaseModel, Field, AliasChoices
from typing import Optional, List, Any
import uuid
from opi.shared.constants import AC_BORDERS, DC_BORDERS, CROSS_BORDER_CONFIG, DICT_OF_AREAS, PARTY_X_CODE
# from settings import BORDERS_CAPACITY_CALCULATION_INPUT

logger = logging.getLogger(__name__)


def attr_to_dict(obj: object):
    """
    Method to return class variables/attributes as dictionary
    Example: LimitViolation(subject_id='e4', subject_name='', limit_type=HIGH_VOLTAGE,
    limit=450.0, limit_name='', acceptable_duration=2147483647, limit_reduction=1.0, value=555.6890952917897, side=ONE)
    loadflow_engine2._pypowsybl.LimitViolation -> dict
    :param obj:
        object
    :return:
        dict
    """
    attribs = [attr for attr in dir(obj) if (not ismethod(getattr(obj, attr)) and not attr.startswith("_"))]
    result_dict = {attr_key: getattr(obj, attr_key) for attr_key in attribs}

    return result_dict


def get_all_available_borders(present_igms: list, all_inner_borders: list = AC_BORDERS,
                              all_outer_borders: list = DC_BORDERS):
    """
    Based on the available region in the model, checks which cross-borders can be used
    :param present_igms: list
        Cross-borders that are available in the model.
        Note: Check from loadflow_engine2.capacity.py get_present_igms()
    :param all_inner_borders: list, default constants.INNER_BORDERS
        A list of all the inner cross-borders
    :param all_outer_borders: list, default constants.OUTER_BORDERS
        A list of all the outer cross-borders
    :return:
        list
            A list of all the available cross-borders
    """
    available_inner_borders = [item for item in all_inner_borders if
                               all(value in present_igms for value in item.split('-'))]
    available_outer_borders = [key for key in all_outer_borders if
                               any(area in key for area in present_igms)]
    all_available_borders = available_inner_borders + available_outer_borders
    return all_available_borders


# def get_available_borders_with_calculation_input_data(borders: list,
#                                                       czc_shift_amounts: dict = BORDERS_CAPACITY_CALCULATION_INPUT):
#     """
#     Filters out the unavailable cross-borders input_data
#     :param borders: list
#         Available cross-borders.
#         Note: meant to be used with the results gotten from get_all_available_borders()
#     :param czc_shift_amounts:  dict, default constants.BORDERS_CAPACITY_CALCULATION_INPUT.
#         Dictionary of the cross-borders and their input data.
#     :return:
#         dict
#             A dictionary of the filtered out cross-border input data
#     """
#     all_available_borders = borders
#     all_available_borders_with_calculation_input = {key: czc_shift_amounts[key] for key in all_available_borders if
#                                                     key in czc_shift_amounts}
#     return all_available_borders_with_calculation_input


def switch_rows(input_data: pd.DataFrame, comparison_column: str, row1: str, row2: str):
    """
    Switching the order of two rows in a DataFrame.
    :param input_data: DataFrame
        A DataFrame where the row switching will take place
    :param comparison_column: str
        A column used to identify the two rows
    :param row1: str
        An identification of the first row to be switched based on comparison_column
    :param row2: str
        An identification of the second row to be switched based on comparison_column
    :return:
        DataFrame
            A DataFrame with the rows switched
    """
    input_data = input_data.reset_index()
    row_nr_1 = input_data.loc[input_data[comparison_column] == row1].index[0]
    row_nr_2 = input_data.loc[input_data[comparison_column] == row2].index[0]
    input_data.iloc[row_nr_1], input_data.iloc[row_nr_2] = (input_data.iloc[row_nr_2].copy(),
                                                            input_data.iloc[row_nr_1].copy())
    output_data = input_data.set_index('id')
    return output_data


def cross_border_region_name_check(border: str, ee_cgmes_region_name: str = 'EE'):
    """
    Redefines the cross-border areas which names are defined differently in the model e.g. EE is defined as Estonia.
    Also used for reading results from HVDC links due to CIM modeling.
    :param border: str
        A cross-border e.g. 'EE-FI'
    :param ee_cgmes_region_name: str
        Sometimes cgmes region name can be 'EESTI'
    :returns:
        tuple
            Strings where the import and export regions are separated from the cross-border string and defined to areas
            definitions found in the model e.g. cross-border = 'EE-FI' -> up = 'Estonia', down = 'Estonia'
            up: first area of the cross-border
            down: second area of the cross-border
    """
    up, down = border.split('-')

    up_map = {
        'EE': ee_cgmes_region_name,
        'FI': ee_cgmes_region_name,
        'SE': 'LT'}

    down_map = {
        'EE': ee_cgmes_region_name,
        'FI': ee_cgmes_region_name,
        'SE': 'LT'}

    if up in up_map.keys():
        up = up_map[up]

    if down in down_map.keys():
        down = down_map[down]

    return up, down


def direction_check_hvdc(cross_border, xborder_flow):
    """
    Function to adjust the sign according to the naming and flow direction convention for HVDCs (if positive, flows
    according to described cross_border.
    E.g. If FI-EE flow is 100 MW, it flows from FI to EE, if flow is -100 MW, flows from EE to FI
    :param cross_border: cross border
    :param xborder_flow: calculated flow on the described cross border
    :return:
    """
    border_map = {
        'EE-FI': xborder_flow,
        'FI-EE': xborder_flow * -1,
        'LT-SE': xborder_flow,
        'SE-LT': xborder_flow * -1}

    if cross_border in border_map.keys():
        flow = border_map[cross_border]
    else:
        flow = xborder_flow

    return flow


def xlines_in_me_check(monitored_elements: pd.DataFrame, cross_borders_ids: dict[list] = CROSS_BORDER_CONFIG):
    """
    Checks for cross-borders defined in monitored element DataFrame and adds the missing ones
    :param monitored_elements: DataFrame
        An input Dataframe of monitored elements
    :param cross_borders_ids: dict[list], default constants.CROSS_BORDER_CONFIG
        A dictionary consisting of every cross-border and its dangling lines IDs
    :return:
        DataFrame
            An updated DataFrame consisting of monitored elements with the cross-borders
    """

    cross_border_id = [value for values in cross_borders_ids.values() for value in
                       values]
    for ID in cross_border_id:
        if ID not in monitored_elements['metas.registered_resource'].values:
            logger.warning(f'\t\tCross-border line {ID} was not found in CRAC')
            monitored_elements = monitored_elements._append({'metas.registered_resource': ID}, ignore_index=True)
    return monitored_elements


def link_dataframes(*df: pd.DataFrame):
    """
    Links DataFrames together
    :param df: args
        DataFrames to be linked
    :return:
        DataFrame
            A DataFrame of all the input DataFrames
    """
    return pd.concat(df)


def extract_df_values_to_list(dataframe: pd.DataFrame, **columns: str):
    """
    Extracts values from specified columns in a DataFrame to a list
    :param dataframe: DataFrame
        A DataFrame where the values will be extracted
    :param columns: kwargs
        The column name(s) to be extracted to a list from the DataFrame
    :return: list of the extracted values
    """
    values_list = dataframe[list(columns.values())].values.flatten().tolist()
    return values_list


def create_dataframe_from_dictionaries(*dicts: dict, column_names: list[str] = None):
    """
    Creates a DataFrame from unlimited dictionaries having the same keys (keys will be the index)
    :param dicts: dict
        Dictionaries that will each be a column in the newly created DataFrame
    :param column_names: list[str], default None
        Names of the newly created DataFrame's columns
    :return: DataFrame consisting of the given dictionaries.
    """

    # Check if all dictionaries have the same keys
    keys_set = set(dicts[0].keys())
    if not all(set(d.keys()) == keys_set for d in dicts[1:]):
        raise ValueError("All dictionaries must have the same keys")

    # If column_names are not provided, use default as "0", "1" etc.
    if column_names is None:
        column_names = [str(i) for i in range(len(dicts[0]))]

    data_dict = {col: dicts[i] for i, col in enumerate(column_names)}

    df = pd.DataFrame(data_dict)

    return df


def change_type_of_df_columns(*columns: str, df: pd.DataFrame, dtype: str):
    """
    Changes the type of given column(s)
    :param columns: tuple
        Columns to pass the type change
    :param dtype: str, data type, Series or Mapping of column name -> data type
        check pandas.DataFrame.astype for more information
    :param df: pd.DataFrame
        The DataFrame where the column types will be changed
    :return: pd.DataFrame
        Update input DataFrame df
    """
    for col in columns:
        df[col] = df[col].astype(dtype)
    return df


def get_cross_border_area_codes(border: str = 'EE-LV', dict_of_areas: dict[str, str] = DICT_OF_AREAS):
    """
    Returns list of cross-border's area (IEC) codes.
    :param border: string
        cross-border
    :param dict_of_areas: dict[str, str]
        dictionary of areas names and areas IEC codes, e.g. {'EE': '10Y1001A1001A39I', 'LV': '10YLV-1001A00074'}
    :return: list
        of (2) areas codes
    """
    area1, area2 = border.split('-')
    area1_code = dict_of_areas[area1]
    area2_code = dict_of_areas[area2]
    return [area1_code, area2_code]


def map_with_logging(value):
    """
    Returns IEC X party code.
    :param value: value/row of a pd.DataFrame
    :return:
    """
    if value in PARTY_X_CODE:
        return PARTY_X_CODE[value]
    else:
        logger.warning(f"TSO {value} unknown")


class AssessedElement(BaseModel):
    mRID: Optional[str] = Field(default_factory=lambda: f"{uuid.uuid4()}")
    name: Optional[str] = ""
    type: Optional[str] = Field(default="AssessedElement", validation_alias=AliasChoices("type", "Type"))
    equipment: str = Field(validation_alias=AliasChoices("equipment", "ConductingEquipment"))
    area: Optional[str] = ""
    contingency: Optional[List[str]] = None
    remedial_action: Optional[List[str]] = None


class Contingency(BaseModel):
    mRID: Optional[str] = Field(default_factory=lambda: f"{uuid.uuid4()}")
    name: Optional[str] = ""
    type: Optional[str] = Field(default="Ordinary", validation_alias=AliasChoices("type", "Type"))
    equipment: List[str]
    eq_eic: Optional[str]
    #
    # @field_validator('mRID', 'equipment')
    # def validate_mrids(cls, value):
    #     return remove_leading_underscore(value)

    def model_post_init(self, __context: Any) -> None:
        if len(self.equipment) > 1 and self.type != 'OutOfRange':
            self.type = "Exceptional"