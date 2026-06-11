"""Q.850 cause code labels."""

from __future__ import annotations

_Q850: dict[int, str] = {
    1: "Unallocated (unassigned) number",
    2: "No route to specified transit network",
    3: "No route to destination",
    6: "Channel unacceptable",
    7: "Call awarded and being delivered in established channel",
    16: "Normal call clearing",
    17: "User busy",
    18: "No user responding",
    19: "No answer from user (user alerted)",
    20: "Subscriber absent",
    21: "Call rejected",
    22: "Number changed",
    26: "Non-selected user clearing",
    27: "Destination out of order",
    28: "Invalid number format (incomplete number)",
    29: "Facility rejected",
    30: "Response to STATUS ENQUIRY",
    31: "Normal, unspecified",
    34: "No circuit/channel available",
    38: "Network out of order",
    41: "Temporary failure",
    42: "Switching equipment congestion",
    43: "Access information discarded",
    44: "Requested circuit/channel not available",
    47: "Resource unavailable, unspecified",
    49: "Quality of service unavailable",
    50: "Requested facility not subscribed",
    57: "Bearer capability not authorized",
    58: "Bearer capability not presently available",
    63: "Service or option not available, unspecified",
    65: "Bearer capability not implemented",
    66: "Channel type not implemented",
    69: "Requested facility not implemented",
    70: "Only restricted digital information bearer capability is available",
    79: "Service or option not implemented, unspecified",
    81: "Invalid call reference value",
    82: "Identified channel does not exist",
    83: "A suspended call exists, but this call identity does not",
    84: "Call identity in use",
    85: "No call suspended",
    86: "Call having the requested call identity has been cleared",
    87: "User not member of CUG",
    88: "Incompatible destination",
    90: "Non-existent CUG",
    91: "Invalid transit network selection",
    95: "Invalid message, unspecified",
    96: "Mandatory information element is missing",
    97: "Message type non-existent or not implemented",
    98: "Message not compatible with call state or message type non-existent",
    99: "Information element/parameter non-existent or not implemented",
    100: "Invalid information element contents",
    101: "Message not compatible with call state",
    102: "Recovery on timer expiry",
    103: "Parameter non-existent or not implemented — passed on",
    110: "Message with unrecognized parameter, discarded",
    111: "Protocol error, unspecified",
    127: "Interworking, unspecified",
}


# Public dict used by API for analytics breakdown
CAUSE_CODES: dict[int, dict] = {code: {"label": lbl} for code, lbl in _Q850.items()}


def label(code: int | None) -> str:
    if code is None:
        return ""
    return _Q850.get(code, f"Cause {code}")


def is_normal(code: int | None) -> bool:
    """Return True for cause codes that indicate a normal call completion."""
    return code in (16, 17, 19, 31)
