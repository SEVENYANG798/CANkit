# XCP Variable Reader — PCAN / Vector

Windows desktop tool that talks **XCP on Classic CAN / CAN FD** via **PEAK PCAN** or **Vector XL** hardware, loads **A2L** or **ELF** symbols, and reads / writes online variables (Polling or DAQ).

## Features

### Connection & hardware
- Hardware: **PCAN** or **Vector** (UI selectable)
- **Vector discovery**: enumerates connected interfaces (e.g. VN1630) and CAN channels via XL Driver; ↻ refresh; falls back to Channel 0–3 when nothing is detected
- Connect / disconnect XCP slave
- Connection essentials always visible (hardware, device/channel, TX/RX IDs); **高级配置** folds bus timing, session resilience, and Security & Cal page
- **A2L IF_DATA XCP**: may suggest baudrate / CAN FD only; **Master/Slave CAN IDs stay manual**

### Symbols & A2L tooling
- Load A2L (MEASUREMENT / CHARACTERISTIC + COMPU_METHOD) or ELF (symbol addresses)
- Address paths: `ECU_ADDRESS`, `LINK_MAP`, `SYMBOL_LINK`, `REF_MEMORY_SEGMENT`(+OFFSET)
- **Resolve addresses from ELF…**: in-memory overlay for A2L symbols still at address 0
- **ELF → A2L…**: create a new A2L from ELF globals (bundled XCP template, optional DWARF struct expansion). Adapted from the `toa2l` tool
- **Trace Viewer…**: offline ASC / BLF / CSV and live CAN observation (ID column filter, optional DBC decode)
- **Update A2L from ELF…**: ASAP2-style rewrite of `ECU_ADDRESS`, CHARACTERISTIC positional address (`VALUE …`), and `LINK_MAP`; GHS-style name aliases (`_Foo`, `..path` suffix) + DWARF struct member expansion when available  
  - Runs in a **background thread** with a **non-modal** progress window  
  - Matching depends on the ELF/map; MICROSAR `SYMBOL_LINK` names that are absent from the ELF remain in *missing*
- **Async symbol load** (also non-modal): large A2L/ELF can load while you edit connection settings
- Symbol filter: substring, or paste a multi-name list (exact match; shows matched + missing)

### Measurement / calibration
- **Polling** tab: periodic `SHORT_UPLOAD` / `UPLOAD`
- **DAQ** tab: dynamic DAQ list (ALLOC / WRITE_DAQ / START_STOP) + DTO decode
- **Online calibration write**: `Write value…` via XCP DOWNLOAD (A2L CHARACTERISTIC preferred)
- Physical value conversion when A2L provides a COMPU_METHOD
- **Seed&Key unlock**: Python plugin (`.py`) or SeedNKey-style DLL; optional auto-unlock after CONNECT
- **Calibration page**: SET/GET_CAL_PAGE; optional auto SET after CONNECT
- **Write diagnostics**: locked / wrong page / write-protected / verify mismatch / timeout

### Session resilience & UX
- **CTO retries + auto reconnect**; DAQ DTO silence → recover + rebuild list
- Resilience knobs in UI / `.xcpp` (retries, backoff, DAQ silence timeout)
- **Live curves**: Polling/DAQ **Curves…**
- **Log filter**: All / App / XCP / CAN / Errors; optional Verbose
- **Project file** (`.xcpp`): connection, security, resilience, symbol path, Poll/DAQ watches; auto-restore on launch

Not included: flash programming, CANape/CANoe COM automation, full ASAP2 Updater parity (no `.map` importer yet).

## Prerequisites

1. Python 3.11+
2. Hardware driver (one of):
   - **PCAN**: [PEAK drivers](https://www.peak-system.com/Drivers.523.0.html) (`PCANBasic.dll`)
   - **Vector**: Vector XL Driver (`vxlapi64.dll`). Prefer selecting a discovered device/channel. If discovery shows *Not detected*, assign channels in **Vector Hardware Config** to the **Vector app name** (default `XcpReader`)
3. An XCP-capable ECU (or simulator)

**Note:** Do not run this tool as a second XCP master on the same channel while CANape/CANoe already owns that measurement session.

## Install

```powershell
cd E:\Code_Repository\XcpOnPcan
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

If PyPI downloads are slow, use a mirror, for example:

```powershell
pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple
```

GUI dependency is **PySide6-Essentials** (lighter than full PySide6).

## Run

```powershell
python -m xcp_pcan
```

Projects default to `%USERPROFILE%\.xcp_pcan\default.xcpp` and are auto-saved on exit. Use toolbar **Open / Save / Save project as…**.

## Typical workflow

1. Select **Hardware**: PCAN or Vector  
   - PCAN: choose `PCAN_USBBUS1` etc.  
   - Vector: pick **Device** (e.g. VN1630) + **Channel** (CAN1…); optional **Vector app name** under 高级配置  
   - Set TX/RX CAN IDs; bitrate / CAN FD under 高级配置 if needed
2. (Optional) Open **高级配置 → Security & Cal page**: Seed&Key, Auto unlock / Auto SET_CAL_PAGE
3. Click **Connect**
4. **Load A2L/ELF…** (or restore from project). For address-0 A2L symbols: **Resolve addresses from ELF…**. To rewrite a file on disk: **Update A2L from ELF…**
5. Filter / select symbols → **Add to current tab** (Polling or DAQ)
6. Polling: interval → **Start poll** · DAQ: event / prescaler → **Start DAQ**
7. Calibration: select a watch row → **Write value…**

### Seed&Key plugin

Python plugin must define:

```python
def compute_key(resource: int, seed: bytes) -> bytes:
    ...
```

DLL default export: `XCP_ComputeKeyFromSeed` (CANape/Vector-style). See `examples/seedkey_echo.py` (key==seed, for open/sim slaves only).

Polling and DAQ cannot run at the same time on one session — stop one before starting the other.  
Writes use `SHORT_DOWNLOAD` when the CTO is large enough (CAN FD), otherwise `SET_MTA` + `DOWNLOAD`.  
While polling is running, **Write value…** briefly pauses the poller so CTOs do not interleave.

## Configuration defaults

| Setting          | Default    |
|------------------|------------|
| Hardware         | PCAN       |
| Vector app name  | XcpReader  |
| Nom bitrate      | 500000     |
| CAN FD           | off        |
| Data bitrate     | 2000000    |
| FD f_clock       | 80 MHz (PCAN only) |
| Master CAN ID    | 0x667      |
| Slave CAN ID     | 0x7E1      |
| CTO timeout      | 1000 ms    |
| Poll interval    | 100 ms     |

Bitrate may be suggested from A2L when present; CAN IDs are not overwritten by A2L.

## Session resilience

Under **高级配置 → Session resilience** (saved in `.xcpp` `resilience`):

| Setting | Default | Effect |
|---------|---------|--------|
| CTO retries | 2 | Extra attempts after first timeout |
| CTO retry delay | 50 ms | Pause between CTO retries |
| Auto reconnect | on | Polling / DAQ rebuild may re-CONNECT |
| Reconnect attempts | 5 | Max recover_session tries |
| Reconnect backoff | 400 ms | Base sleep × attempt index |
| DAQ DTO silence | 2000 ms | Rebuild DAQ list if no DTO this long |

## Build standalone EXE

One-click (double-click or from a shell):

```powershell
.\build_exe.bat
```

Or:

```powershell
.\build_exe.ps1
```

Produces `output\XcpVariableReader.exe` (PyInstaller onefile, no console). Intermediate `.pyinstaller\` files are deleted after a successful build. Target PCs still need **PCAN** or **Vector** drivers installed; Python is not required.

## Tests

```powershell
pytest
ruff check src tests
```

## Project layout

```
src/xcp_pcan/
  can/                 # PCAN / Vector transport + Vector discovery
  xcp/                 # protocol + master + poller + DAQ + Seed&Key
  symbols/             # A2L / ELF loaders, address resolve, A2L←ELF updater
  ui/
    main_window.py     # shell: UI build, log, shutdown
    mw_connect.py      # Connect / disconnect / Seed&Key / cal page
    mw_symbols.py      # A2L/ELF load, overlay, A2L update, ELF→A2L
    mw_project.py      # .xcpp + Trace/Help windows
    mw_measure.py      # watch / poll / DAQ / write / charts
    worker_util.py     # shared QThread / progress helpers
    connection_panel.py
    …
```
