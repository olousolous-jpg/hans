"""
Database Management Module
Handles face database operations, loading, saving, and CRUD operations.
"""

import os
import pickle
import shutil
from pathlib import Path
import time


# # p3_db_cleaned
class DatabaseManager:
    """Manages face database operations"""
    
    def __init__(self, config):
        """Initialize database manager with configuration"""
        self.config = config
        self.db_config = config['database']
        
        self.faces_db_path = self.db_config['faces_db_path']
        self.backup_on_save = self.db_config.get('backup_on_save', True)
        self.max_backups = self.db_config.get('max_backups', 5)
        
        # Ensure data directory exists
        Path(self.faces_db_path).parent.mkdir(parents=True, exist_ok=True)
        
        # Load existing database
        self.known_faces = self.load_face_database()
        
        print(f"ðŸ’¾ Database Manager initialized")
        print(f"ðŸ“ Database path: {self.faces_db_path}")
        print(f"ðŸ‘¥ Known faces: {len(self.known_faces)}")
    
    def load_face_database(self):
        """Load known faces from file"""
        if os.path.exists(self.faces_db_path):
            try:
                with open(self.faces_db_path, 'rb') as f:
                    faces_db = pickle.load(f)
                    print(f"ðŸ“š Loaded {len(faces_db)} known faces from database")
                    for name in faces_db.keys():
                        print(f"   - {name}")
                    return faces_db
            except Exception as e:
                print(f"âš ï¸ Error loading face database: {e}")
                # Try to load backup
                backup_loaded = self._try_load_backup()
                if backup_loaded:
                    return backup_loaded
        
        print("ðŸ“š No existing face database found, creating new one")
        return {}
    
    def _try_load_backup(self):
        """Try to load from backup files"""
        backup_dir = Path(self.faces_db_path).parent / "backups"
        if not backup_dir.exists():
            return None
        
        backup_files = sorted(backup_dir.glob("*.pkl"), key=os.path.getmtime, reverse=True)
        
        for backup_file in backup_files:
            try:
                with open(backup_file, 'rb') as f:
                    faces_db = pickle.load(f)
                    print(f"âœ… Loaded backup from {backup_file}")
                    return faces_db
            except Exception as e:
                print(f"âš ï¸ Failed to load backup {backup_file}: {e}")
                continue
        
        return None
    
    def save_face_database(self):
        """Save known faces to file with backup"""
        try:
            # Create backup if enabled
            if self.backup_on_save and os.path.exists(self.faces_db_path):
                self._create_backup()
            
            # Save main database
            with open(self.faces_db_path, 'wb') as f:
                pickle.dump(self.known_faces, f)
            
            print(f"ðŸ’¾ Saved face database with {len(self.known_faces)} faces")
            return True
            
        except Exception as e:
            print(f"âš ï¸ Error saving face database: {e}")
            return False
    
    def _create_backup(self):
        """Create backup of current database"""
        try:
            backup_dir = Path(self.faces_db_path).parent / "backups"
            backup_dir.mkdir(exist_ok=True)
            
            timestamp = int(time.time())
            backup_filename = f"known_faces_backup_{timestamp}.pkl"
            backup_path = backup_dir / backup_filename
            
            shutil.copy2(self.faces_db_path, backup_path)
            print(f"ðŸ”„ Created backup: {backup_filename}")
            
            # Clean old backups
            self._cleanup_old_backups(backup_dir)
            
        except Exception as e:
            print(f"âš ï¸ Failed to create backup: {e}")
    
    def _cleanup_old_backups(self, backup_dir):
        """Remove old backup files beyond max_backups limit"""
        try:
            backup_files = sorted(backup_dir.glob("*.pkl"), key=os.path.getmtime, reverse=True)
            
            if len(backup_files) > self.max_backups:
                for old_backup in backup_files[self.max_backups:]:
                    os.remove(old_backup)
                    print(f"ðŸ—‘ï¸ Removed old backup: {old_backup.name}")
                    
        except Exception as e:
            print(f"âš ï¸ Error cleaning old backups: {e}")
    def add_face_encoding(self, name, embedding):
        """Add a single 128-d dlib embedding (appends to list).
        Used by PicamDisplayController via FaceDB."""
        if not name or embedding is None:
            return False

        if name not in self.known_faces:
            self.known_faces[name] = []

        # Migrate legacy single-vector entries
        if not isinstance(self.known_faces[name], list):
            self.known_faces[name] = [self.known_faces[name]]

        self.known_faces[name].append(embedding)
        success = self.save_face_database()

        if success:
            print(f"+ Added encoding for '{name}' "
                  f"({len(self.known_faces[name])} sample(s) total)")
        return success

    def get_all_encodings(self):
        """Return { name: [list_of_embeddings] } for use with fr.face_distance().
        Wraps legacy single-vector entries in a list automatically."""
        result = {}
        for name, data in self.known_faces.items():
            if isinstance(data, list):
                result[name] = data
            else:
                result[name] = [data]
        return result

    def remove_face(self, name):
        """Remove a face from the database"""
        if name not in self.known_faces:
            print(f"âŒ '{name}' not found in database")
            return False
        
        del self.known_faces[name]
        success = self.save_face_database()
        
        if success:
            print(f"âž– Removed '{name}' from face database")
        
        return success
    
    def update_face(self, name, features):
        """Update an existing face in the database"""
        if name not in self.known_faces:
            print(f"âŒ '{name}' not found in database")
            return False
        
        self.known_faces[name] = features
        success = self.save_face_database()
        
        if success:
            print(f"ðŸ”„ Updated '{name}' in face database")
        
        return success
    
    def get_face(self, name):
        """Get face features for a specific name"""
        return self.known_faces.get(name, None)
    
    def list_faces(self):
        """List all known faces"""
        if self.known_faces:
            print(f"ðŸ“š Known faces ({len(self.known_faces)}):")
            for name in sorted(self.known_faces.keys()):
                print(f"   - {name}")
        else:
            print("ðŸ“š No faces in database")
        
        return list(self.known_faces.keys())
    
    def clear_database(self):
        """Clear entire face database"""
        self.known_faces = {}
        success = self.save_face_database()
        
        if success:
            print("ðŸ—‘ï¸ Face database cleared")
        
        return success
    
    def get_database_stats(self):
        """Get database statistics"""
        stats = {
            'total_faces': len(self.known_faces),
            'database_path': self.faces_db_path,
            'database_exists': os.path.exists(self.faces_db_path),
            'database_size': 0,
            'backup_enabled': self.backup_on_save
        }
        
        if stats['database_exists']:
            try:
                stats['database_size'] = os.path.getsize(self.faces_db_path)
                stats['last_modified'] = time.ctime(os.path.getmtime(self.faces_db_path))
            except:
                pass
        
        # Check for backups
        backup_dir = Path(self.faces_db_path).parent / "backups"
        if backup_dir.exists():
            backup_files = list(backup_dir.glob("*.pkl"))
            stats['backup_count'] = len(backup_files)
        else:
            stats['backup_count'] = 0
        
        return stats
# database_operations_merged
class DatabaseOperations:
    """Handles database operations that can be run from command line"""
    
    def __init__(self):
        """Initialize database operations handler"""
        pass
    
    def handle_database_operations(self, args):
        """Handle database operations that should run and exit"""
        # Only create database manager if needed
        if args.list_faces or args.clear_database:
            # Create a minimal config for database operations
            minimal_config = {
                'database': {
                    'faces_db_path': 'data/known_faces.pkl',
                    'backup_on_save': True,
                    'max_backups': 5
                }
            }
            
            database_manager = DatabaseManager(minimal_config)
            
            if args.list_faces:
                return self._list_faces(database_manager)
            
            if args.clear_database:
                return self._clear_database(database_manager)
        
        return False
    
    def _list_faces(self, database_manager):
        """List all faces in the database"""
        print("\nKnown faces in database:")
        faces = database_manager.list_faces()
        if not faces:
            print("  No faces in database")
        else:
            print(f"  Total: {len(faces)} faces")
            for i, name in enumerate(faces, 1):
                print(f"  {i:2d}. {name}")
        
        # Show database statistics
        stats = database_manager.get_database_stats()
        print(f"\nDatabase Statistics:")
        print(f"  Database file: {stats['database_path']}")
        print(f"  File exists: {stats['database_exists']}")
        if stats['database_exists']:
            print(f"  File size: {stats.get('database_size', 0)} bytes")
            if 'last_modified' in stats:
                print(f"  Last modified: {stats['last_modified']}")
        print(f"  Backup enabled: {stats['backup_enabled']}")
        print(f"  Available backups: {stats.get('backup_count', 0)}")
        
        return True
    
    def _clear_database(self, database_manager):
        """Clear the entire face database"""
        print("\nClear Face Database")
        print("==================")
        
        # Show current database status
        faces = list(database_manager.known_faces.keys())
        if not faces:
            print("Database is already empty.")
            return True
        
        print(f"Current database contains {len(faces)} faces:")
        for i, name in enumerate(faces, 1):
            print(f"  {i:2d}. {name}")
        
        print("\nThis will permanently delete all face data!")
        print("A backup will be created automatically.")
        
        confirm = input("\nAre you sure you want to clear the entire database? Type 'YES' to confirm: ")
        if confirm == 'YES':
            if database_manager.clear_database():
                print("OK Face database cleared successfully")
                print("OK Backup created automatically")
            else:
                print("FAIL Failed to clear database")
        else:
            print("Database clear cancelled")
        
        return True
