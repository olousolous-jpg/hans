"""
System Manager Module
Main FaceRecognitionSystem class.
Active pipeline: picamera2 → hailo_inference_server (Unix socket) → PicamDisplayController
"""

import threading
import time
from pathlib import Path

from scripts.database_manager import DatabaseManager
from scripts.servo_controller import ServoController
from scripts.tts_speaker import TTSSpeaker
from scripts.display_controller_picam import PicamDisplayController
from scripts.config_manager import should_debug, debug_print, conditional_print

# Eye controller (optional — works without displays)
try:
    from scripts.Eye_sphere import SphereEyeController as EyeController
    EYE_CONTROLLER_AVAILABLE = True
except ImportError:
    EYE_CONTROLLER_AVAILABLE = False
    print("⚠️  Eye_sphere module not found — eye displays disabled")

from scripts.openwebui_direct_handler import OpenWebUIDirectHandler



# component_status_merged
class ComponentStatus:
    """Track component health and status."""

    def __init__(self):
        self.components  = {}
        self.error_counts = {}
        self.last_errors  = {}

    def set_status(self, component, status, error_msg=None):
        self.components[component] = status
        if error_msg:
            self.last_errors[component]  = error_msg
            self.error_counts[component] = self.error_counts.get(component, 0) + 1

    def is_healthy(self, component):
        return self.components.get(component, False)

    def get_status_summary(self):
        healthy = sum(1 for s in self.components.values() if s)
        return f"{healthy}/{len(self.components)} components healthy"

    def get_failed_components(self):
        return [n for n, s in self.components.items() if not s]

    def get_error_summary(self):
        return {c: {"error_count": self.error_counts.get(c, 0),
                    "last_error":  self.last_errors.get(c, ""),
                    "healthy":     self.is_healthy(c)}
                for c in self.error_counts}

    def reset_component(self, component):
        self.components.pop(component, None)
        self.error_counts.pop(component, None)
        self.last_errors.pop(component, None)

    def get_critical_status(self):
        critical = ["database_manager", "mediapipe_processor", "camera_manager"]
        failed   = [c for c in critical if not self.is_healthy(c)]
        return len(failed) == 0, failed

# p3_system_manager_cleaned
class FaceRecognitionSystem:
    """Face recognition system — picamera2 + Hailo socket pipeline"""

    def __init__(self, config):
        print("Initializing Face Recognition System...")
        self.config = config
        self.processing = True

        self.component_status = ComponentStatus()

        # HW_CAPABILITIES_V1 — detekuj HW PŘED initem subsystémů: vynuť mandatory
        # (kamera/Hailo), auto-vypni optional flagy (config.features.*) pro chybějící
        # lokální HW. Subsystémy níže pak flagy respektují. Nikdy nehází.
        try:
            from scripts.hw_capabilities import apply as _hw_apply
            self.hw_caps = _hw_apply(self.config)
        except Exception as _hwe:
            self.hw_caps = {}
            print(f"⚠  HW capabilities probe failed (continuing): {_hwe}")

        # Performance tracking
        self.last_performance_report = time.time()
        self.performance_report_interval = 30

        # ── EyeController FIRST — must claim SPI pins before Robot HAT ──
        self.eye_controller = None
        if (EYE_CONTROLLER_AVAILABLE
                and self.config.get('eyes', {}).get('enabled', False)
                and self.config.get('features', {}).get('dual_eye_display', True)):
            try:
                self.eye_controller = EyeController(self.config)
                self.component_status.set_status('eye_controller', True)
                time.sleep(2.0)  # Give displays time before Robot HAT init
            except Exception as e:
                self.component_status.set_status('eye_controller', False, str(e))
                print(f"⚠  eye_controller init failed (continuing): {e}")

        self._initialize_components()

        print("System initialization complete!")
        print(f"Status: {self.component_status.get_status_summary()}")

    # ── Component initialization ─────────────────────────────────────────

    def _initialize_components(self):
        """Initialize all components with error recovery."""

        essential = [
            ('database_manager', self._init_database_manager),
        ]

        optional = [
            ('servo_controller',  self._init_servo_controller),
            ('tts_speaker',       self._init_tts_speaker),
            ('openwebui_chat',    self._init_chat_handler),
        ]

        for name, fn in essential:
            try:
                fn()
                self.component_status.set_status(name, True)
                print(f"✓ {name} initialized")
            except Exception as e:
                self.component_status.set_status(name, False, str(e))
                print(f"✗ CRITICAL: {name} failed: {e}")
                raise

        for name, fn in optional:
            try:
                fn()
                self.component_status.set_status(name, True)
                print(f"✓ {name} initialized")
            except Exception as e:
                self.component_status.set_status(name, False, str(e))
                print(f"⚠  {name} init failed (continuing): {e}")

    def _init_database_manager(self):
        # Select DB path based on active hailo server mode so SCRFD and
        # personface embeddings never mix in the same file.
        mode = self.config.get('hailo_server', {}).get('mode', 'scrfd')
        db_cfg = self.config.get('database', {})

        if mode == 'personface':
            path = db_cfg.get('faces_db_path_personface',
                              'data/known_faces_personface.pkl')
        else:
            path = db_cfg.get('faces_db_path', 'data/known_faces.pkl')

        # Override path in config so DatabaseManager picks it up
        self.config['database']['faces_db_path'] = path
        print(f"[DB] Mode '{mode}' → database: {path}")
        self.database_manager = DatabaseManager(self.config)

    def _init_servo_controller(self):
        if not self.config.get('features', {}).get('servo_tracking', False):
            self.servo_controller = None
            raise Exception("Servo tracking disabled in config")
        self.servo_controller = ServoController(self.config)
        if self.eye_controller and self.eye_controller.hw_ok:
            self._patch_servo_for_eyes()

    def _patch_servo_for_eyes(self):
        """Monkey-patch ServoController to update eyes after every move."""
        servo = self.servo_controller
        eyes  = self.eye_controller
        orig_move = servo.move_servos_smooth

        def patched_move(target_pan, target_tilt):
            result = orig_move(target_pan, target_tilt)
            eyes.update_pan(servo.current_pan)
            eyes.update_tilt(servo.current_tilt)
            return result

        servo.move_servos_smooth = patched_move

        if hasattr(servo, 'update_scanning_position'):
            orig_scan = servo.update_scanning_position

            def patched_scan():
                orig_scan()
                eyes.update_pan(servo.current_pan)
                eyes.update_tilt(servo.current_tilt)

            servo.update_scanning_position = patched_scan

        print("  👁  Servo ↔ Eye link active")

    def _init_tts_speaker(self):
        if not self.config.get('tts', {}).get('enabled', False):
            self.tts_speaker = None
            raise Exception("TTS disabled in config")
        if not self.config.get('features', {}).get('tts_audio', True):  # HW_CAPABILITIES_V1
            self.tts_speaker = None
            raise Exception("TTS audio disabled (no audio-out HW)")
        self.tts_speaker = TTSSpeaker(self.config)

    def _init_chat_handler(self):
        direct_cfg = self.config.get('openwebui_direct', {})
        chat_cfg   = self.config.get('openwebui_chat', {})

        if not direct_cfg.get('enabled', False) and not chat_cfg.get('enabled', False):
            self.openwebui_chat = None
            raise Exception("Chat integration disabled")

        self.openwebui_chat = OpenWebUIDirectHandler(self.config)

        # Wire up TTS if available
        tts = getattr(self, 'tts_speaker', None)
        if tts and self.openwebui_chat:
            if hasattr(self.openwebui_chat, 'set_tts_speaker'):
                self.openwebui_chat.set_tts_speaker(tts)
            else:
                self.openwebui_chat.tts_speaker = tts

        # Preload LLM do VRAM hned při startu
        if self.openwebui_chat and hasattr(self.openwebui_chat, 'ping_model'):
            import threading
            def _preload():
                try:
                    print("[Chat] Přednahrávám LLM model do VRAM...")
                    self.openwebui_chat.ping_model()
                    print("[Chat] LLM model připraven v VRAM")
                except Exception as e:
                    print(f"[Chat] Preload selhal: {e}")
            threading.Thread(target=_preload, daemon=True).start()

        # HANS_NOTIFIER_V1 — notifikační mosty (Telegram + Matrix E2E) přes
        # fan-out Notifier. Volající (hans_idle, display) drží .telegram; backend
        # je swappable configem. Cíl (23.7.): až Matrix ověřen naživo →
        # telegram.enabled=false → provozně jen Matrix; pak Telegram kód smazat.
        self.telegram = None
        try:
            from scripts.hans_notifier import Notifier
            _bridges = []
            try:
                from scripts.hans_telegram import TelegramBridge
                _tg = TelegramBridge(self.config, self.openwebui_chat)
                if _tg.enabled:
                    _bridges.append(_tg)
                    print("[Telegram] most zapnut")
            except Exception as _te:
                print(f"[Telegram] init selhal: {_te}")
            try:
                from scripts.hans_matrix import MatrixBridge
                _mx = MatrixBridge(self.config, self.openwebui_chat)
                if _mx.enabled:
                    _bridges.append(_mx)
                    print("[Matrix] most zapnut (E2E)")
            except Exception as _me:
                print(f"[Matrix] init selhal: {_me}")
            if _bridges:
                _notif = Notifier(_bridges)
                _notif.start()
                self.telegram = _notif
                if self.openwebui_chat is not None:
                    self.openwebui_chat.telegram = _notif
                print(f"[Notifier] aktivní ({len(_bridges)} most/y)")
        except Exception as _ne:
            print(f"[Notifier] init selhal: {_ne}")

    def show_system_status(self):
        print("\nSystem Configuration:")
        print(f"  Components: {self.component_status.get_status_summary()}")
        print(f"  Debug: {'ON' if self.config.get('debug', False) else 'OFF'}")
        servo = getattr(self, 'servo_controller', None)
        print(f"  Servo tracking: {'ON' if servo else 'OFF'}")
        chat = getattr(self, 'openwebui_chat', None)
        print(f"  Chat: {'ON' if chat else 'OFF'}")
        tts = getattr(self, 'tts_speaker', None)
        print(f"  TTS: {'ON' if tts else 'OFF'}")
        eyes = self.eye_controller
        print(f"  Eye displays: {'ON' if eyes and eyes.hw_ok else 'OFF'}")

    def validate_critical_components(self):
        if not self.component_status.is_healthy('database_manager'):
            print("CRITICAL: database_manager failed — cannot continue")
            return False
        return True

    # ── System start ─────────────────────────────────────────────────────

    def start_system(self, args):
        """Entry point — always uses PicamDisplayController."""
        return self._start_picam(args)

    def _start_picam(self, args):
        # Start servo tracking
        servo = getattr(self, 'servo_controller', None)
        if servo and not getattr(servo, '_tracking_started', False):
            if servo.start_tracking():
                servo._tracking_started = True
            else:
                print("[Servo] Tracking thread not started — check hardware")

        # Start eye animations
        if self.eye_controller and self.eye_controller.hw_ok:
            if not self.eye_controller._running:
                self.eye_controller.start()

        # Performance monitor
        if not getattr(self, '_perf_thread_started', False):
            threading.Thread(target=self._performance_monitor, daemon=True).start()
            self._perf_thread_started = True

        # Keepalive ping — prevents Ollama unloading model between greetings
        chat = getattr(self, 'openwebui_chat', None)
        if chat and hasattr(chat, 'ping_model'):
            if not getattr(self, '_keepalive_started', False):
                def _keepalive():
                    import time
                    while True:
                        try:
                            chat.ping_model()
                        except Exception:
                            pass
                        time.sleep(240)   # ping every 4 minutes
                threading.Thread(target=_keepalive, daemon=True).start()
                self._keepalive_started = True
                print("[Chat] Model keepalive started (ping every 5 min)")

        # ── Ollama direct warmup + keepalive ──────────────────────
        # OLLAMA_WARMUP_PATCH
        if not getattr(self, '_ollama_warmup_started', False):
            def _ollama_warmup_loop():
                from scripts.ollama_client import ollama_warmup
                import time as _t
                # Modely které se volají přímo (ne přes OpenWebUI)
                models = set()
                models.add(self.config.get("models", {}).get("dialog", ""))
                models.add(self.config.get("models", {}).get("utility", ""))
                models.discard("")
                if not models:
                    models = {"hans-czech:latest"}
                base = self.config.get("openwebui_chat", {}).get(
                    "base_url", "http://127.0.0.1:11434")
                for m in models:
                    print(f"[Ollama] Warmup: {m} ...")
                    ollama_warmup(m, ollama_url=base)
                # Keepalive loop — ping každé 4 minuty
                while True:
                    _t.sleep(240)
                    for m in models:
                        try:
                            ollama_warmup(m, ollama_url=base)
                        except Exception:
                            pass
            threading.Thread(target=_ollama_warmup_loop, daemon=True).start()
            self._ollama_warmup_started = True

        ctrl = PicamDisplayController(
            self.config,
            database_manager  = self.database_manager,
            openwebui_chat    = getattr(self, 'openwebui_chat', None),
            servo_controller  = getattr(self, 'servo_controller', None),
        )
        return ctrl.start_loop()

    # ── Background tasks ─────────────────────────────────────────────────

    def _performance_monitor(self):
        while self.processing:
            try:
                time.sleep(self.performance_report_interval)
                if self.config.get('debug', False):
                    self._report_performance_stats()
            except Exception as e:
                debug_print(self.config, f"Performance monitor error: {e}")

    def _report_performance_stats(self):
        print("\n=== PERFORMANCE REPORT ===")
        print(f"  Components: {self.component_status.get_status_summary()}")
        servo = getattr(self, 'servo_controller', None)
        if servo:
            pos = servo.get_current_position()
            print(f"  Servo — pan: {pos['pan']:.1f}°  tilt: {pos['tilt']:.1f}°  "
                  f"tracking: {pos['tracking_active']}  scanning: {pos['scanning_active']}")
        print("===========================")

    # ── Shutdown ──────────────────────────────────────────────────────────

    def stop(self):
        print("Stopping system...")
        self.processing = False

        cleanup_order = [
            ('tts_speaker',      'cleanup'),
            ('servo_controller', 'cleanup'),
            ('openwebui_chat',   'cleanup'),
        ]

        for name, method in cleanup_order:
            obj = getattr(self, name, None)
            if obj and hasattr(obj, method):
                try:
                    getattr(obj, method)()
                    print(f"✓ {name} cleaned up")
                except Exception as e:
                    print(f"⚠  {name} cleanup error: {e}")

        # Eye controller last (owns SPI)
        if self.eye_controller:
            try:
                self.eye_controller.stop()
                print("✓ eye_controller cleaned up")
            except Exception as e:
                print(f"⚠  eye_controller cleanup error: {e}")

        print("System stopped.")
