from dataclasses import dataclass
from pandas import Timestamp

@dataclass
class Outage:
    end_date: str | None
    Cause_of_request: str | None
    Unavailable_capacity: float | None
    # Color: str | None
    createdDateTime: int | None
    date_of_last_change: str | None
    OrderID: str | None
    Restitution_time: float | None
    Area: str | None
    # Score: int | None
    outage_type: str | None
    mrid: str | None
    CASE_ID: str | None
    Merge: str | None
    asset_type: str | None
    name: str | None
    reportParsedDate: Timestamp | None
    eic: str | None
    start_date: str | None
