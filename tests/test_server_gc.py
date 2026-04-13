from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import types

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

if "uvicorn" not in sys.modules:
    sys.modules["uvicorn"] = types.SimpleNamespace(run=lambda *args, **kwargs: None)

if "fastapi" not in sys.modules:
    class _DummyFastAPI:
        def __init__(self, *args, **kwargs):
            self.state = types.SimpleNamespace()

        def get(self, *args, **kwargs):
            return lambda func: func

        def post(self, *args, **kwargs):
            return lambda func: func

    sys.modules["fastapi"] = types.SimpleNamespace(
        FastAPI=_DummyFastAPI,
        HTTPException=Exception,
        status=types.SimpleNamespace(
            HTTP_202_ACCEPTED=202,
            HTTP_400_BAD_REQUEST=400,
            HTTP_404_NOT_FOUND=404,
            HTTP_409_CONFLICT=409,
        ),
    )

if "fastapi.responses" not in sys.modules:
    sys.modules["fastapi.responses"] = types.SimpleNamespace(FileResponse=object)

if "pydantic" not in sys.modules:
    class _DummyBaseModel:
        pass

    def _dummy_field(default=None, **kwargs):
        return default

    sys.modules["pydantic"] = types.SimpleNamespace(BaseModel=_DummyBaseModel, Field=_dummy_field)

if "tts_win.cli" not in sys.modules:
    fake_cli = types.SimpleNamespace(
        DEFAULT_LANGUAGE="ru",
        DEFAULT_MAX_CHARS=180,
        DEFAULT_SHARED_DIR=PROJECT_ROOT / "shared",
        PROJECT_ROOT=PROJECT_ROOT,
        XTTS_MODEL="tts_models/multilingual/multi-dataset/xtts_v2",
        convert_reference_to_wav=lambda *args, **kwargs: args[0],
        estimate_audio_duration_seconds=lambda text: float(len(text.split())),
        find_reference_in_shared=lambda *args, **kwargs: PROJECT_ROOT / "shared" / "reference.wav",
        load_tts=lambda *args, **kwargs: object(),
        require_ffmpeg=lambda value: value,
        resolve_ffmpeg=lambda value: value,
        resolve_reference_path=lambda path: Path(path),
        resolve_shared_dir=lambda value: Path(value),
        select_device=lambda value: "cpu" if value == "auto" else value,
        synthesize_chunked=lambda *args, **kwargs: 0,
        synthesize_to_file=lambda *args, **kwargs: None,
    )
    sys.modules["tts_win.cli"] = fake_cli

from tts_win.server import JobStore


class DummyRequest:
    def __init__(
        self,
        *,
        input: str,
        model: str = "tts_models/multilingual/multi-dataset/xtts_v2",
        voice: str = "reference",
        response_format: str = "wav",
        metadata: dict | None = None,
        reference_audio_base64: str | None = None,
        reference_audio_filename: str | None = None,
    ):
        self.input = input
        self.model = model
        self.voice = voice
        self.response_format = response_format
        self.metadata = metadata
        self.reference_audio_base64 = reference_audio_base64
        self.reference_audio_filename = reference_audio_filename


def iso_utc(hours_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


class TestJobStoreCleanup(unittest.TestCase):
    def test_mark_downloaded_updates_job_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = JobStore(Path(temp_dir))
            job = store.create_job(DummyRequest(input="hello"))
            store.update_job(job["id"], status="completed", completed_at=iso_utc(1), audio_ready=True)
            store.audio_path(job["id"]).write_bytes(b"RIFF")

            updated = store.mark_downloaded(job["id"])

            self.assertEqual(updated["download_count"], 1)
            self.assertIsNotNone(updated["first_downloaded_at"])
            self.assertIsNotNone(updated["last_downloaded_at"])

    def test_cleanup_removes_old_completed_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = JobStore(Path(temp_dir))
            job = store.create_job(DummyRequest(input="hello"))
            store.update_job(
                job["id"],
                status="completed",
                completed_at=iso_utc(30),
                audio_ready=True,
            )
            store.audio_path(job["id"]).write_bytes(b"RIFF")

            removed = store.cleanup_expired(
                job_retention=timedelta(hours=24),
                downloaded_job_retention=timedelta(hours=6),
            )

            self.assertEqual(removed, [job["id"]])
            self.assertFalse(store.job_dir(job["id"]).exists())
            self.assertIsNone(store.get_job(job["id"]))

    def test_cleanup_uses_download_time_when_present(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = JobStore(Path(temp_dir))
            stale = store.create_job(DummyRequest(input="stale"))
            fresh = store.create_job(DummyRequest(input="fresh"))

            store.update_job(
                stale["id"],
                status="completed",
                completed_at=iso_utc(30),
                audio_ready=True,
                last_downloaded_at=iso_utc(8),
                first_downloaded_at=iso_utc(8),
                download_count=1,
            )
            store.update_job(
                fresh["id"],
                status="completed",
                completed_at=iso_utc(30),
                audio_ready=True,
                last_downloaded_at=iso_utc(1),
                first_downloaded_at=iso_utc(1),
                download_count=1,
            )
            store.audio_path(stale["id"]).write_bytes(b"RIFF")
            store.audio_path(fresh["id"]).write_bytes(b"RIFF")

            removed = store.cleanup_expired(
                job_retention=timedelta(hours=24),
                downloaded_job_retention=timedelta(hours=6),
            )

            self.assertEqual(removed, [stale["id"]])
            self.assertIsNone(store.get_job(stale["id"]))
            self.assertIsNotNone(store.get_job(fresh["id"]))


if __name__ == "__main__":
    unittest.main()
