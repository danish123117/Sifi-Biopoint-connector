
"""
BioPoint -> LSL streamer with optional local CSV recording (Windows version).

For Linux use the other script in this folder.

A small Tkinter app that:
  1. scans for SiFi Labs BioPoint devices over BLE,
  2. connects to the one you pick,
  3. lets you choose which signals (ECG, EMG, EDA, IMU, PPG, temperature) to
     acquire and at what sampling rate,
  4. exposes each selected signal as its own Lab Streaming Layer outlet,
  5. and/or saves each selected signal to a CSV file, at the same time.

Built on sifi-bridge-py 2.x (https://docs.sifilabs.com/python/api-reference)
and pylsl. See README.md for install and run steps.
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
import ctypes
import struct

# ---------------------------------------------------------------------------
# Windows specifics
# ---------------------------------------------------------------------------

if sys.platform != "win32":
    sys.exit("This is the Windows version. On Linux run biopoint_lsl_linux.py instead.")

if struct.calcsize("P") * 8 != 64:
    sys.exit("SiFi's sifibridge only ships for 64-bit Windows. Install the 64-bit Python "
             "3.10-3.12 from python.org and try again.")

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    sys.exit("Tkinter is missing. Re-run the python.org installer, choose Modify, and tick "
             "'tcl/tk and IDLE'.")

import pylsl
import sifi_bridge_py as sbp

PLATFORM_NAME = "Windows"
DEFAULT_SAVE_DIR = os.path.join(os.path.expanduser("~"), "Documents", "BioPoint Recordings")
SCAN_EMPTY_HINT = (
    "No devices found. Check that Bluetooth is on (Settings > Bluetooth & devices), that the "
    "BioPoint is on, and that it is not connected to another app or phone."
)


def platform_preflight() -> list:
    """Things on this machine that will stop BLE from working, as readable hints."""
    hints = []
    ps = shutil.which("powershell")
    if ps:
        try:
            out = subprocess.run(
                [ps, "-NoProfile", "-Command",
                 "(Get-PnpDevice -Class Bluetooth -PresentOnly -ErrorAction SilentlyContinue | "
                 "Measure-Object).Count"],
                capture_output=True, text=True, timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout.strip()
            if out == "0":
                hints.append("Windows reports no Bluetooth adapter. Plug in or enable a "
                             "Bluetooth 4.2+ adapter (5.0 recommended).")
        except (OSError, subprocess.SubprocessError):
            pass
    return hints


def error_hint(err: Exception) -> str:
    msg = str(err).lower()
    if "adapter" in msg or "bluetooth" in msg or "radio" in msg:
        return ("\n\nCheck that Bluetooth is turned on in Settings > Bluetooth & devices and "
                "that the adapter driver is installed in Device Manager.")
    return ""


def platform_setup() -> None:
    # Sharp text on high-DPI screens instead of blurry bitmap scaling.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass

log = logging.getLogger("biopoint_lsl")


# ---------------------------------------------------------------------------
# Signal definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalSpec:
    key: str  # packet_type sent by sifibridge
    label: str  # shown in the GUI
    lsl_type: str  # LSL stream type
    channels: tuple  # channel keys inside packet["data"], in output order
    unit: str  # only set where the docs state it
    rates: tuple  # selectable sampling rates (Hz)
    default_rate: float


# Channel names come from sifi_bridge_py's SensorChannel enum; rate choices were
# checked against the sifibridge 2.0.1 command-line parser.
SIGNALS: dict[str, SignalSpec] = {
    "ecg": SignalSpec("ecg", "ECG", "ECG", ("ecg",), "", (250, 500, 1000, 2000), 500),
    "emg": SignalSpec("emg", "EMG", "EMG", ("emg",), "", (500, 1000, 1600,2000), 1000),
    "eda": SignalSpec("eda", "EDA / BioZ", "EDA", ("eda",), "", (5, 25, 50, 100), 25),
    "imu": SignalSpec(
        "imu", "IMU", "IMU",
        ("qw", "qx", "qy", "qz", "ax", "ay", "az"),
        "", (25, 50, 100, 200), 100,
    ),
    "ppg": SignalSpec(
        "ppg", "PPG", "PPG", ("ir", "r", "g", "b"), "",
        (50, 100, 200, 400, 800), 100,
    ),
    "temperature": SignalSpec(
        "temperature", "Skin temperature", "Temperature", ("temperature",), "degC",
        (0.1, 1, 2, 10), 1,
    ),
}


# ---------------------------------------------------------------------------
# Per-signal sinks: LSL outlet and CSV writer
# ---------------------------------------------------------------------------


class LslSink:
    """One LSL outlet for one signal."""

    def __init__(self, spec: SignalSpec, rate: float, device_name: str, device_id: str):
        info = pylsl.StreamInfo(
            name=f"{device_name}_{spec.lsl_type}",
            type=spec.lsl_type,
            channel_count=len(spec.channels),
            nominal_srate=float(rate),
            channel_format=pylsl.cf_float32,
            source_id=f"{device_id or device_name}_{spec.key}",
        )
        chns = info.desc().append_child("channels")
        for ch in spec.channels:
            c = chns.append_child("channel")
            c.append_child_value("label", ch)
            if spec.unit:
                c.append_child_value("unit", spec.unit)
            c.append_child_value("type", spec.lsl_type)
        info.desc().append_child_value("manufacturer", "SiFi Labs")
        info.desc().append_child_value("device", device_name)
        self.outlet = pylsl.StreamOutlet(info)

    def push(self, samples: list, lsl_times: list):
        for sample, ts in zip(samples, lsl_times):
            self.outlet.push_sample(sample, ts)

    def close(self):
        # Dropping the reference destroys the outlet and removes the stream.
        self.outlet = None


class CsvSink:
    """One CSV file for one signal."""

    def __init__(self, spec: SignalSpec, folder: str):
        self.path = os.path.join(folder, f"{spec.key}.csv")
        self._fh = open(self.path, "w", newline="")
        self._w = csv.writer(self._fh)
        self._w.writerow(["device_time_s", "unix_time_s", "lsl_time_s", *spec.channels])
        self._last_flush = time.monotonic()

    def write(self, dev_times, unix_times, lsl_times, samples):
        for t, u, l, s in zip(dev_times, unix_times, lsl_times, samples):
            self._w.writerow([
                f"{t:.6f}" if t is not None else "",
                f"{u:.6f}" if u is not None else "",
                f"{l:.6f}",
                *s,
            ])
        if time.monotonic() - self._last_flush > 1.0:
            self._fh.flush()
            self._last_flush = time.monotonic()

    def close(self):
        try:
            self._fh.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Acquisition worker (no GUI code, so it can run headless too)
# ---------------------------------------------------------------------------


class Acquisition:
    """Reads packets from a SifiBridge and fans them out to LSL and CSV sinks.

    Runs in its own thread so the GUI stays responsive. LSL and CSV are just two
    sinks on the same packet, which is what makes streaming and saving run in
    parallel.
    """

    def __init__(self, bridge, specs: dict, rates: dict, lsl_on: bool,
                 save_folder: str | None, device_name: str, device_id: str):
        self.bridge = bridge
        self.specs = specs  # key -> SignalSpec, only the selected ones
        self.rates = rates
        self.lsl = {}
        self.csv = {}
        self.counts = {k: 0 for k in specs}
        self.start_time_unix: float | None = None
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        if lsl_on:
            for k, spec in specs.items():
                self.lsl[k] = LslSink(spec, rates[k], device_name, device_id)
        if save_folder:
            os.makedirs(save_folder, exist_ok=True)
            for k, spec in specs.items():
                self.csv[k] = CsvSink(spec, save_folder)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="acquisition", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        for s in (*self.lsl.values(), *self.csv.values()):
            s.close()

    def _drain_typed_queues(self):
        # sifi_bridge_py queues every packet twice: once for get_data() and once
        # for the per-sensor getters. We only read get_data(), so empty the
        # per-sensor queues regularly to keep memory flat on long recordings.
        for getter in (self.bridge.get_ecg, self.bridge.get_emg, self.bridge.get_eda,
                       self.bridge.get_imu, self.bridge.get_ppg,
                       self.bridge.get_temperature, self.bridge.get_event):
            while getter(timeout=0):
                pass

    def _run(self):
        last_drain = time.monotonic()
        try:
            while not self._stop.is_set():
                packet = self.bridge.get_data(timeout=0.2)
                if time.monotonic() - last_drain > 1.0:
                    self._drain_typed_queues()
                    last_drain = time.monotonic()
                if not packet:
                    continue
                self.handle_packet(packet)
        except Exception as e:  # surface to the GUI instead of dying silently
            log.exception("Acquisition thread crashed")
            self.error = str(e)

    def handle_packet(self, packet: dict):
        ptype = packet.get("packet_type")
        if ptype == sbp.PacketType.START_TIME.value:
            self.start_time_unix = packet.get("start_time")
            return
        if ptype == sbp.PacketType.EMG_ARMBAND.value:
            ptype = "emg"
        spec = self.specs.get(ptype)
        if spec is None:
            return  # a signal we did not select, or status/event packets

        data = packet.get("data") or {}
        chans = [c for c in spec.channels if c in data]
        if not chans:
            return
        n = min(len(data[c]) for c in chans)
        if n == 0:
            return
        samples = [[float(data[c][i]) if c in data else float("nan") for c in spec.channels]
                   for i in range(n)]

        # Device timestamps are seconds since acquisition start.
        dev_ts = packet.get("timestamps")
        if not isinstance(dev_ts, list) or len(dev_ts) < n:
            dev_ts = None

        # LSL time: the newest sample is stamped "now", older ones are
        # back-dated by their device-time offset (or by the nominal rate).
        now = pylsl.local_clock()
        if dev_ts is not None:
            last = dev_ts[n - 1]
            lsl_times = [now - (last - dev_ts[i]) for i in range(n)]
        else:
            period = 1.0 / float(self.rates[spec.key])
            lsl_times = [now - (n - 1 - i) * period for i in range(n)]

        if spec.key in self.lsl:
            self.lsl[spec.key].push(samples, lsl_times)
        if spec.key in self.csv:
            if dev_ts is not None and self.start_time_unix is not None:
                unix_times = [self.start_time_unix + dev_ts[i] for i in range(n)]
            else:
                unix_times = [None] * n
            self.csv[spec.key].write(dev_ts[:n] if dev_ts else [None] * n,
                                     unix_times, lsl_times, samples)
        self.counts[spec.key] += n


def configure_device(bridge, selected: dict, rates: dict):
    """Enable exactly the selected sensors and apply their sampling rates."""
    try:
        bridge.set_memory_mode(sbp.MemoryMode.STREAMING)
    except sbp.SifiBridgeError as e:
        log.warning("Could not set streaming memory mode: %s", e)

    bridge.configure_sensors(
        ecg="ecg" in selected,
        emg="emg" in selected,
        eda="eda" in selected,
        imu="imu" in selected,
        ppg="ppg" in selected,
    )
    if "ecg" in selected:
        bridge.configure_ecg(fs=int(rates["ecg"]))
    if "emg" in selected:
        bridge.configure_emg(fs=int(rates["emg"]))
    if "eda" in selected:
        bridge.configure_eda(fs=int(rates["eda"]))
    if "imu" in selected:
        bridge.configure_imu(fs=int(rates["imu"]))
    if "ppg" in selected:
        bridge.configure_ppg_fs(fs=int(rates["ppg"]))
    if "temperature" in selected:
        bridge.configure_temperature(fs=float(rates["temperature"]))


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"BioPoint → LSL ({PLATFORM_NAME})")
        self.minsize(560, 560)

        self.bridge: sbp.SifiBridge | None = None
        self.devices: list[dict] = []
        self.connected: dict | None = None
        self.acq: Acquisition | None = None
        self._ui_queue: queue.Queue = queue.Queue()
        self._busy = False

        self._build()
        self._set_state()
        self.after(100, self._poll_ui_queue)
        self.after(500, self._refresh_counts)
        self.after(300, self._preflight)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----- layout ---------------------------------------------------------
    def _build(self):
        pad = {"padx": 8, "pady": 4}

        dev = ttk.LabelFrame(self, text="1. Device")
        dev.pack(fill="x", **pad)
        btns = ttk.Frame(dev)
        btns.pack(fill="x", padx=6, pady=4)
        self.scan_btn = ttk.Button(btns, text="Search", command=self.on_scan)
        self.scan_btn.pack(side="left")
        self.connect_btn = ttk.Button(btns, text="Connect", command=self.on_connect)
        self.connect_btn.pack(side="left", padx=6)
        self.disconnect_btn = ttk.Button(btns, text="Disconnect", command=self.on_disconnect)
        self.disconnect_btn.pack(side="left")
        self.dev_list = tk.Listbox(dev, height=5, exportselection=False)
        self.dev_list.pack(fill="x", padx=6, pady=(0, 6))
        self.dev_list.bind("<Double-Button-1>", lambda _e: self.on_connect())

        sig = ttk.LabelFrame(self, text="2. Signals to acquire")
        sig.pack(fill="x", **pad)
        self.sig_vars: dict[str, tk.BooleanVar] = {}
        self.rate_vars: dict[str, tk.StringVar] = {}
        self.count_vars: dict[str, tk.StringVar] = {}
        ttk.Label(sig, text="Signal").grid(row=0, column=0, sticky="w", padx=6)
        ttk.Label(sig, text="Rate (Hz)").grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(sig, text="Samples").grid(row=0, column=2, sticky="w", padx=6)
        for r, (k, spec) in enumerate(SIGNALS.items(), start=1):
            v = tk.BooleanVar(value=k in ("ecg", "imu"))
            self.sig_vars[k] = v
            ttk.Checkbutton(sig, text=spec.label, variable=v).grid(row=r, column=0, sticky="w", padx=6)
            rv = tk.StringVar(value=_fmt_rate(spec.default_rate))
            self.rate_vars[k] = rv
            ttk.Combobox(sig, textvariable=rv, width=7, state="readonly",
                         values=[_fmt_rate(x) for x in spec.rates]).grid(row=r, column=1, padx=6, pady=1)
            cv = tk.StringVar(value="–")
            self.count_vars[k] = cv
            ttk.Label(sig, textvariable=cv, width=12).grid(row=r, column=2, sticky="w", padx=6)
        ttk.Label(sig, foreground="gray",
                  text="Temperature only arrives while another sensor is on.").grid(
            row=len(SIGNALS) + 1, column=0, columnspan=3, sticky="w", padx=6, pady=(2, 4))

        out = ttk.LabelFrame(self, text="3. Outputs (both can be on at once)")
        out.pack(fill="x", **pad)
        self.lsl_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(out, text="Stream to LSL (one outlet per signal)",
                        variable=self.lsl_var).grid(row=0, column=0, columnspan=3, sticky="w", padx=6)
        self.save_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(out, text="Save locally to CSV in:",
                        variable=self.save_var).grid(row=1, column=0, sticky="w", padx=6)
        self.folder_var = tk.StringVar(value=DEFAULT_SAVE_DIR)
        ttk.Entry(out, textvariable=self.folder_var, width=40).grid(row=1, column=1, sticky="we", padx=4)
        ttk.Button(out, text="…", width=3, command=self._pick_folder).grid(row=1, column=2, padx=4, pady=4)
        out.columnconfigure(1, weight=1)

        run = ttk.Frame(self)
        run.pack(fill="x", **pad)
        self.start_btn = ttk.Button(run, text="Start", command=self.on_start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(run, text="Stop", command=self.on_stop)
        self.stop_btn.pack(side="left", padx=6)

        self.status_var = tk.StringVar(value="Click Search to look for BioPoint devices.")
        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w").pack(
            fill="x", side="bottom", padx=8, pady=6)

    def _preflight(self):
        hints = platform_preflight()
        if hints:
            messagebox.showwarning("Bluetooth setup", "\n\n".join(hints))

    def _pick_folder(self):
        d = filedialog.askdirectory(initialdir=self.folder_var.get() or os.getcwd())
        if d:
            self.folder_var.set(d)

    def _set_state(self):
        running = self.acq is not None
        conn = self.connected is not None
        idle = not self._busy

        def en(w, on):
            w.configure(state="normal" if on else "disabled")

        en(self.scan_btn, idle and not running)
        en(self.connect_btn, idle and not running and bool(self.devices))
        en(self.disconnect_btn, idle and conn and not running)
        en(self.start_btn, idle and conn and not running)
        en(self.stop_btn, idle and running)

    # ----- background work plumbing ---------------------------------------
    def _bg(self, label: str, fn, on_done=None):
        """Run fn() off the Tk thread; call on_done(result) back on it."""
        self._busy = True
        self.status_var.set(label)
        self._set_state()

        def worker():
            try:
                res, err = fn(), None
            except Exception as e:  # noqa: BLE001
                log.exception(label)
                res, err = None, e
            self._ui_queue.put((on_done, res, err))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_ui_queue(self):
        try:
            while True:
                cb, res, err = self._ui_queue.get_nowait()
                self._busy = False
                if err is not None:
                    self.status_var.set(f"Error: {err}")
                    messagebox.showerror("BioPoint", str(err) + error_hint(err))
                elif cb:
                    cb(res)
                self._set_state()
        except queue.Empty:
            pass
        self.after(100, self._poll_ui_queue)

    def _ensure_bridge(self):
        if self.bridge is None:
            # First run downloads the sifibridge CLI if it is missing.
            self.bridge = sbp.SifiBridge()
        return self.bridge

    # ----- actions --------------------------------------------------------
    def on_scan(self):
        def work():
            return self._ensure_bridge().list_devices(sbp.ListSources.BLE)

        def done(devs):
            devs = [d if isinstance(d, dict) else {"name": str(d)} for d in (devs or [])]
            # Show BioPoints first, but keep everything in case it was renamed.
            devs.sort(key=lambda d: "biopoint" not in str(d.get("name", "")).lower())
            self.devices = devs
            self.dev_list.delete(0, "end")
            for d in devs:
                self.dev_list.insert("end", f"{d.get('name', '?')}    [{d.get('id', '')}]")
            if devs:
                self.dev_list.selection_set(0)
            self.status_var.set(f"Found {len(devs)} device(s)." if devs else
                                SCAN_EMPTY_HINT)

        self._bg("Searching for BLE devices…", work, done)

    def on_connect(self):
        sel = self.dev_list.curselection()
        if not sel:
            messagebox.showinfo("BioPoint", "Pick a device in the list first.")
            return
        d = self.devices[sel[0]]
        handle = d.get("id") or d.get("name")

        def work():
            b = self._ensure_bridge()
            if not b.connect(handle, timeout=15):
                raise RuntimeError(f"Could not connect to {d.get('name', handle)}.")
            try:
                batt = b.get_battery()
            except sbp.SifiBridgeError:
                batt = None
            return batt

        def done(batt):
            self.connected = d
            b = f", battery {batt}%" if batt is not None else ""
            self.status_var.set(f"Connected to {d.get('name', handle)}{b}.")

        self._bg(f"Connecting to {d.get('name', handle)}…", work, done)

    def on_disconnect(self):
        def done(_):
            self.connected = None
            self.status_var.set("Disconnected.")

        self._bg("Disconnecting…", lambda: self.bridge.disconnect(), done)

    def on_start(self):
        selected = {k: SIGNALS[k] for k, v in self.sig_vars.items() if v.get()}
        if not selected:
            messagebox.showinfo("BioPoint", "Select at least one signal.")
            return
        if set(selected) == {"temperature"}:
            messagebox.showinfo("BioPoint", "Temperature needs at least one other sensor on.")
            return
        lsl_on, save_on = self.lsl_var.get(), self.save_var.get()
        if not (lsl_on or save_on):
            messagebox.showinfo("BioPoint", "Turn on LSL streaming, saving, or both.")
            return
        rates = {k: float(self.rate_vars[k].get()) for k in selected}
        folder = None
        if save_on:
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            folder = os.path.join(self.folder_var.get(), f"biopoint_{stamp}")
        dev = self.connected or {}
        name = str(dev.get("name") or "BioPoint")
        dev_id = str(dev.get("id") or "")

        def work():
            b = self.bridge
            configure_device(b, selected, rates)
            acq = Acquisition(b, selected, rates, lsl_on, folder, name, dev_id)
            b.clear_data_buffer()
            acq.start()
            try:
                if not b.start():
                    raise RuntimeError("Device refused to start the acquisition.")
            except Exception:
                acq.stop()
                raise
            return acq

        def done(acq):
            self.acq = acq
            parts = []
            if lsl_on:
                parts.append(f"streaming {len(selected)} LSL outlet(s)")
            if save_on:
                parts.append(f"saving to {folder}")
            self.status_var.set("Running: " + " and ".join(parts) + ".")

        self._bg("Configuring sensors and starting…", work, done)

    def on_stop(self):
        acq = self.acq

        def work():
            try:
                self.bridge.stop()
            finally:
                acq.stop()
            return acq

        def done(a):
            self.acq = None
            total = ", ".join(f"{SIGNALS[k].label}: {n}" for k, n in a.counts.items())
            saved = f" Files in {os.path.dirname(next(iter(a.csv.values())).path)}." if a.csv else ""
            self.status_var.set(f"Stopped. Samples {total}.{saved}")

        self._bg("Stopping…", work, done)

    def _refresh_counts(self):
        acq = self.acq
        for k, cv in self.count_vars.items():
            cv.set(str(acq.counts[k]) if acq and k in acq.counts else "–")
        if acq and acq.error:
            self.status_var.set(f"Acquisition error: {acq.error}")
        self.after(500, self._refresh_counts)

    def _on_close(self):
        try:
            if self.acq is not None:
                try:
                    self.bridge.stop()
                finally:
                    self.acq.stop()
            if self.bridge is not None:
                if self.connected is not None:
                    try:
                        self.bridge.disconnect()
                    except sbp.SifiBridgeError:
                        pass
                self.bridge.close()
        finally:
            self.destroy()


def _fmt_rate(x: float) -> str:
    return f"{x:g}"


def main():
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    platform_setup()
    App().mainloop()


if __name__ == "__main__":
    main()
