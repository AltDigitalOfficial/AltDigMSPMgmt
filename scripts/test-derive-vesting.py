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
import json
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


def run_report(q):
    """As run(), but the human report, which is where warnings appear."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "q.yaml"
        path.write_text(yaml.safe_dump(q, sort_keys=False), encoding="utf-8")
        p = subprocess.run(
            [sys.executable, str(SCRIPT), "--questionnaire", str(path)],
            capture_output=True, text=True, encoding="utf-8",
        )
    return p.returncode, p.stdout + p.stderr


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


def run_json(q):
    """As run(), but the derived values parsed, for asserting on what was computed."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "q.yaml"
        path.write_text(yaml.safe_dump(q, sort_keys=False), encoding="utf-8")
        p = subprocess.run(
            [sys.executable, str(SCRIPT), "--questionnaire", str(path), "--json"],
            capture_output=True, text=True, encoding="utf-8",
        )
    return p.returncode, (json.loads(p.stdout) if p.returncode == 0 else p.stderr)


WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]
EVERY_DAY = WEEKDAYS + ["sat", "sun"]


class Contacts(unittest.TestCase):
    """3.5: a breach notification contact is required; security ops is not."""

    def setUp(self):
        self.base = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))

    def test_breach_contact_is_required(self):
        q = copy.deepcopy(self.base)
        q["identity"].pop("breach_notification_contact")
        rc, err = run(q)
        self.assertEqual(rc, 1, err)
        self.assertIn("breach_notification_contact", err)

    def test_security_contact_is_optional(self):
        q = copy.deepcopy(self.base)
        q["identity"].pop("security_contact")
        rc, err = run(q)
        self.assertEqual(rc, 0, err)


class Ticketing(unittest.TestCase):
    """Form 12.4: each ticketing system needs its own fields and no others."""

    def setUp(self):
        self.base = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))

    def q(self, **ticketing):
        q = copy.deepcopy(self.base)
        q["change"]["ticketing"] = ticketing
        return q

    def test_client_jira_without_site_or_key_warns_but_vests(self):
        # Form 12.4 is Dev, not Vest: account creation must not wait on a
        # ticketing project that does not exist yet (OEight, 2026-10-01).
        rc, out = run_report(self.q(system="client-jira", product="jira-service-management"))
        self.assertEqual(rc, 0, out)
        self.assertIn("project_key, site not yet given", out)
        self.assertIn("UNAVAILABLE", out)

    def test_altdigital_jira_refuses_a_site(self):
        rc, err = run(self.q(system="altdigital-jira", product="jira-service-management",
                             project_key="ARC8", site="https://x.atlassian.net"))
        self.assertEqual(rc, 1, err)
        self.assertIn("always AltDigital's own site", err)

    def test_a_non_atlassian_site_fails_the_schema(self):
        rc, err = run(self.q(system="client-jira", product="jira-service-management",
                             project_key="ARC8", site="https://jira.example.com"))
        self.assertEqual(rc, 1, err)

    def test_none_is_allowed_and_warned(self):
        rc, out = run_report(self.q(system="none"))
        self.assertEqual(rc, 0, out)
        self.assertIn("UNAVAILABLE", out)

    def test_jira_software_is_warned(self):
        rc, out = run_report(self.q(system="altdigital-jira", product="jira-software",
                                    project_key="ARC8"))
        self.assertEqual(rc, 0, out)
        self.assertIn("cannot check WHO approved", out)


class Frameworks(unittest.TestCase):
    """4.1: hitrust is askable, and nothing applied for it is said out loud."""

    def setUp(self):
        self.base = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))

    def test_hitrust_vests_and_keeps_hipaa_retention(self):
        q = copy.deepcopy(self.base)
        q["compliance"]["frameworks"] = ["hipaa", "hitrust", "soc2"]
        rc, d = run_json(q)
        self.assertEqual(rc, 0, d)
        self.assertEqual(d["retention_floor_days"], 2190)
        self.assertEqual(d["frameworks_without_pack"], ["hitrust"])

    def test_hitrust_without_a_pack_is_warned(self):
        # "hipaa, hitrust" derives the HIPAA pack, so a warning that fired
        # only when no pack at all was derived would stay silent.
        q = copy.deepcopy(self.base)
        q["compliance"]["frameworks"] = ["hipaa", "hitrust"]
        rc, out = run_report(q)
        self.assertEqual(rc, 0, out)
        self.assertIn("No conformance pack is applied for: hitrust", out)


class MaintenanceWindows(unittest.TestCase):
    """Form 5.6 / 5.7: two windows, each in its own timezone, compared in UTC."""

    def setUp(self):
        self.base = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))

    def q(self, nonprod=None, prod=None):
        q = copy.deepcopy(self.base)
        if nonprod is not None:
            q["resilience"]["maintenance_windows"]["nonprod"] = nonprod
        if prod is not None:
            q["resilience"]["maintenance_windows"]["prod"] = prod
        return q

    # --- shape ---------------------------------------------------------------

    def test_single_window_is_rejected(self):
        # The old shape must fail loudly rather than be half-read.
        q = copy.deepcopy(self.base)
        q["resilience"].pop("maintenance_windows")
        q["resilience"]["maintenance_window"] = {"days": ["tue"], "start": "08:00", "end": "10:00"}
        rc, err = run(q)
        self.assertEqual(rc, 1, err)
        self.assertIn("schema validation", err)

    def test_timezone_abbreviation_is_rejected(self):
        # "IST" is how the first real questionnaire answered 5.2. It is India,
        # Ireland or Israel depending on who is asked.
        q = copy.deepcopy(self.base)
        q["environments"]["timezone"] = "IST"
        rc, err = run(q)
        self.assertEqual(rc, 1, err)
        self.assertIn("environments.timezone", err)

    def test_unresolvable_timezone_is_rejected(self):
        # Right shape, no such zone: the schema cannot catch it, the check must.
        q = copy.deepcopy(self.base)
        q["environments"]["timezone"] = "America/Atlantis"
        rc, err = run(q)
        self.assertEqual(rc, 1, err)
        self.assertIn("not a timezone", err)

    def test_start_equal_to_end_is_rejected(self):
        rc, err = run(self.q(prod={"days": ["sun"], "start": "02:00", "end": "02:00"}))
        self.assertEqual(rc, 1, err)
        self.assertIn("could mean no time at all", err)

    # --- production ----------------------------------------------------------

    def test_prod_window_crossing_midnight_vests(self):
        # Production is always on, and 23:00-01:00 is how people say a window.
        rc, d = run_json(self.q(prod={"days": EVERY_DAY, "start": "23:00", "end": "01:00",
                                      "timezone": "America/Denver"}))
        self.assertEqual(rc, 0, d)
        self.assertFalse(d["maintenance_windows"]["prod"]["wake_required"])
        self.assertEqual(d["maintenance_windows"]["prod"]["timezone"], "America/Denver")

    # --- non-production, chosen ----------------------------------------------

    def test_outside_developer_hours_is_derived_and_needs_waking(self):
        rc, d = run_json(self.q(nonprod="outside-developer-hours"))
        self.assertEqual(rc, 0, d)
        dev = d["maintenance_windows"]["dev"]
        # The example HOOP ends 19:00; the rule starts two hours after, for two hours.
        self.assertEqual((dev["start"], dev["end"]), ("21:00", "23:00"))
        self.assertEqual(dev["days"], WEEKDAYS)
        self.assertTrue(dev["wake_required"])

    def test_during_developer_hours_needs_no_wake(self):
        rc, d = run_json(self.q(nonprod="during-developer-hours"))
        self.assertEqual(rc, 0, d)
        dev = d["maintenance_windows"]["dev"]
        self.assertEqual((dev["start"], dev["end"]), ("17:00", "19:00"))
        self.assertFalse(dev["wake_required"])
        rc, out = run_report(self.q(nonprod="during-developer-hours"))
        self.assertNotIn("B-141", out)

    def test_outside_window_pushed_past_midnight_moves_its_days(self):
        # A HOOP ending 23:00 puts the outside window at 01:00 the NEXT day.
        q = self.q(nonprod="outside-developer-hours")
        for env in ("dev", "test"):
            q["environments"]["hoop"][env].update({"start": "13:00", "end": "23:00"})
        rc, d = run_json(q)
        self.assertEqual(rc, 0, d)
        dev = d["maintenance_windows"]["dev"]
        self.assertEqual(dev["start"], "01:00")
        self.assertEqual(dev["days"], ["tue", "wed", "thu", "fri", "sat"])

    def test_choice_with_an_always_on_environment_is_rejected(self):
        q = self.q(nonprod="outside-developer-hours")
        q["environments"]["hoop"]["test"] = {"always_on": True}
        rc, err = run(q)
        self.assertEqual(rc, 1, err)
        self.assertIn("test is always on", err)

    # --- non-production, specified -------------------------------------------

    def test_specified_window_outside_hoop_warns(self):
        # A warning, not an error, until B-141: nothing patches yet, so there
        # is no job to fail silently. When B-141 ships patching without the
        # wake, this must become rc 1.
        rc, out = run_report(self.q(nonprod={"days": ["tue"], "start": "21:00", "end": "22:00"}))
        self.assertEqual(rc, 0, out)
        self.assertIn("WAKE REQUIRED", out)
        self.assertIn("B-141", out)

    def test_specified_window_on_non_hoop_days_names_them(self):
        rc, d = run_json(self.q(nonprod={"days": EVERY_DAY, "start": "08:00", "end": "10:00"}))
        self.assertEqual(rc, 0, d)
        self.assertEqual(d["maintenance_windows"]["dev"]["gaps"],
                         {"january": ["sat", "sun"], "july": ["sat", "sun"]})

    def test_window_in_another_timezone_is_compared_in_utc(self):
        # 22:00-23:00 Chicago is 08:30-09:30 in India the next morning in July
        # and 09:30-10:30 in January: inside an India HOOP in both seasons,
        # though as clock times it looks nothing like one.
        q = self.q(nonprod={"days": ["sun", "mon", "tue", "wed", "thu"], "start": "22:00",
                            "end": "23:00", "timezone": "America/Chicago"})
        for env in ("dev", "test"):
            q["environments"]["hoop"][env].update(
                {"start": "08:00", "end": "17:00", "timezone": "Asia/Kolkata"})
        rc, d = run_json(q)
        self.assertEqual(rc, 0, d)
        self.assertEqual(d["maintenance_windows"]["dev"]["gaps"], {})

    def test_daylight_saving_gap_is_reported_for_one_season_only(self):
        # 04:00-05:00 Denver is 15:30-16:30 India in July (MDT) but 16:30-17:30
        # in January (MST): inside an 08:00-17:00 India HOOP half the year.
        # A single-season comparison would pass it.
        q = self.q(nonprod={"days": WEEKDAYS, "start": "04:00", "end": "05:00",
                            "timezone": "America/Denver"})
        for env in ("dev", "test"):
            q["environments"]["hoop"][env].update(
                {"start": "08:00", "end": "17:00", "timezone": "Asia/Kolkata"})
        rc, d = run_json(q)
        self.assertEqual(rc, 0, d)
        gaps = d["maintenance_windows"]["dev"]["gaps"]
        self.assertIn("january", gaps)
        self.assertNotIn("july", gaps)

    def test_oeight_answers_as_given(self):
        # The first real questionnaire, in its own words: India developers,
        # both windows in Mountain time, every day of the week.
        q = self.q(prod={"days": EVERY_DAY, "start": "23:00", "end": "01:00",
                         "timezone": "America/Denver"},
                   nonprod={"days": EVERY_DAY, "start": "10:00", "end": "14:00",
                            "timezone": "America/Denver"})
        q["environments"]["timezone"] = "Asia/Kolkata"
        for env in ("dev", "test"):
            q["environments"]["hoop"][env].update({"start": "08:00", "end": "17:00"})
            q["environments"]["hoop"][env].pop("timezone", None)
        rc, d = run_json(q)
        self.assertEqual(rc, 0, d)
        self.assertEqual(d["maintenance_windows"]["dev"]["timezone"], "America/Denver")
        self.assertTrue(d["maintenance_windows"]["dev"]["wake_required"])
        self.assertFalse(d["maintenance_windows"]["prod"]["wake_required"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
