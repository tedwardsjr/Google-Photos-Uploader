import os
import tempfile
import threading
import time
from html import escape
from pathlib import Path
from watchdog.observers.polling import PollingObserver as Observer
from watchdog.events import FileSystemEventHandler
from gpmc import Client
from PIL import Image
from pillow_heif import register_heif_opener

WATCHED_FOLDER = os.environ.get("WATCHED_FOLDER", "/data")
AUTH_DATA = os.environ.get("AUTH_DATA", "")
UPLOAD_STATUS_INTERVAL = float(os.environ.get("UPLOAD_STATUS_INTERVAL", "3"))
LIVE_PHOTO_PAIR_WAIT = float(os.environ.get("LIVE_PHOTO_PAIR_WAIT", "30"))
FILE_STABLE_SECONDS = float(os.environ.get("FILE_STABLE_SECONDS", "2"))
PHOTO_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.heic')
LIVE_PHOTO_STILL_EXTENSIONS = ('.jpg', '.jpeg', '.heic')
VIDEO_EXTENSIONS = ('.mp4', '.mov')
SUPPORTED_EXTENSIONS = PHOTO_EXTENSIONS + VIDEO_EXTENSIONS

register_heif_opener()


def format_size(size_bytes):
    size_mb = size_bytes / (1024 * 1024)
    return f"{size_mb:.1f} MB"


def live_photo_key(file_path):
    stem = Path(file_path).stem.lower()
    if stem.endswith("_hevc"):
        stem = stem[:-5]
    return stem


def build_motion_photo_xmp(filename, video_size, video_mime):
    safe_filename = escape(filename, quote=True)
    return f'''<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Google Photos Uploader">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
   xmlns:GCamera="http://ns.google.com/photos/1.0/camera/"
   xmlns:Container="http://ns.google.com/photos/1.0/container/"
   xmlns:Item="http://ns.google.com/photos/1.0/container/item/"
   GCamera:MotionPhoto="1"
   GCamera:MotionPhotoVersion="1"
   GCamera:MotionPhotoPresentationTimestampUs="0"
   GCamera:MicroVideo="1"
   GCamera:MicroVideoVersion="1"
   GCamera:MicroVideoOffset="{video_size}">
   <Container:Directory>
    <rdf:Seq>
     <rdf:li rdf:parseType="Resource">
      <Container:Item
       Item:Mime="image/jpeg"
       Item:Semantic="Primary"
       Item:Length="0"
       Item:Padding="0"/>
     </rdf:li>
     <rdf:li rdf:parseType="Resource">
      <Container:Item
       Item:Mime="{video_mime}"
       Item:Semantic="MotionPhoto"
       Item:Length="{video_size}"
       Item:Padding="0"/>
     </rdf:li>
    </rdf:Seq>
   </Container:Directory>
   <GCamera:MicroVideoFilename>{safe_filename}</GCamera:MicroVideoFilename>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>'''.encode("utf-8")


def insert_xmp_into_jpeg(jpeg_bytes, xmp_bytes):
    if not jpeg_bytes.startswith(b"\xff\xd8"):
        raise ValueError("Motion Photo still conversion did not produce a JPEG")

    xmp_header = b"http://ns.adobe.com/xap/1.0/\x00"
    segment_payload = xmp_header + xmp_bytes
    segment_length = len(segment_payload) + 2
    if segment_length > 65535:
        raise ValueError("Motion Photo XMP packet is too large for a JPEG APP1 segment")

    app1_segment = b"\xff\xe1" + segment_length.to_bytes(2, "big") + segment_payload
    return jpeg_bytes[:2] + app1_segment + jpeg_bytes[2:]


def create_motion_photo(still_path, video_path):
    video_size = video_path.stat().st_size
    video_mime = "video/quicktime" if video_path.suffix.lower() == ".mov" else "video/mp4"
    with tempfile.NamedTemporaryFile(prefix=f"{still_path.stem}_", suffix="MP.jpg", delete=False) as output_file:
        output_path = Path(output_file.name)

    with Image.open(still_path) as image:
        image = image.convert("RGB")
        exif = image.info.get("exif")
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as jpeg_file:
            jpeg_path = Path(jpeg_file.name)
        save_kwargs = {"format": "JPEG", "quality": 95}
        if exif:
            save_kwargs["exif"] = exif
        image.save(jpeg_path, **save_kwargs)

    try:
        xmp = build_motion_photo_xmp(still_path.name, video_size, video_mime)
        jpeg_with_xmp = insert_xmp_into_jpeg(jpeg_path.read_bytes(), xmp)
        with open(output_path, "wb") as motion_photo_file:
            motion_photo_file.write(jpeg_with_xmp)
            with open(video_path, "rb") as video_file:
                while chunk := video_file.read(1024 * 1024):
                    motion_photo_file.write(chunk)
    finally:
        jpeg_path.unlink(missing_ok=True)

    return output_path


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
        self.lock = threading.Lock()
        self.processing_paths = set()

    def on_created(self, event):
        if not event.is_directory and event.src_path.lower().endswith(SUPPORTED_EXTENSIONS):
            threading.Thread(target=self.process_path, args=(Path(event.src_path),), daemon=True).start()

    def process_path(self, file_path):
        try:
            file_path = Path(file_path)
            if not self.wait_for_stable_file(file_path):
                return

            if file_path.suffix.lower() in LIVE_PHOTO_STILL_EXTENSIONS:
                video_path = self.wait_for_companion(file_path, VIDEO_EXTENSIONS)
                if video_path:
                    self.upload_live_photo_pair(file_path, video_path)
                elif file_path.exists():
                    self.upload_single_file(file_path)
            elif file_path.suffix.lower() in VIDEO_EXTENSIONS:
                still_path = self.wait_for_companion(file_path, LIVE_PHOTO_STILL_EXTENSIONS)
                if still_path:
                    self.upload_live_photo_pair(still_path, file_path)
                elif file_path.exists():
                    self.upload_single_file(file_path)
            else:
                self.upload_single_file(file_path)
        except Exception as e:
            print(f"Error processing {file_path}: {e}", flush=True)

    def wait_for_stable_file(self, file_path):
        last_size = -1
        stable_since = None
        while file_path.exists():
            try:
                current_size = file_path.stat().st_size
            except OSError:
                return False

            now = time.monotonic()
            if current_size == last_size:
                stable_since = stable_since or now
                if now - stable_since >= FILE_STABLE_SECONDS:
                    return True
            else:
                last_size = current_size
                stable_since = None
            time.sleep(0.5)
        return False

    def wait_for_companion(self, file_path, companion_extensions):
        deadline = time.monotonic() + LIVE_PHOTO_PAIR_WAIT
        while time.monotonic() < deadline:
            if not file_path.exists():
                return None
            companion = self.find_companion(file_path, companion_extensions)
            if companion and self.wait_for_stable_file(companion):
                return companion
            time.sleep(1)
        return None

    def find_companion(self, file_path, companion_extensions):
        target_key = live_photo_key(file_path)
        companion_extensions = {extension.lower() for extension in companion_extensions}
        try:
            for candidate in file_path.parent.iterdir():
                if candidate == file_path or not candidate.is_file():
                    continue
                if live_photo_key(candidate) == target_key and candidate.suffix.lower() in companion_extensions:
                    return candidate
        except FileNotFoundError:
            return None
        return None

    def mark_processing(self, *paths):
        absolute_paths = {Path(path).resolve() for path in paths}
        with self.lock:
            if self.processing_paths & absolute_paths:
                return False
            self.processing_paths.update(absolute_paths)
            return True

    def unmark_processing(self, *paths):
        absolute_paths = {Path(path).resolve() for path in paths}
        with self.lock:
            self.processing_paths.difference_update(absolute_paths)

    def upload_live_photo_pair(self, still_path, video_path):
        if not self.mark_processing(still_path, video_path):
            return

        motion_photo_path = None
        try:
            print(f"Found Live Photo pair: {still_path} + {video_path}", flush=True)
            motion_photo_path = create_motion_photo(still_path, video_path)
            print(
                f"Created Google Motion Photo: {motion_photo_path} "
                f"({format_size(motion_photo_path.stat().st_size)})",
                flush=True,
            )
            uploaded = self.upload_single_file(
                motion_photo_path,
                delete_after_upload=True,
                cleanup_paths=[motion_photo_path, still_path, video_path],
            )
            if not uploaded:
                motion_photo_path.unlink(missing_ok=True)
        except Exception as e:
            print(f"Error creating/uploading Motion Photo from {still_path} and {video_path}: {e}", flush=True)
            if motion_photo_path:
                motion_photo_path.unlink(missing_ok=True)
        finally:
            self.unmark_processing(still_path, video_path)

    def upload_single_file(self, file_path, delete_after_upload=True, cleanup_paths=None):
        file_path = Path(file_path)
        if not self.mark_processing(file_path):
            return

        cleanup_paths = [Path(path) for path in (cleanup_paths or [file_path])]
        try:
            file_size = file_path.stat().st_size
            print(f"Found new file: {file_path} ({format_size(file_size)})", flush=True)
            output = self.client._upload_file(
                file_path=file_path,
                hash_value=None,
                progress=UploadLogProgress(),
                force_upload=False,
                use_quota=False,
                saver=False,
            )
            print(f"Uploaded: {output}", flush=True)

            if delete_after_upload:
                for cleanup_path in cleanup_paths:
                    self.remove_file_with_retries(cleanup_path)
            return True
        except Exception as e:
            print(f"Error uploading {file_path}: {e}", flush=True)
            return False
        finally:
            self.unmark_processing(file_path)

    def remove_file_with_retries(self, file_path):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                file_path.unlink()
                print(f"File removed: {file_path}", flush=True)
                return
            except FileNotFoundError:
                return
            except PermissionError:
                if attempt < max_retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise

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
