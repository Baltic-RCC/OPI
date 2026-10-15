import logging
from pydantic import BaseModel, Field, AliasChoices
from typing import Optional, List, Any
import uuid

logger = logging.getLogger(__name__)


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
