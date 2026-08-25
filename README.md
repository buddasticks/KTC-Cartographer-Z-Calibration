# KTC Cartographer Z Calibration

Standalone Klipper extra for automatic multi-tool Z-offset calibration using Cartographer Touch.

## Requirements

- Klipper
- Klipper toolchanger / KTC-Easy
- Cartographer with Touch support
- `CARTOGRAPHER_TOUCH_HOME`
- `CARTOGRAPHER_TOUCH_PROBE`

## Install

Copy `ktc_cartographer_z_calibrate.py` into your Klipper `extras` directory.

For example:

```text
~/klipper/klippy/extras/ktc_cartographer_z_calibrate.py
```

Add the supplied configuration to `printer.cfg` or another included cfg file:

```text
[include ktc_cartographer_z_calibrate.cfg]
```

Optionally, if you want a nozzle wipe before each probe touch, un comment :

```text
#wipe_gcode: WIPE_NOZZLE
```

## Default configuration

The supplied config assumes 4 tools:

```text
T0 -> extruder
T1 -> extruder1
T2 -> extruder2
T3 -> extruder3
Bed -> heater_bed
```

If you have more or fewer tools, set `tool_count` accordingly and add/remove
`toolN_heater` entries — heater names default to `extruderN` (and `extruder`
for T0) if not given explicitly. The tool list actually calibrated is normally
read straight from the toolchanger; `tool_count` mainly controls how many
heater entries get built and is the fallback if the toolchanger doesn't report
its own tool list.

## Use

Calibrate all detected tools against T0:

```text
KTC_CARTOGRAPHER_Z_CALIBRATE
```

There is currently no per-tool `TOOL=` parameter — every run calibrates the reference
tool plus every other detected tool in one pass.

Show the last results:

```text
KTC_CARTOGRAPHER_Z_STATUS
```

## What it does

1. Initializes the toolchanger from the physically detected tool if needed.
2. Calls `T0` to select the reference tool and load the Cartographer touch model.
3. Heats the bed and all tool heaters at the same time, then waits for every target
   to stabilize.
4. Optionally wipes the nozzle (`wipe_gcode`), then selects T0 and runs
   `CARTOGRAPHER_TOUCH_HOME` as the zero reference.
5. For each other tool: selects it, optionally wipes the nozzle, and runs
   `CARTOGRAPHER_TOUCH_PROBE`.
6. Calculates each tool's Z delta from T0. Aborts with no offsets written if any
   tool's delta exceeds `max_offset`.
7. Applies every `gcode_z_offset` at runtime via `apply_offset_gcode`
   (`SET_TOOL_PARAMETER`).
8. If `persist_offsets` is enabled (default), also registers each value with
   Klipper's `configfile`, so `SAVE_CONFIG` writes it into the matching
   `[tool T{n}]` section at the bottom of `printer.cfg`.
9. Returns to T0 and turns the bed and tool heaters off.

An already-active tool is never redundantly reselected — the reselect step is
skipped entirely if the toolchanger reports that tool as already current, since
re-issuing a `T{n}` macro for an already-active tool has been observed to corrupt
this toolchanger's active-tool state.

## Nozzle wiping

Set `wipe_gcode` to your wipe macro's command (e.g. `WIPE_NOZZLE`) to run it before
every probe touch, including the reference tool. Leave it blank to disable — this
is a clean no-op, not an error.

A standalone brush-wipe macro (`wipe_nozzle.cfg` / `WIPE_NOZZLE`) is provided
separately: it does a back-and-forth drag across a fixed brush position, with no
purge or extrusion involved. Configure the brush's X/Y/Z position in its
`_WIPE_NOZZLE_GLOBALS` section before use.

## Persisting offsets

The module does **not** write `printer.cfg` itself, and `SET_TOOL_PARAMETER` alone
does not either — it only sets the runtime value. With `persist_offsets: True`
(the default), each measured offset is additionally queued with `configfile.set()`
against `config_section` / `config_option` (default `tool T{tool}` /
`gcode_z_offset`). That queued value is only written to disk, and the firmware
restarted, when you run Klipper's normal `SAVE_CONFIG` command or press the
Mainsail/Fluidd **Save Config** button.

Set `persist_offsets: False` if you only want runtime-only calibration with no
`SAVE_CONFIG` queuing.

## Important

Existing `gcode_z_offset` values are not added to the measurement. The calibration
measures the physical difference between the tools and calculates a fresh offset
from T0.

Make sure the printer is homed before starting calibration and verify the probe
position is safe.

## License

GPLv3.
