"""The model: core sets, xl output, placements and the commands they turn into."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qubes_vcpu import model  # noqa: E402

CONFIG = {
    "hard_cores": "4-31",
    "tags": {"performance": "4-15", "efficient": "16-31"},
    "default_cores": "4-31",
}
PLACEMENTS = model.placements_from_config(CONFIG)


def state(**kwargs):
    values = dict(name="work", klass="AppVM", running=True, max_vcpus=8, online=4,
                  softs=("4-15",) * 8, weight=256, nr_cpus=32,
                  placements=PLACEMENTS)
    values.update(kwargs)
    return model.QubeVcpus(**values)


class CpusTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(model.parse_cpus("4-7", 32), frozenset({4, 5, 6, 7}))
        self.assertEqual(model.parse_cpus("0,2,4", 32), frozenset({0, 2, 4}))
        self.assertEqual(model.parse_cpus("all", 4), frozenset(range(4)))
        self.assertEqual(model.parse_cpus("none", 4), frozenset())
        self.assertEqual(model.parse_cpus("^0-1", 4), frozenset({2, 3}))
        self.assertEqual(model.parse_cpus("0-7,^2", 32), frozenset({0, 1, 3, 4, 5, 6, 7}))

    def test_parse_rejects(self):
        for spec in ("x", "5-2", "nodes:0"):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                model.parse_cpus(spec, 32)

    def test_format(self):
        self.assertEqual(model.format_cpus(frozenset({4, 5, 6, 9, 11, 12})), "4-6,9,11-12")
        self.assertEqual(model.format_cpus(frozenset()), "none")


class PlacementTest(unittest.TestCase):
    def test_config_order_and_labels(self):
        self.assertEqual([(p.key, p.text()) for p in PLACEMENTS], [
            ("performance", "P-cores (4-15)"),
            ("efficient", "E-cores (16-31)"),
            ("default", "No preference (4-31)"),
        ])

    def test_matches_by_cpu_set_not_text(self):
        self.assertEqual(state(softs=("4-31",) * 8).current_placement(), model.DEFAULT)
        self.assertEqual(state(softs=("16-31",) * 8).current_placement(), "efficient")
        # "all" on a 32-CPU host is 0-31, which is none of the sets.
        self.assertEqual(state(softs=("all",) * 8).current_placement(), model.CURRENT)

    def test_unmatched_soft_is_offered_first(self):
        s = state(softs=("4-7",) * 8)
        self.assertEqual(s.choices()[0], model.Placement(model.CURRENT, "Current", "4-7"))
        self.assertEqual(len(s.choices()), 4)

    def test_mixed_softs(self):
        s = state(softs=("4-15", "16-31"))
        self.assertIsNone(s.soft_cpus())
        self.assertEqual(s.choices()[0].cores, "mixed")

    def test_load_missing_and_broken(self):
        self.assertEqual(model.load_placements("/nonexistent/cpu-affinity.json"), ())
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "c.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{")
            with self.assertRaises(ValueError):
                model.load_placements(path)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(CONFIG, handle)
            self.assertEqual(model.load_placements(path), PLACEMENTS)


class CommandsTest(unittest.TestCase):
    def test_nothing_changed(self):
        s = state()
        self.assertEqual(model.commands(s, s.current()), [])

    def test_each_change(self):
        s = state(stubdomain="work-dm")
        settings = model.Settings(online=2, placement="efficient", weight=512)
        self.assertEqual(model.commands(s, settings), [
            ["vcpu-set", "work", "2"],
            ["vcpu-pin", "work", "all", "-", "16-31"],
            ["vcpu-pin", "work-dm", "all", "-", "16-31"],
            ["sched-credit", "-d", "work", "-w", "512"],
        ])

    def test_credit2_weight(self):
        s = state(scheduler="credit2")
        settings = model.Settings(4, "performance", 128)
        self.assertEqual(model.commands(s, settings),
                         [["sched-credit2", "-d", "work", "-w", "128"]])

    def test_adds_vcpus(self):
        s = state()
        self.assertTrue(model.adds_vcpus(s, model.Settings(5, "performance", 256)))
        self.assertFalse(model.adds_vcpus(s, model.Settings(4, "efficient", 256)))
        self.assertFalse(model.adds_vcpus(s, model.Settings(3, "performance", 256)))

    def test_check(self):
        s = state()
        self.assertEqual(model.check(s, model.Settings(8, "default", 65535)), [])
        self.assertEqual(len(model.check(s, model.Settings(9, "nope", 0))), 3)
        self.assertEqual(len(model.check(s, model.Settings(0, "current", 256))), 2)


class SaveChangesTest(unittest.TestCase):
    def test_nothing_to_save(self):
        s = state(tags=frozenset({"performance", "other"}), feature_value="4")
        self.assertEqual(model.save_changes(s, s.current()), [])
        self.assertTrue(s.savable)

    def test_tag_swap_and_feature(self):
        s = state(tags=frozenset({"performance"}))
        changes = model.save_changes(s, model.Settings(2, "efficient", 256))
        self.assertEqual([c.text("work") for c in changes], [
            "qvm-tags work del performance",
            "qvm-tags work add efficient",
            "qvm-features work vcpus-online 2",
        ])

    def test_default_drops_every_config_tag(self):
        s = state(tags=frozenset({"performance", "efficient", "other"}),
                  feature_value="4")
        changes = model.save_changes(s, model.Settings(4, model.DEFAULT, 256))
        self.assertEqual({c.name for c in changes}, {"performance", "efficient"})

    def test_all_online_unsets_the_feature(self):
        s = state(tags=frozenset(), feature_value="4",
                  vcpus_feature="my-vcpus")
        changes = model.save_changes(s, model.Settings(8, "performance", 256))
        self.assertEqual(changes[-1], model.SaveChange("feature", "my-vcpus", None))
        self.assertEqual(changes[-1].text("work"),
                         "qvm-features --unset work my-vcpus")

    def test_current_keeps_the_tags(self):
        s = state(softs=("4-7",) * 8, tags=frozenset({"efficient"}),
                  feature_value="4")
        self.assertEqual(model.save_changes(s, s.current()), [])

    def test_unknown_tags_cannot_be_saved(self):
        self.assertFalse(state().savable)
        self.assertFalse(state(tags=frozenset(), running=False).savable)


class TextTest(unittest.TestCase):
    def test_summary(self):
        s = state()
        self.assertEqual(model.summary(s),
                         "AppVM · 4 of 8 vCPUs online · weight 256 · credit scheduler")
        self.assertTrue(model.summary(s, model.Settings(2, "performance", 256))
                        .endswith("· not applied"))
        self.assertTrue(model.summary(state(tags=frozenset())).endswith("· not saved"))
        self.assertEqual(model.summary(model.QubeVcpus("vault", "AppVM", False)),
                         "AppVM · not running")

    def test_notes(self):
        self.assertEqual(model.notes(state()), [])
        self.assertEqual(model.notes(state(klass=model.ADMIN_VM)), [model.ADMIN_NOTE])
        self.assertIn("credit2", model.notes(state(scheduler="credit2"))[0])
        self.assertIn("No core sets", model.notes(state(placements=()))[0])
        self.assertIn("not running", model.notes(model.QubeVcpus("v", "AppVM", False))[0])

    def test_dom0_is_read_only_and_named_for_xl(self):
        s = state(name="dom0", klass=model.ADMIN_VM)
        self.assertFalse(s.editable)
        self.assertEqual(s.xl_name, "Domain-0")


class XlOutputTest(unittest.TestCase):
    def test_info(self):
        info = model.parse_xl_info("host : dom0\nnr_cpus                : 32\n"
                                   "xen_scheduler          : credit\n")
        self.assertEqual((info["nr_cpus"], info["xen_scheduler"]), ("32", "credit"))

    def test_list(self):
        text = ("Name                                        ID   Mem VCPUs\tState\tTime(s)\n"
                "work                                        12  4000     2     -b----     123.4\n")
        self.assertEqual(model.parse_xl_list(text), 2)

    def test_vcpu_list(self):
        text = (
            "Name                                ID  VCPU   CPU State   Time(s) Affinity (Hard / Soft)\n"
            "work                                12     0    5   -b-     10.1  4-31 / 4-15\n"
            "work                                12     1    -   --p      0.0  4-31 / 4-15\n"
            "work                                12     2    7   r--      3.0  all / all\n")
        self.assertEqual(model.parse_vcpu_list(text), ("4-15", "4-15", "all"))

    def test_sched(self):
        text = ("Name                                ID Weight  Cap\n"
                "work                                12    512    0\n")
        self.assertEqual(model.parse_sched(text), 512)
        with_pool = "Cpupool Pool-0: tslice=30ms ratelimit=1000us\n" + text
        self.assertEqual(model.parse_sched(with_pool), 512)


if __name__ == "__main__":
    unittest.main()
