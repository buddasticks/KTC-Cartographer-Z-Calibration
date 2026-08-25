# KTC Cartographer Z Calibration
# Standalone Klippy extra for automatic multi-tool Z offset calibration.
#
# Requires:
#   - Klipper toolchanger
#   - Cartographer Touch
#
# Calibration flow:
#   1. Heat bed and all tool heaters in parallel.
#   2. Select T0 and run CARTOGRAPHER_TOUCH_HOME.
#   3. Treat T0 as the zero reference.
#   4. Select each remaining tool and run CARTOGRAPHER_TOUCH_PROBE.
#   5. Calculate tool_delta = tool_contact - T0_contact.
#   6. Apply the resulting gcode_z_offset values at runtime.
#   7. Do NOT edit printer.cfg automatically.
#   8. Turn all heaters off after calibration.
#   9. Use Klipper SAVE_CONFIG / the Save Config button to persist values.
#
# Default heater mapping for the SV08/KTC-Easy setup:
#   T0 -> extruder
#   T1 -> extruder1
#   T2 -> extruder2
#   T3 -> extruder3
#
# GPLv3

import os


class KTCCartographerZCalibrate:
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.reactor = self.printer.get_reactor()

        self.reference_tool = config.getint("reference_tool", 0)
        self.probe_x = config.getfloat("probe_x", None)
        self.probe_y = config.getfloat("probe_y", None)

        self.lift_z = config.getfloat("lift_z", 10.0, above=0.0)
        self.travel_speed = config.getfloat("travel_speed", 100.0, above=0.0)
        self.z_move_speed = config.getfloat("z_move_speed", 10.0, above=0.0)

        self.touch_home_gcode = config.get("touch_home_gcode", "CARTOGRAPHER_TOUCH_HOME")
        self.touch_probe_gcode = config.get("touch_probe_gcode", "CARTOGRAPHER_TOUCH_PROBE")

        self.max_offset = config.getfloat("max_offset", 3.0, above=0.0)
        self.tool_calibrate_temperature = config.getfloat(
            "tool_calibrate_temperature", 145.0, above=0.0
        )
        self.bed_calibrate_temperature = config.getfloat(
            "bed_calibrate_temperature", 60.0, above=0.0
        )
        self.temperature_wait = config.getboolean("temperature_wait", True)

        # Explicit heater mapping.  This avoids relying on tool object names.
        self.bed_heater_name = config.get("bed_heater", "heater_bed")
        self.heater_map = {}
        for tool_no, default_name in (
            (0, "extruder"),
            (1, "extruder1"),
            (2, "extruder2"),
            (3, "extruder3"),
        ):
            self.heater_map[tool_no] = config.get(
                "tool%d_heater" % tool_no, default_name
            )

        # Optional additional mappings, e.g. tool4_heater: extruder4.
        for tool_no in range(4, 16):
            value = config.get("tool%d_heater" % tool_no, None)
            if value is not None:
                self.heater_map[tool_no] = value

        self.offset_decimals = config.getint(
            "offset_decimals", 4, minval=3, maxval=6
        )

        self.running = False
        self.last_reference_z = None
        self.last_results = {}
        self.last_run_success = False

        self.gcode.register_command(
            "KTC_CARTOGRAPHER_Z_CALIBRATE",
            self.cmd_CALIBRATE,
            desc="Automatically calibrate tool Z offsets using Cartographer Touch",
        )
        self.gcode.register_command(
            "KTC_CARTOGRAPHER_Z_STATUS",
            self.cmd_STATUS,
            desc="Show last KTC Cartographer Z calibration results",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_toolchanger(self):
        tc = self.printer.lookup_object("toolchanger", None)
        if tc is None:
            raise self.gcode.error("KTC Cartographer Z Calibration requires [toolchanger].")
        return tc

    def _get_toolhead(self):
        return self.printer.lookup_object("toolhead")

    def _get_cartographer(self):
        obj = self.printer.lookup_object("cartographer", None)
        if obj is None:
            raise self.gcode.error("KTC Cartographer Z Calibration requires Cartographer.")
        return obj

    def _get_tool_numbers(self):
        tc = self._get_toolchanger()
        numbers = getattr(tc, "tool_numbers", None)
        if numbers is not None:
            try:
                result = sorted(set(int(x) for x in numbers if int(x) >= 0))
                if result:
                    return result
            except Exception:
                pass

        tools = []
        for name in self.printer.lookup_objects():
            if not name.startswith("tool T"):
                continue
            try:
                tool = self.printer.lookup_object(name)
                number = int(getattr(tool, "tool_number"))
                if number >= 0:
                    tools.append(number)
            except Exception:
                pass
        return sorted(set(tools))

    def _get_tool_object(self, tool_no):
        for name in ("tool T%d" % tool_no, "tool%d" % tool_no, "T%d" % tool_no):
            obj = self.printer.lookup_object(name, None)
            if obj is not None:
                return obj
        return None

    def _get_current_tool(self):
        for tool_no in self._get_tool_numbers():
            tool = self._get_tool_object(tool_no)
            if tool is None:
                continue
            if getattr(tool, "active", False):
                return tool_no
            if hasattr(tool, "get_status"):
                try:
                    status = tool.get_status(self.reactor.monotonic())
                    if isinstance(status, dict) and status.get("active", False):
                        return tool_no
                except Exception:
                    pass
        return -1

    def _read_cartographer_result(self):
        cartographer = self._get_cartographer()
        now = self.reactor.monotonic()

        try:
            status = cartographer.get_status(now)
            if isinstance(status, dict):
                touch = status.get("touch")
                if isinstance(touch, dict) and touch.get("last_z_result") is not None:
                    return float(touch["last_z_result"])
                if status.get("last_z_result") is not None:
                    return float(status["last_z_result"])
        except Exception:
            pass

        for obj_name in ("scanner",):
            obj = self.printer.lookup_object(obj_name, None)
            if obj is None:
                continue
            try:
                status = obj.get_status(now)
                if isinstance(status, dict) and status.get("last_z_result") is not None:
                    return float(status["last_z_result"])
            except Exception:
                pass
            try:
                if hasattr(obj, "last_z_result"):
                    return float(obj.last_z_result)
            except Exception:
                pass

        try:
            return float(cartographer.last_z_result)
        except Exception:
            return None

    def _run_gcode(self, script):
        self.gcode.run_script_from_command(script)
        self._get_toolhead().wait_moves()

    def _is_homed(self):
        try:
            status = self._get_toolhead().get_status(self.reactor.monotonic())
            homed = status.get("homed_axes", "")
            return all(axis in homed for axis in ("x", "y", "z"))
        except Exception:
            return False

    def _lift(self):
        toolhead = self._get_toolhead()
        pos = toolhead.get_position()
        if pos[2] < self.lift_z:
            toolhead.manual_move([None, None, self.lift_z], self.z_move_speed)
        toolhead.wait_moves()

    def _move_to_probe_position(self):
        if self.probe_x is None or self.probe_y is None:
            return
        toolhead = self._get_toolhead()
        pos = toolhead.get_position()
        safe_z = max(float(pos[2]), self.lift_z)
        toolhead.manual_move([None, None, safe_z], self.z_move_speed)
        toolhead.manual_move([self.probe_x, self.probe_y, None], self.travel_speed)
        toolhead.wait_moves()

    def _select_tool(self, tool_no):
        current = self._get_current_tool()
        if current == tool_no:
            self.gcode.respond_info("T%d is already selected." % tool_no)
            return

        self._lift()
        self.gcode.respond_info("Selecting T%d..." % tool_no)
        self._run_gcode("T%d" % tool_no)

        selected = self._get_current_tool()
        if selected != tool_no:
            self.reactor.pause(self.reactor.monotonic() + 0.10)
            selected = self._get_current_tool()
        if selected != tool_no:
            raise self.gcode.error(
                "Tool change failed: requested T%d, toolchanger reports T%d."
                % (tool_no, selected)
            )
        self.gcode.respond_info("T%d confirmed active." % tool_no)

    # ------------------------------------------------------------------
    # Heater handling
    # ------------------------------------------------------------------

    def _heater_names(self, tools):
        return [self.heater_map[t] for t in tools if t in self.heater_map]

    def _find_heater(self, name):
        # Klipper's heater object registry is the authoritative source.
        heaters = self.printer.lookup_object("heaters", None)
        if heaters is not None:
            try:
                heater = heaters.lookup_heater(name)
                if heater is not None:
                    return heater
            except Exception:
                pass

        # Fallback to direct object lookup for installations exposing heaters.
        return self.printer.lookup_object(name, None)

    def _validate_heaters(self, tools):
        missing = []
        for tool_no in tools:
            name = self.heater_map.get(tool_no)
            if not name or self._find_heater(name) is None:
                missing.append("T%d=%s" % (tool_no, name or "<unset>"))
        if self._find_heater(self.bed_heater_name) is None:
            missing.append("bed=%s" % self.bed_heater_name)
        if missing:
            raise self.gcode.error(
                "Could not find required heater(s): %s" % ", ".join(missing)
            )

    def _set_temperature(self, heater_name, temp, wait=False):
        # SET_HEATER_TEMPERATURE works with both standard Klipper heaters and
        # extruders and does not require us to know their internal classes.
        self._run_gcode(
            "SET_HEATER_TEMPERATURE HEATER=%s TARGET=%.1f"
            % (heater_name, temp)
        )

    def _wait_heater(self, heater_name, target):
        if not self.temperature_wait or target <= 0:
            return
        # TEMPERATURE_WAIT is a Klipper command and waits for the actual heater.
        self._run_gcode(
            "TEMPERATURE_WAIT SENSOR=%s MINIMUM=%.1f"
            % (heater_name, target)
        )

    def _preheat_all(self, tools):
        self._validate_heaters(tools)
        self.gcode.respond_info("========================================")
        self.gcode.respond_info(" PREHEATING FOR CALIBRATION")
        self.gcode.respond_info("========================================")
        self.gcode.respond_info("Bed temperature: %.1f C" % self.bed_calibrate_temperature)
        self.gcode.respond_info(
            "Tool calibration temperature: %.1f C" % self.tool_calibrate_temperature
        )

        # Start every heater first so they heat concurrently.
        self._set_temperature(self.bed_heater_name, self.bed_calibrate_temperature)
        for tool_no in tools:
            self.gcode.respond_info(
                "Setting T%d calibration temperature to %.1fC..."
                % (tool_no, self.tool_calibrate_temperature)
            )
            self._set_temperature(
                self.heater_map[tool_no], self.tool_calibrate_temperature
            )

        # Only then wait. This is substantially faster than heating one tool at a time.
        if self.temperature_wait:
            self.gcode.respond_info("Waiting for bed to stabilize...")
            self._wait_heater(self.bed_heater_name, self.bed_calibrate_temperature)
            for tool_no in tools:
                self.gcode.respond_info("Waiting for T%d to stabilize..." % tool_no)
                self._wait_heater(
                    self.heater_map[tool_no], self.tool_calibrate_temperature
                )

    def _turn_heaters_off(self, tools):
        self.gcode.respond_info("Turning heaters off...")
        names = set([self.bed_heater_name])
        for tool_no in tools:
            if tool_no in self.heater_map:
                names.add(self.heater_map[tool_no])
        for name in names:
            try:
                self._set_temperature(name, 0.0)
            except Exception as e:
                self.gcode.respond_info("Warning: could not turn off %s: %s" % (name, str(e)))

    # ------------------------------------------------------------------
    # Runtime offsets
    # ------------------------------------------------------------------

    def _apply_runtime_offset(self, tool_no, offset):
        # This is deliberately done through the toolchanger command so the
        # value is active immediately and SAVE_CONFIG can persist it later.
        script = (
            "SET_TOOL_PARAMETER T=%d PARAMETER=gcode_z_offset VALUE=%.6f"
            % (tool_no, offset)
        )
        self._run_gcode(script)

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------

    def _calibrate_reference(self):
        self._select_tool(self.reference_tool)
        self._move_to_probe_position()
        self.gcode.respond_info("Running %s..." % self.touch_home_gcode)
        self._run_gcode(self.touch_home_gcode)
        result = self._read_cartographer_result()
        if result is None:
            raise self.gcode.error("Unable to read Cartographer reference result after %s." % self.touch_home_gcode)
        self.last_reference_z = result
        self.gcode.respond_info("T%d is now the ZERO reference." % self.reference_tool)
        self.gcode.respond_info("T%d Cartographer reference: %.6f mm" % (self.reference_tool, result))
        return result

    def _calibrate_tool(self, tool_no, reference_z):
        self._select_tool(tool_no)
        self._move_to_probe_position()
        self.gcode.respond_info("T%d: running %s..." % (tool_no, self.touch_probe_gcode))
        self._run_gcode(self.touch_probe_gcode)
        measured = self._read_cartographer_result()
        if measured is None:
            raise self.gcode.error("T%d did not provide a readable Cartographer Z result." % tool_no)

        delta = measured - reference_z
        if abs(delta) > self.max_offset:
            raise self.gcode.error(
                "T%d produced Z delta %.4f mm, exceeding max_offset %.4f mm."
                % (tool_no, delta, self.max_offset)
            )

        result = {
            "tool": tool_no,
            "measured_z": measured,
            "delta": delta,
            "offset": delta,
        }
        self.gcode.respond_info("T%d contact: %.6f mm" % (tool_no, measured))
        self.gcode.respond_info("T%d delta: %.6f mm" % (tool_no, delta))
        self.gcode.respond_info("T%d suggested gcode_z_offset: %.6f" % (tool_no, delta))
        return result

    def _run_calibration(self, tools):
        self.running = True
        self.last_run_success = False
        self.last_results = {}
        try:
            if not self._is_homed():
                raise self.gcode.error("Printer must be fully homed before Cartographer Z calibration.")
            if self.reference_tool not in tools:
                raise self.gcode.error("Reference T%d is not present." % self.reference_tool)

            # Heat everything before doing any tool measurements.
            self._preheat_all(tools)

            reference_z = self._calibrate_reference()
            self.last_results[self.reference_tool] = {
                "tool": self.reference_tool,
                "measured_z": reference_z,
                "delta": 0.0,
                "offset": 0.0,
            }

            # Measure every non-reference tool.
            for tool_no in tools:
                if tool_no == self.reference_tool:
                    continue
                self.last_results[tool_no] = self._calibrate_tool(tool_no, reference_z)

            # Nothing is applied until every measurement has passed.
            self.gcode.respond_info("")
            self.gcode.respond_info("========================================")
            self.gcode.respond_info(" ALL TOUCH MEASUREMENTS PASSED")
            self.gcode.respond_info("========================================")

            for tool_no in sorted(self.last_results):
                offset = self.last_results[tool_no]["offset"]
                self._apply_runtime_offset(tool_no, offset)
                self.gcode.respond_info(
                    "T%d -> gcode_z_offset = %.*f"
                    % (tool_no, self.offset_decimals, offset)
                )

            self.last_run_success = True
            self.gcode.respond_info("========================================")
            self.gcode.respond_info(" OFFSETS APPLIED TO RUNTIME")
            self.gcode.respond_info("========================================")
            self.gcode.respond_info("Use the normal SAVE_CONFIG / Save Config button to persist them.")
            self.gcode.respond_info("Offsets have NOT been written automatically.")

        finally:
            self._turn_heaters_off(tools)
            self.gcode.respond_info("========================================")
            self.gcode.respond_info("Heaters have been turned off.")
            self.gcode.respond_info("Use SAVE_CONFIG / Save Config to persist them.")
            self.gcode.respond_info("All calibrated offsets are active.")
            self.gcode.respond_info("T%d is the reference at 0.0000." % self.reference_tool)
            self.gcode.respond_info("========================================")
            self.running = False

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def cmd_CALIBRATE(self, gcmd):
        if self.running:
            raise gcmd.error("KTC Cartographer Z calibration is already running.")

        all_tools = self._get_tool_numbers()
        if not all_tools:
            raise gcmd.error("No tools were found in the toolchanger.")

        requested = gcmd.get_int("TOOL", default=-1)
        if requested >= 0:
            if requested == self.reference_tool:
                gcmd.respond_info("T%d is the reference tool; nothing else needs calibrating." % requested)
                return
            if requested not in all_tools:
                raise gcmd.error("T%d is not present in the toolchanger." % requested)
            tools = [self.reference_tool, requested]
        else:
            tools = list(all_tools)

        self.gcode.respond_info("")
        self.gcode.respond_info("========================================")
        self.gcode.respond_info(" KTC CARTOGRAPHER Z CALIBRATION")
        self.gcode.respond_info("========================================")
        self.gcode.respond_info("Reference tool: T%d" % self.reference_tool)
        self.gcode.respond_info("Tools: %s" % ", ".join("T%d" % x for x in tools))
        self.gcode.respond_info("Existing gcode_z_offset values are NOT used in the measurement.")
        self.gcode.respond_info("Bed temperature: %.1f C" % self.bed_calibrate_temperature)
        self.gcode.respond_info("Tool calibration temperature: %.1f C" % self.tool_calibrate_temperature)
        self.gcode.respond_info("")

        try:
            self._run_calibration(tools)
        except Exception:
            self.last_run_success = False
            self.gcode.respond_info("")
            self.gcode.respond_info("========================================")
            self.gcode.respond_info(" NO OFFSETS WERE WRITTEN")
            self.gcode.respond_info(" CALIBRATION ABORTED")
            self.gcode.respond_info("========================================")
            try:
                self._lift()
            except Exception:
                pass
            try:
                self._select_tool(self.reference_tool)
            except Exception:
                pass
            raise

        try:
            self._lift()
            self._select_tool(self.reference_tool)
        except Exception as e:
            self.gcode.respond_info(
                "Warning: calibration succeeded but could not return to T%d: %s"
                % (self.reference_tool, str(e))
            )

        self.gcode.respond_info("")
        self.gcode.respond_info("========================================")
        self.gcode.respond_info(" CARTOGRAPHER Z CALIBRATION COMPLETE")
        self.gcode.respond_info("========================================")

    def cmd_STATUS(self, gcmd):
        gcmd.respond_info("========================================")
        gcmd.respond_info(" KTC CARTOGRAPHER Z STATUS")
        gcmd.respond_info("========================================")
        if self.last_reference_z is None:
            gcmd.respond_info("No calibration reference available.")
        else:
            gcmd.respond_info("Reference T%d raw result: %.6f" % (self.reference_tool, self.last_reference_z))
        gcmd.respond_info("Reference tool: T%d" % self.reference_tool)
        gcmd.respond_info("Last run: %s" % ("SUCCESS" if self.last_run_success else "FAILED / NONE"))
        gcmd.respond_info("Running: %s" % ("YES" if self.running else "NO"))
        for tool_no in sorted(self.last_results):
            r = self.last_results[tool_no]
            gcmd.respond_info(
                "T%d: contact=%.6f delta=%+.6f offset=%+.6f"
                % (tool_no, r["measured_z"], r["delta"], r["offset"])
            )
        gcmd.respond_info("========================================")

    def get_status(self, eventtime):
        return {
            "running": self.running,
            "success": self.last_run_success,
            "reference_tool": self.reference_tool,
            "reference_z": self.last_reference_z,
            "results": {str(k): dict(v) for k, v in self.last_results.items()},
        }


def load_config(config):
    return KTCCartographerZCalibrate(config)
