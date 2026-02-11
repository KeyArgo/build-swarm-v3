"""
Release management for Build Swarm v3.

Handles staging → versioned release snapshots → promotion via symlink swap.
All filesystem operations are local to the control plane host.
"""

import glob as _glob
import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config as cfg

log = logging.getLogger('swarm-v3')


class ReleaseManager:
    """Manage versioned binary package releases."""

    def __init__(self, db):
        self.db = db
        self.releases_base = cfg.RELEASES_BASE_PATH
        self.binhost_symlink = cfg.BINHOST_SYMLINK_PATH
        self.staging_path = cfg.BINHOST_PRIMARY_PATH or '/var/cache/binpkgs'

    # ── Public API ──────────────────────────────────────────────────

    def list_releases(self) -> list:
        """List all non-deleted releases."""
        rows = self.db.fetchall(
            "SELECT * FROM releases WHERE status != 'deleted' ORDER BY created_at DESC"
        )
        return [dict(r) for r in rows]

    def get_release(self, version: str) -> Optional[dict]:
        """Get release details."""
        row = self.db.fetchone(
            "SELECT * FROM releases WHERE version = ? AND status != 'deleted'",
            (version,))
        if not row:
            return None
        result = dict(row)
        # Add live filesystem stats if directory exists
        if os.path.isdir(result['path']):
            pkgs = self._scan_packages(result['path'])
            result['package_count_live'] = len(pkgs)
            result['size_mb_live'] = round(
                sum(p['size_bytes'] for p in pkgs) / 1048576, 1)
        return result

    def get_release_packages(self, version: str) -> list:
        """List packages in a release directory."""
        row = self.db.fetchone(
            "SELECT path FROM releases WHERE version = ? AND status != 'deleted'",
            (version,))
        if not row:
            return []
        return self._scan_packages(row['path'])

    def create_release(self, version: str = None, name: str = None,
                       notes: str = None, created_by: str = 'api') -> dict:
        """Create a new release by hardlinking staging packages."""
        # Determine staging source
        staging = self._resolve_staging()
        if not os.path.isdir(staging):
            return {'status': 'error', 'error': f'Staging directory not found: {staging}'}

        staging_pkgs = self._scan_packages(staging)
        if not staging_pkgs:
            return {'status': 'error', 'error': 'No packages in staging'}

        # Auto-generate version if not provided
        if not version:
            version = self._generate_version()

        # Ensure releases base directory exists
        os.makedirs(self.releases_base, exist_ok=True)

        release_dir = os.path.join(self.releases_base, version)
        if os.path.exists(release_dir):
            return {'status': 'error', 'error': f'Release directory already exists: {version}'}

        # Check DB for duplicate
        existing = self.db.fetchone(
            "SELECT id FROM releases WHERE version = ?", (version,))
        if existing:
            return {'status': 'error', 'error': f'Release version already exists: {version}'}

        # Hardlink the entire staging tree
        try:
            file_count, total_bytes = self._hardlink_tree(staging, release_dir)
        except Exception as e:
            # Clean up partial release
            if os.path.exists(release_dir):
                shutil.rmtree(release_dir, ignore_errors=True)
            return {'status': 'error', 'error': f'Hardlink failed: {e}'}

        size_mb = round(total_bytes / 1048576, 1)

        # Write manifest
        manifest = {
            'version': version,
            'name': name,
            'package_count': file_count,
            'size_mb': size_mb,
            'created_at': time.time(),
            'created_by': created_by,
            'notes': notes,
        }
        self._write_manifest(release_dir, manifest)

        # Insert DB record
        self.db.execute("""
            INSERT INTO releases (version, name, status, package_count, size_mb,
                                  path, created_at, created_by, notes)
            VALUES (?, ?, 'staging', ?, ?, ?, ?, ?, ?)
        """, (version, name, file_count, size_mb, release_dir,
              time.time(), created_by, notes))

        # Emit event
        self._emit_event('release', f'Release {version} created ({file_count} packages, {size_mb} MB)')

        log.info(f"Created release {version}: {file_count} packages, {size_mb} MB")
        return {
            'status': 'ok',
            'version': version,
            'package_count': file_count,
            'size_mb': size_mb,
            'path': release_dir,
        }

    def promote_release(self, version: str) -> dict:
        """Make a release the active one (swap symlink)."""
        row = self.db.fetchone(
            "SELECT * FROM releases WHERE version = ? AND status != 'deleted'",
            (version,))
        if not row:
            return {'status': 'error', 'error': f'Release not found: {version}'}
        if row['status'] == 'active':
            return {'status': 'error', 'error': f'Release {version} is already active'}
        if not os.path.isdir(row['path']):
            return {'status': 'error', 'error': f'Release directory missing: {row["path"]}'}

        # Archive current active release
        active = self.db.fetchone(
            "SELECT * FROM releases WHERE status = 'active'")
        if active:
            self.db.execute(
                "UPDATE releases SET status = 'archived', archived_at = ? WHERE id = ?",
                (time.time(), active['id']))

        # Atomic symlink swap
        try:
            self._atomic_symlink(row['path'], self.binhost_symlink)
        except Exception as e:
            return {'status': 'error', 'error': f'Symlink swap failed: {e}'}

        # Update DB
        self.db.execute(
            "UPDATE releases SET status = 'active', promoted_at = ? WHERE id = ?",
            (time.time(), row['id']))

        self._emit_event('release', f'Release {version} promoted to active')
        log.info(f"Promoted release {version} to active")
        return {'status': 'ok', 'version': version, 'previous': active['version'] if active else None}

    def rollback(self) -> dict:
        """Switch to the most recently promoted archived release."""
        previous = self.db.fetchone("""
            SELECT * FROM releases
            WHERE status = 'archived' AND promoted_at IS NOT NULL
            ORDER BY promoted_at DESC LIMIT 1
        """)
        if not previous:
            return {'status': 'error', 'error': 'No previous release to rollback to'}
        return self.promote_release(previous['version'])

    def archive_release(self, version: str) -> dict:
        """Mark a release as archived."""
        row = self.db.fetchone(
            "SELECT * FROM releases WHERE version = ? AND status != 'deleted'",
            (version,))
        if not row:
            return {'status': 'error', 'error': f'Release not found: {version}'}
        if row['status'] == 'archived':
            return {'status': 'ok', 'version': version, 'message': 'Already archived'}

        # If archiving the active release, warn but allow
        self.db.execute(
            "UPDATE releases SET status = 'archived', archived_at = ? WHERE id = ?",
            (time.time(), row['id']))

        if row['status'] == 'active':
            # Remove symlink since nothing is active now
            log.warning(f"Archived active release {version} — no release is now active")

        self._emit_event('release', f'Release {version} archived')
        return {'status': 'ok', 'version': version}

    def delete_release(self, version: str) -> dict:
        """Delete an archived release from disk and database."""
        row = self.db.fetchone(
            "SELECT * FROM releases WHERE version = ?", (version,))
        if not row:
            return {'status': 'error', 'error': f'Release not found: {version}'}
        if row['status'] == 'active':
            return {'status': 'error', 'error': 'Cannot delete the active release'}
        if row['status'] not in ('archived', 'staging'):
            return {'status': 'error', 'error': f'Can only delete archived/staging releases (status={row["status"]})'}

        # Remove from disk
        if os.path.isdir(row['path']):
            try:
                shutil.rmtree(row['path'])
            except Exception as e:
                return {'status': 'error', 'error': f'Failed to remove directory: {e}'}

        # Mark deleted in DB
        self.db.execute(
            "UPDATE releases SET status = 'deleted' WHERE id = ?", (row['id'],))

        self._emit_event('release', f'Release {version} deleted')
        log.info(f"Deleted release {version}")
        return {'status': 'ok', 'version': version}

    def diff_releases(self, from_version: str, to_version: str) -> dict:
        """Compare packages between two releases."""
        from_pkgs = {f"{p['category']}/{p['package']}-{p['version']}": p
                     for p in self.get_release_packages(from_version)}
        to_pkgs = {f"{p['category']}/{p['package']}-{p['version']}": p
                   for p in self.get_release_packages(to_version)}

        if not from_pkgs and not to_pkgs:
            return {'status': 'error', 'error': 'Could not read packages from either release'}

        # Compare by category/package (ignoring version for "changed" detection)
        from_by_cp = {}
        for key, p in from_pkgs.items():
            cp = f"{p['category']}/{p['package']}"
            from_by_cp[cp] = p

        to_by_cp = {}
        for key, p in to_pkgs.items():
            cp = f"{p['category']}/{p['package']}"
            to_by_cp[cp] = p

        added = []
        removed = []
        changed = []
        unchanged = 0

        for cp, p in to_by_cp.items():
            if cp not in from_by_cp:
                added.append(p)
            elif p['version'] != from_by_cp[cp]['version']:
                changed.append({
                    'category': p['category'],
                    'package': p['package'],
                    'from_version': from_by_cp[cp]['version'],
                    'to_version': p['version'],
                })
            else:
                unchanged += 1

        for cp, p in from_by_cp.items():
            if cp not in to_by_cp:
                removed.append(p)

        return {
            'from': from_version,
            'to': to_version,
            'added': added,
            'removed': removed,
            'changed': changed,
            'unchanged_count': unchanged,
            'summary': {
                'added': len(added),
                'removed': len(removed),
                'changed': len(changed),
                'unchanged': unchanged,
            }
        }

    def get_binhost_status(self) -> dict:
        """Enhanced binhost status for monitoring."""
        active = self.db.fetchone(
            "SELECT * FROM releases WHERE status = 'active'")
        releases = self.db.fetchall(
            "SELECT version, status, package_count, size_mb, created_at, promoted_at "
            "FROM releases WHERE status != 'deleted' ORDER BY created_at DESC")

        # Staging stats — show the symlink path (what drones upload to)
        staging_display = self.staging_path
        staging_real = self._resolve_staging()
        staging_pkgs = []
        staging_size = 0
        if os.path.isdir(staging_real):
            staging_pkgs = self._scan_packages(staging_real)
            staging_size = sum(p['size_bytes'] for p in staging_pkgs)

        # Symlink target
        symlink_target = None
        if os.path.islink(self.binhost_symlink):
            symlink_target = os.readlink(self.binhost_symlink)

        return {
            'active_release': dict(active) if active else None,
            'staging_packages': len(staging_pkgs),
            'staging_size_mb': round(staging_size / 1048576, 1),
            'staging_path': staging_display,
            'total_releases': len(releases),
            'releases': [dict(r) for r in releases],
            'symlink': self.binhost_symlink,
            'symlink_target': symlink_target,
            'releases_base': self.releases_base,
        }

    def migrate_to_release_system(self) -> dict:
        """One-time migration from flat binpkgs dir to release-based layout."""
        binhost = self.binhost_symlink

        # Already migrated?
        if os.path.islink(binhost):
            target = os.readlink(binhost)
            return {'status': 'error',
                    'error': f'Already migrated: {binhost} is a symlink → {target}'}

        if not os.path.isdir(binhost):
            return {'status': 'error',
                    'error': f'Binhost directory not found: {binhost}'}

        # Create releases base
        os.makedirs(self.releases_base, exist_ok=True)

        initial_dir = os.path.join(self.releases_base, 'initial')
        if os.path.exists(initial_dir):
            return {'status': 'error',
                    'error': f'Initial release dir already exists: {initial_dir}'}

        # Move current binpkgs to releases/initial/
        try:
            os.rename(binhost, initial_dir)
        except OSError as e:
            return {'status': 'error', 'error': f'Failed to move binpkgs: {e}'}

        # Create symlink: /var/cache/binpkgs → releases/initial
        try:
            os.symlink(initial_dir, binhost)
        except OSError as e:
            # Rollback: move it back
            os.rename(initial_dir, binhost)
            return {'status': 'error', 'error': f'Symlink creation failed: {e}'}

        # Scan the initial release
        pkgs = self._scan_packages(initial_dir)
        total_size = sum(p['size_bytes'] for p in pkgs)
        size_mb = round(total_size / 1048576, 1)

        # Write manifest
        manifest = {
            'version': 'initial',
            'name': 'Initial migration',
            'package_count': len(pkgs),
            'size_mb': size_mb,
            'created_at': time.time(),
            'created_by': 'migration',
            'notes': 'Auto-created from existing binpkgs directory',
        }
        self._write_manifest(initial_dir, manifest)

        # Insert DB record
        self.db.execute("""
            INSERT OR IGNORE INTO releases (version, name, status, package_count, size_mb,
                                            path, created_at, promoted_at, created_by, notes)
            VALUES ('initial', 'Initial migration', 'active', ?, ?, ?, ?, ?, 'migration',
                    'Auto-created from existing binpkgs directory')
        """, (len(pkgs), size_mb, initial_dir, time.time(), time.time()))

        self._emit_event('release', f'Migrated to release system: initial ({len(pkgs)} packages, {size_mb} MB)')

        log.info(f"Migrated to release system: {binhost} → {initial_dir} ({len(pkgs)} packages)")
        return {
            'status': 'ok',
            'version': 'initial',
            'package_count': len(pkgs),
            'size_mb': size_mb,
            'path': initial_dir,
            'symlink': f'{binhost} → {initial_dir}',
        }

    # ── Private Helpers ─────────────────────────────────────────────

    def _resolve_staging(self) -> str:
        """Resolve the actual staging directory (follow symlinks)."""
        staging = self.staging_path
        if os.path.islink(staging):
            staging = os.path.realpath(staging)
        return staging

    def _generate_version(self) -> str:
        """Generate YYYY.MM.DD[.N] version string."""
        base = datetime.now().strftime('%Y.%m.%d')
        version = base
        n = 2
        while self.db.fetchone(
                "SELECT id FROM releases WHERE version = ?", (version,)):
            version = f'{base}.{n}'
            n += 1
        return version

    def _atomic_symlink(self, target: str, link_path: str):
        """Atomically replace a symlink using tmp+rename."""
        tmp = link_path + '.tmp.' + str(os.getpid())
        try:
            # Remove stale tmp if exists
            if os.path.islink(tmp) or os.path.exists(tmp):
                os.remove(tmp)
            os.symlink(target, tmp)
            os.rename(tmp, link_path)
        except Exception:
            # Clean up tmp on failure
            if os.path.islink(tmp):
                os.remove(tmp)
            raise

    def _hardlink_tree(self, src: str, dst: str) -> tuple:
        """Recursively hardlink all files from src to dst.

        Returns (file_count, total_bytes).
        """
        file_count = 0
        total_bytes = 0

        for dirpath, dirnames, filenames in os.walk(src):
            # Create corresponding directory in dst
            rel = os.path.relpath(dirpath, src)
            dst_dir = os.path.join(dst, rel)
            os.makedirs(dst_dir, exist_ok=True)

            for filename in filenames:
                src_file = os.path.join(dirpath, filename)
                dst_file = os.path.join(dst_dir, filename)

                try:
                    # Try hardlink first (zero copy)
                    os.link(src_file, dst_file)
                except OSError:
                    # Fall back to copy if cross-device
                    shutil.copy2(src_file, dst_file)

                file_count += 1
                try:
                    total_bytes += os.path.getsize(dst_file)
                except OSError:
                    pass

        return file_count, total_bytes

    def _scan_packages(self, directory: str) -> list:
        """Walk a directory and return list of .gpkg.tar files with metadata."""
        if not os.path.isdir(directory):
            return []

        packages = []
        for filepath in _glob.glob(os.path.join(directory, '**/*.gpkg.tar'), recursive=True):
            rel = os.path.relpath(filepath, directory)
            parts = rel.split(os.sep)

            # Expected: category/package-version.gpkg.tar
            if len(parts) >= 2:
                category = parts[0]
                filename = parts[-1]
                # Strip .gpkg.tar
                pv = filename.replace('.gpkg.tar', '')
                # Split package-version (last hyphen before a digit)
                pkg_name = pv
                pkg_version = ''
                for i in range(len(pv) - 1, 0, -1):
                    if pv[i - 1] == '-' and pv[i].isdigit():
                        pkg_name = pv[:i - 1]
                        pkg_version = pv[i:]
                        break
            else:
                category = ''
                pkg_name = os.path.basename(filepath).replace('.gpkg.tar', '')
                pkg_version = ''

            try:
                size = os.path.getsize(filepath)
            except OSError:
                size = 0

            packages.append({
                'category': category,
                'package': pkg_name,
                'version': pkg_version,
                'size_bytes': size,
                'path': rel,
            })

        packages.sort(key=lambda p: (p['category'], p['package']))
        return packages

    def _write_manifest(self, release_dir: str, data: dict):
        """Write release.json manifest into the release directory."""
        manifest_path = os.path.join(release_dir, 'release.json')
        try:
            with open(manifest_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.warning(f"Failed to write manifest: {e}")

    def _emit_event(self, event_type: str, message: str):
        """Emit an event to the events table."""
        try:
            self.db.execute("""
                INSERT INTO events (timestamp, event_type, message)
                VALUES (?, ?, ?)
            """, (time.time(), event_type, message))
        except Exception as e:
            log.debug(f"Event emission failed: {e}")
