"""Synthetic X12 856 builder used by the test suite (not production code)."""

from __future__ import annotations

ISA_FIELDS = (
    "00", "          ", "00", "          ", "ZZ", "SENDER         ",
    "ZZ", "RECEIVER       ", "261114", "1200", None, "00501", None, "0",
    "T", None,
)


def isa_segment(
    *, element: str = "*", component: str = ":", segment: str = "~",
    repetition: str = "^", control: str = "000000001",
) -> str:
    fields = list(ISA_FIELDS)
    fields[10] = repetition
    fields[12] = control
    fields[15] = component
    return "ISA" + element + element.join(fields) + segment


def render(segment_list, element: str = "*") -> list[str]:
    return [element.join([tag, *elements]) for tag, *elements in segment_list]


def make_transaction(
    body,
    *, element: str = "*", st_control: str = "0001",
    st_set: str = "856", se_count: int | None = None,
    se_control: str | None = None,
) -> str:
    body_rendered = render(body, element)
    actual = 2 + len(body_rendered)
    declared = actual if se_count is None else se_count
    control = st_control if se_control is None else se_control
    lines = [element.join(["ST", st_set, st_control])]
    lines.extend(body_rendered)
    lines.append(element.join(["SE", str(declared), control]))
    return "~".join(lines)  # caller replaces if needed? no: use group delimiter


def make_transaction_d(
    body,
    *, element: str = "*", segment: str = "~", st_control: str = "0001",
    st_set: str = "856", se_count: int | None = None,
    se_control: str | None = None,
) -> str:
    body_rendered = render(body, element)
    actual = 2 + len(body_rendered)
    declared = actual if se_count is None else se_count
    control = st_control if se_control is None else se_control
    lines = [element.join(["ST", st_set, st_control])]
    lines.extend(body_rendered)
    lines.append(element.join(["SE", str(declared), control]))
    return segment.join(lines)


def make_group(
    transactions: list[str],
    *, element: str = "*", segment: str = "~",
    gs_control: str = "1", ge_count: int | None = None,
    ge_control: str | None = None,
) -> str:
    gs = element.join(
        ["GS", "SH", "SENDER", "RECEIVER", "20261114", "1200", gs_control, "X", "005010"]
    )
    declared = len(transactions) if ge_count is None else ge_count
    control = gs_control if ge_control is None else ge_control
    ge = element.join(["GE", str(declared), control])
    return segment.join([gs, *transactions, ge])


def make_document(
    groups: list[str],
    *, element: str = "*", component: str = ":", segment: str = "~",
    repetition: str = "^", isa_control: str = "000000001",
    iea_count: int | None = None, iea_control: str | None = None,
) -> str:
    declared = len(groups) if iea_count is None else iea_count
    control = isa_control if iea_control is None else iea_control
    iea = element.join(["IEA", str(declared), control])
    return (
        isa_segment(
            element=element, component=component, segment=segment,
            repetition=repetition, control=isa_control,
        )
        + segment.join(groups)
        + segment
        + iea
        + segment
    )


def shipment_body(
    *,
    hl_s_id: str = "1", order_id: str = "2", pack_id: str = "3",
    item_ids=("4", "5"), order_number: str = "PO-01",
    pack_code: str = "PACK-01", product: str = "SKU-1",
    quantities=(("2.50", "EA"), ("1", "EA")),
    pack_for_item=(True, False),
    extra_before_hl: list | None = None,
    extra_after_prf: list | None = None,
) -> list:
    body = [("BSN", "00", "SHIP1", "20261114", "1200")]
    body.extend(extra_before_hl or [])
    body.append(("HL", hl_s_id, "", "S"))
    if pack_id is not None:
        body.append(("HL", pack_id, order_id, "P"))
        body.append(("MAN", "GM", pack_code))
    body.append(("HL", order_id, hl_s_id, "O"))
    body.append(("PRF", order_number))
    body.extend(extra_after_prf or [])
    for item_id, (qty, unit), has_pack in zip(item_ids, quantities, pack_for_item):
        body.append(("HL", item_id, pack_id if (has_pack and pack_id) else order_id, "I"))
        body.append(("LIN", "", "VP", product))
        body.append(("SN1", "", qty, unit))
    return body
