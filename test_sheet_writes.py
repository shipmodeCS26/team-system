import unittest
from unittest.mock import patch

import sheet_writes
from sheet_writes import check_writes

MAPS = {"onset": {("Incoming Stocks", "OS-FOU-30 Amazon bags status"): "G328",
                  ("Incoming Stocks", "OS-FOU-30 Amazon bags note"): "M328"}}


def write(cell="G328", value="Received", key="OS-FOU-30 Amazon bags status"):
    return {"tab": "Incoming Stocks", "key": key, "cell": cell, "value": value}


@patch.dict(sheet_writes.CELL_MAPS, MAPS, clear=True)
class SheetWrites(unittest.TestCase):
    def test_write_in_place(self):
        self.assertEqual(check_writes("onset", [write()], {("Incoming Stocks", "G328"): "Received"}), [])

    def test_wrong_row_is_caught(self):
        # The Oct 27 mistake: status written to G329 instead of G328.
        problems = check_writes("onset", [write("G329")], {("Incoming Stocks", "G329"): "Received"})
        self.assertEqual(problems, ["Incoming Stocks G329 (expected G328 for OS-FOU-30 Amazon bags status)"])

    def test_cell_case_insensitive(self):
        self.assertEqual(check_writes("onset", [write("g328")], {("Incoming Stocks", "G328"): "Received"}), [])

    def test_no_approved_cell(self):
        problems = check_writes("onset", [write(key="Unknown row")], {})
        self.assertIn("no approved cell", problems[0])

    def test_client_without_a_map(self):
        self.assertIn("no approved cell", check_writes("muravai", [write()], {})[0])

    def test_not_re_read(self):
        self.assertIn("not re-read", check_writes("onset", [write()], {})[0])

    def test_re_read_differs(self):
        problems = check_writes("onset", [write(value="1,709")], {("Incoming Stocks", "G328"): "1,790"})
        self.assertIn("re-read '1,790', wrote '1,709'", problems[0])

    def test_number_formatting_is_not_a_difference(self):
        self.assertEqual(check_writes("onset", [write(value=1709)], {("Incoming Stocks", "G328"): "1,709"}), [])

    def test_commas_in_text_still_count(self):
        note = write("M328", "Amazon bags, OS-FOU-30", "OS-FOU-30 Amazon bags note")
        problems = check_writes("onset", [note], {("Incoming Stocks", "M328"): "Amazon bags OS-FOU-30"})
        self.assertIn("re-read", problems[0])

    def test_every_write_checked(self):
        writes = [write(), write("M329", "Amazon bags = OS-FOU-30", "OS-FOU-30 Amazon bags note")]
        problems = check_writes("onset", writes, {("Incoming Stocks", "G328"): "Received",
                                                   ("Incoming Stocks", "M329"): "Amazon bags = OS-FOU-30"})
        self.assertEqual(problems, ["Incoming Stocks M329 (expected M328 for OS-FOU-30 Amazon bags note)"])


class NoMapsApproved(unittest.TestCase):
    def test_every_write_held_until_maps_are_approved(self):
        self.assertEqual(sheet_writes.CELL_MAPS, {})
        self.assertIn("no approved cell", check_writes("onset", [write()], {("Incoming Stocks", "G328"): "Received"})[0])


if __name__ == "__main__":
    unittest.main()
