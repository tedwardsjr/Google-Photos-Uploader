import os
import time
from pathlib import Path
from watchdog.observers.polling import PollingObserver as Observer
from watchdog.events import FileSystemEventHandler
from gpmc import Client

WATCHED_FOLDER = os.environ.get("WATCHED_FOLDER", "/data")
AUTH_DATA = os.environ.get("AUTH_DATA", "")
UPLOAD_STATUS_INTERVAL = float(os.environ.get("UPLOAD_STATUS_INTERVAL", "3"))
SUPPORTED_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.heic', '.mp4', '.mov')


def format_size(size_bytes):
    size_mb = size_bytes / (1024 * 1024)
    return f"{size_mb:.1f} MB"


class UploadLogProgress:
    def __init__(self, interval=UPLOAD_STATUS_INTERVAL):
        self.interval = interval
        self.description = ""
        self.total = 0
        self.completed = 0
        self.started_at = None
        self.last_logged_at = 0

    def add_task(self, description="", total=None, **_kwargs):
        self.description = description
        self.total = total or 0
        self.completed = 0
        return 1

    def update(self, task_id=None, description=None, total=None, completed=None, visible=True, **_kwargs):
        if description is not None:
            self.description = description
        if total is not None:
            self.total = total
        if completed is not None:
            self.completed = completed

    def reset(self, task_id=None, **_kwargs):
        self.completed = 0
        self.started_at = None
        self.last_logged_at = 0

    def open(self, file_path, mode, task_id=None):
        return ProgressFileReader(self, Path(file_path), mode)

    def record_read(self, file_path, chunk_size):
        if "Uploading:" not in self.description or chunk_size <= 0:
            return

        now = time.monotonic()
        if self.started_at is None:
            self.started_at = now
            self.last_logged_at = now
            self.total = file_path.stat().st_size
            print(f"Uploading started: {file_path} ({format_size(self.total)})", flush=True)

        self.completed += chunk_size
        if now - self.last_logged_at >= self.interval or self.completed >= self.total:
            elapsed = max(now - self.started_at, 0.001)
            speed = self.completed / elapsed / (1024 * 1024)
            percent = min((self.completed / self.total) * 100, 100) if self.total else 0
            print(
                f"Uploading status: {file_path.name} "
                f"{format_size(self.completed)} / {format_size(self.total)} "
                f"({percent:.1f}%) at {speed:.2f} MB/s",
                flush=True,
            )
            self.last_logged_at = now


class ProgressFileReader:
    def __init__(self, progress, file_path, mode):
        self.progress = progress
        self.file_path = file_path
        self.mode = mode
        self.file = None

    def __enter__(self):
        self.file = open(self.file_path, self.mode)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.file.close()

    def __getattr__(self, name):
        return getattr(self.file, name)

    def read(self, size=-1):
        data = self.file.read(size)
        self.progress.record_read(self.file_path, len(data))
        return data

    def readinto(self, buffer):
        bytes_read = self.file.readinto(buffer)
        self.progress.record_read(self.file_path, bytes_read or 0)
        return bytes_read


class PhotoHandler(FileSystemEventHandler):
    def __init__(self):
        super().__init__()
        self.client = Client(auth_data=AUTH_DATA)

    def on_created(self, event):
        if not event.is_directory and event.src_path.lower().endswith(SUPPORTED_EXTENSIONS):
            file_size = os.path.getsize(event.src_path)
            print(f"Found new file: {event.src_path} ({format_size(file_size)})", flush=True)
            try:
                # File upload
                output = self.client._upload_file(
                    file_path=event.src_path,
                    hash_value=None,
                    progress=UploadLogProgress(),
                    force_upload=False,
                    use_quota=False,
                    saver=False,
                )
                print(f"Uploaded: {output}")
                
                # Attempt to delete with 3 retries
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        os.remove(event.src_path)
                        print(f"File removed: {event.src_path}")
                        break
                    except PermissionError:
                        if attempt < max_retries - 1:
                            time.sleep(0.5 * (attempt + 1))
                            continue
                        raise
                        
            except Exception as e:
                print(f"Error: {e}")
                # Cleanup of leftover file in case of error
                if os.path.exists(event.src_path):
                    try:
                        os.remove(event.src_path)
                    except:
                        pass

if __name__ == "__main__":
    event_handler = PhotoHandler()
    observer = Observer()
    observer.schedule(event_handler, WATCHED_FOLDER, recursive=True)
    observer.start()
    print(f"Monitoring started on {WATCHED_FOLDER}...")

    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
