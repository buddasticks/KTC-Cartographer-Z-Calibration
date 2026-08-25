# KTC Cartographer Z Calibration
#
# Automatic multi-tool Z offset calibration using Cartographer Touch.
#
# Workflow:
#   G28
#   KTC_CARTOGRAPHER_Z_CALIBRATE
#
#   T0                    -> initializes toolchanger + loads Cartographer model
#   Heat bed + all tools  -> in parallel
#   CARTOGRAPHER_TOUCH_HOME
#   T1 -> touch
#   T2 -> touch
#   T3 -> touch
#   Apply runtime gcode_z_offset values
#   Return T0
#   Turn calibration heaters OFF
#   Leave SAVE_CONFIG pending
#
# GPLv3

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

        self.touch_home_gcode = config.get(
            "touch_home_gcode",
            "CARTOGRAPHER_TOUCH_HOME"
        )

        self.touch_probe_gcode = config.get(
            "touch_probe_gcode",
            "CARTOGRAPHER_TOUCH_PROBE"
        )

        # Nozzle wipe macro, run before every probe (reference tool
        # included). Empty string disables wiping.
        self.wipe_gcode = config.get(
            "wipe_gcode",
            ""
        )

        self.t0_gcode = config.get("t0_gcode", "T0")

        self.max_offset = config.getfloat(
            "max_offset",
            3.0,
            above=0.0
        )

        self.offset_decimals = config.getint(
            "offset_decimals",
            4,
            minval=3,
            maxval=6
        )

        self.tool_calibrate_temperature = config.getfloat(
            "tool_calibrate_temperature",
            145.0,
            above=0.0
        )

        self.bed_calibrate_temperature = config.getfloat(
            "bed_calibrate_temperature",
            60.0,
            above=0.0
        )

        self.bed_heater = config.get(
            "bed_heater",
            "heater_bed"
        )

        self.tool_heaters = {}

        for tool in range(4):
            self.tool_heaters[tool] = config.get(
                "tool%d_heater" % tool,
                self._default_heater(tool)
            )

        self.apply_offset_gcode = config.get(
            "apply_offset_gcode",
            "SET_TOOL_PARAMETER "
            "T={tool} "
            "PARAMETER=gcode_z_offset "
            "VALUE={offset}"
        )

        # NEW: SET_TOOL_PARAMETER only sets the runtime value -- it does
        # not register anything with configfile, so SAVE_CONFIG had
        # nothing to write. persist_offsets additionally calls
        # configfile.set() for each tool so SAVE_CONFIG appends/updates
        # the value in printer.cfg's auto-generated bottom section.
        self.persist_offsets = config.getboolean(
            "persist_offsets",
            True
        )

        # {tool} is substituted with the tool number. Must match the
        # actual config section header for each tool, e.g. [tool T0].
        self.config_section = config.get(
            "config_section",
            "tool T{tool}"
        )

        self.config_option = config.get(
            "config_option",
            "gcode_z_offset"
        )

        # How long to poll for the toolchanger to confirm an actual
        # tool change before giving up. Only used when a real reselect
        # was issued (see _select_tool).
        self.select_tool_timeout = config.getfloat(
            "select_tool_timeout",
            2.0,
            above=0.0
        )

        self.last_reference_z = None
        self.last_results = {}
        self.last_run_success = False
        self.running = False
        self.heaters_started = False

        self.gcode.register_command(
            "KTC_CARTOGRAPHER_Z_CALIBRATE",
            self.cmd_CALIBRATE,
            desc="Calibrate tool Z offsets using Cartographer Touch"
        )

        self.gcode.register_command(
            "KTC_CARTOGRAPHER_Z_STATUS",
            self.cmd_STATUS,
            desc="Show Cartographer Z calibration status"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _default_heater(tool):
        if tool == 0:
            return "extruder"
        return "extruder%d" % tool

    def _tool_numbers(self):
        tc = self.printer.lookup_object("toolchanger", None)

        if tc is None:
            raise self.gcode.error(
                "KTC Cartographer Z Calibration requires [toolchanger]."
            )

        numbers = getattr(tc, "tool_numbers", None)

        if numbers:
            try:
                return sorted(
                    set(int(x) for x in numbers if int(x) >= 0)
                )
            except Exception:
                pass

        result = []

        for name in self.printer.lookup_objects():
            if not name.startswith("tool T"):
                continue

            try:
                obj = self.printer.lookup_object(name)
                number = int(obj.tool_number)

                if number >= 0:
                    result.append(number)

            except Exception:
                pass

        if result:
            return sorted(set(result))

        return [0, 1, 2, 3]

    def _toolhead(self):
        return self.printer.lookup_object("toolhead")

    def _cartographer(self):
        obj = self.printer.lookup_object("cartographer", None)

        if obj is None:
            raise self.gcode.error(
                "Cartographer is not loaded."
            )

        return obj

    def _current_tool(self):
        tc = self.printer.lookup_object("toolchanger", None)

        if tc is None:
            return -1

        # FIX: prefer get_status(). Confirmed via the object/status
        # panel that "tool_number" is reliably populated there, but
        # plain getattr(tc, "tool_number") was returning stale/None
        # values, which made the "already active" check below always
        # fail and re-issue the T-macro even when a tool was already
        # selected -- and that redundant reselect is what corrupts
        # this toolchanger back to "uninitialized" / tool_number -1.
        try:
            status = tc.get_status(self.reactor.monotonic())

            if isinstance(status, dict):
                value = status.get("tool_number")

                if value is not None:
                    value = int(value)

                    if value >= 0:
                        return value

        except Exception:
            pass

        for attr in (
            "tool_number",
            "active_tool",
            "current_tool"
        ):
            value = getattr(tc, attr, None)

            if value is None:
                continue

            try:
                value = int(value)

                if value >= 0:
                    return value

            except Exception:
                pass

        return -1

    # ------------------------------------------------------------------
    # Toolchanger initialization
    # ------------------------------------------------------------------

    def _initialize_toolchanger(self):
        """
        T0 is deliberately called first.

        The T0 macro performs:
            SELECT_TOOL T=0
            CARTO_TOUCH_MODEL ACTION=load NAME=t0

        If the toolchanger is uninitialized after G28, SELECT_TOOL can fail.
        Therefore initialise from the physically detected tool first.
        """

        tc = self.printer.lookup_object("toolchanger", None)

        if tc is None:
            raise self.gcode.error(
                "Toolchanger object not found."
            )

        initialized = getattr(tc, "initialized", None)

        if initialized is True:
            return

        status = None

        try:
            status = tc.get_status(
                self.reactor.monotonic()
            )
        except Exception:
            pass

        if isinstance(status, dict):
            if status.get("initialized") is True:
                return

        self.gcode.respond_info(
            "Initializing toolchanger from detected tool..."
        )

        self.gcode.run_script_from_command(
            "_INITIALIZE_FROM_DETECTED_TOOL"
        )

        self._toolhead().wait_moves()

        self.reactor.pause(
            self.reactor.monotonic() + 0.05
        )

    def _select_tool(self, tool):
        # FIX: if this tool is already the active tool, do NOT re-issue
        # the T-macro. On this toolchanger, re-selecting an already-
        # active tool takes an "already selected" shortcut path that
        # does not fully re-run the pickup/selection sequence, which
        # was observed to leave the reported active tool at -1
        # afterwards (see log: "Tool tool T0 already selected" followed
        # by "active tool is T-1"). Skipping the redundant reselect
        # avoids that broken path entirely and is a no-op otherwise.
        if self._current_tool() == tool:
            self.gcode.respond_info(
                "T%d already active, skipping reselect." % tool
            )
            return

        self.gcode.respond_info(
            "Selecting T%d..." % tool
        )

        self._lift()

        self.gcode.run_script_from_command(
            "T%d" % tool
        )

        self._toolhead().wait_moves()

        # FIX: poll for the toolchanger to confirm the new active tool
        # instead of relying on a single fixed 0.05s pause, since state
        # propagation timing can vary between toolchanges.
        selected = -1
        deadline = self.reactor.monotonic() + self.select_tool_timeout

        while self.reactor.monotonic() < deadline:
            self.reactor.pause(
                self.reactor.monotonic() + 0.1
            )

            selected = self._current_tool()

            if selected == tool:
                break

        if selected != tool:
            raise self.gcode.error(
                "T%d selection failed; active tool is T%d."
                % (tool, selected)
            )

    # ------------------------------------------------------------------
    # Motion
    # ------------------------------------------------------------------

    def _is_homed(self):
        try:
            status = self._toolhead().get_status(
                self.reactor.monotonic()
            )

            homed = status.get("homed_axes", "")

            return (
                "x" in homed
                and "y" in homed
                and "z" in homed
            )

        except Exception:
            return False

    def _lift(self):
        toolhead = self._toolhead()
        position = toolhead.get_position()

        if position[2] < self.lift_z:
            toolhead.manual_move(
                [None, None, self.lift_z],
                self.z_move_speed
            )

        toolhead.wait_moves()

    def _move_to_probe_position(self):
        if self.probe_x is None or self.probe_y is None:
            return

        toolhead = self._toolhead()
        position = toolhead.get_position()

        safe_z = max(
            float(position[2]),
            self.lift_z
        )

        toolhead.manual_move(
            [None, None, safe_z],
            self.z_move_speed
        )

        toolhead.manual_move(
            [self.probe_x, self.probe_y, None],
            self.travel_speed
        )

        toolhead.wait_moves()

    def _wipe(self):
        if not self.wipe_gcode:
            return

        self.gcode.respond_info(
            "Wiping nozzle..."
        )

        self.gcode.run_script_from_command(
            self.wipe_gcode
        )

        self._toolhead().wait_moves()

    # ------------------------------------------------------------------
    # Cartographer
    # ------------------------------------------------------------------

    def _cartographer_result(self):
        cartographer = self._cartographer()
        now = self.reactor.monotonic()

        try:
            status = cartographer.get_status(now)

            if isinstance(status, dict):

                touch = status.get("touch")

                if isinstance(touch, dict):
                    value = touch.get("last_z_result")

                    if value is not None:
                        return float(value)

                value = status.get("last_z_result")

                if value is not None:
                    return float(value)

        except Exception:
            pass

        value = getattr(
            cartographer,
            "last_z_result",
            None
        )

        if value is not None:
            try:
                return float(value)
            except Exception:
                pass

        return None

    def _touch(self):
        self.gcode.run_script_from_command(
            self.touch_probe_gcode
        )

        self._toolhead().wait_moves()

        result = self._cartographer_result()

        if result is None:
            raise self.gcode.error(
                "Cartographer did not return a Z result."
            )

        return result

    # ------------------------------------------------------------------
    # Heaters
    # ------------------------------------------------------------------

    def _available_heaters(self):
        heaters = self.printer.lookup_object(
            "heaters",
            None
        )

        if heaters is None:
            return []

        try:
            return list(
                heaters.get_all_heaters()
            )
        except Exception:
            return []

    def _heater_list(self, tools):
        heaters = [self.bed_heater]

        for tool in tools:
            heater = self.tool_heaters[tool]

            if heater not in heaters:
                heaters.append(heater)

        return heaters

    def _check_heaters(self, tools):
        available = self._available_heaters()
        required = self._heater_list(tools)

        missing = [
            heater
            for heater in required
            if heater not in available
        ]

        if missing:
            raise self.gcode.error(
                "Calibration heater(s) not found: %s. "
                "Available heaters: %s"
                % (
                    ", ".join(missing),
                    ", ".join(available)
                )
            )

    def _set_temperature(self, heater, target):
        self.gcode.run_script_from_command(
            "SET_HEATER_TEMPERATURE "
            "HEATER=%s TARGET=%.1f"
            % (heater, target)
        )

    def _wait_temperature(self, heater, target):
        self.gcode.run_script_from_command(
            "TEMPERATURE_WAIT "
            "SENSOR=%s MINIMUM=%.1f"
            % (heater, target)
        )

    def _preheat(self, tools):
        self._check_heaters(tools)

        self.gcode.respond_info("")
        self.gcode.respond_info(
            "========================================"
        )
        self.gcode.respond_info(
            "PREHEATING FOR CALIBRATION"
        )
        self.gcode.respond_info(
            "========================================"
        )

        self.gcode.respond_info(
            "Bed temperature: %.1f C"
            % self.bed_calibrate_temperature
        )

        self.gcode.respond_info(
            "Tool calibration temperature: %.1f C"
            % self.tool_calibrate_temperature
        )

        # Start EVERYTHING before waiting for anything.
        self._set_temperature(
            self.bed_heater,
            self.bed_calibrate_temperature
        )

        for tool in tools:
            heater = self.tool_heaters[tool]

            self.gcode.respond_info(
                "Setting T%d calibration temperature to %.1fC..."
                % (
                    tool,
                    self.tool_calibrate_temperature
                )
            )

            self._set_temperature(
                heater,
                self.tool_calibrate_temperature
            )

        self.heaters_started = True

        # Wait for all tools.
        for tool in tools:
            heater = self.tool_heaters[tool]

            self.gcode.respond_info(
                "Waiting for T%d to stabilize..."
                % tool
            )

            self._wait_temperature(
                heater,
                self.tool_calibrate_temperature
            )

        self.gcode.respond_info(
            "Waiting for bed to stabilize..."
        )

        self._wait_temperature(
            self.bed_heater,
            self.bed_calibrate_temperature
        )

    def _heaters_off(self, tools):
        if not self.heaters_started:
            return

        self.gcode.respond_info(
            "Turning heaters off..."
        )

        for heater in self._heater_list(tools):
            try:
                self._set_temperature(
                    heater,
                    0.0
                )
            except Exception as e:
                self.gcode.respond_info(
                    "Warning: could not turn off %s: %s"
                    % (heater, e)
                )

        self.heaters_started = False

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------

    def _reference(self):
        self._select_tool(
            self.reference_tool
        )

        self._wipe()

        self._move_to_probe_position()

        self.gcode.respond_info(
            "Running %s..."
            % self.touch_home_gcode
        )

        self.gcode.run_script_from_command(
            self.touch_home_gcode
        )

        self._toolhead().wait_moves()

        result = self._cartographer_result()

        if result is None:
            raise self.gcode.error(
                "Unable to read Cartographer reference."
            )

        self.last_reference_z = result

        self.gcode.respond_info(
            "T%d Cartographer reference: %.6f mm"
            % (
                self.reference_tool,
                result
            )
        )

        return result

    def _calibrate_tool(self, tool, reference):
        self._select_tool(tool)

        self._wipe()

        self._move_to_probe_position()

        self.gcode.respond_info(
            "Touching T%d..."
            % tool
        )

        measured = self._touch()

        offset = measured - reference

        if abs(offset) > self.max_offset:
            raise self.gcode.error(
                "T%d offset %.4f exceeds max_offset %.4f."
                % (
                    tool,
                    offset,
                    self.max_offset
                )
            )

        self.gcode.respond_info(
            "T%d contact: %.6f"
            % (tool, measured)
        )

        self.gcode.respond_info(
            "T%d delta: %.6f"
            % (tool, offset)
        )

        self.gcode.respond_info(
            "T%d suggested gcode_z_offset: %.6f"
            % (tool, offset)
        )

        return {
            "tool": tool,
            "measured_z": measured,
            "delta": offset,
            "offset": offset
        }

    def _apply_offset(self, tool, offset):
        value = (
            "%.*f"
            % (
                self.offset_decimals,
                offset
            )
        )

        command = self.apply_offset_gcode.format(
            tool=tool,
            offset=value
        )

        self.gcode.run_script_from_command(
            command
        )

        self.gcode.respond_info(
            "T%d runtime gcode_z_offset = %s"
            % (tool, value)
        )

        self._persist_offset(tool, value)

    def _persist_offset(self, tool, value):
        """
        Register the offset with configfile so SAVE_CONFIG writes it
        into printer.cfg's auto-generated bottom section. This does
        NOT write the file itself -- it only queues the value; the
        user (or a macro) still has to run SAVE_CONFIG / press
        Save Config to actually commit and restart.
        """

        if not self.persist_offsets:
            return

        configfile = self.printer.lookup_object(
            "configfile",
            None
        )

        if configfile is None:
            self.gcode.respond_info(
                "Warning: configfile object not found, "
                "T%d offset was not queued for SAVE_CONFIG."
                % tool
            )
            return

        section = self.config_section.format(tool=tool)

        try:
            configfile.set(
                section,
                self.config_option,
                value
            )
        except Exception as e:
            self.gcode.respond_info(
                "Warning: could not queue T%d offset for "
                "SAVE_CONFIG (section [%s]): %s"
                % (tool, section, e)
            )

    # ------------------------------------------------------------------
    # Main calibration
    # ------------------------------------------------------------------

    def _run(self, tools):
        self.running = True
        self.last_run_success = False
        self.last_results = {}
        self.heaters_started = False

        try:
            if not self._is_homed():
                raise self.gcode.error(
                    "Printer must be fully homed before calibration."
                )

            # IMPORTANT:
            # G28 does not initialise KTC-Easy's software state.
            #
            # This must happen BEFORE T0 is called.
            self._initialize_toolchanger()

            # T0 handles:
            #   SELECT_TOOL T=0
            #   CARTO_TOUCH_MODEL ACTION=load NAME=t0
            #
            # This gives Cartographer the correct model before touching.
            self.gcode.respond_info(
                "Calling T0 to initialise reference tool and "
                "load Cartographer model..."
            )

            self.gcode.run_script_from_command(
                self.t0_gcode
            )

            self._toolhead().wait_moves()

            # Heat all heaters together.
            self._preheat(tools)

            # Reference measurement.
            reference = self._reference()

            self.last_results[
                self.reference_tool
            ] = {
                "tool": self.reference_tool,
                "measured_z": reference,
                "delta": 0.0,
                "offset": 0.0
            }

            # Measure every other tool.
            for tool in tools:
                if tool == self.reference_tool:
                    continue

                self.last_results[tool] = (
                    self._calibrate_tool(
                        tool,
                        reference
                    )
                )

            # Do not apply anything until ALL tools passed.
            self.gcode.respond_info("")
            self.gcode.respond_info(
                "========================================"
            )
            self.gcode.respond_info(
                "ALL TOUCH MEASUREMENTS PASSED"
            )
            self.gcode.respond_info(
                "========================================"
            )

            for tool in sorted(self.last_results):
                offset = self.last_results[
                    tool
                ]["offset"]

                self.gcode.respond_info(
                    "T%d -> gcode_z_offset = %.*f"
                    % (
                        tool,
                        self.offset_decimals,
                        offset
                    )
                )

            for tool in sorted(self.last_results):
                self._apply_offset(
                    tool,
                    self.last_results[tool]["offset"]
                )

            self.last_run_success = True

        finally:
            self._heaters_off(tools)
            self.running = False

    # ------------------------------------------------------------------
    # G-code commands
    # ------------------------------------------------------------------

    def cmd_CALIBRATE(self, gcmd):
        if self.running:
            raise gcmd.error(
                "Calibration is already running."
            )

        tools = self._tool_numbers()

        if self.reference_tool not in tools:
            raise gcmd.error(
                "Reference T%d not found."
                % self.reference_tool
            )

        gcmd.respond_info("")
        gcmd.respond_info(
            "========================================"
        )
        gcmd.respond_info(
            "KTC_CARTOGRAPHER_Z_CALIBRATE"
        )
        gcmd.respond_info(
            "========================================"
        )
        gcmd.respond_info(
            "Tools: %s"
            % ", ".join(
                "T%d" % x
                for x in tools
            )
        )
        gcmd.respond_info(
            "Reference tool: T%d"
            % self.reference_tool
        )
        gcmd.respond_info(
            "Existing gcode_z_offset values are "
            "NOT used in the measurement."
        )
        gcmd.respond_info(
            "Bed temperature: %.1f C"
            % self.bed_calibrate_temperature
        )
        gcmd.respond_info(
            "Tool calibration temperature: %.1f C"
            % self.tool_calibrate_temperature
        )

        try:
            self._run(tools)

        except Exception:
            self.last_run_success = False

            gcmd.respond_info("")
            gcmd.respond_info(
                "========================================"
            )
            gcmd.respond_info(
                "CALIBRATION ABORTED"
            )
            gcmd.respond_info(
                "NO OFFSETS WERE WRITTEN"
            )
            gcmd.respond_info(
                "========================================"
            )

            raise

        # Return to T0 before finishing.
        try:
            self._lift()

            current = self._current_tool()

            if current != self.reference_tool:
                self._select_tool(
                    self.reference_tool
                )

        except Exception as e:
            gcmd.respond_info(
                "Warning: could not return to T%d: %s"
                % (
                    self.reference_tool,
                    e
                )
            )

        gcmd.respond_info("")
        gcmd.respond_info(
            "========================================"
        )
        gcmd.respond_info(
            "CARTOGRAPHER Z CALIBRATION COMPLETE"
        )
        gcmd.respond_info(
            "========================================"
        )
        gcmd.respond_info(
            "T%d is the reference at 0.0000."
            % self.reference_tool
        )
        gcmd.respond_info(
            "All calibrated offsets are active."
        )

        if self.persist_offsets:
            gcmd.respond_info(
                "Offsets queued for SAVE_CONFIG -- press Save Config "
                "in Mainsail/Fluidd to write them to printer.cfg "
                "(this will restart the firmware)."
            )
        else:
            gcmd.respond_info(
                "persist_offsets is disabled; offsets are runtime-only."
            )
        gcmd.respond_info(
            "Heaters have been turned off."
        )
        gcmd.respond_info(
            "========================================"
        )

    def cmd_STATUS(self, gcmd):
        gcmd.respond_info(
            "========================================"
        )
        gcmd.respond_info(
            "KTC CARTOGRAPHER Z STATUS"
        )
        gcmd.respond_info(
            "========================================"
        )

        if self.last_reference_z is None:
            gcmd.respond_info(
                "No calibration reference available."
            )
        else:
            gcmd.respond_info(
                "T%d reference: %.6f"
                % (
                    self.reference_tool,
                    self.last_reference_z
                )
            )

        gcmd.respond_info(
            "Last run: %s"
            % (
                "SUCCESS"
                if self.last_run_success
                else "FAILED / NONE"
            )
        )

        gcmd.respond_info(
            "Running: %s"
            % (
                "YES"
                if self.running
                else "NO"
            )
        )

        for tool in sorted(self.last_results):
            result = self.last_results[tool]

            gcmd.respond_info(
                "T%d: contact=%.6f "
                "delta=%+.6f "
                "offset=%+.6f"
                % (
                    tool,
                    result["measured_z"],
                    result["delta"],
                    result["offset"]
                )
            )

        gcmd.respond_info(
            "========================================"
        )

    def get_status(self, eventtime):
        return {
            "running": self.running,
            "success": self.last_run_success,
            "reference_tool": self.reference_tool,
            "reference_z": self.last_reference_z,
            "results": {
                str(tool): dict(result)
                for tool, result in self.last_results.items()
            }
        }


def load_config(config):
    return KTCCartographerZCalibrate(config)
