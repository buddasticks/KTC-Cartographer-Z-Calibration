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

Add the supplied configuration to `printer.cfg` or another included cfg file.

## Default configuration

The supplied config assumes:

```text
T0 -> extruder
T1 -> extruder1
T2 -> extruder2
T3 -> extruder3
Bed -> heater_bed
```

Change the `toolN_heater` settings if your printer uses different heater names.

## Use

Calibrate all tools:

```text
KTC_CARTOGRAPHER_Z_CALIBRATE
```

Calibrate one tool against a fresh T0 reference:

```text
KTC_CARTOGRAPHER_Z_CALIBRATE TOOL=1
```

Show the last results:

```text
KTC_CARTOGRAPHER_Z_STATUS
```

## What it does

1. Heats the bed and all tool heaters at the same time.
2. Waits for the requested temperatures.
3. Selects T0 and runs `CARTOGRAPHER_TOUCH_HOME`.
4. Uses T0 as the zero reference.
5. Selects each other tool and runs `CARTOGRAPHER_TOUCH_PROBE`.
6. Calculates each tool's Z delta from T0.
7. Applies the new `gcode_z_offset` values to the running printer.
8. Turns the bed and tool heaters off.
9. Leaves the offsets active in runtime.

The module does **not** edit `printer.cfg` automatically.

Use Klipper's normal `SAVE_CONFIG` command or the Mainsail/Fluidd **Save Config** button to persist the offsets.

## Important

Existing `gcode_z_offset` values are not added to the measurement. The calibration measures the physical difference between the tools and calculates a fresh offset from T0.

Make sure the printer is homed before starting calibration and verify the probe position is safe.

## License

GPLv3.
