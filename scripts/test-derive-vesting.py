#!/usr/bin/env python3
"""Tests for derive-vesting.py's gates.

    python scripts/test-derive-vesting.py

Runs derive-vesting.py as a subprocess against variants of
vesting/questionnaire.example.yaml, so what is tested is what an operator
runs: the exit status and the message, not an internal function.

Exit status contract being tested:
    0  vestable
    1  schema or cross-field error (the form is wrong)
    2  Halt (the form is coherent and describes something the platform must not do)

Needs no AWS account.
"""

import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "derive-vesting.py"
EXAMPLE = REPO_ROOT / "vesting" / "questionnaire.example.yaml"


def run(q):
    """Write q to a temp file, run derive-vesting.py on it, return (rc, stderr)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "q.yaml"
        path.write_text(yaml.safe_dump(q, sort_keys=False), encoding="utf-8")
        p = subprocess.run(
            [sys.executable, str(SCRIPT), "--questionnaire", str(path), "--json"],
            capture_output=True, text=True, encoding="utf-8",
        )
    return p.returncode, p.stderr


class Baseline(unittest.TestCase):
    """The example must vest. If it does not, every other result is meaningless."""

    def test_example_vests(self):
        base = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
        rc, err = run(base)
        self.assertEqual(rc, 0, err)


class CardholderScope(unittest.TestCase):
    """D-025: any PCI answer halts until the platform has a CDE design."""

    def setUp(self):
        self.base = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
        self.assertFalse(self.base["compliance"]["cardholder_data"])
        self.assertNotIn("pci-dss", self.base["compliance"]["frameworks"])

    def variant(self, cardholder, pci):
        q = copy.deepcopy(self.base)
        q["compliance"]["cardholder_data"] = cardholder
        if pci:
            q["compliance"]["frameworks"].append("pci-dss")
        return q

    def assertHalts(self, q):
        rc, err = run(q)
        self.assertEqual(rc, 2, f"expected Halt (2), got {rc}:\n{err}")
        self.assertIn("HALT", err)
        self.assertIn("D-025", err)
        # The message must name every decision that unblocks it, or the
        # operator has nothing to escalate.
        for needle in ("CDE design", "dedicated OU", "'locked' egress", "B-010",
                       "Security and Infrastructure"):
            self.assertIn(needle, err)
        return err

    def test_cardholder_without_pci_halts(self):
        self.assertIn("cardholder_data is true", self.assertHalts(self.variant(True, False)))

    def test_cardholder_with_pci_halts(self):
        err = self.assertHalts(self.variant(True, True))
        self.assertIn("cardholder_data is true and 'pci-dss' is in frameworks", err)

    def test_pci_without_cardholder_halts(self):
        # The SAQ A case. Halts by founder decision; the message must say so,
        # so the answer is escalated rather than edited away.
        err = self.assertHalts(self.variant(False, True))
        self.assertIn("'pci-dss' is in frameworks", err)
        self.assertIn("SAQ A", err)
        self.assertIn("Do not change", err)

    def test_halt_still_reports_form_errors(self):
        # Halting checks run last so earlier form errors are not hidden.
        q = self.variant(True, False)
        q["compliance"]["frameworks"] = ["none", "hipaa"]
        rc, err = run(q)
        self.assertEqual(rc, 2, err)
        self.assertIn("'none' cannot be combined", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
