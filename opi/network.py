import re
import logging
import pandas as pd
from io import BytesIO
import pypowsybl as pp
from zipfile import ZipFile, ZIP_DEFLATED
from xml.etree import ElementTree
from pypowsybl.network import ElementType
from loadflow_engine.helper import get_network_elements
from numpy import sign, isnan
from opi.region import ASSESSED_AREAS, AREA_ALIASES


logger = logging.getLogger(__name__)


def unzip_zipped_profiles(path_or_buffer: str):
    """
    Extracts zipped profiles of a model.
    Detects automatically if the zipped folder is read from file bath or BytesIO buffer.
    :param path_or_buffer: path of zipped model or BytesIO buffer
    :return: output_zip_buffer
    """
    # Checks if path_or_buffer is string
    if isinstance(path_or_buffer, str):
        with open(path_or_buffer, 'rb') as original_zip_file:
            original_zip_buffer = BytesIO(original_zip_file.read())

    # Checks if path_or_buffer is BytesIO class
    elif isinstance(path_or_buffer, BytesIO):
        original_zip_buffer = path_or_buffer
    else:
        logger.error(f"Provided variable is nor string nor BytesIO object")

    output_zip_buffer = BytesIO()

    # Read the original zipped folder from provided buffer
    with ZipFile(original_zip_buffer, 'r') as original_zip:
        # Create a new zip file where we will store the unzipped XMLs
        with ZipFile(output_zip_buffer, 'w', ZIP_DEFLATED) as new_zip:
            # Iterate trough each file in the original zip
            for file_name in original_zip.namelist():
                # Read the zipped xml file
                with original_zip.open(file_name) as zipped_xml_file:
                    with ZipFile(zipped_xml_file) as zipped_xml:
                        # Extract each file and add it to the new zip
                        for xml_file_name in zipped_xml.namelist():
                            xml_data = zipped_xml.read(xml_file_name)
                            # Add unzipped XML file to the new zip
                            new_zip.writestr(xml_file_name, xml_data)

    output_zip_buffer.seek(0)

    return output_zip_buffer


def load_network(network, parameters):

    logger.info("Loading model into pypowsybl")

    if bytes == type(network):
        try:
            network = unzip_zipped_profiles(BytesIO(network))
        except Exception as error:
            logger.error(error)
        network = pp.network.load_from_binary_buffer(network, parameters=parameters)
    if str == type(network):
        try:
            network = unzip_zipped_profiles(BytesIO(network))
            network = pp.network.load_from_binary_buffer(network, parameters=parameters)
        except Exception as error:
            logger.error(error)
            network = pp.network.load(network, parameters=parameters)

    return network


def get_power_losses(network: pp.network, by_region: bool = False, present_igms: list = None):
    """
    Gets power losses in MegaWatts from the model
    :param network: loadflow_engine2.network.load()
        Network created in PyPowSyBl using pp.network.load()
    :param by_region: bool, default False
        True: calculates power losses to each area and for tie lie lines separately
        False: calculates the power losses for whole model
    :param present_igms: list, default None
        Cross-borders that are available in the model.
        Note: Check from loadflow_engine2.capacity.py get_present_igms()
    :returns:
        DataFrame
            by_region = True -> a 1xn DataFrame where n represents the areas + tie-lines and the rows consist of power
            losses
        float
            by_region = False -> a float value of the whole area's power losses
    """

    # Reads and calculates losses on lines
    lines = get_network_elements(network=network, element_type=pp.network.ElementType.LINE)
    lines['P_loss'] = abs(abs(lines.p1) - abs(lines.p2))
    P_loss_line = lines['P_loss'].sum()

    tie_lines = get_network_elements(network=network, element_type=pp.network.ElementType.TIE_LINE)
    dang = get_network_elements(network=network, element_type=pp.network.ElementType.BOUNDARY_LINE)
    tie_lines_and_dang1 = tie_lines.merge(dang[['p']], left_on='dangling_line1_id', right_index=True).rename(
        columns={'p': 'p1'})
    tie_lines_and_dang1_and_dang2 = tie_lines_and_dang1.merge(dang[['p']], left_on='dangling_line2_id',
                                                              right_index=True).rename(columns={'p': 'p2'})
    tie_lines_and_dang1_and_dang2['P_losses'] = tie_lines_and_dang1_and_dang2[['p1', 'p2']].abs().max(axis=1) - \
                                                tie_lines_and_dang1_and_dang2[['p1', 'p2']].abs().min(axis=1)
    P_loss_tie_line = tie_lines_and_dang1_and_dang2['P_losses'].sum()

    # Reads and calculates losses on two winding transformers
    transformers_two = get_network_elements(network=network,
                                            element_type=pp.network.ElementType.TWO_WINDINGS_TRANSFORMER)
    transformers_two['P_loss'] = abs(abs(transformers_two.p1) - abs(transformers_two.p2))
    P_loss_trans_two = transformers_two['P_loss'].sum()

    # Reads and calculates losses on three winding transformers
    transformers_three = get_network_elements(network=network,
                                              element_type=pp.network.ElementType.THREE_WINDINGS_TRANSFORMER)
    transformers_three['P_loss'] = transformers_three.p1 + transformers_three.p2 + transformers_three.p3
    P_loss_trans_three = transformers_three['P_loss'].sum()

    system_power_losses = (P_loss_line + P_loss_tie_line + P_loss_trans_two + P_loss_trans_three)

    if by_region:
        all_elements = pd.concat([lines, transformers_two, transformers_three])
        all_region_losses_dict = {}

        for region in present_igms:
            region_losses = all_elements[all_elements["CGMES.regionName1"] == region]['P_loss'].sum()
            all_region_losses_dict[region] = pd.DataFrame({region: [region_losses]})

        all_region_losses_dict['Tie_lines'] = pd.DataFrame({'Tie_lines': [P_loss_tie_line]})
        df_losses = pd.concat(all_region_losses_dict.values(), axis=1)

        return round(df_losses, 2)

    return round(system_power_losses, 2)


def get_boundary_iso_codes(model_data: bytes) -> dict[str, tuple[str, str]]:
    """Boundary node name (X-node) -> (fromEndIsoCode, toEndIsoCode), read from the boundary set in the model zip."""
    iso_codes = {}
    with ZipFile(BytesIO(model_data)) as model_zip:
        for name in model_zip.namelist():
            with ZipFile(model_zip.open(name)) as profile_zip:
                for xml_name in profile_zip.namelist():
                    if not re.search(r'(EQ|TP)_?BD', xml_name.upper()):
                        continue
                    for node in ElementTree.parse(profile_zip.open(xml_name)).getroot():
                        fields = {child.tag.rsplit('.', 1)[-1]: child.text for child in node}
                        if 'fromEndIsoCode' in fields:
                            iso_codes[fields.get('name')] = (fields['fromEndIsoCode'], fields.get('toEndIsoCode'))

    return iso_codes


def get_system_balance(network: pp.network.Network, areas: list, border_flows: pd.DataFrame) -> pd.DataFrame:
    result_df = pd.DataFrame(index=areas,
                             columns=['P Load MW', 'P Gen MW', 'P Losses MW', 'HVDC flow', 'ACNP'])

    generation = get_network_elements(network, element_type=pp.network.ElementType.GENERATOR)
    load = get_network_elements(network, element_type=pp.network.ElementType.LOAD)
    p_losses = get_power_losses(network, present_igms=areas,
                                by_region=True).drop('Tie_lines', axis=1).T

    result_df['P Losses MW'] = p_losses
    for tso in areas:
        result_df.loc[tso, 'P Load MW'] = load[load['CGMES.regionName'] == tso]['p'].sum()
        result_df.loc[tso, 'P Gen MW'] = abs(generation[generation['CGMES.regionName'] == tso]['p'].sum())
        result_df.loc[tso, 'HVDC flow'] = border_flows.loc[border_flows['hvdc'].astype(bool) &
                                                          border_flows['Cross_border'].str.startswith(f'{tso}-'), 'P_MW'].sum()

        result_df.loc[tso, 'ACNP'] = (result_df.loc[tso, 'P Gen MW'] - result_df.loc[tso, 'P Load MW']
                                      - result_df.loc[tso, 'P Losses MW'] - result_df.loc[tso, 'HVDC flow'])

    return round(result_df, 2).reset_index(names=['area'])


def get_cross_border_flows(network: pp.network.Network, model_data: bytes) -> pd.DataFrame:
    """
    Flows on tie lines and HVDC links of the assessed areas, one row per X-node.
    P_MW is positive from the first area of Cross_border to the second.
    HVDC links are boundary lines with the CGMES import property isHvdc, their far side comes from the boundary set.
    """
    try:
        iso_codes = get_boundary_iso_codes(model_data)
    except Exception as error:
        logger.warning(f"Boundary set iso codes not read from model: {error!r}")
        iso_codes = {}

    lines = network.get_elements(ElementType.BOUNDARY_LINE, all_attributes=True)
    if 'isHvdc' not in lines.columns:
        lines['isHvdc'] = ''
    substations = network.get_voltage_levels()['substation_id']
    regions = network.get_substations(all_attributes=True)['CGMES.regionName'].replace(AREA_ALIASES)
    lines['area'] = lines['voltage_level_id'].map(substations).map(regions)
    lines = lines.drop_duplicates(subset=['pairing_key', 'area'])

    # Border labels in ASSESSED_AREAS order: LT-LV, LV-EE, LT-PL, ... Lines without an assessed area are not reported
    order = {area: i for i, area in enumerate(ASSESSED_AREAS)}
    rows = []
    for xnode, halves in lines.groupby('pairing_key'):
        halves = halves.sort_values('area', key=lambda area: area.map(order).fillna(len(order)))
        first = halves.iloc[0]
        if first['area'] not in order:
            continue
        hvdc = first['isHvdc'] == 'true'
        if len(halves) > 1:
            second_area = halves.iloc[1]['area']
            p = sign(first['p']) * min(abs(first['p']), abs(halves.iloc[1]['p']))
        elif hvdc:
            second_area = next((iso for iso in iso_codes.get(xnode, ()) if iso != first['area']), None)
            if second_area is None:
                logger.warning(f"No boundary iso codes for HVDC X-node {xnode}, far side unknown")
            p = first['p']
        else:
            # AC line to an area outside the model
            continue
        rows.append({'Cross_border': f"{first['area']}-{second_area}", 'name': first['name'],
                     'P_MW': 0 if isnan(p) else p, 'hvdc': hvdc})

    return pd.DataFrame(rows, columns=['Cross_border', 'name', 'P_MW', 'hvdc'])


def get_tie_line_halves(network: pp.network.Network) -> dict[str, list[str]]:
    """Boundary line or tie line id -> both boundary lines of the tie line, for boundary lines paired in the model."""
    lines = network.get_boundary_lines(attributes=['tie_line_id'])
    # Paired as in EMFOS: tie_line_id set; an unpaired boundary line is switched on its own side only
    paired = lines[lines['tie_line_id'].fillna('') != '']
    halves = {tie_line_id: group.index.tolist() for tie_line_id, group in paired.groupby('tie_line_id')}

    return {**halves, **{line_id: halves[tie_line_id] for line_id, tie_line_id in paired['tie_line_id'].items()}}
