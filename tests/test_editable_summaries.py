import unittest
from pathlib import Path

import pymupdf as fitz
from openpyxl import load_workbook

from core.export import build_pdf, build_xlsx, summarise
from core.match import matching_totals, merged_rows, parents
from core.summary import (build_summary, group_id, natural_key, summary_pairs,
                          summary_totals)
from core.tagrename import propagate_rename


def record(name, page, x, *, original=None, unit_no="", category="floor"):
    return {
        "page": page, "unit": name,
        "original_unit": name if original is None else original,
        "unit_no": unit_no, "matched_text": name, "source": "pdf",
        "x": x, "y": 10, "w": 20, "h": 8, "floor_level": f"LEVEL {page}",
    }


class EditableSummaryTests(unittest.TestCase):
    def test_global_rename_keeps_group_and_category_isolated(self):
        floor = [record("A2", 1, 10), record("A2", 1, 50), record("A2", 2, 20)]
        units = [record("A2", 3, 10, category="unit")]
        propagate_rename(floor, floor[0], "A2.1")
        self.assertEqual([r["unit"] for r in floor], ["A2.1"] * 3)
        self.assertEqual(units[0]["unit"], "A2")
        propagate_rename(floor, floor[1], "A2.2")
        self.assertEqual([r["unit"] for r in floor], ["A2.2"] * 3)
        self.assertEqual([r["original_unit"] for r in floor], ["A2"] * 3)
        self.assertEqual(build_summary(floor, {}, "floor")[0]["Name"], "A2.2")
        self.assertEqual(build_summary(units, {}, "unit")[0]["Name"], "A2")

    def test_summary_corrections_are_separate_and_group_ids_stable(self):
        records = [record("A2", 1, 10), record("A2", 2, 10), record("A10", 2, 50)]
        before = [dict(r) for r in records]
        rows = build_summary(records, {}, "floor")
        row = next(r for r in rows if r["Name"] == "A2")
        corrected = build_summary(records, {row["Group ID"]: 1}, "floor")
        changed = next(r for r in corrected if r["Name"] == "A2")
        self.assertEqual((changed["Detected Qty"], changed["Corrected Qty"]), (2, 1))
        self.assertEqual(group_id(records[0], "floor"), row["Group ID"])
        self.assertNotEqual(group_id(records[0], "floor"), group_id(records[0], "unit"))
        self.assertEqual(records, before)
        self.assertEqual([r["Name"] for r in rows], ["A2", "A10"])

    def test_floor_and_unit_summary_rows_use_the_canonical_name_key(self):
        floor_rows = build_summary([record("MA2", 1, 10)], {}, "floor")
        unit_rows = build_summary([record("UNIT10", 2, 20)], {}, "unit")

        # These are the exact row structures consumed after editable_summary returns.
        floor_names = [row["Name"] for row in floor_rows]
        unit_names = [row["Name"] for row in unit_rows]
        self.assertEqual(floor_names, ["MA2"])
        self.assertEqual(unit_names, ["UNIT10"])
        for row in floor_rows + unit_rows:
            self.assertIn("Detected Qty", row)
            self.assertIn("Corrected Qty", row)

    def test_three_independent_grand_totals(self):
        floor_rows = [
            {"Detected Qty": 5, "Corrected Qty": 3},
            {"Detected Qty": 2, "Corrected Qty": 4},
        ]
        unit_rows = [
            {"Detected Qty": 4, "Corrected Qty": 1},
        ]
        self.assertEqual(summary_totals(floor_rows), {"detected": 7, "corrected": 7})
        self.assertEqual(summary_totals(unit_rows), {"detected": 4, "corrected": 1})
        matching = matching_totals(merged_rows(
            [("A2", 3), ("A10", 4)], {"UNIT A": ["A2", "A10"]}, []))
        self.assertEqual(matching, {"qty": 7, "merged": 7})

    def test_summary_sections_follow_their_scan_sections_once(self):
        source = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")
        floor_scan = source.index('st.success(f"**{len(tags)} units** found')
        floor_summary = source.index('st.subheader("2. Floor Tag Summary")')
        floor_review = source.index('st.subheader("3. Check every tag")')
        unit_scan = source.index('st.success(f"**{len(units)} unit labels**')
        unit_summary = source.index('st.subheader("6. Unit Plan Summary")')
        unit_review = source.index('walkthrough(store="units"')
        matching = source.index('st.subheader("7. Put each floor tag under its unit plan")')
        self.assertLess(floor_scan, floor_summary)
        self.assertLess(floor_summary, floor_review)
        self.assertLess(unit_scan, unit_summary)
        self.assertLess(unit_summary, unit_review)
        self.assertLess(unit_summary, matching)
        self.assertEqual(source.count('st.subheader("2. Floor Tag Summary")'), 1)
        self.assertEqual(source.count('st.subheader("6. Unit Plan Summary")'), 1)
        self.assertEqual(source.count("show_summary_total(floor_summary_rows"), 1)
        self.assertEqual(source.count("show_summary_total(unit_summary_rows"), 1)

    def test_normalized_name_groups_match_global_rename_semantics(self):
        records = [record("A2", 1, 10), record("a2", 2, 20, original=" a2 ")]
        rows = build_summary(records, {}, "floor")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Detected Qty"], 2)

    def test_corrected_floor_quantity_flows_through_matching_and_merge(self):
        records = [record("MA1", 1, 10) for _ in range(5)]
        rows = build_summary(records, {}, "floor")
        pairs = summary_pairs([{**rows[0], "Corrected Qty": 3}])
        cards, pool = parents(pairs, ["MA1"])
        assigned = {c["unit"]: [kid["tag"] for kid in c["kids"]] for c in cards}
        table = merged_rows(pairs, assigned, [])
        self.assertEqual(table[0]["qty"], 3)
        self.assertEqual(table[0]["merged"], 3)
        self.assertEqual(len(records), 5)

    def test_manual_assignment_survives_quantity_change(self):
        old = [("A2", 4), ("A10", 2)]
        new = [("A2", 3), ("A10", 2)]
        assigned = {"UNIT A": ["A2"], "UNIT B": ["A10"]}
        manual = ["A10"]
        table = merged_rows(new, assigned, manual)
        a10 = next(row for row in table if row["tag"] == "A10")
        self.assertEqual(a10["unit"], "UNIT B")
        self.assertTrue(a10["manual"])
        self.assertEqual(sum(row["qty"] for row in table), 5)
        self.assertEqual(sum(row["merged"] for row in table), 5)
        self.assertEqual(sum(qty for _, qty in old), 6)

    def test_grand_totals_update_for_corrected_qty_and_manual_assignment(self):
        summary = [("A2", 3), ("A10", 2)]
        before = merged_rows(summary, {"UNIT A": ["A2", "A10"]}, [])
        after = merged_rows(summary, {"UNIT A": ["A2"], "UNIT B": ["A10"]}, ["A10"])
        self.assertEqual(matching_totals(before), {"qty": 5, "merged": 5})
        self.assertEqual(matching_totals(after), {"qty": 5, "merged": 5})

        corrected = merged_rows([("A2", 1), ("A10", 2)],
                                {"UNIT A": ["A2"], "UNIT B": ["A10"]}, ["A10"])
        self.assertEqual(matching_totals(corrected), {"qty": 3, "merged": 3})

    def test_matcher_html_renders_one_visible_total_outside_scroll_region(self):
        html = (Path(__file__).parents[1] / "components" / "matcher" / "index.html").read_text()
        self.assertEqual(html.count('id="grand-total"'), 1)
        self.assertIn('class="total-table" aria-label="Matching totals"', html)
        self.assertLess(html.index("</div>\n<table class=\"total-table\""),
                        html.index('<script>'))
        self.assertIn("max-height: 520px", html)
        self.assertIn('totalBody.textContent = "";', html)
        self.assertIn("totalBody.appendChild(total);", html)
        self.assertIn("groups.reduce((sum, group) => sum + (Number(group.mergeQty) || 0), 0)", html)
        self.assertIn("tr.grand-total td { font-weight: 700;", html)

    def test_excel_corrected_summaries_and_actual_details(self):
        floor = [record("MA1", 1, 10) for _ in range(5)]
        units = [record("UNIT2", 1, 100), record("UNIT2", 2, 100)]
        plans, pool = parents([("MA1", 3)], ["UNIT2"])
        merged = merged_rows([("MA1", 3)], {}, [])
        output = build_xlsx(
            floor, units, merged, {1: "S1", 2: "S2"},
            floor_summary=[("MA1", 3)], unit_summary=[("UNIT2", 1)],
        )
        workbook = load_workbook(__import__("io").BytesIO(output), data_only=True)
        self.assertEqual(workbook["Floor Tags"]["B2"].value, 3)
        self.assertEqual(workbook["Unit Tags"]["B2"].value, 1)
        self.assertEqual(workbook["Merged"]["C2"].value, 3)
        self.assertEqual(workbook["Merged"]["D2"].value, 3)
        detail = workbook["Floor Tags by Level & Sheet"]
        self.assertEqual(sum(detail.cell(r, 5).value or 0 for r in range(2, detail.max_row)), 5)

    def test_pdf_still_marks_every_actual_occurrence(self):
        records = [record("A2", 1, 10) for _ in range(5)]
        document = fitz.open()
        document.new_page(width=200, height=200)
        output = fitz.open(stream=build_pdf(document.tobytes(), records), filetype="pdf")
        annotations = list(output[0].annots() or [])
        self.assertEqual(len(annotations), 5)

    def test_natural_sort_supports_numeric_and_text_labels(self):
        names = ["MA12", "2101", "MA2", "MA10", "ma1"]
        self.assertEqual(sorted(names, key=natural_key), ["2101", "ma1", "MA2", "MA10", "MA12"])
        self.assertEqual([name for name, _ in summarise([record(n, 1, i) for i, n in enumerate(names)])],
                         ["2101", "ma1", "MA2", "MA10", "MA12"])

    def test_matching_rows_use_natural_order_inside_plan_groups(self):
        rows = merged_rows([("MA10", 1), ("MA2", 1), ("MA1", 1)],
                           {"UNIT A": ["MA10", "MA2", "MA1"]}, [])
        self.assertEqual([row["tag"] for row in rows], ["MA1", "MA2", "MA10"])


if __name__ == "__main__":
    unittest.main()
