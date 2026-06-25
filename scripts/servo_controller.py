"""
Servo Controller Module - FIXED DEBUG CONTROL
Handles servo tracking functionality for face tracking using Robot HAT.
ENHANCED: Added automatic scanning behavior when no face is detected.
"""

import time
import threading

# Import debug utilities
try:
    from scripts.config_manager import should_debug, debug_print, conditional_print
except ImportError:
    # Fallback if debug_utils not available
    def should_debug(config):
        return config.get('ui', {}).get('enable_debug_output', False)

    def debug_print(config, message):
        if should_debug(config):
            print(message)

    def conditional_print(config, message, always_print=False):
        if always_print or should_debug(config):
            print(message)

from scripts.logger import get_logger
_srvlog = get_logger('servo')

try:
    from robot_hat import Servo
    ROBOT_HAT_AVAILABLE = True
except ImportError:
    ROBOT_HAT_AVAILABLE = False
    print("Warning: robot_hat library not available. Servo tracking disabled.")


# # p3_servo_cleaned
class ServoController:
    """Manages servo-based face tracking system with automatic scanning - FIXED DEBUG"""

    def __init__(self, config):
        """Initialize servo controller with configuration"""
        self.config = config
        self.servo_config = config.get('servo_tracking', {})

        # Servo settings
        self.pan_channel = self.servo_config.get('pan_channel', 'P0')
        self.tilt_channel = self.servo_config.get('tilt_channel', 'P1')
        self.enable_tracking = self.servo_config.get('enable_tracking', True)

        # Initial movement limits (will be calibrated)
        self.pan_min = self.servo_config.get('pan_min', -90)
        self.pan_max = self.servo_config.get('pan_max', 90)
        self.tilt_min = self.servo_config.get('tilt_min', -30)
        self.tilt_max = self.servo_config.get('tilt_max', 30)

        # Actual calibrated limits (detected during calibration)
        self.actual_pan_min = self.servo_config.get('calibrated_pan_min', self.pan_min)
        self.actual_pan_max = self.servo_config.get('calibrated_pan_max', self.pan_max)
        self.actual_tilt_min = self.servo_config.get('calibrated_tilt_min', self.tilt_min)
        self.actual_tilt_max = self.servo_config.get('calibrated_tilt_max', self.tilt_max)

        self.pan_speed = self.servo_config.get('pan_speed', 5.0)
        self.tilt_speed = self.servo_config.get('tilt_speed', 4.0)

        # Calibration settings
        self.auto_calibrate = self.servo_config.get('auto_calibrate', True)
        self.calibration_done = self.servo_config.get('calibration_completed', False)
        self.obstacle_detection_enabled = self.servo_config.get('obstacle_detection', True)
        self.movement_threshold = self.servo_config.get('movement_threshold', 1.0)
        self.stall_detection_time = self.servo_config.get('stall_detection_time', 2.0)

        # Tracking parameters
        self.center_tolerance = self.servo_config.get('center_tolerance', 30)
        self.tracking_sensitivity = self.servo_config.get('tracking_sensitivity', 2.0)
        self.smoothing_factor = self.servo_config.get('smoothing_factor', 0.6)
        self.tracking_delay = self.servo_config.get('tracking_delay', 0.05)
        self.min_movement = 0.5
        self.max_step_degrees = self.servo_config.get('max_step_degrees', 8.0)

        # Axis inversion options
        self.invert_pan = self.servo_config.get('invert_pan', False)
        self.invert_tilt = self.servo_config.get('invert_tilt', False)

        # Default starting position offsets
        self.default_pan_offset = self.servo_config.get('default_pan_offset', 0)
        self.default_tilt_offset = self.servo_config.get('default_tilt_offset', 20)

        # Current positions
        self.current_pan = 0
        self.current_tilt = 0
        self.target_pan = 0
        self.target_tilt = 0

        # SERVO_MANUAL_CALIB_V1 — when True the tracking loop is suspended so the
        # interactive calibration wizard (key 'K' in preview) can drive the servos
        # by hand. Absolute jog bounds keep the servo off its mechanical stops.
        self.calibrating = False
        self.CAL_PAN_LIMIT = float(self.servo_config.get('cal_pan_limit', 85.0))
        self.CAL_TILT_LIMIT = float(self.servo_config.get('cal_tilt_limit', 45.0))

        # Tracking state
        self.tracking_active = False
        self.last_face_time = time.time()  # prevents instant scan-mode on boot
        self.face_lost_timeout = self.servo_config.get('face_lost_timeout', 3.0)
        self.tracking_thread = None
        self.running = False
        self.calibrated = self.calibration_done

        # Face tracking data
        self.current_face_center = None
        self.image_center = (640, 480)  # updated by update_image_dimensions() at runtime
        self.lock = threading.Lock()

        # Smoothed target — exponential moving average applied to the pixel
        # coordinates before they reach the servo PID. Eliminates jitter when
        # the camera switches between two people or recognition flickers.
        self._smooth_cx = None
        self._smooth_cy = None
        self._target_smooth = 0.25   # 0=frozen, 1=instant (lower=smoother)

        # Scanning behavior settings
        self.scanning_enabled = self.servo_config.get('scanning_enabled', True)
        self.scanning_active = False
        self.scanning_speed = self.servo_config.get('scanning_speed', 15.0)  # degrees per second
        self.scanning_pause_time = self.servo_config.get('scanning_pause_time', 0.5)  # seconds at each end
        self.scanning_delay = self.servo_config.get('scanning_delay', 0.1)  # update interval during scan

        # Scanning range limits (independent of full calibrated range)
        self.scanning_pan_min = self.servo_config.get('scanning_pan_min', -45.0)  # Limited scanning range
        self.scanning_pan_max = self.servo_config.get('scanning_pan_max', 45.0)   # Limited scanning range

        # Scanning state variables
        self.scan_direction = 1  # 1 for right, -1 for left
        self.scan_target_pan = 0
        self.scan_pause_start = 0
        self.scan_pausing = False
        self.last_scan_update = 0

        # Initialize servos
        self.pan_servo = None
        self.tilt_servo = None

        if ROBOT_HAT_AVAILABLE and self.enable_tracking:
            self.initialize_servos()
        else:
            conditional_print(self.config, "Servo tracking disabled or Robot HAT not available", always_print=True)

        conditional_print(self.config, f"Servo Controller initialized", always_print=True)
        if self.scanning_enabled:
            conditional_print(self.config, "Automatic scanning enabled - camera will search for faces when none detected", always_print=True)
        if self.calibrated:
            conditional_print(self.config, f"Using stored calibration - Pan: {self.actual_pan_min:.1f} to {self.actual_pan_max:.1f}", always_print=True)
            conditional_print(self.config, f"Using stored calibration - Tilt: {self.actual_tilt_min:.1f} to {self.actual_tilt_max:.1f}", always_print=True)

    def initialize_servos(self):
        """Initialize servo hardware with calibration"""
        try:
            # Reset the Robot HAT MCU before initialising servos.
            # This is required by SunFounder's robot_hat — without it the MCU
            # may not be ready and Servo() will fail or commands will be silently ignored.
            try:
                from robot_hat import utils
                utils.reset_mcu()
                time.sleep(0.2)
                print("[Servo] MCU reset OK")
            except Exception as e:
                print(f"[Servo] MCU reset skipped: {e}")

            self.pan_servo  = Servo(self.pan_channel)
            self.tilt_servo = Servo(self.tilt_channel)

            conditional_print(self.config, "Servo hardware initialized", always_print=True)

            # Check if calibration already done
            if self.calibrated:
                conditional_print(self.config, "Using stored calibration data - skipping calibration", always_print=True)
            else:
                conditional_print(self.config, "Starting automatic calibration and centering...", always_print=True)
                # Auto-calibrate if enabled and not done before
                if self.auto_calibrate:
                    success = self.auto_calibrate_limits()
                    if success:
                        conditional_print(self.config, "Auto-calibration completed successfully", always_print=True)
                        self.calibrated = True
                        # Save calibration to config
                        self.save_calibration_to_config()
                    else:
                        conditional_print(self.config, "Auto-calibration failed, using configured limits", always_print=True)

            # Move to center position
            self.move_to_center()
            time.sleep(1)  # Allow servos to reach position

            conditional_print(self.config, "Servos initialized and centered", always_print=True)
            return True

        except Exception as e:
            conditional_print(self.config, f"Servo initialization failed: {e}", always_print=True)
            self.pan_servo = None
            self.tilt_servo = None
            return False
    def get_servo_feedback_position(self, servo):
        """Get current servo position (estimated since most servos don't have feedback)"""
        if servo == self.pan_servo:
            return self.current_pan
        elif servo == self.tilt_servo:
            return self.current_tilt
        return 0
    def detect_movement_stall(self, servo, target_angle, max_wait_time=4.0):
        """
        Move servo toward target_angle and detect if it stalls before reaching it.
        Returns (stalled: bool, reached_angle: float).
        Since servos have no position feedback, we use the configured limit as
        the reached angle and treat hitting the configured boundary as a stall.
        """
        if not servo:
            return True, target_angle
        try:
            servo.angle(target_angle)
            import time as _t
            _t.sleep(max_wait_time * 0.5)
            return False, target_angle
        except Exception as e:
            debug_print(self.config, f"detect_movement_stall error: {e}")
            return True, target_angle

    def auto_calibrate_limits(self):
        """Automatically detect actual movement limits with obstacle detection"""
        if not self.pan_servo or not self.tilt_servo:
            return False

        conditional_print(self.config, "Starting automatic limit detection...", always_print=True)

        try:
            # Start from center
            debug_print(self.config, "Moving to approximate center...")
            self.pan_servo.angle(0)
            self.tilt_servo.angle(0)
            time.sleep(2)

            # Calibrate PAN limits
            debug_print(self.config, "Detecting PAN movement limits...")

            # Test pan left (negative direction)
            debug_print(self.config, "  Testing pan left limit...")
            stalled, actual_min = self.detect_movement_stall(
                self.pan_servo, self.pan_min - 10, max_wait_time=4.0
            )
            if stalled:
                self.actual_pan_min = self.pan_min
                debug_print(self.config, f"    Pan left blocked at configured limit: {self.pan_min}")
            else:
                self.actual_pan_min = max(actual_min + 5, self.pan_min)  # Add safety margin
                debug_print(self.config, f"    Pan left limit detected: {self.actual_pan_min}")

            # Return to center
            self.pan_servo.angle(0)
            time.sleep(1)

            # Test pan right (positive direction)
            debug_print(self.config, "  Testing pan right limit...")
            stalled, actual_max = self.detect_movement_stall(
                self.pan_servo, self.pan_max + 10, max_wait_time=4.0
            )
            if stalled:
                self.actual_pan_max = self.pan_max
                debug_print(self.config, f"    Pan right blocked at configured limit: {self.pan_max}")
            else:
                self.actual_pan_max = min(actual_max - 5, self.pan_max)  # Add safety margin
                debug_print(self.config, f"    Pan right limit detected: {self.actual_pan_max}")

            # Return to center
            self.pan_servo.angle(0)
            time.sleep(1)

            # Calibrate TILT limits
            debug_print(self.config, "Detecting TILT movement limits...")

            # Test tilt down (negative direction)
            debug_print(self.config, "  Testing tilt down limit...")
            stalled, actual_min = self.detect_movement_stall(
                self.tilt_servo, self.tilt_min - 10, max_wait_time=4.0
            )
            if stalled:
                self.actual_tilt_min = self.tilt_min
                debug_print(self.config, f"    Tilt down blocked at configured limit: {self.tilt_min}")
            else:
                self.actual_tilt_min = max(actual_min + 3, self.tilt_min)  # Add safety margin
                debug_print(self.config, f"    Tilt down limit detected: {self.actual_tilt_min}")

            # Return to center
            self.tilt_servo.angle(0)
            time.sleep(1)

            # Test tilt up (positive direction)
            debug_print(self.config, "  Testing tilt up limit...")
            stalled, actual_max = self.detect_movement_stall(
                self.tilt_servo, self.tilt_max + 10, max_wait_time=4.0
            )
            if stalled:
                self.actual_tilt_max = self.tilt_max
                debug_print(self.config, f"    Tilt up blocked at configured limit: {self.tilt_max}")
            else:
                self.actual_tilt_max = min(actual_max - 3, self.tilt_max)  # Add safety margin
                debug_print(self.config, f"    Tilt up limit detected: {self.actual_tilt_max}")

            # Summary
            conditional_print(self.config, "Auto-calibration results:", always_print=True)
            conditional_print(self.config, f"  Pan range: {self.actual_pan_min:.1f}° to {self.actual_pan_max:.1f}°", always_print=True)
            conditional_print(self.config, f"  Tilt range: {self.actual_tilt_min:.1f}° to {self.actual_tilt_max:.1f}°", always_print=True)

            return True

        except Exception as e:
            debug_print(self.config, f"Auto-calibration error: {e}")
            # Use configured limits as fallback
            self.actual_pan_min = self.pan_min
            self.actual_pan_max = self.pan_max
            self.actual_tilt_min = self.tilt_min
            self.actual_tilt_max = self.tilt_max
            return False

    def start_tracking(self):
        """Start the servo tracking thread"""
        if not ROBOT_HAT_AVAILABLE:
            print("[Servo] robot_hat not available — tracking disabled")
            return False
        if not self.enable_tracking:
            print("[Servo] tracking disabled in config")
            return False
        if not self.pan_servo or not self.tilt_servo:
            print("[Servo] servos not initialized — tracking disabled")
            return False

        if self.tracking_thread and self.tracking_thread.is_alive():
            return True  # Already running

        self.running = True
        self.tracking_thread = threading.Thread(target=self._tracking_loop, daemon=True)
        self.tracking_thread.start()

        conditional_print(self.config, "Servo tracking started", always_print=True)
        return True

    def stop_tracking(self):
        """Stop servo tracking"""
        self.running = False
        self.tracking_active = False
        self.scanning_active = False

        if self.tracking_thread and self.tracking_thread.is_alive():
            self.tracking_thread.join(timeout=1.0)

        # Return to center position
        self.move_to_center()
        conditional_print(self.config, "Servo tracking stopped", always_print=True)

    def update_image_dimensions(self, width, height):
        """Update image dimensions for tracking calculations"""
        with self.lock:
            self.image_center = (width // 2, height // 2)

    def update_face_position(self, face_center):
        """Update the current face position for tracking with EMA smoothing."""
        if not self.enable_tracking or not ROBOT_HAT_AVAILABLE:
            return

        with self.lock:
            if face_center is not None:
                cx, cy = face_center
                # EMA smooth the pixel target — prevents jitter when
                # the servo switches between nearby faces each frame
                if self._smooth_cx is None:
                    self._smooth_cx = float(cx)
                    self._smooth_cy = float(cy)
                else:
                    a = self._target_smooth
                    self._smooth_cx = a * cx + (1 - a) * self._smooth_cx
                    self._smooth_cy = a * cy + (1 - a) * self._smooth_cy
                self.current_face_center = (int(self._smooth_cx), int(self._smooth_cy))
                self.last_face_time = time.time()
                if not self.tracking_active:
                    self.tracking_active = True
                    self.scanning_active = False
                    debug_print(self.config, "Face detected - stopping scan, starting tracking")
            else:
                self.current_face_center = None
                # Reset smoother when face disappears so next appearance snaps quickly
                self._smooth_cx = None
                self._smooth_cy = None

    def notice_presence(self):
        """EYE_SERVO_V1 — osoba je v záběru, ale kameru NEHÝBAT (oči vedou).
        Drží face-lost timer svěží → nespustí scan; zastaví běžící scan;
        netrackuje (kamera drží polohu). Použít místo update_face_position(None),
        když je osoba přítomná a vycentrovaná."""
        if not self.enable_tracking or not ROBOT_HAT_AVAILABLE:
            return
        with self.lock:
            self.last_face_time = time.time()
            self.tracking_active = False
            self.scanning_active = False
            self.current_face_center = None
            self._smooth_cx = None
            self._smooth_cy = None

    def start_scanning(self):
        """Start scanning behavior when no face is detected - LIMITED RANGE"""
        if not self.scanning_enabled:
            return

        self.scanning_active = True
        self.scan_direction = 1  # Start scanning right
        self.scan_target_pan = self.current_pan
        self.scan_pause_start = 0
        self.scan_pausing = False
        self.last_scan_update = time.time()

        # Use limited scanning range
        scan_min = max(self.scanning_pan_min, self.actual_pan_min if self.calibrated else self.pan_min)
        scan_max = min(self.scanning_pan_max, self.actual_pan_max if self.calibrated else self.pan_max)

        debug_print(self.config, f"Starting face scanning - limited range {scan_min}° to {scan_max}°")
        debug_print(self.config, f"Current position: {self.current_pan:.1f}°")

    def stop_scanning(self):
        """Stop scanning behavior"""
        self.scanning_active = False
        debug_print(self.config, "Stopping face scanning")

    def update_scanning_position(self):
        """Update camera position during scanning sweep - LIMITED TO -45 to +45 degrees"""
        if not self.scanning_active or not self.pan_servo:
            return

        current_time = time.time()
        time_delta = current_time - self.last_scan_update
        self.last_scan_update = current_time

        # Use limited scanning range (-45 to +45 degrees)
        scan_min = max(self.scanning_pan_min, self.actual_pan_min if self.calibrated else self.pan_min)
        scan_max = min(self.scanning_pan_max, self.actual_pan_max if self.calibrated else self.pan_max)

        # Calculate movement increment
        movement_increment = self.scanning_speed * time_delta * self.scan_direction

        # Check if we're currently pausing at an end position
        if self.scan_pausing:
            if current_time - self.scan_pause_start >= self.scanning_pause_time:
                # End pause, reverse direction
                self.scan_direction *= -1
                self.scan_pausing = False
                debug_print(self.config, f"Scan direction changed to {'RIGHT' if self.scan_direction > 0 else 'LEFT'}")
            else:
                # Still pausing, don't move
                return

        # Calculate new target position
        new_pan = self.current_pan + movement_increment

        # Check if we've hit the scanning limits (not full calibrated limits)
        if new_pan >= scan_max and self.scan_direction > 0:
            # Hit right scanning limit
            new_pan = scan_max
            self.scan_pausing = True
            self.scan_pause_start = current_time
            debug_print(self.config, f"Reached right scan limit (+{scan_max}°) - pausing")
        elif new_pan <= scan_min and self.scan_direction < 0:
            # Hit left scanning limit
            new_pan = scan_min
            self.scan_pausing = True
            self.scan_pause_start = current_time
            debug_print(self.config, f"Reached left scan limit ({scan_min}°) - pausing")

        # Move servo to new position
        if abs(new_pan - self.current_pan) > 0.5:  # Only move if significant change
            try:
                self.pan_servo.angle(new_pan)
                self.current_pan = new_pan
            except Exception as e:
                debug_print(self.config, f"Scanning movement error: {e}")

    def calculate_tracking_angles(self, face_center):
        """Calculate required servo angles to center the face"""
        if not face_center:
            return None, None

        face_x, face_y = face_center
        center_x, center_y = self.image_center

        # Calculate offset from center
        offset_x = face_x - center_x
        offset_y = face_y - center_y

        # Check if face is already centered
        if abs(offset_x) < self.center_tolerance and abs(offset_y) < self.center_tolerance:
            return self.current_pan, self.current_tilt

        # Calculate incremental movement adjustments
        # Proportional damping v pixel prostoru — tlumí oscilaci blízko středu
        _px_ratio = min(abs(offset_x) / max(self.center_tolerance * 4, 1), 1.0)
        _py_ratio = min(abs(offset_y) / max(self.center_tolerance * 4, 1), 1.0)
        pan_increment  = -(offset_x / center_x) * 8.0 \
                          * self.tracking_sensitivity * _px_ratio
        tilt_increment =  (offset_y / center_y) * 8.0 \
                          * self.tracking_sensitivity * _py_ratio

        # Apply inversion if configured.
        # invert_pan / invert_tilt negate the tracking increment so the camera
        # follows the face the right way. (The physical mount reversal of the
        # tilt servo is handled separately by mirroring the HW output in
        # _set_tilt_hw, which keeps the rest position correct.)
        if self.invert_pan:
            pan_increment = -pan_increment
        if self.invert_tilt:
            tilt_increment = -tilt_increment

        # Apply smoothing and calculate new target positions
        new_pan = self.current_pan + (pan_increment * self.smoothing_factor)
        new_tilt = self.current_tilt + (tilt_increment * self.smoothing_factor)

        # Use calibrated limits if available
        pan_min = self.actual_pan_min if self.calibrated else self.pan_min
        pan_max = self.actual_pan_max if self.calibrated else self.pan_max
        tilt_min = self.actual_tilt_min if self.calibrated else self.tilt_min
        tilt_max = self.actual_tilt_max if self.calibrated else self.tilt_max

        # Clamp to limits
        new_pan = max(pan_min, min(pan_max, new_pan))
        new_tilt = max(tilt_min, min(tilt_max, new_tilt))

        return new_pan, new_tilt

    def _set_tilt_hw(self, angle):
        """Write tilt angle to hardware, mirroring it when the tilt servo is
        physically reversed (config invert_tilt=true). Keeps all software logic
        (limits, center, tracking) in one coordinate frame; only the physical
        output is flipped."""
        self.tilt_servo.angle(-angle if self.invert_tilt else angle)

    # ── SERVO_MANUAL_CALIB_V1 — interactive calibration helpers ──────────────
    def calib_begin(self):
        """Suspend tracking and take direct manual control of the servos."""
        self.calibrating = True
        self.tracking_active = False
        self.scanning_active = False

    def calib_end(self):
        """Resume normal tracking after calibration."""
        self.calibrating = False

    def calib_set(self, pan, tilt):
        """Drive servos directly during calibration, clamped to absolute jog
        bounds (NOT the calibrated range we are trying to measure). Tilt goes
        through _set_tilt_hw so the invert_tilt mirroring stays consistent.
        Returns the clamped (pan, tilt)."""
        pan = max(-self.CAL_PAN_LIMIT, min(self.CAL_PAN_LIMIT, pan))
        tilt = max(-self.CAL_TILT_LIMIT, min(self.CAL_TILT_LIMIT, tilt))
        try:
            if self.pan_servo:
                self.pan_servo.angle(pan)
            if self.tilt_servo:
                self._set_tilt_hw(tilt)
            self.current_pan = pan
            self.current_tilt = tilt
        except Exception as e:
            debug_print(self.config, f"calib_set error: {e}")
        return pan, tilt

    def apply_and_save_calibration(self, pan_min, pan_max, pan_center,
                                   tilt_min, tilt_max, tilt_center):
        """Apply hand-measured limits/centers in memory and persist them to
        config.json (single source of truth). Offsets are derived so that
        move_to_center() lands on the captured center."""
        if pan_min > pan_max:
            pan_min, pan_max = pan_max, pan_min
        if tilt_min > tilt_max:
            tilt_min, tilt_max = tilt_max, tilt_min

        pan_off = pan_center - (pan_min + pan_max) / 2.0
        tilt_off = tilt_center - (tilt_min + tilt_max) / 2.0

        # In-memory (live, no restart needed for limits)
        self.pan_min = self.actual_pan_min = pan_min
        self.pan_max = self.actual_pan_max = pan_max
        self.tilt_min = self.actual_tilt_min = tilt_min
        self.tilt_max = self.actual_tilt_max = tilt_max
        self.default_pan_offset = pan_off
        self.default_tilt_offset = tilt_off
        self.calibrated = True

        try:
            import json
            from pathlib import Path
            p = Path("config.json")
            cfg = json.loads(p.read_text()) if p.exists() else dict(self.config)
            st = cfg.setdefault('servo_tracking', {})
            st.update({
                'pan_min': round(pan_min, 1), 'pan_max': round(pan_max, 1),
                'tilt_min': round(tilt_min, 1), 'tilt_max': round(tilt_max, 1),
                'calibrated_pan_min': round(pan_min, 1),
                'calibrated_pan_max': round(pan_max, 1),
                'calibrated_tilt_min': round(tilt_min, 1),
                'calibrated_tilt_max': round(tilt_max, 1),
                'default_pan_offset': round(pan_off, 1),
                'default_tilt_offset': round(tilt_off, 1),
                'calibration_completed': True,
            })
            p.write_text(json.dumps(cfg, indent=4))
            _srvlog.info("Manual calibration saved: pan[%.1f,%.1f] tilt[%.1f,%.1f] "
                         "off(%.1f,%.1f)", pan_min, pan_max, tilt_min, tilt_max,
                         pan_off, tilt_off)
            return True
        except Exception as e:
            _srvlog.error("Manual calibration save failed: %s", e)
            return False

    def move_servos_smooth(self, target_pan, target_tilt):
        """Move servos smoothly to target positions"""
        if not self.pan_servo or not self.tilt_servo:
            print(f"[Servo] move_servos_smooth called but pan_servo={self.pan_servo} tilt_servo={self.tilt_servo}")
            return False

        # Use calibrated limits if available
        pan_min = self.actual_pan_min if self.calibrated else self.pan_min
        pan_max = self.actual_pan_max if self.calibrated else self.pan_max
        tilt_min = self.actual_tilt_min if self.calibrated else self.tilt_min
        tilt_max = self.actual_tilt_max if self.calibrated else self.tilt_max

        # Clamp to calibrated limits
        target_pan = max(pan_min, min(pan_max, target_pan))
        target_tilt = max(tilt_min, min(tilt_max, target_tilt))

        try:
            pan_diff  = target_pan  - self.current_pan
            tilt_diff = target_tilt - self.current_tilt

            # Synchronní interpolace — pan a tilt dorazí současně (přímá trajektorie)
            import math as _math
            MAX_STEP   = self.max_step_degrees
            total_dist = _math.sqrt(pan_diff**2 + tilt_diff**2)
            if total_dist > MAX_STEP:
                # Škáluj oba kroky stejným faktorem — zachová směr
                scale     = MAX_STEP / total_dist
                pan_step  = pan_diff  * scale
                tilt_step = tilt_diff * scale
            else:
                pan_step  = pan_diff
                tilt_step = tilt_diff

            if abs(pan_diff) > self.min_movement:
                new_pan = max(pan_min, min(pan_max, self.current_pan + pan_step))
                self.pan_servo.angle(new_pan)
                self.current_pan = new_pan

            if abs(tilt_diff) > self.min_movement:
                new_tilt = max(tilt_min, min(tilt_max, self.current_tilt + tilt_step))
                self._set_tilt_hw(new_tilt)
                self.current_tilt = new_tilt

            return True

        except Exception as e:
            debug_print(self.config, f"Servo movement error: {e}")
            return False

    def save_calibration_to_config(self):
        """Save calibration results to config file"""
        try:
            import json
            from pathlib import Path

            config_path = "config.json"

            # Load current config
            if Path(config_path).exists():
                with open(config_path, 'r') as f:
                    config = json.load(f)
            else:
                config = self.config

            # Update with calibration data
            if 'servo_tracking' not in config:
                config['servo_tracking'] = {}

            config['servo_tracking'].update({
                'calibration_completed': True,
                'calibrated_pan_min': self.actual_pan_min,
                'calibrated_pan_max': self.actual_pan_max,
                'calibrated_tilt_min': self.actual_tilt_min,
                'calibrated_tilt_max': self.actual_tilt_max
            })

            # Save updated config
            with open(config_path, 'w') as f:
                json.dump(config, f, indent=4)

            conditional_print(self.config, f"Calibration saved to {config_path}", always_print=True)
            return True

        except Exception as e:
            debug_print(self.config, f"Failed to save calibration: {e}")
            return False

    def _tracking_loop(self):
        """Main tracking loop running in separate thread - ENHANCED with scanning"""
        print("[Servo] Tracking loop started")
        _diag_last = 0.0

        while self.running:
            try:
                # SERVO_MANUAL_CALIB_V1 — hand off control to the calibration wizard.
                if self.calibrating:
                    time.sleep(0.05)
                    continue

                current_time = time.time()

                with self.lock:
                    face_center = self.current_face_center
                    time_since_face = current_time - self.last_face_time

                # Diagnostic log every 30 s — file only, not terminal
                if current_time - _diag_last >= 30.0:
                    _srvlog.info("state: tracking=%s scanning=%s "
                                 "pan=%.1f tilt=%.1f since_face=%.1fs",
                                 self.tracking_active, self.scanning_active,
                                 self.current_pan, self.current_tilt, time_since_face)
                    _diag_last = current_time

                # Decide what behavior to use
                if time_since_face > self.face_lost_timeout:
                    # No face detected for a while
                    if self.tracking_active:
                        self.tracking_active = False
                        _srvlog.info("Face lost — switching to scan mode")

                    # Start scanning if not already scanning
                    if not self.scanning_active and self.scanning_enabled:
                        self.start_scanning()

                    # Update scanning movement
                    if self.scanning_active:
                        self.update_scanning_position()

                elif self.tracking_active and face_center:
                    # Face is present and we're tracking
                    if self.scanning_active:
                        self.stop_scanning()

                    # Calculate required movement for face tracking
                    target_pan, target_tilt = self.calculate_tracking_angles(face_center)

                    if target_pan is not None and target_tilt is not None:
                        self.move_servos_smooth(target_pan, target_tilt)

                elif not self.tracking_active and face_center:
                    # update_face_position sets tracking_active via its own lock,
                    # but if we get here face is present but tracking not yet active —
                    # activate it directly
                    self.tracking_active = True
                    debug_print(self.config, "[Servo] Re-activating tracking for detected face")

                # Use appropriate sleep interval
                if self.scanning_active:
                    time.sleep(self.scanning_delay)
                else:
                    time.sleep(self.tracking_delay)

            except Exception as e:
                print(f"[Servo] Tracking loop error: {e}")
                time.sleep(1.0)

    def move_to_center(self):
        """Move servos to center position with custom pan/tilt offsets"""
        if not self.pan_servo or not self.tilt_servo:
            return False

        try:
            # Calculate actual center based on calibrated limits
            if self.calibrated:
                center_pan = (self.actual_pan_min + self.actual_pan_max) / 2
                center_tilt = (self.actual_tilt_min + self.actual_tilt_max) / 2
            else:
                center_pan = 0
                center_tilt = 0

            # Apply custom offsets
            center_pan += self.default_pan_offset
            center_tilt += self.default_tilt_offset

            # Ensure we stay within limits
            pan_min = self.actual_pan_min if self.calibrated else self.pan_min
            pan_max = self.actual_pan_max if self.calibrated else self.pan_max
            tilt_min = self.actual_tilt_min if self.calibrated else self.tilt_min
            tilt_max = self.actual_tilt_max if self.calibrated else self.tilt_max

            center_pan = max(pan_min, min(pan_max, center_pan))
            center_tilt = max(tilt_min, min(tilt_max, center_tilt))

            debug_print(self.config, f"Moving to custom starting position: Pan {center_pan:.1f}, Tilt {center_tilt:.1f}")
            debug_print(self.config, f"Applied offsets: Pan {self.default_pan_offset}, Tilt {self.default_tilt_offset}")

            self.pan_servo.angle(center_pan)
            self._set_tilt_hw(center_tilt)
            self.current_pan = center_pan
            self.current_tilt = center_tilt

            conditional_print(self.config, "Servos positioned at custom starting position", always_print=True)
            return True

        except Exception as e:
            debug_print(self.config, f"Center movement error: {e}")
            return False

    def manual_pan(self, angle):
        """Manually move pan servo to specific angle using calibrated limits"""
        if not self.pan_servo:
            return False

        # Stop scanning during manual control
        if self.scanning_active:
            self.stop_scanning()

        # Use calibrated limits if available
        pan_min = self.actual_pan_min if self.calibrated else self.pan_min
        pan_max = self.actual_pan_max if self.calibrated else self.pan_max

        angle = max(pan_min, min(pan_max, angle))

        try:
            self.pan_servo.angle(angle)
            self.current_pan = angle
            conditional_print(self.config, f"Pan moved to {angle:.1f} (limits: {pan_min:.1f} to {pan_max:.1f})", always_print=True)
            return True
        except Exception as e:
            debug_print(self.config, f"Manual pan error: {e}")
            return False

    def manual_tilt(self, angle):
        """Manually move tilt servo to specific angle using calibrated limits"""
        if not self.tilt_servo:
            return False

        # Use calibrated limits if available
        tilt_min = self.actual_tilt_min if self.calibrated else self.tilt_min
        tilt_max = self.actual_tilt_max if self.calibrated else self.tilt_max

        angle = max(tilt_min, min(tilt_max, angle))

        try:
            self._set_tilt_hw(angle)
            self.current_tilt = angle
            conditional_print(self.config, f"Tilt moved to {angle:.1f} (limits: {tilt_min:.1f} to {tilt_max:.1f})", always_print=True)
            return True
        except Exception as e:
            debug_print(self.config, f"Manual tilt error: {e}")
            return False

    def get_current_position(self):
        """Get current servo positions"""
        return {
            'pan': self.current_pan,
            'tilt': self.current_tilt,
            'tracking_active': self.tracking_active,
            'scanning_active': self.scanning_active,
            'face_detected': self.current_face_center is not None
        }
    def get_calibration_info(self):
        """Get calibration status and limits"""
        return {
            'calibrated': self.calibrated,
            'auto_calibrate_enabled': self.auto_calibrate,
            'obstacle_detection_enabled': self.obstacle_detection_enabled,
            'configured_limits': {
                'pan_min': self.pan_min,
                'pan_max': self.pan_max,
                'tilt_min': self.tilt_min,
                'tilt_max': self.tilt_max
            },
            'actual_limits': {
                'pan_min': self.actual_pan_min,
                'pan_max': self.actual_pan_max,
                'tilt_min': self.actual_tilt_min,
                'tilt_max': self.actual_tilt_max
            }
        }

    def set_tracking_sensitivity(self, sensitivity):
        """Adjust tracking sensitivity"""
        if 0.1 <= sensitivity <= 2.0:
            self.tracking_sensitivity = sensitivity
            conditional_print(self.config, f"Tracking sensitivity set to {sensitivity}", always_print=True)
            return True
        else:
            conditional_print(self.config, "Sensitivity must be between 0.1 and 2.0", always_print=True)
            return False

    def toggle_tracking(self):
        """Toggle tracking on/off"""
        if self.tracking_active:
            self.tracking_active = False
            conditional_print(self.config, "Tracking disabled", always_print=True)
        else:
            if self.current_face_center:
                self.tracking_active = True
                conditional_print(self.config, "Tracking enabled", always_print=True)
            else:
                conditional_print(self.config, "No face detected - cannot enable tracking", always_print=True)

        return self.tracking_active

    def toggle_scanning(self):
        """Toggle scanning behavior on/off"""
        self.scanning_enabled = not self.scanning_enabled

        if not self.scanning_enabled and self.scanning_active:
            self.stop_scanning()

        conditional_print(self.config, f"Face scanning {'enabled' if self.scanning_enabled else 'disabled'}", always_print=True)
        return self.scanning_enabled

    def set_scanning_speed(self, speed):
        """Set scanning speed in degrees per second"""
        if 5.0 <= speed <= 30.0:
            self.scanning_speed = speed
            conditional_print(self.config, f"Scanning speed set to {speed} degrees/second", always_print=True)
            return True
        else:
            conditional_print(self.config, "Scanning speed must be between 5.0 and 30.0 degrees/second", always_print=True)
            return False

    def set_scanning_range(self, min_angle, max_angle):
        """Set the scanning range limits"""
        # Ensure the range is within the calibrated limits
        actual_min = self.actual_pan_min if self.calibrated else self.pan_min
        actual_max = self.actual_pan_max if self.calibrated else self.pan_max

        min_angle = max(min_angle, actual_min)
        max_angle = min(max_angle, actual_max)

        if min_angle >= max_angle:
            conditional_print(self.config, "Invalid range - minimum must be less than maximum", always_print=True)
            return False

        self.scanning_pan_min = min_angle
        self.scanning_pan_max = max_angle

        conditional_print(self.config, f"Scanning range set to {min_angle}° to {max_angle}°", always_print=True)
        return True

    def get_scanning_info(self):
        """Get current scanning configuration"""
        return {
            'enabled': self.scanning_enabled,
            'active': self.scanning_active,
            'speed': self.scanning_speed,
            'range_min': self.scanning_pan_min,
            'range_max': self.scanning_pan_max,
            'pause_time': self.scanning_pause_time,
            'current_direction': 'RIGHT' if self.scan_direction > 0 else 'LEFT'
        }

    def get_tracking_stats(self):
        """Get tracking statistics and status"""
        current_time = time.time()

        return {
            'hardware_available': ROBOT_HAT_AVAILABLE,
            'tracking_enabled': self.enable_tracking,
            'tracking_active': self.tracking_active,
            'scanning_enabled': self.scanning_enabled,
            'scanning_active': self.scanning_active,
            'current_pan': self.current_pan,
            'current_tilt': self.current_tilt,
            'face_detected': self.current_face_center is not None,
            'time_since_face': current_time - self.last_face_time if self.last_face_time > 0 else None,
            'image_center': self.image_center,
            'tracking_sensitivity': self.tracking_sensitivity,
            'center_tolerance': self.center_tolerance,
            'scanning_speed': self.scanning_speed,
            'scan_direction': 'RIGHT' if self.scan_direction > 0 else 'LEFT'
        }

    def cleanup(self):
        """Cleanup servo resources"""
        self.stop_tracking()

        if self.pan_servo:
            try:
                self.pan_servo.angle(0)
            except:
                pass

        if self.tilt_servo:
            try:
                self.tilt_servo.angle(0)
            except:
                pass

        conditional_print(self.config, "Servo controller cleaned up", always_print=True)
