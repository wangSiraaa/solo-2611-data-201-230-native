"""Tests for the restricted X12 856 loader.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _edi import (  # noqa: E402
    isa_segment,
    make_document,
    make_group,
    make_transaction,
    make_transaction_d,
    render,
    shipment_body,
)
from x12_856.loader import (  # noqa: E402
    ALLOWED_CHILD_LEVELS,
    KNOWN_SEGMENTS,
    export,
    parse,
    parse_file,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def parse_fixture(name: str):
    return parse_file(FIXTURES / name)


def codes(result, severity: str = "error") -> list[str]:
    return [d.code for d in result.diagnostics if d.severity == severity]


def build(
    body=None, *, element="*", component=":", segment="~",
    doc_kwargs=None, group_kwargs=None, **tx_kwargs,
):
    body = shipment_body() if body is None else body
    tx = make_transaction_d(body, element=element, segment=segment, **tx_kwargs)
    group = make_group(
        [tx], element=element, segment=segment, **(group_kwargs or {})
    )
    return make_document(
        [group], element=element, component=component, segment=segment,
        **(doc_kwargs or {}),
    )


class DelimiterTests(unittest.TestCase):
    def test_fixture_delimiters_are_read_from_isa(self):
        star = parse_fixture("shipment.edi")
        pipe = parse_fixture("alternate.edi")
        self.assertEqual(star.delimiters.as_dict(), {
            "element": "*", "component": ":", "segment": "~",
            "repetition": "^",
        })
        self.assertEqual(pipe.delimiters.as_dict(), {
            "element": "|", "component": ">", "segment": "!",
            "repetition": "^",
        })

    def test_both_delimiter_versions_yield_identical_facts(self):
        star = parse_fixture("shipment.edi")
        pipe = parse_fixture("alternate.edi")
        for a, b in zip(star.items, pipe.items):
            self.assertEqual(a.hl_id, b.hl_id)
            self.assertEqual(a.product_code, b.product_code)
            self.assertEqual(a.quantity, b.quantity)
            self.assertEqual(a.unit, b.unit)
            self.assertEqual(a.package_code, b.package_code)
            self.assertEqual(a.ancestor_ids, b.ancestor_ids)

    def test_fixed_isa_offsets_not_hardcoded_symbols(self):
        body = shipment_body()
        tx = make_transaction_d(body, element="^", segment="\x1c")
        group = make_group([tx], element="^", segment="\x1c")
        doc = make_document(
            [group], element="^", component="]", repetition="%",
            segment="\x1c",
        )
        result = parse(doc)
        self.assertTrue(result.ok, codes(result))
        self.assertEqual(result.delimiters.as_dict(), {
            "element": "^", "component": "]", "segment": "\x1c",
            "repetition": "%",
        })
        self.assertEqual(len(result.items), 2)

    def test_comma_and_control_character_delimiters(self):
        tx = make_transaction_d(shipment_body(), element=",", segment="\x1e")
        group = make_group([tx], element=",", segment="\x1e")
        doc = make_document(
            [group], element=",", component="|", repetition="^", segment="\x1e"
        )
        result = parse(doc)
        self.assertTrue(result.ok, codes(result))
        self.assertEqual(result.delimiters.element, ",")
        self.assertEqual(result.delimiters.segment, "\x1e")

    def test_blank_line_and_crlf_transport_formatting_is_tolerated(self):
        tx = make_transaction_d(shipment_body(), element="*", segment="~")
        group = make_group([tx], element="*", segment="~")
        doc = make_document([group], element="*", component=":", segment="~")
        result = parse(doc.replace("~", "~\r\n"))
        self.assertTrue(result.ok, codes(result))
        self.assertEqual(len(result.items), 2)
        self.assertEqual(len(result.segments), 18)

    def test_bad_isa_delimiter_is_reported(self):
        doc = parse_fixture("shipment.edi").raw_text
        broken = doc[:3] + "A" + doc[4:]
        result = parse(broken)
        self.assertFalse(result.ok)
        self.assertIn("ISA_DELIMITER", codes(result))
        self.assertEqual(result.scope_statuses[0].status, "failed")


class EnvelopeTests(unittest.TestCase):
    def test_good_envelope_counts_st_to_se_inclusive(self):
        result = parse_fixture("shipment.edi")
        self.assertTrue(result.ok)
        transaction = [c for c in result.envelope_checks if c["scope"] == "transaction"][0]
        self.assertEqual(transaction["declared_count"], 14)
        self.assertEqual(transaction["actual_count"], 14)
        self.assertTrue(transaction["count_matches"])
        self.assertTrue(transaction["control_matches"])
        # ST..SE is 14 segments: ST, BSN, HL(S), HL(P), MAN, HL(O), PRF,
        # HL(I), LIN, SN1, HL(I), LIN, SN1, SE.
        self.assertEqual(
            transaction["actual_count"],
            transaction["segment_end"] - transaction["segment_start"] + 1,
        )

    def test_bad_count_is_transaction_scope_and_has_range(self):
        result = parse_fixture("bad-count.edi")
        self.assertFalse(result.ok)
        self.assertIn("SE_COUNT", codes(result))
        statuses = {
            (s.scope, s.scope_id): s for s in result.scope_statuses
        }
        self.assertEqual(statuses[("interchange", "1")].status, "ok")
        self.assertEqual(statuses[("group", "1.1")].status, "ok")
        tx_status = statuses[("transaction", "1.1.1")]
        self.assertEqual(tx_status.status, "failed")
        self.assertEqual(tx_status.reason, "SE_COUNT")
        diag = [d for d in result.diagnostics if d.code == "SE_COUNT"][0]
        self.assertEqual(diag.segment_no, 16)
        self.assertEqual(diag.end_segment_no, 3)

    def test_iea_count_failure_poisons_whole_interchange(self):
        result = parse(build(doc_kwargs={"iea_count": 2}))
        self.assertFalse(result.ok)
        self.assertIn("IEA_COUNT", codes(result))
        statuses = {s.scope_id: s for s in result.scope_statuses}
        self.assertEqual(statuses["1"].status, "failed")
        self.assertEqual(statuses["1.1"].status, "failed")
        self.assertEqual(statuses["1.1.1"].status, "failed")
        self.assertEqual(result.items, [])

    def test_ge_count_failure_poisons_group_only(self):
        tx = make_transaction_d(shipment_body())
        doc = make_document([make_group([tx], ge_count=2)])
        result = parse(doc)
        self.assertFalse(result.ok)
        self.assertIn("GE_COUNT", codes(result))
        statuses = {s.scope_id: s for s in result.scope_statuses}
        self.assertEqual(statuses["1"].status, "ok")
        self.assertEqual(statuses["1.1"].status, "failed")
        self.assertEqual(statuses["1.1.1"].status, "failed")
        self.assertEqual(result.items, [])

    def test_control_number_mismatches_have_distinct_codes(self):
        iea = parse(build(doc_kwargs={"iea_control": "000000002"}))
        self.assertIn("ISA_IEA_CONTROL", codes(iea))
        group = parse(
            make_document([make_group([make_transaction_d(shipment_body())], ge_control="9")])
        )
        self.assertIn("GS_GE_CONTROL", codes(group))
        transaction = parse(build(se_control="9999"))
        self.assertIn("ST_SE_CONTROL", codes(transaction))

    def test_missing_se_fails_only_that_transaction(self):
        body = [("BSN", "00", "S1", "20261114", "1200"), ("HL", "1", "", "S")]
        tx_ok = make_transaction_d(shipment_body(), st_control="0001")
        tx_bad = make_transaction_d(body, st_control="0002")
        tx_bad = tx_bad[: tx_bad.rindex("~SE")]
        doc = make_document([make_group([tx_ok, tx_bad])])
        result = parse(doc)
        statuses = {
            (s.scope, s.scope_id): s for s in result.scope_statuses
        }
        self.assertEqual(statuses[("transaction", "1.1.1")].status, "ok")
        self.assertEqual(statuses[("transaction", "1.1.2")].status, "failed")
        self.assertIn("SE_MISSING", codes(result))
        # The valid transaction still emits facts, the failed one does not.
        self.assertEqual(len(result.shipments), 1)
        self.assertEqual(len(result.orders), 1)

    def test_multiple_transactions_scope_is_first_st_through_its_se(self):
        tx1 = make_transaction_d(shipment_body(order_number="PO-01"), st_control="0001")
        tx2 = make_transaction_d(shipment_body(order_number="PO-02"), st_control="0002")
        result = parse(make_document([make_group([tx1, tx2])]))
        self.assertTrue(result.ok, codes(result))
        self.assertEqual([s.shipment_number for s in result.shipments], ["SHIP1", "SHIP1"])
        self.assertEqual([o.order_number for o in result.orders], ["PO-01", "PO-02"])
        transaction_scopes = [
            s for s in result.scope_statuses if s.scope == "transaction"
        ]
        self.assertEqual([s.scope_id for s in transaction_scopes], ["1.1.1", "1.1.2"])
        first, second = transaction_scopes
        self.assertLess(first.segment_end, second.segment_start)


class HierarchyTests(unittest.TestCase):
    def test_items_keep_full_ancestor_chain(self):
        result = parse_fixture("shipment.edi")
        packed, direct = result.items
        self.assertEqual(packed.ancestor_ids, ("1", "2", "3"))
        self.assertEqual(packed.ancestor_levels, ("S", "O", "P"))
        self.assertEqual(packed.path_ids, ("1", "2", "3", "4"))
        self.assertEqual(packed.package_code, "PACK-01")
        self.assertEqual(packed.package_hl_id, "3")
        self.assertEqual(direct.ancestor_ids, ("1", "2"))
        self.assertEqual(direct.ancestor_levels, ("S", "O"))
        self.assertIsNone(direct.package_code)

    def test_forward_parent_reference_is_accepted(self):
        body = [
            ("BSN", "00", "S1", "20261114", "1200"),
            ("HL", "1", "", "S"),
            ("HL", "2", "1", "O"), ("PRF", "PO-01"),
            # I appears before its P parent.
            ("HL", "4", "3", "I"), ("LIN", "", "VP", "X"), ("SN1", "", "1", "EA"),
            ("HL", "3", "2", "P"), ("MAN", "GM", "PACK-01"),
        ]
        result = parse(build(body))
        self.assertTrue(result.ok, codes(result))
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertEqual(item.hl_id, "4")
        self.assertEqual(item.parent_hl_id, "3")
        self.assertEqual(item.package_code, "PACK-01")
        self.assertEqual(item.ancestor_ids, ("1", "2", "3"))

    def test_missing_parent_is_not_reattached(self):
        body = [
            ("BSN", "00", "S1", "20261114", "1200"),
            ("HL", "1", "", "S"),
            ("HL", "2", "1", "O"), ("PRF", "PO-01"),
            ("HL", "4", "99", "I"), ("LIN", "", "VP", "X"), ("SN1", "", "1", "EA"),
        ]
        result = parse(build(body))
        self.assertFalse(result.ok)
        self.assertIn("HL_PARENT_MISSING", codes(result))
        self.assertEqual(result.items, [])
        fact = [f for f in result.hl_facts if f.hl_id == "4"][0]
        self.assertFalse(fact.valid)
        self.assertIn("HL_PARENT_MISSING", fact.issues)
        # It must not appear under the O node.
        order_children = [
            f for f in result.hl_facts if f.hl_id in ("2", "4")
        ]
        self.assertEqual(len(order_children), 2)

    def test_adjacent_segments_do_not_supply_missing_fields(self):
        body = [
            ("BSN", "00", "S1", "20261114", "1200"),
            ("HL", "1", "", "S"),
            ("HL", "2", "1", "O"), ("PRF", "PO-01"),
            ("HL", "3", "2", "P"),  # P without MAN, followed by another O
            ("HL", "6", "1", "O"), ("PRF", "PO-02"),
            ("HL", "4", "3", "I"), ("LIN", "", "VP", "X"), ("SN1", "", "1", "EA"),
        ]
        result = parse(build(body))
        self.assertFalse(result.ok)
        self.assertIn("MAN_MISSING", codes(result))
        self.assertEqual(result.items, [])
        self.assertEqual(
            [d.hl_id for d in result.diagnostics if d.code == "MAN_MISSING"], ["3"]
        )

    def test_level_local_segments_reject_wrong_level(self):
        body = [
            ("BSN", "00", "S1", "20261114", "1200"),
            ("HL", "1", "", "S"), ("PRF", "PO-WRONG"),
            ("HL", "2", "1", "O"), ("PRF", "PO-01"),
            ("HL", "4", "2", "I"), ("LIN", "", "VP", "X"), ("SN1", "", "1", "EA"),
        ]
        result = parse(build(body))
        self.assertFalse(result.ok)
        self.assertIn("LEVEL_MISMATCH", codes(result))
        mismatch = [d for d in result.diagnostics if d.code == "LEVEL_MISMATCH"][0]
        self.assertEqual(mismatch.segment_no, 6)
        self.assertEqual(mismatch.hl_id, "1")

    def test_unsupported_level_path(self):
        body = [
            ("BSN", "00", "S1", "20261114", "1200"),
            ("HL", "1", "", "S"),
            ("HL", "2", "1", "O"), ("PRF", "PO-01"),
            ("HL", "4", "2", "I"), ("LIN", "", "VP", "X"), ("SN1", "", "1", "EA"),
            ("HL", "5", "4", "I"), ("LIN", "", "VP", "Y"), ("SN1", "", "1", "EA"),
        ]
        result = parse(build(body))
        self.assertFalse(result.ok)
        self.assertIn("HL_LEVEL_PATH", codes(result))

    def test_cycle_detected_and_both_members_invalid(self):
        result = parse_fixture("bad-cycle.edi")
        self.assertFalse(result.ok)
        self.assertIn("HL_CYCLE", codes(result))
        self.assertIn("HL_LEVEL_PATH", codes(result))
        self.assertEqual(result.items, [])
        invalid = {f.hl_id for f in result.hl_facts if not f.valid}
        self.assertTrue({"3", "4"}.issubset(invalid))

    def test_duplicate_hl_identity_never_merges(self):
        body = [
            ("BSN", "00", "S1", "20261114", "1200"),
            ("HL", "1", "", "S"),
            ("HL", "2", "1", "O"), ("PRF", "PO-01"),
            ("HL", "3", "2", "P"), ("MAN", "GM", "PK1"),
            ("HL", "3", "2", "P"), ("MAN", "GM", "PK2"),
            ("HL", "4", "3", "I"), ("LIN", "", "VP", "X"), ("SN1", "", "1", "EA"),
        ]
        result = parse(build(body))
        self.assertFalse(result.ok)
        self.assertIn("HL_ID_DUPLICATE", codes(result))
        self.assertEqual(result.items, [])
        duplicates = [f for f in result.hl_facts if f.hl_id == "3"]
        self.assertEqual(len(duplicates), 2)

    def test_duplicate_pack_codes_and_same_product_remain_separate_facts(self):
        body = [
            ("BSN", "00", "S1", "20261114", "1200"),
            ("HL", "1", "", "S"),
            ("HL", "2", "1", "O"), ("PRF", "PO-01"),
            ("HL", "3", "2", "P"), ("MAN", "GM", "SAME"),
            ("HL", "4", "3", "I"), ("LIN", "", "VP", "SKU"), ("SN1", "", "1", "EA"),
            ("HL", "5", "2", "P"), ("MAN", "GM", "SAME"),
            ("HL", "6", "5", "I"), ("LIN", "", "VP", "SKU"), ("SN1", "", "2", "EA"),
        ]
        result = parse(build(body))
        self.assertTrue(result.ok, codes(result))
        self.assertEqual(len(result.packages), 2)
        self.assertEqual(len(result.items), 2)
        self.assertEqual([i.hl_id for i in result.items], ["4", "6"])
        self.assertEqual({i.package_code for i in result.items}, {"SAME"})

    def test_cross_transaction_hl_ids_are_reusable_but_not_parents(self):
        tx_ok = make_transaction_d(shipment_body(), st_control="0001")
        tx_ok2 = make_transaction_d(shipment_body(), st_control="0002")
        reuse = parse(make_document([make_group([tx_ok, tx_ok2])]))
        self.assertTrue(reuse.ok, codes(reuse))
        self.assertEqual(len(reuse.items), 4)

        cross_body = [
            ("BSN", "00", "S2", "20261114", "1200"),
            ("HL", "7", "", "S"),
            ("HL", "8", "1", "O"), ("PRF", "PO-02"),
            ("HL", "9", "5", "I"), ("LIN", "", "VP", "X"), ("SN1", "", "1", "EA"),
        ]
        tx_cross = make_transaction_d(cross_body, st_control="0002")
        cross = parse(
            make_document([make_group([tx_ok, tx_cross])])
        )
        self.assertFalse(cross.ok)
        self.assertIn("HL_PARENT_MISSING", codes(cross))
        self.assertEqual(len(cross.shipments), 1)

    def test_multiple_orders_in_one_transaction(self):
        body = [
            ("BSN", "00", "S2", "20261114", "1200"),
            ("HL", "1", "", "S"),
            ("HL", "2", "1", "O"), ("PRF", "PO-A"),
            ("HL", "3", "1", "O"), ("PRF", "PO-B"),
            ("HL", "4", "2", "I"), ("LIN", "", "VP", "A-1"), ("SN1", "", "3", "EA"),
            ("HL", "5", "3", "P"), ("MAN", "GM", "PK-B"),
            ("HL", "6", "5", "I"), ("LIN", "", "VP", "B-1"), ("SN1", "", "7", "KG"),
        ]
        result = parse(build(body))
        self.assertTrue(result.ok, codes(result))
        self.assertEqual([o.order_number for o in result.orders], ["PO-A", "PO-B"])
        by_hl = {i.hl_id: i for i in result.items}
        self.assertEqual(by_hl["4"].order_number, "PO-A")
        self.assertIsNone(by_hl["4"].package_code)
        self.assertEqual(by_hl["6"].order_number, "PO-B")
        self.assertEqual(by_hl["6"].package_code, "PK-B")

    def test_unknown_extension_preserved_without_hierarchy_change(self):
        body = [
            ("BSN", "00", "S1", "20261114", "1200"),
            ("REF", "ZZ", "ABC"),
            ("HL", "1", "", "S"),
            ("HL", "2", "1", "O"), ("PRF", "PO-01"), ("DTM", "011", "20261115"),
            ("HL", "3", "2", "P"), ("MAN", "GM", "PK1"), ("TD1", "PALLET"),
            ("HL", "4", "3", "I"), ("LIN", "", "VP", "X"), ("SN1", "", "1", "EA"),
            ("ZZZ", "a", "b"),
        ]
        result = parse(build(body))
        self.assertTrue(result.ok, codes(result))
        self.assertEqual(len(result.items), 1)
        unknown = [s for s in result.segments if s.segment_id == "ZZZ"]
        self.assertEqual(len(unknown), 1)
        self.assertIn("SEGMENT_UNKNOWN", codes(result, severity="warning"))
        # The unknown segment is inside the I HL group and changes no parent.
        self.assertEqual(unknown[0].owner_hl_id, "4")

    def test_required_fields_per_level(self):
        cases = {
            "PRF_MISSING": [
                ("BSN", "00", "S1", "20261114", "1200"),
                ("HL", "1", "", "S"),
                ("HL", "2", "1", "O"),
            ],
            "MAN_MISSING": [
                ("BSN", "00", "S1", "20261114", "1200"),
                ("HL", "1", "", "S"),
                ("HL", "2", "1", "O"), ("PRF", "PO-01"),
                ("HL", "3", "2", "P"),
            ],
            "LIN_MISSING": [
                ("BSN", "00", "S1", "20261114", "1200"),
                ("HL", "1", "", "S"),
                ("HL", "2", "1", "O"), ("PRF", "PO-01"),
                ("HL", "4", "2", "I"), ("SN1", "", "1", "EA"),
            ],
            "SN1_MISSING": [
                ("BSN", "00", "S1", "20261114", "1200"),
                ("HL", "1", "", "S"),
                ("HL", "2", "1", "O"), ("PRF", "PO-01"),
                ("HL", "4", "2", "I"), ("LIN", "", "VP", "X"),
            ],
            "BSN_MISSING": [
                ("HL", "1", "", "S"),
                ("HL", "2", "1", "O"), ("PRF", "PO-01"),
            ],
        }
        for expected, body in cases.items():
            with self.subTest(code=expected):
                result = parse(build(body))
                self.assertFalse(result.ok)
                self.assertIn(expected, codes(result))
                self.assertEqual(result.items, [])

    def test_second_item_with_same_product_is_two_facts(self):
        result = parse_fixture("shipment.edi")
        self.assertEqual(len(result.items), 2)
        self.assertEqual({i.product_code for i in result.items}, {"SKU-1"})
        self.assertEqual(
            [i.quantity for i in result.items], [Decimal("2.50"), Decimal("1")]
        )
        self.assertEqual({i.unit for i in result.items}, {"EA"})

    def test_quantities_are_exact_and_units_not_summed(self):
        result = parse_fixture("shipment.edi")
        first, second = result.items
        self.assertIsInstance(first.quantity, Decimal)
        self.assertEqual(first.quantity_raw, "2.50")
        # Decimal preserves the authored scale; 2.50 is not rewritten to 2.5.
        self.assertEqual(str(first.quantity), "2.50")
        self.assertEqual(first.quantity.as_tuple().exponent, -2)
        # No conversion: 2.50 EA and 1 EA remain distinct, and 1 KG would not
        # be added to EA.
        body = [
            ("BSN", "00", "S1", "20261114", "1200"),
            ("HL", "1", "", "S"),
            ("HL", "2", "1", "O"), ("PRF", "PO-01"),
            ("HL", "4", "2", "I"), ("LIN", "", "VP", "X"), ("SN1", "", "0.10", "EA"),
            ("HL", "5", "2", "I"), ("LIN", "", "VP", "X"), ("SN1", "", "0.20", "EA"),
            ("HL", "6", "2", "I"), ("LIN", "", "VP", "X"), ("SN1", "", "1", "KG"),
        ]
        mixed = parse(build(body))
        self.assertTrue(mixed.ok, codes(mixed))
        self.assertEqual(
            [i.quantity for i in mixed.items],
            [Decimal("0.10"), Decimal("0.20"), Decimal("1")],
        )
        self.assertEqual([i.unit for i in mixed.items], ["EA", "EA", "KG"])
        self.assertNotIn("total_quantity", mixed.items[0].__dict__)

    def test_invalid_quantities(self):
        for bad in ("0", "-1", "abc", "1e3", "0.000"):
            with self.subTest(qty=bad):
                body = [
                    ("BSN", "00", "S1", "20261114", "1200"),
                    ("HL", "1", "", "S"),
                    ("HL", "2", "1", "O"), ("PRF", "PO-01"),
                    ("HL", "4", "2", "I"), ("LIN", "", "VP", "X"),
                    ("SN1", "", bad, "EA"),
                ]
                result = parse(build(body))
                self.assertFalse(result.ok)
                self.assertIn("SN1_QUANTITY_INVALID", codes(result))
                self.assertEqual(result.items, [])


class ProvenanceTests(unittest.TestCase):
    def test_every_item_points_back_to_st_string_and_segment_range(self):
        result = parse_fixture("shipment.edi")
        for item in result.items:
            self.assertEqual(item.source.st_control_number, "0001")
            self.assertEqual(item.source.st_segment_no, 3)
            self.assertEqual(item.source.se_segment_no, 16)
            self.assertEqual(item.source.interchange_no, 1)
            self.assertEqual(item.source.group_no, 1)
            self.assertEqual(item.source.transaction_no, 1)
            self.assertLessEqual(item.source.segment_start, item.source.segment_end)
            self.assertLessEqual(item.lin_segment_no, item.source.segment_end)
            self.assertLessEqual(item.sn1_segment_no, item.source.segment_end)
            ids = item.source.segment_ids.split(",")
            self.assertIn("LIN", ids)
            self.assertIn("SN1", ids)
            row = item.source.as_row()
            self.assertEqual(row["st_segment_no"], "3")
            self.assertEqual(row["se_segment_no"], "16")

    def test_hl_facts_carry_group_ranges_and_own_segments(self):
        result = parse_fixture("shipment.edi")
        facts = {f.hl_id: f for f in result.hl_facts}
        self.assertEqual(facts["1"].group_start, 5)
        self.assertEqual(facts["1"].group_end, 5)
        self.assertEqual(facts["2"].group_start, 8)
        self.assertEqual(facts["2"].group_end, 9)
        self.assertEqual(facts["3"].group_start, 6)
        self.assertEqual(facts["3"].group_end, 7)
        self.assertEqual(facts["4"].group_start, 10)
        self.assertEqual(facts["4"].group_end, 12)
        self.assertEqual(facts["5"].group_start, 13)
        self.assertEqual(facts["5"].group_end, 15)

    def test_segment_table_ownership_covers_all_hl_groups(self):
        result = parse_fixture("shipment.edi")
        owners = {
            seg.owner_hl_id: seg
            for seg in result.segments
            if seg.segment_id == "HL"
        }
        self.assertEqual(set(owners), {"1", "2", "3", "4", "5"})
        # Each non-envelope segment belongs to exactly one HL group or the
        # transaction preamble, never to two groups.
        for seg in result.segments:
            if seg.segment_id in {"HL", "LIN", "SN1", "PRF", "MAN"}:
                self.assertIsNotNone(seg.owner_hl_id, seg.segment_id)

    def test_failed_hl_groups_are_marked_in_segment_table(self):
        result = parse_fixture("bad-cycle.edi")
        failed = [s for s in result.segments if s.status == "failed"]
        self.assertTrue(failed)
        self.assertTrue(any("HL_CYCLE" in s.detail for s in failed))


class ExportTests(unittest.TestCase):
    def test_export_writes_expected_tables(self):
        result = parse_fixture("shipment.edi")
        with tempfile.TemporaryDirectory() as tmp:
            paths = export(result, tmp)
            expected = {
                "shipments", "orders", "packages", "items", "hl_groups",
                "segments", "scopes", "diagnostics", "envelope_checks",
                "delimiters", "summary",
            }
            self.assertEqual(set(paths), expected)
            for path in paths.values():
                self.assertTrue(Path(path).is_file())

            with Path(paths["items"]).open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            first = rows[0]
            self.assertEqual(first["product_code"], "SKU-1")
            self.assertEqual(first["quantity_raw"], "2.50")
            self.assertEqual(first["unit"], "EA")
            self.assertEqual(first["ancestor_ids"], "1>2>3")
            self.assertEqual(first["path_ids"], "1>2>3>4")
            self.assertEqual(first["st_segment_no"], "3")
            self.assertEqual(first["se_segment_no"], "16")
            self.assertEqual(first["source_segment_start"], "10")
            self.assertEqual(first["source_segment_end"], "12")

            with Path(paths["segments"]).open(newline="", encoding="utf-8") as handle:
                seg_rows = list(csv.DictReader(handle))
            self.assertEqual(len(seg_rows), 18)
            self.assertEqual(seg_rows[0]["segment_id"], "ISA")
            self.assertEqual(seg_rows[-1]["segment_id"], "IEA")

            summary = json.loads(Path(paths["summary"]).read_text())
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["counts"]["items"], 2)
            self.assertEqual(summary["delimiters"]["segment"], "~")

    def test_export_of_failed_fixture_has_no_fact_rows_but_has_diagnostics(self):
        result = parse_fixture("bad-count.edi")
        with tempfile.TemporaryDirectory() as tmp:
            paths = export(result, tmp)
            with Path(paths["items"]).open(newline="", encoding="utf-8") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])
            with Path(paths["diagnostics"]).open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertIn("SE_COUNT", {row["code"] for row in rows})

    def test_alternate_delimiter_export_is_equivalent(self):
        result = parse_fixture("alternate.edi")
        with tempfile.TemporaryDirectory() as tmp:
            paths = export(result, tmp)
            with Path(paths["items"]).open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([r["product_code"] for r in rows], ["SKU-1", "SKU-1"])
            self.assertEqual([r["quantity_raw"] for r in rows], ["2.50", "1"])


class SupportBoundaryTests(unittest.TestCase):
    def test_scope_constants_match_fixture_scope(self):
        scope = json.loads((FIXTURES / "scope.json").read_text())
        self.assertEqual(set(scope["known_segments"]), set(KNOWN_SEGMENTS))
        for parent, children in scope["hl_levels"].items():
            self.assertEqual(
                set(children), set(ALLOWED_CHILD_LEVELS[parent])
            )

    def test_package_and_item_rows_are_separate_relationship_tables(self):
        result = parse_fixture("shipment.edi")
        self.assertEqual(len(result.shipments), 1)
        self.assertEqual(len(result.orders), 1)
        self.assertEqual(len(result.packages), 1)
        self.assertEqual(len(result.items), 2)
        self.assertEqual(result.items[0].order_number, result.orders[0].order_number)
        self.assertEqual(
            result.items[0].package_code, result.packages[0].pack_code
        )


if __name__ == "__main__":
    unittest.main()


class FailureIsolationTests(unittest.TestCase):
    def test_bad_transaction_inside_group_does_not_kill_sibling(self):
        good = make_transaction_d(shipment_body(order_number="PO-GOOD"), st_control="0001")
        bad = make_transaction_d(
            shipment_body(order_number="PO-BAD"), st_control="0002", se_count=13
        )
        result = parse(make_document([make_group([good, bad])]))
        statuses = {(s.scope, s.scope_id): s for s in result.scope_statuses}
        self.assertEqual(statuses[("transaction", "1.1.1")].status, "ok")
        self.assertEqual(statuses[("transaction", "1.1.2")].status, "failed")
        self.assertEqual(statuses[("group", "1.1")].status, "ok")
        self.assertEqual(statuses[("interchange", "1")].status, "ok")
        self.assertEqual([o.order_number for o in result.orders], ["PO-GOOD"])
        self.assertEqual(len(result.shipments), 1)

    def test_bad_group_does_not_kill_sibling_group(self):
        good = make_group(
            [make_transaction_d(shipment_body(order_number="G1"), st_control="0001")],
            gs_control="1",
        )
        bad = make_group(
            [make_transaction_d(shipment_body(order_number="G2"), st_control="0001")],
            gs_control="2",
            ge_count=5,
        )
        result = parse(make_document([good, bad]))
        statuses = {s.scope_id: s for s in result.scope_statuses if s.scope != "transaction"}
        self.assertEqual(statuses["1"].status, "ok")
        self.assertEqual(statuses["1.1"].status, "ok")
        self.assertEqual(statuses["1.2"].status, "failed")
        self.assertEqual([o.order_number for o in result.orders], ["G1"])

    def test_bad_interchange_does_not_kill_sibling_interchange(self):
        good = make_document(
            [make_group([make_transaction_d(shipment_body(order_number="I2"), st_control="0001")])],
            isa_control="000000002",
        )
        bad = make_document(
            [make_group([make_transaction_d(shipment_body(order_number="I1"), st_control="0001")])],
            isa_control="000000001",
            iea_count=9,
        )
        result = parse(bad + good)
        interchange = [s for s in result.scope_statuses if s.scope == "interchange"]
        self.assertEqual([s.status for s in interchange], ["failed", "ok"])
        self.assertEqual([o.order_number for o in result.orders], ["I2"])

    def test_failure_reasons_name_the_original_segment(self):
        result = parse_fixture("bad-cycle.edi")
        cycle = [d for d in result.diagnostics if d.code == "HL_CYCLE"][0]
        self.assertEqual(cycle.segment_no, 6)
        self.assertEqual(cycle.hl_id, "3")
        level = [d for d in result.diagnostics if d.code == "HL_LEVEL_PATH"][0]
        self.assertEqual(level.segment_no, 6)
        self.assertIn("S->O->P->I", level.message)

    def test_segment_range_is_independent_of_status_flags(self):
        # Hand-count a constructed document and compare with the rescan.
        result = parse_fixture("shipment.edi")
        transaction = [c for c in result.envelope_checks if c["scope"] == "transaction"][0]
        window = [
            s
            for s in result.segments
            if transaction["segment_start"] <= s.segment_no <= transaction["segment_end"]
        ]
        manual = [
            "ST", "BSN", "HL", "HL", "MAN", "HL", "PRF",
            "HL", "LIN", "SN1", "HL", "LIN", "SN1", "SE",
        ]
        self.assertEqual([s.segment_id for s in window], manual)
        self.assertEqual(len(window), 14)
        self.assertEqual(transaction["declared_count"], 14)


class StreamTests(unittest.TestCase):
    def test_two_interchanges_with_different_delimiters(self):
        first = make_document(
            [make_group([make_transaction_d(shipment_body(order_number="PO-1"))])]
        )
        second = make_document(
            [
                make_group(
                    [
                        make_transaction_d(
                            shipment_body(order_number="PO-2"),
                            element="|",
                            segment="!",
                        )
                    ],
                    element="|",
                    segment="!",
                )
            ],
            element="|",
            component=">",
            segment="!",
            isa_control="000000002",
        )
        result = parse(first + second)
        self.assertTrue(result.ok, codes(result))
        self.assertEqual(
            [(o.order_number, o.source.interchange_no) for o in result.orders],
            [("PO-1", 1), ("PO-2", 2)],
        )
        self.assertEqual(len(result.segments), 36)
        self.assertEqual(result.segments[18].segment_no, 19)
        transaction_checks = [
            c for c in result.envelope_checks if c["scope"] == "transaction"
        ]
        self.assertEqual(len(transaction_checks), 2)
        for check in transaction_checks:
            self.assertEqual(check["actual_count"], 14)
            self.assertTrue(check["count_matches"])

    def test_no_isa_stream_is_one_failed_interchange(self):
        result = parse("GS*SH*a*b*20261114*1200*1*X*005010~")
        self.assertFalse(result.ok)
        self.assertIn("ISA_MISSING", codes(result))
        self.assertEqual(len(result.scope_statuses), 1)
        self.assertEqual(result.scope_statuses[0].status, "failed")

    def test_empty_stream(self):
        result = parse("")
        self.assertFalse(result.ok)
        self.assertIn("ISA_MISSING", codes(result))
