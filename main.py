#!/usr/bin/env python3
"""
ENHANCED Face Recognition System - Refactored Main Module
Simplified main entry point with logic moved to appropriate modules
"""

import sys
import json
import argparse
import threading
from pathlib import Path

# Add scripts directory to path
sys.path.append(str(Path(__file__).parent / "scripts"))

# Import system manager and utilities
from scripts.system_manager import FaceRecognitionSystem
from scripts.config_manager import ConfigManager
from scripts.database_manager import DatabaseOperations


# argument_parser_merged


import argparse


class ArgumentParser:
    """Handles command line argument parsing"""
    
    def __init__(self):
        """Initialize argument parser"""
        self.parser = self._create_parser()
    
    def _create_parser(self):
        """Create and configure argument parser"""
        parser = argparse.ArgumentParser(
            description='Enhanced Face Recognition System - Fixed Debug Control'
        )
        
        # Input/Output options
        parser.add_argument('--input', default='rpi', 
                          help='Input source (default: rpi)')
        parser.add_argument('--config', default='config.json', 
                          help='Configuration file path')
        parser.add_argument('--no-display', action='store_true', 
                          help='Run without display window')
        
        # Pipeline options
        parser.add_argument('--mediapipe-only', action='store_true', 
                          help='Use MediaPipe-only mode')
        parser.add_argument('--no-hailo', action='store_true', 
                          help='Skip Hailo pipeline')
        
        # Resolution options
        parser.add_argument('--low-res', action='store_true', 
                          help='Force standard resolution')
        parser.add_argument('--high-res', action='store_true', 
                          help='Force high resolution')
        
        # Feature toggles
        parser.add_argument('--no-servo', action='store_true', 
                          help='Disable servo tracking')
        parser.add_argument('--no-chat', action='store_true', 
                          help='Disable chat integration')
        parser.add_argument('--no-tts', action='store_true', 
                          help='Disable TTS integration')
        
        # Debug controls - FIXED
        parser.add_argument('--enable-debug', action='store_true', 
                          help='Enable debug output')
        parser.add_argument('--disable-debug', action='store_true', 
                          help='Disable debug output')
        
        # Recognition options
        parser.add_argument('--recognition-threshold', type=float, 
                          help='Set recognition confidence threshold (0.1-0.9)')
        
        # Database operations
        parser.add_argument('--list-faces', action='store_true', 
                          help='List known faces and exit')
        parser.add_argument('--clear-database', action='store_true', 
                          help='Clear face database and exit')
        
        return parser
    
    def parse_arguments(self):
        """Parse command line arguments"""
        return self.parser.parse_args()
    
    def validate_arguments(self, args):
        """Validate parsed arguments for consistency"""
        errors = []
        
        # Check for conflicting resolution options
        if args.low_res and args.high_res:
            errors.append("Cannot specify both --low-res and --high-res")
        
        # Check for conflicting debug options
        if args.enable_debug and args.disable_debug:
            errors.append("Cannot specify both --enable-debug and --disable-debug")
        
        # Validate recognition threshold
        if args.recognition_threshold is not None:
            if not (0.1 <= args.recognition_threshold <= 0.9):
                errors.append("Recognition threshold must be between 0.1 and 0.9")
        
        if errors:
            print("Argument validation errors:")
            for error in errors:
                print(f"  - {error}")
            return False
        
        return True

def main():
    """Simplified main execution function"""
    print("ENHANCED Face Recognition System - Refactored Version")
    print("Features:")
    print("    FIXED debug output control - respects config.json setting")
    print("    Manual face enrollment via terminal")
    print("    Configuration via config.json or CLI arguments")
    print("    Component health monitoring")
    print("    Error recovery and graceful degradation")
    print("\nUse --help for all available command line options")
    
    # Parse arguments using dedicated parser
    parser = ArgumentParser()
    args = parser.parse_arguments()
    
    try:
        # Create data directory
        Path("data").mkdir(exist_ok=True)
        
        # Handle database operations first (these may exit early)
        db_ops = DatabaseOperations()
        if db_ops.handle_database_operations(args):
            return 0
        
        # Load and manage configuration
        config_manager = ConfigManager(args.config)
        config = config_manager.load_config()
        
        # Initialize system
        print("\nCreating Enhanced Face Recognition System...")
        system = FaceRecognitionSystem(config)
        
        # Apply command line overrides
        print("\nApplying command line overrides...")
        config_manager.apply_command_line_overrides(system, args)
        
        # Show final configuration status
        system.show_system_status()
        
        # Check for critical component failures
        if not system.validate_critical_components():
            return 1
        
        # Start appropriate mode
        print(f"\nStarting system...")
        return system.start_system(args)
            
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        return 0
    except Exception as e:
        print(f"CRITICAL SYSTEM ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        if 'system' in locals():
            try:
                system.stop()
            except Exception as e:
                print(f"Error during cleanup: {e}")


if __name__ == "__main__":
    exit_code = main()
    if exit_code != 0:
        print(f"System exited with code {exit_code}")
    exit(exit_code)
