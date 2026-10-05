# BioPoint → LSL

A small desktop app that connects to a SiFi Labs BioPoint over Bluetooth LE and
streams its signals to [Lab Streaming Layer](https://labstreaminglayer.org/) (LSL),
saves them to CSV, or both at once.

Supported signals: ECG, EMG, EDA/BioZ, IMU, PPG and skin temperature.

There is one script per platform:

| Platform | Script |
| --- | --- |
| Windows | `Windows/biopoint_lsl_windows.py` |
| Linux | `Linux/biopoint_lsl_linux.py` |

## Requirements

- Python 3.10–3.12 with Tkinter
- A Bluetooth 4.2+ adapter (5.0 recommended)
- **Windows:** 64-bit Python from python.org (tick "tcl/tk and IDLE" during install)
- **Linux:** Tkinter (`sudo apt install python3-tk` on Ubuntu/Debian), the
  `bluetooth` service running, and your user in the `bluetooth` group.
  The pip `pylsl` package bundles liblsl only for x86_64 with glibc 2.35+
  (Ubuntu 22.04 or newer); otherwise install liblsl from
  [its releases page](https://github.com/sccn/liblsl/releases).

## Install

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r Windows\requirements.txt
```

Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r Linux/requirements.txt
```

On first run, `sifi-bridge-py` downloads the `sifibridge` command-line tool if it
is missing, so you need an internet connection the first time.

## Run

```powershell
python Windows\biopoint_lsl_windows.py      # Windows
```

```bash
python3 Linux/biopoint_lsl_linux.py         # Linux
```

## Usage

1. **Device** – Turn the BioPoint on, click **Search**, select it in the list and
   click **Connect** (or double-click it). Make sure it isn't connected to a phone
   or another app.
2. **Signals** – Tick the signals you want and pick a sampling rate for each.
   Temperature only arrives while at least one other sensor is on.
3. **Outputs** – Choose **Stream to LSL**, **Save locally to CSV**, or both, and
   pick the save folder.
4. Click **Start**. The sample counters show data coming in. Click **Stop** when
   done, then **Disconnect**.

## Output

### LSL

One outlet per signal, named `<device name>_<type>` (for example
`BioPoint_ECG`), with stream type `ECG`, `EMG`, `EDA`, `IMU`, `PPG` or
`Temperature`. Receive them in LabRecorder or any LSL client.

### CSV

Each recording goes into its own folder, `biopoint_YYYYMMDD_HHMMSS`, inside the
chosen save folder (default `Documents\BioPoint Recordings` on Windows,
`~/biopoint_recordings` on Linux). There is one file per signal, e.g. `ecg.csv`,
`imu.csv`, with these columns:

| Column | Meaning |
| --- | --- |
| `device_time_s` | Seconds since acquisition start, from the device |
| `unix_time_s` | Wall-clock time (Unix seconds) |
| `lsl_time_s` | LSL timestamp, matching the LSL stream |
| remaining columns | Signal channels, e.g. `qw,qx,qy,qz,ax,ay,az` for IMU or `ir,r,g,b` for PPG |

## Troubleshooting

- **No devices found** – Check that Bluetooth is on, the BioPoint is on, and it
  isn't connected elsewhere. On Linux, check `bluetoothctl show`,
  `rfkill list` and `systemctl status bluetooth`.
- **Linux in WSL or a container** – BLE needs the host's BlueZ over D-Bus; run on
  the host instead.
- The app shows a warning at startup if it detects a Bluetooth setup problem.
