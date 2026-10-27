#!/usr/bin/env python3
"""JJ-TTS - GTK4 / libadwaita text-to-speech for gamers.

Engines: gTTS (online), Piper (offline), Kokoro (offline, kokoro-onnx)
Run `jj-tts.py` to open the window, `jj-tts.py --quick` for the quick-type popup.
"""
import hashlib
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

APP_ID = "dev.jjtts.App"
CONFIG = Path(GLib.get_user_config_dir()) / "jj-tts" / "config.json"
DATA = Path(GLib.get_user_data_dir())
KOKORO_DIR = DATA / "kokoro"  # kokoro-v1.0.onnx + voices-v1.0.bin
PIPER_DIRS = [DATA / "piper-voices", Path.home() / "piper-voices", Path("/usr/share/piper-voices")]
VIRTUAL_SINK = "JJ_TTS"
PIPER_HOME = DATA / "piper-voices"  # voices downloaded/imported by the voice manager live here
PIPER_CATALOG_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json"
PIPER_FILE_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
CATALOG_CACHE = Path(GLib.get_user_cache_dir()) / "jj-tts" / "piper-voices.json"
QUALITIES = ["Any quality", "x_low", "low", "medium", "high"]

ENGINES = ["piper", "kokoro", "gtts"]
ENGINE_LABELS = ["Piper (offline)", "Kokoro (offline)", "gTTS (online)"]
GTTS_LANGS = ["en", "es", "fr", "de", "pt", "it", "ja", "ko", "ru", "nl", "pl", "tr"]
KOKORO_VOICES = ["af_heart", "af_bella", "af_sarah", "af_nicole", "af_sky",
                 "am_adam", "am_michael", "bf_emma", "bf_isabella", "bm_george", "bm_lewis"]

DEFAULTS = {
    "engine": "piper",
    "voice": {"piper": "", "kokoro": "af_heart", "gtts": "en"},
    "speed": 1.0,
    "devices": [],
    "mic_source": "",
    "auto_vmic": False,
    "mix_latency": 60,
    "mix_method": "pipewire",
    "vrc_enabled": False,
    "vrc_host": "127.0.0.1",
    "vrc_port": 9000,
    "vrc_mode": "both",
    "vrc_notify": True,
    "vrc_typing": True,
    "vrc_long": "split",
    "vrc_part_delay": 3,
    "vrc_prefix": "",
    "vrc_sticky_on": False,
    "vrc_sticky_text": "",
    "vrc_sticky_every": 4,
    "vrc_sticky_pause": 8,
    "quick_dismiss": False,
    "volume": 1.0,
    "pitch": 0.0,
    "voice_presets": [],
    "font_size": 18,
    "clear_after": True,
    "stt_model": "base.en",
    "theme": "Dark",
    "presets": ["gg", "Nice shot!", "Thanks!", "Need healing", "On my way", "Sorry, I'm mute - using TTS"],
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    old = Path(GLib.get_user_config_dir()) / "mutetts" / "config.json"
    try:
        saved = json.loads((CONFIG if CONFIG.exists() else old).read_text())
        for k, v in saved.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))


def piper_models():
    found = {}
    for d in PIPER_DIRS:
        if d.is_dir():
            for p in sorted(d.rglob("*.onnx")):
                found[p.stem] = str(p)
    return found


def _osc_pad(b):
    return b + b"\0" * (4 - len(b) % 4)


def osc_message(addr, *args):
    """Minimal OSC encoder (strings, bools, ints, floats) so no python-osc dependency is needed."""
    tags, data = ",", b""
    for a in args:
        if isinstance(a, bool):
            tags += "T" if a else "F"
        elif isinstance(a, int):
            tags += "i"
            data += struct.pack(">i", a)
        elif isinstance(a, float):
            tags += "f"
            data += struct.pack(">f", a)
        else:
            tags += "s"
            data += _osc_pad(str(a).encode("utf-8"))
    return _osc_pad(addr.encode()) + _osc_pad(tags.encode()) + data


def pactl(*args, check=True):
    """Run pactl with a timeout so a wedged audio server can never freeze the app."""
    return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=6, check=check)


def list_sinks():
    """Return [(pulse_name, description)] using pactl (works on PipeWire too)."""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sinks"],
                             capture_output=True, text=True, check=True, timeout=5).stdout
        return [(s["name"], s.get("description", s["name"])) for s in json.loads(out)]
    except Exception:
        return []


def list_sources():
    """Return [(pulse_name, description)] of real input devices (no monitors, no our own mic)."""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sources"],
                             capture_output=True, text=True, check=True, timeout=5).stdout
        return [(s["name"], s.get("description", s["name"])) for s in json.loads(out)
                if not s["name"].endswith(".monitor") and not s["name"].startswith(VIRTUAL_SINK)]
    except Exception:
        return []


# ----------------------------------------------------------------- synthesis
_kokoro = None


def _atempo_chain(t):
    """ffmpeg atempo only accepts 0.5-2.0 per stage, so chain stages for other values."""
    chain = []
    while t < 0.5:
        chain.append("atempo=0.5")
        t /= 0.5
    while t > 2.0:
        chain.append("atempo=2.0")
        t /= 2.0
    if abs(t - 1) > 0.001:
        chain.append(f"atempo={t:.4f}")
    return chain


def postprocess(wav, cfg, speed_done):
    """Apply speed (unless the engine already did), pitch (semitones) and volume with ffmpeg."""
    speed = 1.0 if speed_done else float(cfg.get("speed", 1.0))
    pitch = float(cfg.get("pitch", 0.0))
    vol = float(cfg.get("volume", 1.0))
    if abs(speed - 1) < 0.01 and abs(pitch) < 0.05 and abs(vol - 1) < 0.01:
        return
    out = wav + ".fx.wav"
    ratio = 2 ** (pitch / 12)
    tail = []
    if abs(vol - 1) >= 0.01:
        tail = [f"volume={vol:.3f}", "alimiter=limit=0.95"]

    def run(filters):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav, "-af", ",".join(filters), out],
                       check=True)

    try:
        fx = [f"rubberband=pitch={ratio:.5f}:tempo={speed:.4f}"] if (abs(pitch) >= 0.05 or abs(speed - 1) >= 0.01) else []
        run(fx + tail)
    except subprocess.CalledProcessError:  # ffmpeg without rubberband: resample trick instead
        import soundfile as sf
        sr = sf.info(wav).samplerate
        fx = []
        tempo = speed
        if abs(pitch) >= 0.05:
            fx += [f"asetrate={sr * ratio:.2f}", f"aresample={sr}"]
            tempo = speed / ratio
        run(fx + _atempo_chain(tempo) + tail)
    os.replace(out, wav)


def synth(text, cfg, wav):
    engine = cfg["engine"]
    voice = cfg["voice"].get(engine, "")

    if engine == "gtts":
        from gtts import gTTS
        mp3 = wav + ".mp3"
        gTTS(text, lang=voice or "en").save(mp3)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp3, wav], check=True)
        os.remove(mp3)

    elif engine == "piper":
        model = piper_models().get(voice)
        if not model:
            raise RuntimeError("No Piper voice selected. Put .onnx voices in ~/.local/share/piper-voices")
        exe = shutil.which("piper") or shutil.which("piper-tts")
        if not exe:
            raise RuntimeError("piper not found in PATH")
        subprocess.run([exe, "-m", model, "-f", wav], input=text, text=True, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    elif engine == "kokoro":
        global _kokoro
        import soundfile as sf
        if _kokoro is None:
            from kokoro_onnx import Kokoro
            m, v = KOKORO_DIR / "kokoro-v1.0.onnx", KOKORO_DIR / "voices-v1.0.bin"
            if not (m.exists() and v.exists()):
                raise RuntimeError(f"Kokoro model files missing in {KOKORO_DIR}")
            _kokoro = Kokoro(str(m), str(v))
        lang = "en-gb" if voice.startswith("b") else "en-us"
        samples, sr = _kokoro.create(text, voice=voice or "af_heart", speed=float(cfg["speed"]), lang=lang)
        sf.write(wav, samples, sr)

    postprocess(wav, cfg, engine == "kokoro")


def resolve_outputs(devices):
    """Work out where to play. Returns (sink_names, description). Never silently falls back to the default
    output when specific devices were chosen: PipeWire sends a stream to the DEFAULT sink if the requested one
    doesn't exist, which is how TTS ended up in people's headphones."""
    if not devices:
        return [], "default output (no device ticked)"
    sinks = dict(list_sinks())
    present = [d for d in devices if d in sinks]
    if not present:
        raise RuntimeError("None of your ticked output devices exist right now. If you picked JJ_TTS, "
                           "create the virtual mic on the Audio page first.")
    return present, ", ".join(sinks[d] for d in present)


def play(wav, devices):
    targets = devices or [None]
    procs = []
    for dev in targets:
        cmd = ["paplay"] + ([f"--device={dev}"] if dev else []) + [wav]
        procs.append(subprocess.Popen(cmd))
    for p in procs:
        p.wait()


# -------------------------------------------------- global keybind (portal)
class GlobalShortcuts:
    """Registers a system-wide shortcut through the XDG GlobalShortcuts portal.

    Works on KDE Plasma (Wayland/X11), GNOME 48+, and Hyprland (xdg-desktop-portal-hyprland).
    The user can rebind it later in the desktop's own shortcut settings.
    """
    BUS = "org.freedesktop.portal.Desktop"
    PATH = "/org/freedesktop/portal/desktop"
    IFACE = "org.freedesktop.portal.GlobalShortcuts"
    DEFAULT_TRIGGER = "CTRL+ALT+SPACE"

    def __init__(self, on_activate, on_status):
        self.on_activate = on_activate
        self.on_status = on_status
        self.session = None
        self.counter = 0
        try:
            self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self.sender = self.conn.get_unique_name()[1:].replace(".", "_")
            self._register_host_app()
            self.conn.signal_subscribe(self.BUS, self.IFACE, "Activated", self.PATH, None,
                                       Gio.DBusSignalFlags.NONE, self._on_activated)
            self._create_session()
        except Exception as e:  # noqa: BLE001
            on_status(f"unavailable ({e})")

    def _register_host_app(self):
        # Lets non-sandboxed apps identify themselves to the portal (best effort).
        try:
            self.conn.call_sync(self.BUS, "/org/freedesktop/host/portal/Registry",
                                "org.freedesktop.host.portal.Registry", "Register",
                                GLib.Variant("(sa{sv})", (APP_ID, {})), None,
                                Gio.DBusCallFlags.NONE, 2000, None)
        except Exception:  # noqa: BLE001
            pass

    def _call(self, method, build, on_response):
        self.counter += 1
        token = f"jjtts_{os.getpid()}_{self.counter}"
        req = f"/org/freedesktop/portal/desktop/request/{self.sender}/{token}"
        sub = [None]

        def handler(conn, _sender, _path, _iface, _sig, params, *_):
            conn.signal_unsubscribe(sub[0])
            code, results = params.unpack()
            on_response(code, results)

        sub[0] = self.conn.signal_subscribe(self.BUS, "org.freedesktop.portal.Request", "Response", req,
                                            None, Gio.DBusSignalFlags.NONE, handler)
        self.conn.call(self.BUS, self.PATH, self.IFACE, method, build(token), None,
                       Gio.DBusCallFlags.NONE, -1, None, self._call_done)

    def _call_done(self, conn, res, *_):
        try:
            conn.call_finish(res)
        except Exception as e:  # noqa: BLE001
            self.on_status(f"unavailable ({e})")

    def _create_session(self):
        def build(token):
            return GLib.Variant("(a{sv})", ({"handle_token": GLib.Variant("s", token),
                                             "session_handle_token": GLib.Variant("s", token + "_s")},))
        self._call("CreateSession", build, self._session_created)

    def _session_created(self, code, results):
        if code != 0:
            self.on_status("unavailable (portal refused the session)")
            return
        self.session = results["session_handle"]

        def build(token):
            shortcuts = [("quick-type", {
                "description": GLib.Variant("s", "Open the JJ-TTS quick-type box"),
                "preferred_trigger": GLib.Variant("s", self.DEFAULT_TRIGGER)})]
            return GLib.Variant("(oa(sa{sv})sa{sv})",
                                (self.session, shortcuts, "", {"handle_token": GLib.Variant("s", token)}))
        self._call("BindShortcuts", build, self._bound)

    def _bound(self, code, results):
        if code != 0:
            self.on_status("not bound (cancelled or refused)")
            return
        trigger = self.DEFAULT_TRIGGER.replace("+", " + ").title()
        for _sid, props in results.get("shortcuts", []):
            trigger = props.get("trigger_description", trigger)
        self.on_status(f"active: {trigger}")

    def _on_activated(self, _conn, _sender, _path, _iface, _sig, params, *_):
        _session, shortcut_id, _ts, _opts = params.unpack()
        if shortcut_id == "quick-type":
            self.on_activate()


# ------------------------------------------------------------------------ UI
CSS = """
.jj-quick-entry { font-size: 1.35em; min-height: 54px; }
.jj-status { opacity: 0.7; }
.jj-tts-btn { font-weight: 800; font-size: 1.3em; min-width: 92px; min-height: 92px; }
.jj-big { font-weight: 800; font-size: 1.2em; min-height: 62px; }
textview.jj-text, textview.jj-text text { background: transparent; }
"""

def find_sidebar_image():
    """Sidebar picture: sidebar.png next to the script, or in the app's data folder."""
    for d in (Path(__file__).resolve().parent, DATA / "jj-tts"):
        for name in ("sidebar.png", "sidebar.webp", "sidebar.jpg"):
            if (d / name).is_file():
                return str(d / name)
    return None


NAV = [
    ("dashboard", "Dashboard", "go-home-symbolic"),
    ("tts", "Text to Speech", "audio-input-microphone-symbolic"),
    ("phrases", "Phrases", "view-list-symbolic"),
    ("voices", "Piper Voices", "audio-x-generic-symbolic"),
    ("audio", "Audio", "audio-speakers-symbolic"),
    ("vrchat", "VRChat", "applications-games-symbolic"),
    ("settings", "Settings", "emblem-system-symbolic"),
]
THEMES = {"System": Adw.ColorScheme.DEFAULT, "Dark": Adw.ColorScheme.FORCE_DARK,
          "Light": Adw.ColorScheme.FORCE_LIGHT}
STT_MODELS = ["tiny.en", "base.en", "small.en"]


def esc(text):
    return GLib.markup_escape_text(str(text))


def load_css(provider, css):
    try:
        provider.load_from_string(css)
    except AttributeError:
        provider.load_from_data(css.encode())


class JJTTS(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.cfg = load_config()
        self.win = None
        self.quick = None
        self.speak_lock = threading.Lock()
        self.rec = None
        self.rec_path = None
        self.keybind_text = "registering..."
        self.history = []
        self.sliders = {}
        self.mic_sources = []
        self.dev_rows = []
        self.phrase_rows = []
        self.recent_rows = []
        self.syncing = False
        self._dismiss_timer = None
        self.catalog = None
        self.lang_names = ["All languages"]
        self.result_rows = []
        self.inst_rows = []
        self.dl_btns = {}
        self.dl_active = set()
        self._filter_timer = None
        self._cat_populating = False
        self._cat_loading = False
        self.vrc_sock = None
        self.vrc_last_msg = 0.0
        self.vrc_last_sticky = 0.0
        self.vrc_gen = 0
        self._typing_timer = None
        self._typing_on = False
        self._lat_timer = None
        self.pw_proc = None
        self.mix_lock = threading.Lock()
        self._populating = False
        self._populating_voice = False
        self._populating_preset = False
        self.add_main_option("quick", ord("q"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Show the quick-type popup", None)

    # ---------------------------------------------------------- app lifecycle
    def do_command_line(self, cmdline):
        opts = cmdline.get_options_dict().end().unpack()
        if self.win is None:
            self.hold()  # keep running in the background after the window is closed
            self.apply_theme(self.cfg.get("theme", "Dark"))
            display = Gdk.Display.get_default()
            base = Gtk.CssProvider()
            load_css(base, CSS)
            Gtk.StyleContext.add_provider_for_display(display, base, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.font_provider = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(display, self.font_provider,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.apply_font()
            self.build_actions()
            self.build_main()
            self.build_quick()
            self.shortcuts = GlobalShortcuts(self.show_quick, self.set_keybind_status)
            if self.cfg.get("auto_vmic"):
                # wait a few seconds so the audio stack (PipeWire/WirePlumber) has settled after login
                GLib.timeout_add_seconds(4, lambda: (self.auto_setup(), False)[1])
            if "quick" not in opts:
                self.win.present()
        if "quick" in opts:
            self.show_quick()
        elif self.win is not None:
            self.win.present()
        return 0

    def build_actions(self):
        def add(name, fn, accels=None):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_: fn())
            self.add_action(a)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)
        add("quick", self.show_quick, ["<Ctrl><Shift>space"])
        add("quit-app", self.quit, ["<Ctrl>q"])

    def apply_theme(self, name):
        Adw.StyleManager.get_default().set_color_scheme(THEMES.get(name, Adw.ColorScheme.FORCE_DARK))

    def apply_font(self):
        load_css(self.font_provider, f".jj-text {{ font-size: {int(self.cfg['font_size'])}px; }}")

    # ---------------------------------------------------------- status + speech
    def status(self, msg):
        GLib.idle_add(self.status_label.set_text, msg)

    def set_keybind_status(self, msg):
        if msg.startswith("active"):
            msg += "  (change it in System Settings → Shortcuts → JJ-TTS)"
        else:
            msg += "\nFallback: bind a shortcut of your own to the command: jj-tts --quick"
        self.keybind_text = msg
        GLib.idle_add(self.keybind_row.set_subtitle, esc(msg))

    def speak(self, text):
        text = text.strip()
        if not text:
            return
        GLib.idle_add(self.add_history, text)
        mode = self.cfg["vrc_mode"] if self.cfg["vrc_enabled"] else "voice"
        if mode in ("both", "chat"):
            GLib.idle_add(self.vrc_send_message, text)
        if mode in ("both", "voice"):
            cfg = json.loads(json.dumps(self.cfg))
            threading.Thread(target=self._speak_job, args=(text, cfg), daemon=True).start()

    def _speak_job(self, text, cfg):
        with self.speak_lock:
            wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
            try:
                targets, where = resolve_outputs(cfg["devices"])
                self.status(f"Speaking → {where}: {text[:50]}")
                synth(text, cfg, wav)
                play(wav, targets)
                self.status("Ready")
            except Exception as e:  # noqa: BLE001
                self.status(f"Error: {e}")
            finally:
                if os.path.exists(wav):
                    os.remove(wav)

    # ---------------------------------------------------------- window + navigation
    def make_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        clamp = Adw.Clamp(maximum_size=760, margin_top=18, margin_bottom=18, margin_start=18, margin_end=18,
                          child=box)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=clamp)
        return scroller, box

    def build_main(self):
        self.win = Adw.ApplicationWindow(application=self, title="JJ-TTS", default_width=960, default_height=700)
        self.win.connect("close-request", lambda w: (w.set_visible(False), True)[1])
        self.status_label = Gtk.Label(label="Ready", xalign=0, ellipsize=3, margin_top=6, margin_bottom=6,
                                      margin_start=14, margin_end=14, css_classes=["caption", "jj-status"])

        # --- sidebar
        menu = Gio.Menu()
        menu.append("Quick-type box", "app.quick")
        menu.append("Quit JJ-TTS", "app.quit-app")
        sb_header = Adw.HeaderBar()
        sb_header.set_title_widget(Adw.WindowTitle(title="JJ-TTS", subtitle=""))
        sb_header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))
        brand = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=14, margin_bottom=14)
        img = find_sidebar_image()
        if img:
            pic = Gtk.Picture.new_for_filename(img)
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            pic.set_can_shrink(True)
            pic.set_size_request(-1, 130)
            pic.set_margin_start(12)
            pic.set_margin_end(12)
            brand.append(pic)
        else:
            brand.append(Adw.Avatar(size=84, text="JJ TTS", show_initials=True, halign=Gtk.Align.CENTER))
        brand.append(Gtk.Label(label="Talk without talking", css_classes=["dim-label", "caption"]))
        self.nav = Gtk.ListBox(css_classes=["navigation-sidebar"], selection_mode=Gtk.SelectionMode.SINGLE)
        for name, title, icon in NAV:
            row = Gtk.ListBoxRow()
            b = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10, margin_start=8, margin_end=8)
            b.append(Gtk.Image(icon_name=icon))
            b.append(Gtk.Label(label=title, xalign=0))
            row.set_child(b)
            row.page_name, row.page_title = name, title
            self.nav.append(row)
        side_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        side_box.append(brand)
        side_box.append(self.nav)
        side_tb = Adw.ToolbarView()
        side_tb.add_top_bar(sb_header)
        side_tb.set_content(Gtk.ScrolledWindow(child=side_box, vexpand=True,
                                               hscrollbar_policy=Gtk.PolicyType.NEVER))
        sidebar_page = Adw.NavigationPage(title="JJ-TTS", child=side_tb)

        # --- content
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.page_dashboard(), "dashboard")
        self.stack.add_named(self.page_tts(), "tts")
        self.stack.add_named(self.page_phrases(), "phrases")
        self.stack.add_named(self.page_voices(), "voices")
        self.stack.add_named(self.page_audio(), "audio")
        self.stack.add_named(self.page_vrchat(), "vrchat")
        self.stack.add_named(self.page_settings(), "settings")
        content_tb = Adw.ToolbarView()
        content_tb.add_top_bar(Adw.HeaderBar())
        content_tb.set_content(self.stack)
        content_tb.add_bottom_bar(self.status_label)
        self.content_page = Adw.NavigationPage(title="Text to Speech", child=content_tb)

        self.split = Adw.NavigationSplitView(min_sidebar_width=210, max_sidebar_width=260,
                                             sidebar_width_fraction=0.25)
        self.split.set_sidebar(sidebar_page)
        self.split.set_content(self.content_page)
        self.win.set_content(self.split)

        GLib.timeout_add_seconds(1, self.vrc_tick)
        self.nav.connect("row-selected", self.on_nav)
        self.nav.select_row(self.nav.get_row_at_index(1))  # open on Text to Speech

    def on_nav(self, _lb, row):
        if row is None:
            return
        self.stack.set_visible_child_name(row.page_name)
        self.content_page.set_title(row.page_title)
        self.split.set_show_content(True)
        if row.page_name == "dashboard":
            self.refresh_dashboard()
        elif row.page_name == "audio":
            self.refresh_devices()
        elif row.page_name == "voices":
            self.refresh_installed()
            if self.catalog is None and not self._cat_loading:
                self.load_catalog()

    # ---------------------------------------------------------- page: dashboard
    def page_dashboard(self):
        scroller, box = self.make_page()
        group = Adw.PreferencesGroup(title="Overview")
        self.dash = {}
        for key, title in [("voice", "Voice"), ("output", "Output"), ("vmic", "Virtual mic"),
                           ("mix", "Real mic mix"), ("vrc", "VRChat"), ("key", "Quick-type shortcut")]:
            row = Adw.ActionRow(title=title)
            self.dash[key] = row
            group.add(row)
        box.append(group)
        self.recent_group = Adw.PreferencesGroup(title="Recent messages", description="Click one to say it again")
        box.append(self.recent_group)
        self.refresh_dashboard()
        return scroller

    def refresh_dashboard(self):
        c = self.cfg
        engine = c["engine"]
        self.dash["voice"].set_subtitle(esc(
            f"{ENGINE_LABELS[ENGINES.index(engine)]} · {c['voice'].get(engine) or 'none'} · "
            f"speed {c['speed']:.2f}× · pitch {c['pitch']:+.1f} st · volume {int(c['volume'] * 100)}%"))
        sinks = dict(list_sinks())
        names = [sinks.get(d, d) for d in c["devices"]]
        self.dash["output"].set_subtitle(esc(", ".join(names) if names else "System default output"))
        have_vmic = VIRTUAL_SINK in sinks
        self.dash["vmic"].set_subtitle(esc(
            f"Ready. Pick '{VIRTUAL_SINK}_Mic' as your mic in Discord / VRChat / your game"
            if have_vmic else "Not created yet (Audio page → Create virtual mic)"))
        srcs = dict(list_sources())
        self.dash["mix"].set_subtitle(esc(srcs.get(c["mic_source"], c["mic_source"]) if c["mic_source"] else "Off"))
        mode_names = dict(self.VRC_MODES)
        self.dash["vrc"].set_subtitle(esc(
            f"On → {c['vrc_host']}:{c['vrc_port']} · {mode_names.get(c['vrc_mode'], '')}"
            f"{' · sticky on' if c['vrc_sticky_on'] else ''}" if c["vrc_enabled"] else "Off"))
        self.dash["key"].set_subtitle(esc(self.keybind_text.split("\n")[0]))
        self.refresh_recent()

    def add_history(self, text):
        if text in self.history:
            self.history.remove(text)
        self.history.append(text)
        self.history = self.history[-10:]
        self.refresh_recent()

    def refresh_recent(self):
        for r in self.recent_rows:
            self.recent_group.remove(r)
        self.recent_rows = []
        items = list(reversed(self.history)) or None
        if items is None:
            row = Adw.ActionRow(title="Nothing spoken yet")
            self.recent_group.add(row)
            self.recent_rows.append(row)
            return
        for text in items:
            row = Adw.ActionRow(title=esc(text[:90]), activatable=True)
            row.add_prefix(Gtk.Image(icon_name="media-playback-start-symbolic"))
            row.connect("activated", lambda _r, t=text: self.speak(t))
            self.recent_group.add(row)
            self.recent_rows.append(row)

    # ---------------------------------------------------------- page: text to speech
    def page_tts(self):
        scroller, box = self.make_page()

        # text area + side buttons
        top = Gtk.Box(spacing=12)
        card = Gtk.Overlay(hexpand=True, css_classes=["card"])
        self.textview = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=14, right_margin=14,
                                     top_margin=12, bottom_margin=42, css_classes=["jj-text"])
        self.buffer = self.textview.get_buffer()
        self.buffer.connect("changed", lambda b: self.count_label.set_text(str(b.get_char_count())))
        self.buffer.connect("changed", lambda b: self.on_typing(b.get_text(b.get_start_iter(), b.get_end_iter(), False)))
        card.set_child(Gtk.ScrolledWindow(child=self.textview, min_content_height=190, hexpand=True))
        self.count_label = Gtk.Label(label="0", halign=Gtk.Align.START, valign=Gtk.Align.END,
                                     margin_start=14, margin_bottom=12, css_classes=["dim-label", "caption"])
        card.add_overlay(self.count_label)
        tools = Gtk.Box(spacing=2, halign=Gtk.Align.END, valign=Gtk.Align.END, margin_end=6, margin_bottom=6)
        save_phrase = Gtk.Button(icon_name="list-add-symbolic", css_classes=["flat"],
                                 tooltip_text="Save this text as a phrase")
        save_phrase.connect("clicked", self.on_save_phrase)
        clear = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["flat"], tooltip_text="Clear text")
        clear.connect("clicked", lambda *_: self.buffer.set_text(""))
        tools.append(save_phrase)
        tools.append(clear)
        card.add_overlay(tools)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_text_key)
        self.textview.add_controller(keys)
        top.append(card)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.START)
        tts_btn = Gtk.Button(label="TTS", css_classes=["suggested-action", "jj-tts-btn"])
        tts_btn.set_tooltip_text("Speak (Enter). Shift+Enter makes a new line")
        tts_btn.connect("clicked", lambda *_: self.speak_textbox())
        bigger = Gtk.Button(label="▲ Aa", tooltip_text="Bigger text")
        bigger.connect("clicked", lambda *_: self.change_font(+2))
        smaller = Gtk.Button(label="▼ Aa", tooltip_text="Smaller text")
        smaller.connect("clicked", lambda *_: self.change_font(-2))
        for w in (tts_btn, bigger, smaller):
            side.append(w)
        top.append(side)
        box.append(top)

        # speech to text to speech
        self.stt_btn = Gtk.ToggleButton(label="Speech To Text To Speech", css_classes=["jj-big", "pill"])
        self.stt_btn.set_tooltip_text("Click, talk, click again: your words are transcribed and spoken in the TTS voice")
        self.stt_btn.connect("toggled", self.on_stt_toggled)
        box.append(self.stt_btn)

        # voice customization
        group = Adw.PreferencesGroup(title="Voice Customization Options")
        reset = Gtk.Button(label="Reset sliders", css_classes=["flat"], valign=Gtk.Align.CENTER)
        reset.connect("clicked", self.on_reset_sliders)
        group.set_header_suffix(reset)

        self.engine_row = Adw.ComboRow(title="Text to Speech Mode", model=Gtk.StringList.new(ENGINE_LABELS))
        self.engine_row.set_selected(ENGINES.index(self.cfg["engine"]))
        self.engine_row.connect("notify::selected", self.on_engine_changed)
        self.voice_row = Adw.ComboRow(title="Voice")
        self.voice_row.connect("notify::selected", self.on_voice_changed)

        self.preset_row = Adw.ComboRow(title="Presets", subtitle="Saved voice + speed/pitch/volume combos")
        self.preset_row.connect("notify::selected", self.on_preset_selected)
        pbox = Gtk.Box(spacing=2, valign=Gtk.Align.CENTER)
        psave = Gtk.Button(icon_name="document-save-symbolic", css_classes=["flat"],
                           tooltip_text="Save current settings as a preset")
        psave.connect("clicked", self.on_save_preset)
        pdel = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["flat"],
                          tooltip_text="Delete the selected preset")
        pdel.connect("clicked", self.on_delete_preset)
        pbox.append(psave)
        pbox.append(pdel)
        self.preset_row.add_suffix(pbox)

        for w in (self.engine_row, self.voice_row, self.preset_row,
                  self.slider_row("Volume", "Louder than 100% is limited to avoid clipping", "volume",
                                  0.0, 2.0, 0.05, lambda v: f"{int(round(v * 100))}%"),
                  self.slider_row("Speed", "1.00× is normal", "speed", 0.5, 2.0, 0.05, lambda v: f"{v:.2f}×"),
                  self.slider_row("Pitch", "In semitones, 0 is normal (12 = one octave)", "pitch",
                                  -12.0, 12.0, 0.5, lambda v: f"{v:+.1f} st")):
            group.add(w)
        box.append(group)
        self.populate_voices()
        self.refresh_preset_model()
        return scroller

    def slider_row(self, title, subtitle, key, lo, hi, step, fmt):
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        scale.set_size_request(260, -1)
        scale.set_valign(Gtk.Align.CENTER)
        scale.set_draw_value(True)
        scale.set_value_pos(Gtk.PositionType.RIGHT)

        def fmt_fn(_s, v, *_a):
            return fmt(v)
        scale.set_format_value_func(fmt_fn)
        scale.set_value(self.cfg[key])
        scale.connect("value-changed", self.on_slider, key)
        row.add_suffix(scale)
        self.sliders[key] = scale
        return row

    def on_slider(self, scale, key):
        if self.syncing:
            return
        self.cfg[key] = round(scale.get_value(), 2)
        save_config(self.cfg)

    def on_reset_sliders(self, *_):
        self.cfg.update({"speed": 1.0, "pitch": 0.0, "volume": 1.0})
        save_config(self.cfg)
        self.sync_voice_widgets()

    def on_text_key(self, _ctrl, keyval, _code, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not state & Gdk.ModifierType.SHIFT_MASK:
            self.speak_textbox()
            return True
        return False

    def speak_textbox(self):
        text = self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), False)
        self.speak(text)
        if self.cfg.get("clear_after", True) and text.strip():
            self.buffer.set_text("")

    def change_font(self, delta):
        self.cfg["font_size"] = max(10, min(48, int(self.cfg["font_size"]) + delta))
        save_config(self.cfg)
        self.apply_font()

    # ---------------------------------------------------------- voices + presets
    def voices_for(self, engine):
        if engine == "piper":
            return list(piper_models().keys())
        if engine == "kokoro":
            return KOKORO_VOICES
        return GTTS_LANGS

    def populate_voices(self):
        engine = self.cfg["engine"]
        voices = self.voices_for(engine)
        cur = self.cfg["voice"].get(engine, "")
        self._populating_voice = True
        self.voice_row.set_model(Gtk.StringList.new(voices or ["(no voices found)"]))
        if cur in voices:
            self.voice_row.set_selected(voices.index(cur))
        elif voices:
            self.cfg["voice"][engine] = voices[0]
        self._populating_voice = False
        self.voice_row.set_subtitle(
            {"piper": "Voices found in ~/.local/share/piper-voices", "kokoro": "Kokoro voice pack",
             "gtts": "Language (needs internet)"}[engine])

    def on_engine_changed(self, row, _p):
        if self.syncing:
            return
        self.cfg["engine"] = ENGINES[row.get_selected()]
        self.populate_voices()
        save_config(self.cfg)

    def on_voice_changed(self, row, _p):
        if self._populating_voice or self.syncing:
            return
        voices = self.voices_for(self.cfg["engine"])
        i = row.get_selected()
        if voices and i < len(voices):
            self.cfg["voice"][self.cfg["engine"]] = voices[i]
            save_config(self.cfg)

    def sync_voice_widgets(self):
        self.syncing = True
        self.engine_row.set_selected(ENGINES.index(self.cfg["engine"]))
        self.populate_voices()
        for key, scale in self.sliders.items():
            scale.set_value(self.cfg[key])
        self.syncing = False

    def refresh_preset_model(self, select=None):
        self._populating_preset = True
        names = [p["name"] for p in self.cfg["voice_presets"]]
        self.preset_row.set_model(Gtk.StringList.new(["Non Selected"] + names))
        self.preset_row.set_selected(names.index(select) + 1 if select in names else 0)
        self._populating_preset = False

    def on_preset_selected(self, row, _p):
        i = row.get_selected()
        if self._populating_preset or i == 0 or i > len(self.cfg["voice_presets"]):
            return
        p = self.cfg["voice_presets"][i - 1]
        self.cfg["engine"] = p["engine"]
        self.cfg["voice"][p["engine"]] = p["voice"]
        for k in ("speed", "pitch", "volume"):
            self.cfg[k] = p.get(k, DEFAULTS[k])
        save_config(self.cfg)
        self.sync_voice_widgets()
        self.status(f"Loaded preset '{p['name']}'")

    def on_save_preset(self, *_):
        entry = Gtk.Entry(placeholder_text="Preset name", activates_default=True)
        dlg = Adw.AlertDialog(heading="Save voice preset",
                              body="Saves the current mode, voice, volume, speed and pitch.")
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("save", "Save")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("save")
        dlg.set_close_response("cancel")
        dlg.connect("response", self.on_save_preset_response, entry)
        dlg.present(self.win)

    def on_save_preset_response(self, _dlg, response, entry):
        name = entry.get_text().strip()
        if response != "save" or not name:
            return
        engine = self.cfg["engine"]
        preset = {"name": name, "engine": engine, "voice": self.cfg["voice"].get(engine, ""),
                  "speed": self.cfg["speed"], "pitch": self.cfg["pitch"], "volume": self.cfg["volume"]}
        self.cfg["voice_presets"] = [p for p in self.cfg["voice_presets"] if p["name"] != name] + [preset]
        save_config(self.cfg)
        self.refresh_preset_model(select=name)
        self.status(f"Saved preset '{name}'")

    def on_delete_preset(self, *_):
        i = self.preset_row.get_selected()
        if 0 < i <= len(self.cfg["voice_presets"]):
            gone = self.cfg["voice_presets"].pop(i - 1)
            save_config(self.cfg)
            self.refresh_preset_model()
            self.status(f"Deleted preset '{gone['name']}'")

    # ---------------------------------------------------------- page: phrases
    def page_phrases(self):
        scroller, box = self.make_page()
        add_group = Adw.PreferencesGroup(title="Phrases",
                                         description="One-click lines for when you can't type fast enough")
        entry = Adw.EntryRow(title="Add a phrase", show_apply_button=True)
        entry.connect("apply", self.on_phrase_add)
        add_group.add(entry)
        box.append(add_group)
        self.phrase_group = Adw.PreferencesGroup()
        box.append(self.phrase_group)
        self.refresh_phrases()
        return scroller

    def refresh_phrases(self):
        for r in self.phrase_rows:
            self.phrase_group.remove(r)
        self.phrase_rows = []
        if not self.cfg["presets"]:
            row = Adw.ActionRow(title="No phrases yet")
            self.phrase_group.add(row)
            self.phrase_rows.append(row)
        for text in self.cfg["presets"]:
            row = Adw.ActionRow(title=esc(text), activatable=True)
            row.add_prefix(Gtk.Image(icon_name="media-playback-start-symbolic"))
            row.connect("activated", lambda _r, t=text: self.speak(t))
            delete = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["flat"], valign=Gtk.Align.CENTER,
                                tooltip_text="Delete phrase")
            delete.connect("clicked", lambda _b, t=text: self.remove_phrase(t))
            row.add_suffix(delete)
            self.phrase_group.add(row)
            self.phrase_rows.append(row)

    def on_phrase_add(self, row):
        text = row.get_text().strip()
        if text and text not in self.cfg["presets"]:
            self.cfg["presets"].append(text)
            save_config(self.cfg)
            self.refresh_phrases()
        row.set_text("")

    def on_save_phrase(self, *_):
        text = self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), False).strip()
        if text and text not in self.cfg["presets"]:
            self.cfg["presets"].append(text)
            save_config(self.cfg)
            self.refresh_phrases()
            self.status("Saved to Phrases")

    def remove_phrase(self, text):
        if text in self.cfg["presets"]:
            self.cfg["presets"].remove(text)
            save_config(self.cfg)
            self.refresh_phrases()

    # ---------------------------------------------------------- page: Piper voice manager
    def page_voices(self):
        scroller, box = self.make_page()

        self.inst_group = Adw.PreferencesGroup(title="Installed Piper voices")
        tools = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        add_btn = Gtk.Button(label="Add from file…", css_classes=["flat"],
                             tooltip_text="Import a .onnx voice (and its .onnx.json) you downloaded yourself")
        add_btn.connect("clicked", lambda *_: self.import_voice_dialog())
        folder_btn = Gtk.Button(icon_name="folder-open-symbolic", css_classes=["flat"], tooltip_text="Open voices folder")
        folder_btn.connect("clicked", lambda *_: self.open_voices_folder())
        tools.append(add_btn)
        tools.append(folder_btn)
        self.inst_group.set_header_suffix(tools)
        box.append(self.inst_group)

        cat = Adw.PreferencesGroup(title="Download voices",
                                   description="The official rhasspy/piper-voices collection on Hugging Face")
        self.cat_status = Adw.ActionRow(title="Voice catalog", subtitle="Not loaded yet")
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", css_classes=["flat"], valign=Gtk.Align.CENTER,
                             tooltip_text="Re-download the catalog")
        refresh.connect("clicked", lambda *_: self.load_catalog(force=True))
        self.cat_status.add_suffix(refresh)
        cat.add(self.cat_status)
        self.search_row = Adw.EntryRow(title="Search (name, language, country)")
        self.search_row.connect("changed", lambda *_: self.schedule_catalog_filter())
        cat.add(self.search_row)
        self.lang_row = Adw.ComboRow(title="Language", model=Gtk.StringList.new(self.lang_names))
        self.lang_row.connect("notify::selected", lambda *_: self.schedule_catalog_filter())
        cat.add(self.lang_row)
        self.qual_row = Adw.ComboRow(title="Quality", subtitle="Higher quality sounds better but is larger and slower",
                                     model=Gtk.StringList.new(QUALITIES))
        self.qual_row.connect("notify::selected", lambda *_: self.schedule_catalog_filter())
        cat.add(self.qual_row)
        box.append(cat)

        self.results_group = Adw.PreferencesGroup()
        box.append(self.results_group)
        self.refresh_installed()
        return scroller

    @staticmethod
    def fmt_size(n):
        return f"{n / 1e6:.0f} MB" if n >= 1e6 else f"{n / 1e3:.0f} KB"

    # --- installed voices
    def refresh_installed(self):
        for r in self.inst_rows:
            self.inst_group.remove(r)
        self.inst_rows = []
        models = piper_models()
        piper_ok = bool(shutil.which("piper") or shutil.which("piper-tts"))
        self.inst_group.set_description("" if piper_ok else
                                        "The 'piper' command wasn't found, so these voices can't play yet. Run install.sh again.")
        if not models:
            row = Adw.ActionRow(title="No Piper voices installed yet", subtitle="Download one below")
            self.inst_group.add(row)
            self.inst_rows.append(row)
        for name, path in sorted(models.items()):
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            in_use = self.cfg["engine"] == "piper" and self.cfg["voice"].get("piper") == name
            row = Adw.ActionRow(title=esc(name), subtitle=esc(f"{self.fmt_size(size)} · {Path(path).parent}"))
            if in_use:
                row.add_prefix(Gtk.Image(icon_name="emblem-ok-symbolic", tooltip_text="Currently selected"))
            preview = Gtk.Button(icon_name="media-playback-start-symbolic", css_classes=["flat"],
                                 valign=Gtk.Align.CENTER, tooltip_text="Preview this voice")
            preview.connect("clicked", lambda _b, n=name: self.preview_voice(n))
            use = Gtk.Button(label="Use", valign=Gtk.Align.CENTER, sensitive=not in_use)
            use.connect("clicked", lambda _b, n=name: self.use_voice(n))
            row.add_suffix(preview)
            row.add_suffix(use)
            if str(path).startswith(str(PIPER_HOME)):  # only manage files in our own folder
                delete = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["flat", "destructive-action"],
                                    valign=Gtk.Align.CENTER, tooltip_text="Delete this voice")
                delete.connect("clicked", lambda _b, n=name: self.confirm_delete_voice(n))
                row.add_suffix(delete)
            self.inst_group.add(row)
            self.inst_rows.append(row)

    def use_voice(self, name):
        self.cfg["engine"] = "piper"
        self.cfg["voice"]["piper"] = name
        save_config(self.cfg)
        self.sync_voice_widgets()
        self.refresh_installed()
        self.status(f"Using Piper voice '{name}'")

    def preview_voice(self, name):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["engine"] = "piper"
        cfg["voice"]["piper"] = name
        threading.Thread(target=self._speak_job, args=("Hello! This is how this voice sounds.", cfg),
                         daemon=True).start()

    def confirm_delete_voice(self, name):
        dlg = Adw.AlertDialog(heading=f"Delete {name}?", body="This removes the voice files from your disk. "
                                                           "You can download it again later.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, resp, n=name: self.delete_voice(n) if resp == "delete" else None)
        dlg.present(self.win)

    def delete_voice(self, name):
        path = piper_models().get(name)
        if not path or not str(path).startswith(str(PIPER_HOME)):
            self.status("That voice isn't in the JJ-TTS folder, so I won't delete it")
            return
        path = Path(path)
        for f in (path, Path(str(path) + ".json")):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                self.status(f"Couldn't delete {f.name}: {e}")
                return
        parent = path.parent
        if parent != PIPER_HOME and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
        if self.cfg["voice"].get("piper") == name:
            self.cfg["voice"]["piper"] = ""
            save_config(self.cfg)
        self.populate_voices()
        self.refresh_installed()
        self.refresh_catalog_rows()
        self.status(f"Deleted '{name}'")

    def open_voices_folder(self):
        PIPER_HOME.mkdir(parents=True, exist_ok=True)
        try:
            Gio.AppInfo.launch_default_for_uri(PIPER_HOME.as_uri(), None)
        except GLib.Error as e:
            self.status(f"Couldn't open the folder: {e.message}")

    def import_voice_dialog(self):
        flt = Gtk.FileFilter()
        flt.set_name("Piper voice (*.onnx)")
        flt.add_pattern("*.onnx")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(flt)
        dlg = Gtk.FileDialog(title="Choose a Piper voice (.onnx)", filters=filters)

        def done(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return  # cancelled
            self.import_voice(Path(f.get_path()))
        dlg.open(self.win, None, done)

    def import_voice(self, onnx):
        cfg_file = Path(str(onnx) + ".json")
        if not cfg_file.exists():
            self.status(f"Missing {cfg_file.name} next to the model. Piper needs both files in the same folder.")
            return
        dest = PIPER_HOME / onnx.stem
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(onnx, dest / onnx.name)
        shutil.copy2(cfg_file, dest / cfg_file.name)
        self.populate_voices()
        self.refresh_installed()
        self.refresh_catalog_rows()
        self.status(f"Imported '{onnx.stem}'")

    # --- catalog
    def load_catalog(self, force=False):
        self._cat_loading = True
        self.cat_status.set_subtitle("Loading…")
        threading.Thread(target=self._load_catalog_job, args=(force,), daemon=True).start()

    def _load_catalog_job(self, force):
        data, err = None, None
        if not force and CATALOG_CACHE.exists():
            try:
                data = json.loads(CATALOG_CACHE.read_text())
            except (OSError, ValueError):
                data = None
        if data is None:
            try:
                req = urllib.request.Request(PIPER_CATALOG_URL, headers={"User-Agent": "jj-tts"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    raw = r.read()
                data = json.loads(raw)
                CATALOG_CACHE.parent.mkdir(parents=True, exist_ok=True)
                CATALOG_CACHE.write_bytes(raw)
            except Exception as e:  # noqa: BLE001
                err = str(e)
                if CATALOG_CACHE.exists():
                    try:
                        data = json.loads(CATALOG_CACHE.read_text())
                    except (OSError, ValueError):
                        data = None
        GLib.idle_add(self._catalog_loaded, data, err)

    def _catalog_loaded(self, data, err):
        self._cat_loading = False
        if isinstance(data, list):
            data = {v.get("key", str(i)): v for i, v in enumerate(data)}
        if not data:
            self.cat_status.set_subtitle(esc(f"Couldn't load the catalog: {err or 'empty response'}"))
            return
        self.catalog = data
        langs = sorted({v.get("language", {}).get("name_english", "") for v in data.values()} - {""})
        self.lang_names = ["All languages"] + langs
        self._cat_populating = True
        self.lang_row.set_model(Gtk.StringList.new(self.lang_names))
        self.lang_row.set_selected(0)
        self._cat_populating = False
        note = f" (showing the saved copy, couldn't reach the server: {err})" if err else ""
        self.cat_status.set_subtitle(esc(f"{len(data)} voices available{note}"))
        self.refresh_catalog_rows()

    def schedule_catalog_filter(self):
        if self._cat_populating:
            return
        if self._filter_timer:
            GLib.source_remove(self._filter_timer)
        self._filter_timer = GLib.timeout_add(250, self._run_catalog_filter)

    def _run_catalog_filter(self):
        self._filter_timer = None
        self.refresh_catalog_rows()
        return False

    def refresh_catalog_rows(self):
        for r in self.result_rows:
            self.results_group.remove(r)
        self.result_rows = []
        self.dl_btns = {}
        if not self.catalog:
            return
        terms = self.search_row.get_text().lower().split()
        li = self.lang_row.get_selected()
        lang = self.lang_names[li] if 0 < li < len(self.lang_names) else None
        qi = self.qual_row.get_selected()
        qual = QUALITIES[qi] if qi > 0 else None
        installed = set(piper_models())
        items = []
        for key, v in self.catalog.items():
            L = v.get("language", {})
            hay = " ".join([key, L.get("name_english", ""), L.get("country_english", ""),
                            L.get("name_native", "")]).lower()
            if any(t not in hay for t in terms):
                continue
            if lang and L.get("name_english") != lang:
                continue
            if qual and v.get("quality") != qual:
                continue
            items.append((L.get("name_english", ""), key, v))
        items.sort(key=lambda t: (t[0], t[1]))
        limit = 60
        for _lang, key, v in items[:limit]:
            L = v.get("language", {})
            files = {f: m for f, m in v.get("files", {}).items() if f.endswith((".onnx", ".onnx.json"))}
            size = sum(m.get("size_bytes", 0) for m in files.values())
            speakers = v.get("num_speakers", 1)
            sub = (f"{L.get('name_english', '?')} ({L.get('country_english', '?')}) · {v.get('quality', '?')} · "
                   f"{self.fmt_size(size)}" + (f" · {speakers} speakers" if speakers > 1 else ""))
            row = Adw.ActionRow(title=esc(key), subtitle=esc(sub))
            if key in installed:
                btn = Gtk.Button(label="Installed", sensitive=False, valign=Gtk.Align.CENTER)
            elif key in self.dl_active:
                btn = Gtk.Button(label="Downloading…", sensitive=False, valign=Gtk.Align.CENTER)
            else:
                btn = Gtk.Button(label="Download", css_classes=["suggested-action"], valign=Gtk.Align.CENTER)
                btn.connect("clicked", lambda _b, k=key: self.download_voice(k))
            self.dl_btns[key] = btn
            row.add_suffix(btn)
            self.results_group.add(row)
            self.result_rows.append(row)
        if not items:
            row = Adw.ActionRow(title="No voices match", subtitle="Try a different search or filter")
            self.results_group.add(row)
            self.result_rows.append(row)
        elif len(items) > limit:
            row = Adw.ActionRow(title=f"…and {len(items) - limit} more", subtitle="Narrow it down with the search or filters")
            self.results_group.add(row)
            self.result_rows.append(row)

    # --- downloading
    def download_voice(self, key):
        if key in self.dl_active:
            return
        self.dl_active.add(key)
        btn = self.dl_btns.get(key)
        if btn:
            btn.set_sensitive(False)
            btn.set_label("0%")
        threading.Thread(target=self._download_job, args=(key,), daemon=True).start()

    def _download_progress(self, key, pct):
        btn = self.dl_btns.get(key)
        if btn:
            btn.set_label(f"{pct}%")

    def _download_job(self, key):
        v = self.catalog[key]
        files = {f: m for f, m in v.get("files", {}).items() if f.endswith((".onnx", ".onnx.json"))}
        dest = PIPER_HOME / key
        try:
            dest.mkdir(parents=True, exist_ok=True)
            total = sum(m.get("size_bytes", 0) for m in files.values()) or 1
            done = 0
            for rel, meta in files.items():
                target = dest / Path(rel).name
                part = Path(str(target) + ".part")
                md5 = hashlib.md5()
                req = urllib.request.Request(PIPER_FILE_BASE + rel, headers={"User-Agent": "jj-tts"})
                with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as out:
                    while True:
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        out.write(chunk)
                        md5.update(chunk)
                        done += len(chunk)
                        GLib.idle_add(self._download_progress, key, min(99, int(done * 100 / total)))
                want = meta.get("md5_digest")
                if want and md5.hexdigest() != want:
                    part.unlink(missing_ok=True)
                    raise RuntimeError(f"{target.name} failed its checksum, please try again")
                os.replace(part, target)
            self.status(f"Downloaded '{key}'")
        except Exception as e:  # noqa: BLE001
            self.status(f"Download failed for '{key}': {e}")
            for f in dest.glob("*.part"):
                f.unlink(missing_ok=True)
        finally:
            self.dl_active.discard(key)
            GLib.idle_add(self._download_finished)

    def _download_finished(self):
        self.populate_voices()
        self.refresh_installed()
        self.refresh_catalog_rows()

    # ---------------------------------------------------------- page: audio
    def page_audio(self):
        scroller, box = self.make_page()

        self.dev_group = Adw.PreferencesGroup(
            title="Output devices", description="Pick every place the TTS should play (headphones, virtual mic...)")
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", css_classes=["flat"], valign=Gtk.Align.CENTER,
                             tooltip_text="Refresh devices")
        refresh.connect("clicked", lambda *_: self.refresh_devices())
        self.dev_group.set_header_suffix(refresh)
        box.append(self.dev_group)

        vm = Adw.PreferencesGroup(
            title="Virtual microphone",
            description=f"Choose '{VIRTUAL_SINK}_Mic' as your microphone in Discord, VRChat or your game.")
        create_row = Adw.ActionRow(title="Create virtual mic",
                                   subtitle=f"Makes {VIRTUAL_SINK} (output) and {VIRTUAL_SINK}_Mic (input)")
        create = Gtk.Button(label="Create", valign=Gtk.Align.CENTER)
        create.connect("clicked", self.on_create_vmic)
        create_row.add_suffix(create)
        vm.add(create_row)
        self.mic_row = Adw.ComboRow(title="Mix in my real mic",
                                    subtitle="Friends hear you AND your TTS through the virtual mic")
        self.mic_row.connect("notify::selected", self.on_mic_source_changed)
        vm.add(self.mic_row)
        method = Adw.ComboRow(title="Mix method", subtitle="Try the other one if you get crackling or crashes",
                              model=Gtk.StringList.new(["PipeWire loopback (recommended)", "PulseAudio loopback"]))
        method.set_selected(0 if self.cfg.get("mix_method", "pipewire") == "pipewire" else 1)
        method.connect("notify::selected", self.on_mix_method_changed)
        vm.add(method)
        adj = Gtk.Adjustment(value=self.cfg.get("mix_latency", 60), lower=20, upper=250, step_increment=10,
                             page_increment=20)
        lat = Adw.SpinRow(title="Mic mix latency (ms)", adjustment=adj,
                          subtitle="PulseAudio method only. Raise it if the mic sounds crackly; lower = less delay")
        lat.connect("notify::value", self.on_latency_changed)
        vm.add(lat)
        reset_row = Adw.ActionRow(title="Rebuild virtual mic",
                                  subtitle="Removes old/duplicate loopbacks and recreates everything")
        reset_btn = Gtk.Button(label="Rebuild", valign=Gtk.Align.CENTER)
        reset_btn.connect("clicked", self.reset_vmic)
        reset_row.add_suffix(reset_btn)
        vm.add(reset_row)
        auto = Adw.SwitchRow(title="Set up automatically on launch",
                             subtitle="Virtual devices vanish on reboot; this recreates them",
                             active=self.cfg.get("auto_vmic", False))
        auto.connect("notify::active", self.on_auto_toggled)
        vm.add(auto)
        box.append(vm)
        self.refresh_devices()
        return scroller

    def refresh_mics(self):
        self._populating = True
        self.mic_sources = list_sources()
        self.mic_row.set_model(Gtk.StringList.new(["(none - TTS only)"] + [d for _n, d in self.mic_sources]))
        cur = self.cfg.get("mic_source", "")
        self.mic_row.set_selected(next((i + 1 for i, (n, _d) in enumerate(self.mic_sources) if n == cur), 0))
        self._populating = False

    def refresh_devices(self):
        self.refresh_mics()
        for r in self.dev_rows:
            self.dev_group.remove(r)
        self.dev_rows = []
        sinks = list_sinks()
        if not sinks:
            row = Adw.ActionRow(title="No output devices found", subtitle="Is pactl installed?")
            self.dev_group.add(row)
            self.dev_rows.append(row)
        for name, desc in sinks:
            row = Adw.SwitchRow(title=esc(desc), active=name in self.cfg["devices"])
            row.connect("notify::active", self.on_device_toggled, name)
            self.dev_group.add(row)
            self.dev_rows.append(row)

    def on_device_toggled(self, row, _p, name):
        devs = self.cfg["devices"]
        if row.get_active() and name not in devs:
            devs.append(name)
        elif not row.get_active() and name in devs:
            devs.remove(name)
        save_config(self.cfg)

    # ---- virtual mic + real-mic mix. All pactl / pw-loopback work runs off the UI thread with timeouts.
    def ensure_vmic(self):
        """Create the virtual sink + mic if they don't exist yet (without stealing the system defaults)."""
        if any(n == VIRTUAL_SINK for n, _d in list_sinks()):
            return
        prev_sink = pactl("get-default-sink", check=False).stdout.strip()
        prev_src = pactl("get-default-source", check=False).stdout.strip()
        pactl("load-module", "module-null-sink", f"sink_name={VIRTUAL_SINK}",
              f"sink_properties=device.description={VIRTUAL_SINK}", "rate=48000", "channels=2")
        pactl("load-module", "module-remap-source", f"master={VIRTUAL_SINK}.monitor",
              f"source_name={VIRTUAL_SINK}_Mic", f"source_properties=device.description={VIRTUAL_SINK}_Mic")
        # If the new devices became the default output/input, anything that follows the default (including a
        # loopback) would feed the virtual mic back into itself, which sounds like an echo. Put the defaults back.
        if prev_sink and not prev_sink.startswith(VIRTUAL_SINK):
            if pactl("get-default-sink", check=False).stdout.strip() != prev_sink:
                pactl("set-default-sink", prev_sink, check=False)
        if prev_src and not prev_src.startswith(VIRTUAL_SINK):
            if pactl("get-default-source", check=False).stdout.strip() != prev_src:
                pactl("set-default-source", prev_src, check=False)

    def _unload_modules(self, kinds):
        mods = pactl("list", "short", "modules", check=False).stdout
        found = []
        for line in mods.splitlines():
            parts = line.split("\t")
            if len(parts) > 2 and parts[1] in kinds and VIRTUAL_SINK in parts[2]:
                found.append((kinds.index(parts[1]), parts[0]))
        for _order, mid in sorted(found):
            pactl("unload-module", mid, check=False)

    def stop_mix(self):
        """Stop whichever loopback is feeding the virtual mic (ours, or a stale one from an old run)."""
        if self.pw_proc is not None:
            self.pw_proc.terminate()
            try:
                self.pw_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.pw_proc.kill()
            self.pw_proc = None
        subprocess.run(["pkill", "-f", "jj-tts-mix"], capture_output=True)  # orphan from a crashed run
        self._unload_modules(["module-loopback"])

    def start_mix(self):
        """Start the real-mic mix. Returns (how, fell_back)."""
        src = self.cfg.get("mic_source", "")
        if not src:
            return "off", False
        want_pw = self.cfg.get("mix_method", "pipewire") == "pipewire"
        if want_pw and shutil.which("pw-loopback"):
            proc = subprocess.Popen(
                ["pw-loopback", "-n", "jj-tts-mix", "-m", "[ FL FR ]",
                 "--capture-props",
                 f'target.object="{src}" node.target="{src}" node.name=jj-tts-mix-in node.dont-reconnect=true',
                 "--playback-props",
                 f'target.object="{VIRTUAL_SINK}" node.target="{VIRTUAL_SINK}" node.name=jj-tts-mix-out '
                 f'node.dont-reconnect=true'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.2)
            if proc.poll() is None and self._mix_routed_ok():
                self.pw_proc = proc
                return "pipewire", False
            if proc.poll() is None:  # running but linked to the wrong things: that is what causes echo
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        lat = int(self.cfg.get("mix_latency", 60))
        pactl("load-module", "module-loopback", f"source={src}", f"sink={VIRTUAL_SINK}",
              f"latency_msec={lat}", "source_dont_move=true", "sink_dont_move=true")
        return "pulse", want_pw

    def _mix_routed_ok(self):
        """Check with pw-link that our loopback reads the real mic and writes ONLY into the virtual sink."""
        if not shutil.which("pw-link"):
            return True
        try:
            out = subprocess.run(["pw-link", "-l"], capture_output=True, text=True, timeout=4).stdout
        except Exception:  # noqa: BLE001
            return True
        cur = ""
        for line in out.splitlines():
            if line and not line[0].isspace():
                cur = line.strip()
                continue
            link = line.strip()
            if cur.startswith("jj-tts-mix-in") and "<-" in link and VIRTUAL_SINK in link:
                return False  # capturing from our own virtual mic = feedback loop
            if cur.startswith("jj-tts-mix-out") and "->" in link and VIRTUAL_SINK not in link:
                return False  # playing into your speakers/another device
        return True

    def apply_mix_async(self, rebuild=False, created=False):
        def job():
            with self.mix_lock:
                try:
                    self.stop_mix()
                    if rebuild:
                        self._unload_modules(["module-remap-source", "module-null-sink"])
                    self.ensure_vmic()
                    how, fell_back = self.start_mix()
                    if rebuild:
                        msg = "Virtual mic rebuilt"
                    elif created:
                        msg = (f"Virtual mic ready. Pick '{VIRTUAL_SINK}_Mic' as the microphone in "
                               f"Discord/VRChat/your game, and switch on '{VIRTUAL_SINK}' under Output devices.")
                    elif how == "off":
                        msg = "Real mic removed from the virtual mic."
                    else:
                        msg = f"Real mic mixed in. Pick '{VIRTUAL_SINK}_Mic' as your mic in Discord/game."
                    if fell_back:
                        msg += " (PipeWire loopback failed or was routed wrongly, used the PulseAudio one instead)"
                    self.status(msg)
                except subprocess.TimeoutExpired:
                    self.status("The audio server didn't answer in time. Is PipeWire running? (systemctl --user status pipewire-pulse)")
                except subprocess.CalledProcessError as e:
                    self.status(f"Audio setup failed: {(e.stderr or str(e)).strip()}")
                except Exception as e:  # noqa: BLE001
                    self.status(f"Audio setup failed: {e}")
            GLib.idle_add(self.refresh_devices)
        threading.Thread(target=job, daemon=True).start()

    def do_shutdown(self):
        try:
            self.stop_mix()
        except Exception:  # noqa: BLE001
            pass
        Adw.Application.do_shutdown(self)

    def on_create_vmic(self, *_):
        self.apply_mix_async(created=True)

    def reset_vmic(self, *_):
        self.apply_mix_async(rebuild=True)

    def on_mic_source_changed(self, row, _p):
        if self._populating:
            return
        i = row.get_selected()
        self.cfg["mic_source"] = self.mic_sources[i - 1][0] if 0 < i <= len(self.mic_sources) else ""
        save_config(self.cfg)
        self.apply_mix_async()

    def on_mix_method_changed(self, row, _p):
        self.cfg["mix_method"] = ["pipewire", "pulse"][row.get_selected()]
        save_config(self.cfg)
        if self.cfg.get("mic_source"):
            self.apply_mix_async()

    def on_latency_changed(self, row, _p):
        self.cfg["mix_latency"] = int(row.get_value())
        save_config(self.cfg)
        if self._lat_timer:
            GLib.source_remove(self._lat_timer)
        self._lat_timer = GLib.timeout_add(700, self._reapply_mix)  # wait until they stop adjusting

    def _reapply_mix(self):
        self._lat_timer = None
        if self.cfg.get("mic_source") and self.cfg.get("mix_method") == "pulse":
            self.apply_mix_async()
        return False

    def on_auto_toggled(self, row, _p):
        self.cfg["auto_vmic"] = row.get_active()
        save_config(self.cfg)

    def auto_setup(self):
        self.apply_mix_async(created=True)

    # ---------------------------------------------------------- page: VRChat (OSC chatbox)
    VRC_MODES = [("both", "TTS + Text Box"), ("voice", "TTS only"), ("chat", "Text Box only")]
    VRC_LONG = [("split", "Split into parts"), ("truncate", "Cut off at 144 characters")]

    def set_cfg(self, key, value):
        self.cfg[key] = value
        save_config(self.cfg)

    def vrc_switch(self, title, subtitle, key, after=None):
        row = Adw.SwitchRow(title=title, subtitle=subtitle, active=bool(self.cfg[key]))

        def changed(r, _p):
            self.set_cfg(key, r.get_active())
            if after:
                after(r.get_active())
        row.connect("notify::active", changed)
        return row

    def vrc_spin(self, title, subtitle, key, lo, hi, step=1):
        adj = Gtk.Adjustment(value=self.cfg[key], lower=lo, upper=hi, step_increment=step, page_increment=step * 5)
        row = Adw.SpinRow(title=title, subtitle=subtitle, adjustment=adj)
        row.connect("notify::value", lambda r, _p: self.set_cfg(key, int(r.get_value())))
        return row

    def vrc_entry(self, title, key, after=None):
        row = Adw.EntryRow(title=title)
        row.set_text(self.cfg[key])

        def changed(r):
            self.set_cfg(key, r.get_text())
            if after:
                after()
        row.connect("changed", changed)
        return row

    def vrc_combo(self, title, subtitle, key, options):
        keys = [k for k, _l in options]
        row = Adw.ComboRow(title=title, subtitle=subtitle, model=Gtk.StringList.new([l for _k, l in options]))
        row.set_selected(keys.index(self.cfg[key]) if self.cfg[key] in keys else 0)
        row.connect("notify::selected", lambda r, _p: self.set_cfg(key, keys[r.get_selected()]))
        return row

    def action_row(self, title, subtitle, button_label, callback):
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        btn = Gtk.Button(label=button_label, valign=Gtk.Align.CENTER)
        btn.connect("clicked", lambda *_: callback())
        row.add_suffix(btn)
        return row

    def page_vrchat(self):
        scroller, box = self.make_page()

        conn = Adw.PreferencesGroup(
            title="Connection",
            description="In VRChat open the Action Menu → Options → OSC → Enabled. The chatbox listens on port 9000.")
        conn.add(self.vrc_switch("Send to the VRChat chatbox", "Master switch for everything on this page",
                                 "vrc_enabled"))
        conn.add(self.vrc_entry("Host", "vrc_host"))
        conn.add(self.vrc_spin("Port", "VRChat's default is 9000", "vrc_port", 1, 65535))
        conn.add(self.action_row("Send a test message", "Shows 'JJ-TTS connected' in your chatbox", "Test",
                                 self.vrc_test))
        conn.add(self.action_row("Clear the chatbox", "Hides whatever is currently showing", "Clear", self.vrc_clear))
        box.append(conn)

        speak = Adw.PreferencesGroup(title="When I speak")
        speak.add(self.vrc_combo("Send messages to", "Applies to the TTS box, phrases and quick-type",
                                 "vrc_mode", self.VRC_MODES))
        speak.add(self.vrc_entry("Prefix", "vrc_prefix"))
        speak.add(self.vrc_switch("Play the chatbox notification sound", "The little ping others hear",
                                  "vrc_notify"))
        speak.add(self.vrc_switch("Show the typing indicator", "The '...' bubble while you type in the TTS or quick box",
                                  "vrc_typing", after=lambda on: None if on else self._typing_off()))
        speak.add(self.vrc_combo("Long messages", "The chatbox only fits 144 characters", "vrc_long", self.VRC_LONG))
        speak.add(self.vrc_spin("Seconds between parts", "Time to read each part before the next one", "vrc_part_delay", 1, 15))
        box.append(speak)

        sticky = Adw.PreferencesGroup(
            title="Sticky Text Box",
            description="A message that stays in your chatbox (for status, pronouns, 'I use TTS', etc.)")
        sticky.add(self.vrc_switch("Keep the sticky message showing", "Re-sent regularly so it doesn't fade out",
                                   "vrc_sticky_on", after=self.on_sticky_toggled))
        self.sticky_entry = self.vrc_entry("Sticky message", "vrc_sticky_text",
                                           after=lambda: setattr(self, "vrc_last_sticky", 0.0))
        sticky.add(self.sticky_entry)
        sticky.add(self.action_row("Use the text from the TTS box", "Copies what you've typed on the Text to Speech page",
                                   "Copy", self.sticky_from_textbox))
        sticky.add(self.vrc_spin("Refresh every (seconds)", "Lower keeps it up longer but spams more", "vrc_sticky_every", 2, 15))
        sticky.add(self.vrc_spin("Pause after a message (seconds)", "Lets your spoken text stay visible first",
                                 "vrc_sticky_pause", 2, 60))
        box.append(sticky)
        return scroller

    def sticky_from_textbox(self):
        text = self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), False).strip()
        if not text:
            self.status("The TTS box is empty")
            return
        self.sticky_entry.set_text(text[:144])  # also saves it and triggers a resend
        self.status("Sticky message updated")

    def on_sticky_toggled(self, on):
        if on:
            self.vrc_last_sticky = 0.0
        elif self.cfg["vrc_enabled"]:
            self.vrc_clear()

    # --- sending
    def vrc_osc(self, addr, *args):
        try:
            if self.vrc_sock is None:
                self.vrc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.vrc_sock.sendto(osc_message(addr, *args), (self.cfg["vrc_host"], int(self.cfg["vrc_port"])))
            return True
        except OSError as e:
            self.status(f"VRChat OSC error: {e}")
            return False

    def vrc_chunks(self, text):
        limit = 144
        if len(text) <= limit:
            return [text]
        if self.cfg["vrc_long"] == "truncate":
            return [text[:limit - 1].rstrip() + "…"]
        parts, cur = [], ""
        for word in text.split():
            while len(word) > limit:  # a single huge "word"
                if cur:
                    parts.append(cur)
                    cur = ""
                parts.append(word[:limit])
                word = word[limit:]
            if len(cur) + len(word) + (1 if cur else 0) <= limit:
                cur = f"{cur} {word}".strip()
            else:
                parts.append(cur)
                cur = word
        if cur:
            parts.append(cur)
        return parts

    def vrc_send_message(self, text):
        self._cancel_typing()
        parts = self.vrc_chunks((self.cfg["vrc_prefix"] + text).strip())
        self.vrc_gen += 1
        self._vrc_send_part(parts, 0, self.vrc_gen)
        if self.cfg["vrc_mode"] == "chat":
            self.status(f"Sent to VRChat chatbox ({len(parts)} parts)" if len(parts) > 1 else "Sent to VRChat chatbox")

    def _vrc_send_part(self, parts, i, gen):
        if gen != self.vrc_gen:  # a newer message replaced this one
            return
        self.vrc_osc("/chatbox/input", parts[i], True, bool(self.cfg["vrc_notify"]))
        self.vrc_last_msg = time.monotonic()
        if i + 1 < len(parts):
            GLib.timeout_add_seconds(int(self.cfg["vrc_part_delay"]),
                                     lambda: (self._vrc_send_part(parts, i + 1, gen), False)[1])

    def vrc_test(self):
        if self.vrc_osc("/chatbox/input", "JJ-TTS connected ✓", True, True):
            self.status(f"Test sent to {self.cfg['vrc_host']}:{self.cfg['vrc_port']}. "
                        "Nothing in VRChat? Check Action Menu → Options → OSC → Enabled.")

    def vrc_clear(self):
        self.vrc_gen += 1
        self.vrc_osc("/chatbox/input", "", True, False)

    def vrc_tick(self):
        c = self.cfg
        text = c["vrc_sticky_text"].strip()
        if c["vrc_enabled"] and c["vrc_sticky_on"] and text:
            now = time.monotonic()
            if now - self.vrc_last_msg >= c["vrc_sticky_pause"] and now - self.vrc_last_sticky >= c["vrc_sticky_every"]:
                self.vrc_osc("/chatbox/input", text[:144], True, False)
                self.vrc_last_sticky = now
        return True

    # --- typing indicator
    def on_typing(self, text):
        c = self.cfg
        if not (c["vrc_enabled"] and c["vrc_typing"]):
            return
        if self._typing_timer:
            GLib.source_remove(self._typing_timer)
            self._typing_timer = None
        if text.strip():
            if not self._typing_on:
                self._typing_on = True
                self.vrc_osc("/chatbox/typing", True)
            self._typing_timer = GLib.timeout_add_seconds(4, self._typing_off)
        else:
            self._typing_off()

    def _cancel_typing(self):
        if self._typing_timer:
            GLib.source_remove(self._typing_timer)
            self._typing_timer = None
        self._typing_off()

    def _typing_off(self):
        self._typing_timer = None
        if self._typing_on:
            self._typing_on = False
            self.vrc_osc("/chatbox/typing", False)
        return False

    # ---------------------------------------------------------- page: settings
    def page_settings(self):
        scroller, box = self.make_page()

        kb = Adw.PreferencesGroup(title="Quick-type")
        self.keybind_row = Adw.ActionRow(title="Global shortcut", subtitle=esc(self.keybind_text))
        kb.add(self.keybind_row)
        test = Adw.ActionRow(title="Open the quick-type box now", activatable=True)
        test.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        test.connect("activated", lambda *_: self.show_quick())
        kb.add(test)
        dismiss = Adw.SwitchRow(title="Close the box when I click away", subtitle="Off by default; Esc and Enter always close it",
                                active=self.cfg.get("quick_dismiss", False))
        dismiss.connect("notify::active", self.on_quick_dismiss_toggled)
        kb.add(dismiss)
        box.append(kb)

        beh = Adw.PreferencesGroup(title="Behavior")
        clear = Adw.SwitchRow(title="Clear the text after speaking", active=self.cfg.get("clear_after", True))
        clear.connect("notify::active", self.on_clear_toggled)
        beh.add(clear)
        stt = Adw.ComboRow(title="Speech-to-text model", subtitle="Bigger = more accurate but slower (downloads on first use)",
                           model=Gtk.StringList.new(STT_MODELS))
        stt.set_selected(STT_MODELS.index(self.cfg["stt_model"]) if self.cfg["stt_model"] in STT_MODELS else 1)
        stt.connect("notify::selected", self.on_stt_model_changed)
        beh.add(stt)
        box.append(beh)

        look = Adw.PreferencesGroup(title="Appearance")
        theme = Adw.ComboRow(title="Theme", model=Gtk.StringList.new(list(THEMES)))
        theme.set_selected(list(THEMES).index(self.cfg["theme"]) if self.cfg["theme"] in THEMES else 1)
        theme.connect("notify::selected", self.on_theme_changed)
        look.add(theme)
        box.append(look)
        return scroller

    def on_quick_dismiss_toggled(self, row, _p):
        self.cfg["quick_dismiss"] = row.get_active()
        save_config(self.cfg)

    def on_clear_toggled(self, row, _p):
        self.cfg["clear_after"] = row.get_active()
        save_config(self.cfg)

    def on_stt_model_changed(self, row, _p):
        self.cfg["stt_model"] = STT_MODELS[row.get_selected()]
        save_config(self.cfg)

    def on_theme_changed(self, row, _p):
        self.cfg["theme"] = list(THEMES)[row.get_selected()]
        save_config(self.cfg)
        self.apply_theme(self.cfg["theme"])

    # ---------------------------------------------------------- speech to text to speech
    def on_stt_toggled(self, btn):
        if btn.get_active():
            self.rec_path = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
            try:
                self.rec = subprocess.Popen(["parecord", "--file-format=wav", self.rec_path])
                btn.set_label("● Recording... click to stop and speak")
                btn.add_css_class("destructive-action")
                self.status("Recording...")
            except OSError as e:
                btn.set_active(False)
                self.status(f"Can't record: {e}")
        elif self.rec:
            self.rec.send_signal(signal.SIGINT)
            self.rec.wait()
            self.rec = None
            btn.set_label("Speech To Text To Speech")
            btn.remove_css_class("destructive-action")
            threading.Thread(target=self._stt_job, args=(self.rec_path, self.cfg["stt_model"]), daemon=True).start()

    def _stt_job(self, path, model):
        try:
            self.status("Transcribing...")
            from faster_whisper import WhisperModel
            if getattr(self, "_whisper_name", None) != model:
                self._whisper = WhisperModel(model, compute_type="int8")
                self._whisper_name = model
            segs, _ = self._whisper.transcribe(path, vad_filter=True)
            text = " ".join(s.text.strip() for s in segs).strip()
            if text:
                GLib.idle_add(self.buffer.set_text, text)
                self.speak(text)
            else:
                self.status("Didn't catch anything")
        except ImportError:
            self.status("Install faster-whisper for voice input: pip install faster-whisper")
        except Exception as e:  # noqa: BLE001
            self.status(f"STT error: {e}")
        finally:
            if os.path.exists(path):
                os.remove(path)

    # ---------------------------------------------------------- quick-type popup (unchanged)
    def build_quick(self):
        self.quick = Adw.Window(application=self, title="JJ-TTS quick", default_width=540, resizable=False)
        self.quick.connect("close-request", lambda w: (w.set_visible(False), True)[1])
        self._quick_armed = False
        box = Gtk.Box(spacing=10, margin_top=14, margin_bottom=14, margin_start=14, margin_end=14)
        box.append(Gtk.Image(icon_name="audio-input-microphone-symbolic", pixel_size=22))
        self.quick_entry = Gtk.Entry(hexpand=True, css_classes=["jj-quick-entry"],
                                     placeholder_text="Type and press Enter to speak (Esc to cancel)")
        self.quick_entry.connect("activate", self.on_quick_activate)
        self.quick_entry.connect("changed", lambda e: self.on_typing(e.get_text()))
        box.append(self.quick_entry)
        self.quick.set_content(box)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_quick_key)
        self.quick.add_controller(keys)
        self.quick.connect("notify::is-active", self.on_quick_active)

    def show_quick(self):
        print("[jj-tts] quick-type requested", file=sys.stderr, flush=True)
        self._quick_armed = False
        if self._dismiss_timer:
            GLib.source_remove(self._dismiss_timer)
            self._dismiss_timer = None
        self.quick_entry.set_text("")
        self.quick.set_visible(True)
        self.quick.present()
        self.quick_entry.grab_focus()
        GLib.timeout_add(120, lambda: (self.quick_entry.grab_focus(), False)[1])  # some compositors focus late

    def on_quick_active(self, win, _p):
        if win.is_active():
            self._quick_armed = True
            if self._dismiss_timer:
                GLib.source_remove(self._dismiss_timer)
                self._dismiss_timer = None
        elif self._quick_armed and self.cfg.get("quick_dismiss"):
            # only close if focus stays away for a moment (focus can flicker when the window opens)
            self._dismiss_timer = GLib.timeout_add(400, self._dismiss_quick)

    def _dismiss_quick(self):
        self._dismiss_timer = None
        if not self.quick.is_active():
            self.quick.set_visible(False)
        return False

    def on_quick_activate(self, entry):
        self.speak(entry.get_text())
        self.quick.set_visible(False)

    def on_quick_key(self, _ctrl, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape:
            self.quick.set_visible(False)
            return True
        return False


if __name__ == "__main__":
    sys.exit(JJTTS().run(sys.argv))
