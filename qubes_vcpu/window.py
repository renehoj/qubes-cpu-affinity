"""The vCPU window for one qube (GTK 3).

qubes-firewall's window -- the undecorated, translucent chrome of
qubes-systemlist's action dialog, the stock Qubes menu's stylesheet underneath,
and the launcher's red for focus -- over three controls: online vCPUs (a
slider), the core set (soft affinity) and the scheduler weight.

Nothing is applied before Apply, and Apply changes the running qube only: the
next start gets its usual settings again. Save applies too, and also sets the
qube's core tag and vCPU feature, so the daemon gives it the same at its next
start (not the weight).

Keyboard: Tab moves between the controls, Alt + an underlined letter jumps to
one, Ctrl+Enter applies, Ctrl+S saves, Escape closes without applying.

``_is_theme_light``, ``_load_theme`` and ``_prepare_transparency`` are the same
as in qubes-firewall, which mirrors ``qubes_menu.appmenu`` (the Qubes OS
Project, LGPL-2.1+).
"""

from __future__ import annotations

import html
import importlib.resources
import logging
import threading
from typing import Callable

import gi

# Both namespaces must be pinned, and Gtk must come first: dom0 has GTK4
# installed, so a bare Gdk import resolves to 4.0 and Gtk 3.0 then refuses to
# load. Same rule as qubes-firewall.
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Gdk, GLib, Pango  # noqa: E402

from . import model  # noqa: E402
from .backend import Backend  # noqa: E402

logger = logging.getLogger("qubes-vcpu")

WINDOW_NAME = "qubes-vcpu-window"
PANE_CLASS = "vcpu-pane"
#: Window background opacity; qubes-systemlist's default.
WINDOW_ALPHA = 0.92
ALPHA_PLACEHOLDER = "@ALPHA@"
WINDOW_WIDTH = 560
#: Natural width, in characters, of the labels whose text varies. GTK 3 asks
#: for a label's whole text on one line otherwise, and the window, which is not
#: resizable, grows to it; capped, they wrap or ellipsize at WINDOW_WIDTH.
TEXT_CHARS = 40

HINT = "Ctrl+↵ apply  ·  Esc cancel"
SAVE_HINT = "Ctrl+↵ apply  ·  Ctrl+S save  ·  Esc cancel"
READ_ONLY_HINT = "Esc close"


# -- theme (same as qubes-firewall) --------------------------------------------
def _is_theme_light(widget: Gtk.Widget) -> bool:
    """Is the current system theme light? (mirrors qubes_menu.appmenu)"""
    context = widget.get_style_context()
    background = context.get_background_color(Gtk.StateFlags.NORMAL)
    text = context.get_color(Gtk.StateFlags.NORMAL)
    background_intensity = background.red + background.blue + background.green
    text_intensity = text.red + text.blue + text.green
    return text_intensity < background_intensity


def _add_css(css: str) -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(css.encode("utf-8"))
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


def _load_theme(window: Gtk.Widget) -> bool:
    """Apply the stock menu's light or dark stylesheet; False if not installed."""
    try:
        package = importlib.resources.files("qubes_menu")
    except ImportError:
        return False
    name = "qubes-menu-light.css" if _is_theme_light(window) else "qubes-menu-dark.css"
    resource = package / name
    if not resource.is_file():
        return False
    with importlib.resources.as_file(resource) as path:
        provider = Gtk.CssProvider()
        provider.load_from_path(str(path))
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    return True


def _own_css(name: str) -> str:
    return (importlib.resources.files("qubes_vcpu") / name).read_text(
        encoding="utf-8"
    )


def _prepare_transparency(window: Gtk.Window) -> bool:
    """Give the window an RGBA visual so CSS alpha is honoured.

    Must run before the window is first realised, or it stays opaque for good.
    """
    screen = window.get_screen()
    visual = screen.get_rgba_visual() if screen.is_composited() else None
    if visual is None:
        return False
    window.set_visual(visual)
    window.set_app_paintable(True)
    return True


_styled = False


def install_style(widget: Gtk.Widget, alpha: float = WINDOW_ALPHA,
                  theme: bool = True) -> None:
    """Load the stylesheets, once per process.

    ``theme=False`` skips the stock menu stylesheet (or its fallback): a caller
    that already loaded it for the screen, like qubes-systemlist, passes that.
    """
    global _styled  # pylint: disable=global-statement
    if _styled:
        return
    _styled = True
    if theme and not _load_theme(widget):
        logger.info("qubes_menu not installed; using fallback-theme.css")
        _add_css(_own_css("fallback-theme.css"))
    _add_css(_own_css("vcpu.css").replace(ALPHA_PLACEHOLDER, f"{alpha:.3f}"))


# -- small helpers -------------------------------------------------------------
def run_in_thread(fn, *args, on_done=None, on_error=None) -> None:
    """Run a blocking backend call off the GTK thread; report back on it."""

    def deliver(callback, value):
        if callback is not None:
            callback(value)
        return False

    def work():
        try:
            result = fn(*args)
        except Exception as exc:  # pylint: disable=broad-except
            GLib.idle_add(deliver, on_error, exc)
            return
        GLib.idle_add(deliver, on_done, result)

    threading.Thread(target=work, daemon=True).start()


def _title(text: str, widget: Gtk.Widget) -> Gtk.Label:
    title = Gtk.Label.new_with_mnemonic(text)
    title.set_xalign(0)
    title.set_mnemonic_widget(widget)
    title.get_style_context().add_class("field-title")
    return title


def _unit(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0)
    label.get_style_context().add_class("unit")
    return label


# -- the window ----------------------------------------------------------------
class VcpuWindow(Gtk.Window):
    """vCPUs of one qube. Loads, lets you change, applies, closes.

    ``on_close(applied)`` is called once when the window goes away, with
    ``applied`` True when the qube was changed (applied or saved). ``state`` skips loading
    (tests, or a caller that already has it).
    """

    def __init__(self, backend: Backend, name: str, *,
                 parent: Gtk.Window | None = None,
                 on_close: Callable[[bool], None] | None = None,
                 state: model.QubeVcpus | None = None,
                 config_path: str | None = None,
                 alpha: float = WINDOW_ALPHA, theme: bool = True):
        title = f"vCPUs: {name}"
        if backend.is_demo:
            title += f" ({backend.name})"
        super().__init__(title=title)
        self.set_name(WINDOW_NAME)
        self.set_keep_above(True)
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(WINDOW_WIDTH, -1)
        if parent is not None:
            # Over the list, like its action dialog. Not in the taskbar: it is
            # part of the list, which has no entry either.
            self.set_transient_for(parent)
            self.set_modal(True)
            self.set_skip_taskbar_hint(True)
            self.set_skip_pager_hint(True)
            self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        else:
            self.set_position(Gtk.WindowPosition.CENTER)
        translucent = _prepare_transparency(self)
        install_style(self, alpha if translucent else 1.0, theme=theme)

        self.backend = backend
        self.qube_name = name
        self.config_path = config_path
        self.state: model.QubeVcpus | None = None
        self._on_close = on_close
        self._closed = False
        self._busy = False
        self._loading = False

        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        pane.get_style_context().add_class(PANE_CLASS)
        pane.set_size_request(WINDOW_WIDTH, -1)
        self.add(pane)

        heading = Gtk.Label(xalign=0)
        heading.set_markup(f"<b>vCPUs · {html.escape(name)}</b>")
        heading.get_style_context().add_class("dialog-title")
        pane.pack_start(heading, False, False, 0)

        self.subtitle = Gtk.Label(xalign=0)
        # Wraps rather than ellipsizes: "not saved" is at its end.
        self.subtitle.set_line_wrap(True)
        self.subtitle.set_max_width_chars(TEXT_CHARS)
        self.subtitle.get_style_context().add_class("dialog-subtitle")
        pane.pack_start(self.subtitle, False, False, 0)

        self.body = self._build_body()
        self.body.set_margin_start(16)
        self.body.set_margin_end(16)
        pane.pack_start(self.body, True, True, 0)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        bottom.set_margin_top(10)
        bottom.set_margin_bottom(8)
        bottom.set_margin_end(8)
        pane.pack_start(bottom, False, False, 0)

        self.footer = Gtk.Label(label=HINT, xalign=0)
        self.footer.set_ellipsize(Pango.EllipsizeMode.END)
        self.footer.set_max_width_chars(TEXT_CHARS)
        self.footer.get_style_context().add_class("footer")
        bottom.pack_start(self.footer, True, True, 0)

        self.cancel_button = Gtk.Button.new_with_mnemonic("_Cancel")
        self.save_button = Gtk.Button.new_with_mnemonic("_Save")
        self.apply_button = Gtk.Button.new_with_mnemonic("_Apply")
        for button in (self.cancel_button, self.save_button, self.apply_button):
            button.get_style_context().add_class("action")
            bottom.pack_start(button, False, False, 0)
        self.apply_button.get_style_context().add_class("suggested")
        self.apply_button.set_sensitive(False)
        self.save_button.set_sensitive(False)
        self.cancel_button.connect("clicked", lambda *_: self.cancel())
        self.save_button.connect("clicked", lambda *_: self.save())
        self.apply_button.connect("clicked", lambda *_: self.apply())

        self.connect("key-press-event", self._on_key_press)
        self.connect("delete-event", lambda *_: self.cancel() or True)

        # Shown once loaded, not before: the window is centred on the size it
        # has when first shown.
        self.body.set_sensitive(False)
        if state is not None:
            self._loaded(state)
        else:
            run_in_thread(backend.get_state, name,
                          on_done=self._loaded, on_error=self._load_failed)

    # -- the controls ----------------------------------------------------
    def _build_body(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.note = Gtk.Label(xalign=0)
        self.note.set_line_wrap(True)
        self.note.set_max_width_chars(TEXT_CHARS)
        self.note.get_style_context().add_class("note")
        self.note.set_no_show_all(True)
        page.pack_start(self.note, False, False, 0)

        # Hidden for a qube that is not running: it has no vCPUs to show.
        self.controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.pack_start(self.controls, False, False, 0)

        header = Gtk.Label(label="All vCPUs of the qube", xalign=0)
        header.get_style_context().add_class("section")
        self.controls.pack_start(header, False, False, 0)

        grid = Gtk.Grid(column_spacing=14, row_spacing=8)
        self.controls.pack_start(grid, False, False, 0)

        self.online_adjustment = Gtk.Adjustment(value=1, lower=1, upper=1,
                                                step_increment=1, page_increment=1)
        self.f_online = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL,
                                  adjustment=self.online_adjustment)
        self.f_online.set_digits(0)
        self.f_online.set_round_digits(0)
        self.f_online.set_draw_value(False)
        self.f_online.set_hexpand(True)
        self.online_value = _unit("")
        self.online_value.set_width_chars(9)
        self.online_adjustment.connect("value-changed", lambda *_: self._changed())

        self.f_cores = Gtk.ComboBoxText()
        self.f_cores.set_halign(Gtk.Align.START)
        self.f_cores.connect("changed", lambda *_: self._changed())

        self.f_weight = Gtk.SpinButton.new_with_range(
            model.WEIGHT_MIN, model.WEIGHT_MAX, 1)
        self.f_weight.set_increments(64, 256)
        self.f_weight.set_numeric(True)
        self.f_weight.set_halign(Gtk.Align.START)
        self.f_weight.connect("value-changed", lambda *_: self._changed())

        grid.attach(_title("_Online vCPUs", self.f_online), 0, 0, 1, 1)
        grid.attach(self.f_online, 1, 0, 1, 1)
        grid.attach(self.online_value, 2, 0, 1, 1)
        grid.attach(_title("Co_res", self.f_cores), 0, 1, 1, 1)
        grid.attach(self.f_cores, 1, 1, 2, 1)
        grid.attach(_title("Scheduler _weight", self.f_weight), 0, 2, 1, 1)
        weight_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        weight_row.pack_start(self.f_weight, False, False, 0)
        weight_row.pack_start(_unit(f"default {model.WEIGHT_DEFAULT}; relative"),
                              False, False, 0)
        grid.attach(weight_row, 1, 2, 2, 1)

        self.tail = Gtk.Label(label=model.LIVE_NOTE, xalign=0)
        self.tail.set_line_wrap(True)
        self.tail.set_max_width_chars(TEXT_CHARS)
        self.tail.get_style_context().add_class("note")
        page.pack_start(self.tail, False, False, 0)
        return page

    # -- loading ---------------------------------------------------------
    def _loaded(self, state: model.QubeVcpus) -> None:
        if self._closed:
            return
        self.state = state
        self._loading = True
        if state.running:
            self.online_adjustment.configure(state.online, 1, state.max_vcpus, 1, 1, 0)
            for value in range(1, state.max_vcpus + 1):
                self.f_online.add_mark(value, Gtk.PositionType.BOTTOM, None)
            self.f_cores.remove_all()
            for placement in state.choices():
                self.f_cores.append(placement.key, placement.text())
            self.f_cores.set_active_id(state.current_placement())
            self.f_weight.set_value(state.weight)
        self._loading = False
        found = model.notes(state, self.config_path)
        if found:
            self.note.set_text("\n".join(found))
            self.note.show()
        editable = state.editable
        self.body.set_sensitive(editable)
        self.f_online.set_sensitive(editable and state.max_vcpus > 1)
        self.f_cores.set_sensitive(editable and bool(state.placements))
        if not editable:
            self.apply_button.hide()
            self.apply_button.set_no_show_all(True)
            self.tail.set_no_show_all(True)
        if state.savable:
            self.tail.set_text(model.SAVE_NOTE)
            self._set_footer(SAVE_HINT)
        else:
            self.save_button.hide()
            self.save_button.set_no_show_all(True)
        if not state.running:
            self.controls.set_no_show_all(True)
            self._set_footer(READ_ONLY_HINT)
        self._changed()
        self.show_all()
        (self.f_online if editable else self.cancel_button).grab_focus()

    def _load_failed(self, exc: Exception) -> None:
        if self._closed:
            return
        logger.warning("Could not read the vCPUs of %s: %s", self.qube_name, exc)
        self.subtitle.set_text("")
        self._show_error(f"Could not read the vCPUs: {exc}")
        self.show_all()
        self.cancel_button.grab_focus()

    # -- the form --------------------------------------------------------
    def settings(self) -> model.Settings:
        return model.Settings(
            online=int(round(self.online_adjustment.get_value())),
            placement=self.f_cores.get_active_id() or model.CURRENT,
            weight=self.f_weight.get_value_as_int(),
        )

    def set_settings(self, settings: model.Settings) -> None:
        self.online_adjustment.set_value(settings.online)
        self.f_cores.set_active_id(settings.placement)
        self.f_weight.set_value(settings.weight)

    def dirty(self) -> bool:
        return (self.state is not None and self.state.editable
                and self.settings() != self.state.current())

    def unsaved(self) -> bool:
        """Would Save change anything (the running qube, or qubesd)?"""
        return (self.state is not None and self.state.savable
                and (self.dirty()
                     or bool(model.save_changes(self.state, self.settings()))))

    def _changed(self) -> None:
        if self._loading or self.state is None:
            return
        if self.state.running:
            settings = self.settings()
            self.online_value.set_text(f"{settings.online} of {self.state.max_vcpus}")
            self.subtitle.set_text(model.summary(self.state, settings))
        else:
            self.subtitle.set_text(model.summary(self.state))
        self.apply_button.set_sensitive(self.dirty() and not self._busy)
        self.save_button.set_sensitive(self.unsaved() and not self._busy)

    # -- applying --------------------------------------------------------
    def apply(self) -> None:
        if self._busy or self._closed or not self.dirty():
            return
        settings = self.settings()
        problems = model.check(self.state, settings)
        if problems:
            self._show_error(" ".join(problems))
            return
        self._set_busy(True)
        self._set_footer("Applying…")
        logger.info("Applying %s to %s", settings, self.qube_name)
        run_in_thread(self.backend.apply, self.state, settings,
                      on_done=self._applied, on_error=self._apply_failed)

    def save(self) -> None:
        if self._busy or self._closed or not self.unsaved():
            return
        settings = self.settings()
        problems = model.check(self.state, settings)
        if problems:
            self._show_error(" ".join(problems))
            return
        self._set_busy(True)
        self._set_footer("Saving…")
        logger.info("Saving %s for %s", settings, self.qube_name)
        run_in_thread(self.backend.save, self.state, settings,
                      on_done=self._saved, on_error=self._apply_failed)

    def _applied(self, ran: list[list[str]]) -> None:
        for args in ran:
            logger.info("xl %s", " ".join(args))
        self._finish(applied=True)

    def _saved(self, done: tuple[list[list[str]], list[model.SaveChange]]) -> None:
        ran, changes = done
        for change in changes:
            logger.info("%s", change.text(self.qube_name))
        self._applied(ran)

    def _apply_failed(self, exc: Exception) -> None:
        if self._closed:
            return
        logger.warning("Applying to %s failed: %s", self.qube_name, exc)
        self._set_busy(False)
        # Part of it may have been applied: what the qube has now is unknown,
        # so applying again from the old state could be wrong.
        self.apply_button.set_sensitive(False)
        self.save_button.set_sensitive(False)
        self.body.set_sensitive(False)
        self._show_error(str(exc))

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.body.set_sensitive(not busy)
        self.apply_button.set_sensitive(not busy and self.dirty())
        self.save_button.set_sensitive(not busy and self.unsaved())
        self.cancel_button.set_sensitive(not busy)

    # -- footer ----------------------------------------------------------
    def _set_footer(self, text: str, error: bool = False) -> None:
        self.footer.set_text(text)
        self.footer.set_tooltip_text(text if error else None)
        context = self.footer.get_style_context()
        (context.add_class if error else context.remove_class)("error")

    def _show_error(self, text: str) -> None:
        self._set_footer(text, error=True)

    # -- keys and closing ------------------------------------------------
    def _on_key_press(self, _widget, event) -> bool:
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        key = event.keyval
        if key == Gdk.KEY_Escape:
            self.cancel()
            return True
        if ctrl and key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.apply()
            return True
        if ctrl and key in (Gdk.KEY_s, Gdk.KEY_S):
            self.save()
            return True
        return False

    def _finish(self, applied: bool) -> None:
        if self._closed:
            return
        self._closed = True
        self.destroy()
        if self._on_close is not None:
            self._on_close(applied)

    def cancel(self) -> None:
        """Close without applying. Ignored while applying."""
        if not self._busy:
            self._finish(applied=False)

    def dismiss(self) -> None:
        """Close without reporting back (the caller is going away itself)."""
        self._on_close = None
        self._finish(applied=False)
