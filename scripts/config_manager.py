"""
Configuration Manager Module
Handles configuration loading, validation, and command-line overrides
Moved from main.py for better maintainability
"""

import json
from pathlib import Path


class ConfigManager:
    """Manages system configuration loading and validation"""
    
    def __init__(self, config_path="config.json"):
        """Initialize configuration manager"""
        self.config_path = config_path
    
    def load_config(self):
        """Load and validate configuration from JSON file"""
        try:
            with open(self.config_path, 'r') as f:
                config = json.load(f)
            print(f"Configuration loaded from {self.config_path}")
            
            # Validate and fix config
            config = self._validate_and_fix_config(config)
            return config
            
        except FileNotFoundError:
            print(f"Config file {self.config_path} not found, creating default config")
            config = self.get_default_config()
            self._save_config(config, self.config_path)
            return config
        except json.JSONDecodeError as e:
            print(f"Error parsing config JSON: {e}")
            print("Using default configuration")
            return self.get_default_config()
        except Exception as e:
            print(f"Error loading config: {e}")
            print("Using default configuration")
            return self.get_default_config()
    
    def _validate_and_fix_config(self, config):
        """Validate configuration and fix common issues"""
        print("Validating configuration...")
        
        # Ensure required sections exist
        required_sections = [
            'camera', 'recognition', 'enrollment', 'mediapipe', 
            'ui', 'database', 'display', 'performance', 'features'
        ]
        
        for section in required_sections:
            if section not in config:
                print(f"Warning: Missing config section '{section}', adding defaults")
                config[section] = self._get_default_section(section)
        
        # Validate UI config specifically for debug setting
        if 'ui' not in config:
            config['ui'] = {}
        
        # Ensure debug setting is boolean
        debug_setting = config['ui'].get('enable_debug_output')
        if not isinstance(debug_setting, bool):
            config['ui']['enable_debug_output'] = False
            print("Warning: Fixed enable_debug_output to boolean false")
        
        print("Configuration validation complete")
        return config
    
    def _get_default_section(self, section):
        """Get default configuration for a specific section"""
        defaults = {
            'camera': {
                'use_high_resolution': True,
                'high_res_width': 1024,
                'high_res_height': 768,
                'standard_res_width': 640,
                'standard_res_height': 480,
                'processing_width': 640,
                'processing_height': 480,
                'framerate': 25,
                'max_buffers': 2
            },
            'recognition': {
                'recognition_threshold': 0.65,
                'strict_mode_threshold': 0.75,
                'min_feature_similarity': 0.7,
                'validation_samples': 3,
                'min_feature_length': 50
            },
            'ui': {
                'greeting_cooldown': 25.0,
                'enrollment_prompt_cooldown': 15.0,
                'flash_duration': 0.5,
                'fps_update_interval': 30,
                'enable_debug_output': False
            },
            'features': {
                'auto_enrollment': True,
                'manual_pose_control': True,
                'flash_effects': True,
                'high_resolution_support': True,
                'validation_history': True,
                'servo_tracking': False
            },
            'performance': {
                'target_fps': 30,
                'processing_sleep': 0.033,
                'frame_drop_threshold': 5,
                'memory_cleanup_interval': 100
            }
        }
        return defaults.get(section, {})
    
    def _save_config(self, config, config_path):
        """Save configuration to file"""
        try:
            with open(config_path, 'w') as f:
                json.dump(config, f, indent=4)
            print(f"Configuration saved to {config_path}")
        except Exception as e:
            print(f"Warning: Could not save config: {e}")
    
    def get_default_config(self):
        """Complete default configuration"""
        return {
            "camera": {"use_high_resolution": True, "high_res_width": 1024, "high_res_height": 768},
            "recognition": {"recognition_threshold": 0.65, "strict_mode_threshold": 0.75},
            "ui": {"greeting_cooldown": 25.0, "flash_duration": 0.5, "enable_debug_output": False},
            "database": {"faces_db_path": "data/known_faces.pkl"},
            "features": {"auto_enrollment": True, "servo_tracking": False},
            "performance": {"processing_sleep": 0.033, "memory_cleanup_interval": 100}
        }
    
    def apply_command_line_overrides(self, system, args):
        """Apply command line argument overrides to system configuration"""
        config_modified = False
        
        # Resolution overrides
        if args.low_res:
            system.config['camera']['use_high_resolution'] = False
            print("Forced low resolution mode")
            config_modified = True
        elif args.high_res:
            system.config['camera']['use_high_resolution'] = True
            print("Forced high resolution mode")
            config_modified = True
        
        # Feature toggles
        if args.no_servo:
            system.config['features']['servo_tracking'] = False
            print("Servo tracking disabled")
            config_modified = True
        
        if args.no_chat:
            system.config['openwebui_chat']['enabled'] = False
            system.config['openwebui_direct']['enabled'] = False
            print("Chat integration disabled")
            config_modified = True
        
        if args.no_tts:
            system.config['tts_speaker']['enabled'] = False
            print("TTS integration disabled")
            config_modified = True
        
        # Debug toggles - COMPLETELY FIXED!
        if args.enable_debug:
            system.config['ui']['enable_debug_output'] = True
            print("Debug output ENABLED")
            config_modified = True
        elif args.disable_debug:
            system.config['ui']['enable_debug_output'] = False
            print("Debug output DISABLED")
            config_modified = True
        
        # Recognition thresholds
        if args.recognition_threshold:
            if 0.1 <= args.recognition_threshold <= 0.9:
                system.config['recognition']['recognition_threshold'] = args.recognition_threshold
                system.face_recognition.recognition_threshold = args.recognition_threshold
                print(f"Recognition threshold set to: {args.recognition_threshold}")
                config_modified = True
            else:
                print("Warning: Recognition threshold must be between 0.1 and 0.9")
        
        # Save modified config
        if config_modified:
            try:
                with open('config.json', 'w') as f:
                    json.dump(system.config, f, indent=4)
                print("Configuration changes saved to config.json")
            except Exception as e:
                print(f"Warning: Could not save config changes: {e}")
        
        return config_modified

# debug_utils_merged
def should_debug(config):
    """Check if debug output is enabled"""
    return config.get('ui', {}).get('enable_debug_output', False)

def debug_print(config, message):
    """Print debug message only if debug is enabled"""
    if should_debug(config):
        print(message)

def conditional_print(config, message, always_print=False):
    """Print message conditionally based on debug setting"""
    if always_print or should_debug(config):
        print(message)
