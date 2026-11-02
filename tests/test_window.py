"""The GTK window against the demo backend. Skipped without GTK 3 or a display."""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from qubes_vcpu import window
    from gi.repository import Gdk, Gtk
    if not Gtk.init_check()[0]:
        raise ImportError("no display")
except (ImportError, ValueError) as exc:  # ValueError: Gtk 3.0 typelib missing
    window = None
    SKIP = f"GTK 3 not available: {exc}"
else:
    SKIP = ""

from qubes_vcpu import model  # noqa: E402
from qubes_vcpu.backend.mock import MockBackend  # noqa: E402


def press(widget, keyval, state=0):
    event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    event.keyval = keyval
    event.state = state
    widget.emit("key-press-event", event)


def wait_until(condition, timeout=5.0):
    """Run the main loop until ``condition()`` holds; worker threads report
    back through it, at no fixed number of iterations."""
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        drain()
        time.sleep(0.01)
    drain()


def drain():
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)


@unittest.skipIf(window is None, SKIP)
class WindowTest(unittest.TestCase):
    def setUp(self):
        self.backend = MockBackend(delay=0)
        self.closed = []

    def open(self, name):
        win = window.VcpuWindow(
            self.backend, name, state=self.backend.get_state(name),
            on_close=self.closed.append,
        )
        self.addCleanup(win.dismiss)
        return win

    def test_loads_in_background(self):
        win = window.VcpuWindow(self.backend, "work", on_close=self.closed.append)
        self.addCleanup(win.dismiss)
        wait_until(lambda: win.state is not None)
        self.assertEqual(win.settings(), model.Settings(4, "performance", 256))
        self.assertFalse(win.apply_button.get_sensitive())
        self.assertIs(win.get_focus(), win.f_online)

    def test_unknown_qube_shows_the_error(self):
        win = window.VcpuWindow(self.backend, "nope", on_close=self.closed.append)
        self.addCleanup(win.dismiss)
        wait_until(lambda: "nope" in win.footer.get_text())
        self.assertIn("error", win.footer.get_style_context().list_classes())

    def test_change_and_apply(self):
        win = self.open("work")
        win.set_settings(model.Settings(8, "efficient", 512))
        self.assertTrue(win.apply_button.get_sensitive())
        self.assertIn("not applied", win.subtitle.get_text())
        press(win, Gdk.KEY_Return, Gdk.ModifierType.CONTROL_MASK)
        wait_until(lambda: self.closed)
        self.assertEqual(self.closed, [True])
        self.assertEqual(self.backend.get_state("work").current(),
                         model.Settings(8, "efficient", 512))

    def test_changing_back_is_not_dirty(self):
        win = self.open("work")
        win.set_settings(model.Settings(2, "performance", 256))
        win.set_settings(model.Settings(4, "performance", 256))
        self.assertFalse(win.apply_button.get_sensitive())

    def test_unmatched_cores_are_kept(self):
        win = self.open("personal")
        self.assertEqual(win.f_cores.get_active_id(), model.CURRENT)
        self.assertFalse(win.f_online.get_sensitive())  # one vCPU only

    def test_dom0_and_halted_are_read_only(self):
        for name in ("dom0", "vault"):
            with self.subTest(name=name):
                win = self.open(name)
                self.assertFalse(win.body.get_sensitive())
                self.assertFalse(win.apply_button.get_visible())
                self.assertTrue(win.note.get_visible())

    def test_save(self):
        win = self.open("work")
        self.assertTrue(win.save_button.get_visible())
        self.assertFalse(win.save_button.get_sensitive())
        win.set_settings(model.Settings(2, "efficient", 256))
        self.assertIn("not saved", win.subtitle.get_text())
        press(win, Gdk.KEY_s, Gdk.ModifierType.CONTROL_MASK)
        wait_until(lambda: self.closed)
        self.assertEqual(self.closed, [True])
        self.assertEqual(self.backend.tags["work"], {"efficient"})
        self.assertEqual(self.backend.features["work"], {"vcpus-online": "2"})

    def test_save_without_live_change(self):
        win = self.open("sys-firewall")  # running as set, nothing saved yet
        self.assertTrue(win.save_button.get_sensitive())
        self.assertFalse(win.apply_button.get_sensitive())

    def test_dom0_and_halted_cannot_save(self):
        for name in ("dom0", "vault"):
            with self.subTest(name=name):
                self.assertFalse(self.open(name).save_button.get_visible())

    def test_escape_closes_without_applying(self):
        win = self.open("work")
        win.set_settings(model.Settings(1, "efficient", 256))
        press(win, Gdk.KEY_Escape)
        self.assertEqual(self.closed, [False])
        self.assertEqual(self.backend.ran, [])


if __name__ == "__main__":
    unittest.main()
