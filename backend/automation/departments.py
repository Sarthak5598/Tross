"""The eight departments, as a stable enum.

Callers previously had to pass an exact display label like
"IPC TN - Ascension St. Thomas Midtown" — punctuation, spacing and all.
That is miserable to type, easy to get subtly wrong, and it means a
cosmetic rename upstream silently breaks every caller.

So the wire format is a short stable code (`IPC_TN_MIDTOWN`) that we own.
The display label and athenahealth's numeric id hang off it.

Department is a per-request header on the API path, not session state —
which is why one token serves all eight and two departments can be
queried concurrently. On the old browser path this was impossible:
switching department moved every open tab.

Note the numeric id does NOT affect what a care-plan query returns
(verified: byte-identical responses across three departments). It is sent
for fidelity with the app, and the enum mainly buys callers a validated,
self-documenting parameter.
"""

from enum import Enum


class Department(str, Enum):
    """Wire code -> see DEPARTMENTS for its athena id and display label."""

    SH_TN_PATTERSON = "SH_TN_PATTERSON"
    SH_OH_SHAKER = "SH_OH_SHAKER"
    SH_OH_NORTH_CANTON = "SH_OH_NORTH_CANTON"
    SH_OH_WEST_CLEVELAND = "SH_OH_WEST_CLEVELAND"
    IPC_TN_WEST = "IPC_TN_WEST"
    IPC_TN_MIDTOWN = "IPC_TN_MIDTOWN"
    IPC_TN_RIVER_PARK = "IPC_TN_RIVER_PARK"
    IPC_TN_CENTENNIAL = "IPC_TN_CENTENNIAL"

    @property
    def athena_id(self) -> int:
        return DEPARTMENTS[self][0]

    @property
    def label(self) -> str:
        return DEPARTMENTS[self][1]


# code -> (athena numeric id, display label as athenahealth shows it)
DEPARTMENTS: dict[Department, tuple[int, str]] = {
    Department.SH_TN_PATTERSON:      (3,  "SH TN - Patterson"),
    Department.SH_OH_SHAKER:         (4,  "SH OH - Shaker"),
    Department.SH_OH_NORTH_CANTON:   (15, "SH OH - North Canton"),
    Department.SH_OH_WEST_CLEVELAND: (14, "SH OH - West Cleveland"),
    Department.IPC_TN_WEST:          (5,  "IPC TN - Ascension St. Thomas West"),
    Department.IPC_TN_MIDTOWN:       (12, "IPC TN - Ascension St. Thomas Midtown"),
    Department.IPC_TN_RIVER_PARK:    (16, "IPC TN - Ascension St. Thomas River Park"),
    Department.IPC_TN_CENTENNIAL:    (13, "IPC TN - HCA Centennial"),
}

_BY_LABEL = {label.lower(): dept for dept, (_, label) in DEPARTMENTS.items()}
_BY_ATHENA_ID = {str(aid): dept for dept, (aid, _) in DEPARTMENTS.items()}


def resolve(value: str | int | None) -> Department | None:
    """Accept any of the three things a caller might reasonably send.

    * the code, case-insensitively -> `SH_OH_SHAKER`, `sh_oh_shaker`
    * athenahealth's numeric id    -> `4`
    * the display label            -> `SH OH - Shaker`

    The numeric form matters because `GET /api/departments` publishes
    `athenaId` alongside each code; rejecting the very value we advertise
    is a trap. The label form predates the enum and is what the old
    dashboard sent, so it keeps working rather than breaking on upgrade.
    """
    if value is None or value == "":
        return None
    text = str(value).strip()

    found = _BY_ATHENA_ID.get(text)
    if found:
        return found
    try:
        return Department(text.upper().replace(" ", "_").replace("-", "_"))
    except ValueError:
        pass
    found = _BY_LABEL.get(text.lower())
    if found:
        return found
    raise ValueError(
        f"Unknown department {value!r}. Valid codes: "
        f"{[d.value for d in Department]} "
        f"(numeric athena ids and display labels are also accepted)")


def catalog() -> list[dict]:
    """Machine-readable listing, served at /api/departments so a caller
    can discover the codes instead of hardcoding them."""
    return [{"code": d.value, "label": d.label, "athenaId": d.athena_id}
            for d in Department]
