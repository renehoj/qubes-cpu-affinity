"""Backends: the demo data, apply(), and the xl backend against canned xl output."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qubes_vcpu import model  # noqa: E402
from qubes_vcpu.backend import BackendError  # noqa: E402
from qubes_vcpu.backend import xl_backend  # noqa: E402
from qubes_vcpu.backend.mock import MockBackend  # noqa: E402
from qubes_vcpu.backend.xl_backend import XlBackend  # noqa: E402

CONFIG = str(ROOT / "cpu-affinity.json")


class MockBackendTest(unittest.TestCase):
    def setUp(self):
        self.backend = MockBackend(delay=0)

    def test_apply_changes_the_state(self):
        before = self.backend.get_state("games")
        ran = self.backend.apply(before, model.Settings(6, "efficient", 1024))
        self.assertIn(["vcpu-pin", "games-dm", "all", "-", "16-31"], ran)
        after = self.backend.get_state("games")
        self.assertEqual(after.current(), model.Settings(6, "efficient", 1024))

    def test_adding_vcpus_writes_the_udev_rule_first(self):
        s = self.backend.get_state("work")
        self.backend.apply(s, model.Settings(6, "performance", 256))
        self.assertEqual(self.backend.ran_as_root,
                         [("work", model.ONLINE_RULE_SCRIPT)])

    def test_removing_vcpus_needs_no_rule(self):
        s = self.backend.get_state("work")
        self.backend.apply(s, model.Settings(2, "efficient", 512))
        self.assertEqual(self.backend.ran_as_root, [])

    def test_no_rule_no_change(self):
        self.backend.no_qrexec.add("work")
        s = self.backend.get_state("work")
        with self.assertRaises(BackendError) as caught:
            self.backend.apply(s, model.Settings(6, "efficient", 256))
        self.assertIn("Nothing was applied", str(caught.exception))
        self.assertEqual(self.backend.ran, [])

    def test_dom0_and_halted_qubes_are_refused(self):
        for name in ("dom0", "vault"):
            with self.subTest(name=name):
                s = self.backend.get_state(name)
                with self.assertRaises(BackendError):
                    self.backend.apply(s, model.Settings(1, model.DEFAULT, 256))
        self.assertEqual(self.backend.ran, [])

    def test_invalid_settings_run_nothing(self):
        s = self.backend.get_state("work")
        with self.assertRaises(BackendError):
            self.backend.apply(s, model.Settings(99, "performance", 256))
        self.assertEqual(self.backend.ran, [])

    def test_unknown_qube(self):
        with self.assertRaises(BackendError):
            self.backend.get_state("nope")

    def test_save_applies_and_writes_tags_and_feature(self):
        before = self.backend.get_state("games")
        ran, saved = self.backend.save(before, model.Settings(6, "efficient", 256))
        self.assertIn(["vcpu-set", "games", "6"], ran)
        self.assertEqual([c.text("games") for c in saved], [
            "qvm-tags games del performance",
            "qvm-tags games add efficient",
            "qvm-features games vcpus-online 6",
        ])
        self.assertEqual(self.backend.tags["games"], {"efficient", "gaming"})
        after = self.backend.get_state("games")
        self.assertEqual(model.save_changes(after, after.current()), [])

    def test_save_all_vcpus_unsets_the_feature(self):
        s = self.backend.get_state("office")
        self.backend.save(s, model.Settings(4, model.DEFAULT, 256))
        self.assertNotIn(model.VCPUS_FEATURE, self.backend.features["office"])

    def test_save_refuses_dom0_and_halted(self):
        for name in ("dom0", "vault"):
            with self.subTest(name=name):
                s = self.backend.get_state(name)
                with self.assertRaises(BackendError):
                    self.backend.save(s, model.Settings(1, model.DEFAULT, 256))
        self.assertEqual(self.backend.ran, [])


class FakeTags(set):
    def remove(self, elem):
        if elem not in self:
            raise KeyError(elem)
        super().remove(elem)


class FakeVm:
    def __init__(self, klass, running, tags=(), features=None):
        self.klass = klass
        self._running = running
        self.tags = FakeTags(tags)
        self.features = dict(features or {})

    def is_running(self):
        return self._running


class FakeApp:
    def __init__(self, **vms):
        self.domains = vms


class FakeXl(XlBackend):
    """XlBackend with xl replaced by canned output."""

    OUTPUT = {
        ("info",): "nr_cpus                : 32\nxen_scheduler          : credit\n",
        ("list", "games"): "Name ID Mem VCPUs State Time(s)\ngames 7 8000 2 -b---- 1.0\n",
        ("vcpu-list", "games"): (
            "Name ID VCPU CPU State Time(s) Affinity (Hard / Soft)\n"
            "games 7 0 5 -b- 1.0 4-31 / 4-15\n"
            "games 7 1 6 -b- 1.0 4-31 / 4-15\n"
            "games 7 2 - --p 0.0 4-31 / 4-15\n"),
        ("sched-credit", "-d", "games"): "Name ID Weight Cap\ngames 7 512 0\n",
        ("domid", "games"): "7\n",
        ("domid", "games-dm"): "8\n",
    }

    def __init__(self, app, fail=None):
        super().__init__(CONFIG, app=app)
        self.ran = []
        self.fail = fail

    def _run_as_root(self, name, script):
        self.ran.append(["qvm-run", name])

    def _xl(self, args):
        self.ran.append(args)
        if args[0] == self.fail:
            raise BackendError(f"xl {args[0]} failed")
        try:
            return self.OUTPUT[tuple(args)]
        except KeyError:
            if args[0] in ("vcpu-set", "vcpu-pin", "sched-credit"):
                return ""
            raise BackendError(f"xl {' '.join(args)} failed") from None


class XlBackendTest(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp(games=FakeVm("StandaloneVM", True, tags={"performance"},
                                        features={"vcpus-online": "2"}),
                           vault=FakeVm("AppVM", False))

    def test_reads_a_running_qube(self):
        s = FakeXl(self.app).get_state("games")
        self.assertEqual((s.max_vcpus, s.online, s.weight, s.stubdomain),
                         (3, 2, 512, "games-dm"))
        self.assertEqual(s.current_placement(), "performance")
        self.assertEqual((s.tags, s.feature_value), ({"performance"}, "2"))
        self.assertTrue(s.savable)

    def test_without_qubesadmin_save_is_not_offered(self):
        s = FakeXl(None).get_state("games")
        self.assertIsNone(s.tags)
        self.assertFalse(s.savable)

    def test_save_writes_to_qubesd(self):
        backend = FakeXl(self.app)
        s = backend.get_state("games")
        backend.save(s, model.Settings(3, model.DEFAULT, 512))
        vm = self.app.domains["games"]
        self.assertEqual((vm.tags, vm.features), (set(), {}))

    def test_halted_and_unknown(self):
        backend = FakeXl(self.app)
        self.assertFalse(backend.get_state("vault").running)
        self.assertEqual(backend.ran, [])
        with self.assertRaises(BackendError):
            backend.get_state("nope")

    def test_partial_failure_says_what_ran(self):
        backend = FakeXl(self.app, fail="vcpu-pin")
        s = backend.get_state("games")
        with self.assertRaises(BackendError) as caught:
            backend.apply(s, model.Settings(3, "efficient", 256))
        self.assertIn("vcpu-set games 3", str(caught.exception))


class RunAsRootTest(unittest.TestCase):
    """The real _run_as_root, with qvm-run replaced by a shell script that
    plays a hostile qube."""

    def run_qube(self, body):
        with tempfile.TemporaryDirectory() as tmp:
            qvm_run = os.path.join(tmp, "qvm-run")
            with open(qvm_run, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\n" + body + "\n")
            os.chmod(qvm_run, 0o755)
            with mock.patch.object(xl_backend, "QVM_RUN", qvm_run):
                XlBackend(app=object())._run_as_root("work", "true")

    def test_output_goes_nowhere(self):
        # Not a pipe dom0 reads, which a qube could flood: exit 9 otherwise.
        self.run_qube("[ /proc/$$/fd/1 -ef /dev/null ] && "
                      "[ /proc/$$/fd/2 -ef /dev/null ] || exit 9")

    def test_only_the_exit_code_is_reported(self):
        with self.assertRaises(BackendError) as caught:
            self.run_qube("echo 'from the qube'; exit 3")
        self.assertEqual(str(caught.exception), "qvm-run exited with 3.")


if __name__ == "__main__":
    unittest.main()
