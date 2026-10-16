import os
import glob
import shutil
from typing import Callable, Optional, Tuple

# 16 MiB strikes a balance between syscall overhead and how often a caller
# can refresh a progress bar; it matters most here because the destination is
# often a cloud-sync virtual filesystem (see copy_file_with_progress), where
# each write can be far slower than on local disk.
_COPY_CHUNK_SIZE = 16 * 1024 * 1024


def copy_file_with_progress(
    src: str,
    dst: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    chunk_size: int = _COPY_CHUNK_SIZE,
) -> None:
    """Copies `src` to `dst` in chunks, reporting bytes copied so far.

    Plain `shutil.copy2` gives no feedback until it returns, which for a
    multi-gigabyte file written directly into a Google Drive-mounted folder
    can look identical to a hang for a very long time. Chunking the copy lets
    a caller show real progress instead.

    Writes to a temporary file in the SAME directory as `dst` and atomically
    renames it into place once complete, so an interrupted copy or a sync
    daemon error mid-write never leaves a truncated file at the destination.
    """
    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    total = os.path.getsize(src)
    tmp_dst = dst + ".tmp"

    copied = 0
    with open(src, "rb") as fsrc, open(tmp_dst, "wb") as fdst:
        while True:
            chunk = fsrc.read(chunk_size)
            if not chunk:
                break
            fdst.write(chunk)
            copied += len(chunk)
            if progress_callback:
                progress_callback(copied, total)

    try:
        shutil.copystat(src, tmp_dst)
    except OSError:
        # Some cloud-sync filesystems reject metadata-only operations
        # (utime/chmod) even though the write itself succeeded -- the copy
        # is still complete and correct without it.
        pass

    os.replace(tmp_dst, dst)

def find_default_macos_gdrive() -> Optional[str]:
    """Finds active Google Drive path on macOS under ~/Library/CloudStorage/."""
    cloud_storage = os.path.expanduser("~/Library/CloudStorage")
    if not os.path.exists(cloud_storage):
        return None

    matches = glob.glob(os.path.join(cloud_storage, "GoogleDrive-*"))
    for match in matches:
        my_drive = os.path.join(match, "My Drive")
        if os.path.isdir(my_drive):
            return my_drive
        if os.path.isdir(match):
            return match
    return None

class GDriveManager:
    def __init__(self, gdrive_arg: Optional[str] = None, offload_cache: bool = False):
        self.enabled = False
        self.base_path = None
        self.output_dir = None
        self.cache_dir = None
        self.offload_cache = offload_cache

        if gdrive_arg:
            if gdrive_arg.lower() == "auto":
                detected = find_default_macos_gdrive()
                if detected:
                    self.base_path = os.path.join(detected, "MTG_Proxies")
                    self.enabled = True
                    print(f"  [GDrive] Auto-detected Google Drive at: {detected}")
                else:
                    print("  [!] Could not auto-detect Google Drive directory under ~/Library/CloudStorage")
            else:
                expanded = os.path.expanduser(gdrive_arg)
                if os.path.exists(expanded):
                    self.base_path = os.path.join(expanded, "MTG_Proxies")
                    self.enabled = True
                    print(f"  [GDrive] Using configured Google Drive path: {expanded}")
                else:
                    print(f"  [!] Specified Google Drive directory does not exist: {expanded}")

        if self.enabled and self.base_path:
            self.output_dir = os.path.join(self.base_path, "output")
            self.cache_dir = os.path.join(self.base_path, "cache")
            os.makedirs(self.output_dir, exist_ok=True)
            if self.offload_cache:
                os.makedirs(self.cache_dir, exist_ok=True)

    def route_output_path(self, local_filename: str) -> str:
        """If Google Drive is enabled, writes directly to Google Drive output folder."""
        if self.enabled and self.output_dir:
            return os.path.join(self.output_dir, os.path.basename(local_filename))
        return local_filename

    def sync_file_to_drive(
        self,
        local_path: str,
        destination_dir_name: str = "output",
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> Optional[str]:
        if not self.enabled or not self.base_path or not os.path.exists(local_path):
            return None
        dest_dir = os.path.join(self.base_path, destination_dir_name)
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, os.path.basename(local_path))
        copy_file_with_progress(local_path, dest_path, progress_callback=progress_callback)
        return dest_path

    def offload_cache_file(self, local_cache_file: str) -> Optional[str]:
        """Moves or copies a cached high-res image to Google Drive to save local storage."""
        if not self.enabled or not self.cache_dir or not os.path.exists(local_cache_file):
            return None
        dest_path = os.path.join(self.cache_dir, os.path.basename(local_cache_file))
        shutil.copy2(local_cache_file, dest_path)
        return dest_path
