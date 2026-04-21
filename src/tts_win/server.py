from __future__ import annotations

import argparse
import base64
import binascii
import json
import logging
import queue
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import uvicorn
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from tts_win.cli import (
    DEFAULT_ENABLE_TEXT_SPLITTING,
    DEFAULT_LANGUAGE,
    DEFAULT_MAX_CHARS,
    DEFAULT_SPLIT_SENTENCES,
    DEFAULT_SHARED_DIR,
    PROJECT_ROOT,
    XTTS_MODEL,
    build_inference_options,
    convert_reference_to_wav,
    estimate_audio_duration_seconds,
    find_reference_in_shared,
    load_tts,
    require_ffmpeg,
    resolve_ffmpeg,
    resolve_reference_path,
    resolve_shared_dir,
    select_device,
    synthesize_chunked,
    synthesize_to_file,
)

DEFAULT_JOBS_DIR = PROJECT_ROOT / ".data" / "jobs"
DEFAULT_JOB_OUTPUT_NAME = "audio.wav"
SUPPORTED_RESPONSE_FORMATS = {"wav"}
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 8020
DEFAULT_JOB_RETENTION_HOURS = 24
DEFAULT_DOWNLOADED_JOB_RETENTION_HOURS = 6
DEFAULT_CLEANUP_INTERVAL_SECONDS = 900

AUDIO_MIME_TO_EXT = {
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/x-ms-wma": ".wma",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
}


class CreateTTSJobRequest(BaseModel):
    input: str = Field(..., min_length=1)
    model: str = Field(default=XTTS_MODEL)
    voice: str = Field(default="reference")
    response_format: str = Field(default="wav")
    language: str | None = None
    split_sentences: bool | None = None
    enable_text_splitting: bool | None = None
    speed: float | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0)
    length_penalty: float | None = Field(default=None, gt=0)
    repetition_penalty: float | None = Field(default=None, gt=0)
    top_k: int | None = Field(default=None, gt=0)
    top_p: float | None = Field(default=None, gt=0, le=1)
    gpt_cond_len: int | None = Field(default=None, gt=0)
    gpt_cond_chunk_len: int | None = Field(default=None, gt=0)
    max_ref_len: int | None = Field(default=None, gt=0)
    sound_norm_refs: bool | None = None
    reference_audio_base64: str | None = None
    reference_audio_filename: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(slots=True)
class ServerSettings:
    host: str = DEFAULT_SERVER_HOST
    port: int = DEFAULT_SERVER_PORT
    shared_dir: Path = DEFAULT_SHARED_DIR
    jobs_dir: Path = DEFAULT_JOBS_DIR
    model: str = XTTS_MODEL
    device: str = "auto"
    ffmpeg: str = "ffmpeg"
    chunk_mode: str = "auto"
    max_chars: int = DEFAULT_MAX_CHARS
    language: str = DEFAULT_LANGUAGE
    split_sentences: bool = DEFAULT_SPLIT_SENTENCES
    enable_text_splitting: bool = DEFAULT_ENABLE_TEXT_SPLITTING
    speed: float = 1.0
    temperature: float | None = None
    length_penalty: float | None = None
    repetition_penalty: float | None = None
    top_k: int | None = None
    top_p: float | None = None
    gpt_cond_len: int | None = None
    gpt_cond_chunk_len: int | None = None
    max_ref_len: int | None = None
    sound_norm_refs: bool = False
    job_retention_hours: int = DEFAULT_JOB_RETENTION_HOURS
    downloaded_job_retention_hours: int = DEFAULT_DOWNLOADED_JOB_RETENTION_HOURS
    cleanup_interval_seconds: int = DEFAULT_CLEANUP_INTERVAL_SECONDS


@dataclass(slots=True)
class LoadedModel:
    tts: Any
    device: str
    load_seconds: float


logger = logging.getLogger(__name__)


class JobStore:
    def __init__(self, jobs_dir: Path):
        self.jobs_dir = jobs_dir
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._load_existing_jobs()

    def _load_existing_jobs(self) -> None:
        for job_file in self.jobs_dir.glob("*/job.json"):
            job = json.loads(job_file.read_text(encoding="utf-8"))
            if job["status"] in {"queued", "in_progress"}:
                now = utcnow_iso()
                job["status"] = "failed"
                job["error"] = "Server restarted before the job completed."
                job["failed_at"] = now
                job["updated_at"] = now
                job_file.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            self._jobs[job["id"]] = job

    def create_job(self, request: CreateTTSJobRequest) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        now = utcnow_iso()
        job_dir = self.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=False)
        audio_path = job_dir / DEFAULT_JOB_OUTPUT_NAME

        job = {
            "id": job_id,
            "object": "tts.job",
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
            "failed_at": None,
            "model": request.model,
            "voice": request.voice,
            "response_format": request.response_format,
            "input_characters": len(request.input),
            "input_words": len(request.input.split()),
            "estimated_speech_seconds": estimate_audio_duration_seconds(request.input),
            "status_url": f"/v1/tts/jobs/{job_id}",
            "audio_url": f"/v1/tts/jobs/{job_id}/audio",
            "audio_ready": False,
            "audio_path": str(audio_path),
            "error": None,
            "metadata": request.metadata or {},
            "voice_source": "uploaded_reference" if request.reference_audio_base64 else "shared_voice",
            "reference_filename": None,
            "download_count": 0,
            "first_downloaded_at": None,
            "last_downloaded_at": None,
            "external_chunks_used": None,
            "runtime_seconds": None,
            "model_load_seconds": None,
            "synthesis_seconds": None,
        }

        request_payload = {
            "input": request.input,
            "model": request.model,
            "voice": request.voice,
            "response_format": request.response_format,
            "language": request.language,
            "split_sentences": request.split_sentences,
            "enable_text_splitting": request.enable_text_splitting,
            "speed": request.speed,
            "temperature": request.temperature,
            "length_penalty": request.length_penalty,
            "repetition_penalty": request.repetition_penalty,
            "top_k": request.top_k,
            "top_p": request.top_p,
            "gpt_cond_len": request.gpt_cond_len,
            "gpt_cond_chunk_len": request.gpt_cond_chunk_len,
            "max_ref_len": request.max_ref_len,
            "sound_norm_refs": request.sound_norm_refs,
            "metadata": request.metadata or {},
            "reference_audio_uploaded": bool(request.reference_audio_base64),
            "reference_audio_filename": request.reference_audio_filename,
        }

        if request.reference_audio_base64:
            reference_path = decode_reference_audio_to_file(
                encoded=request.reference_audio_base64,
                filename=request.reference_audio_filename,
                job_dir=job_dir,
            )
            request_payload["reference_file"] = reference_path.name
            job["reference_filename"] = reference_path.name

        (job_dir / "request.json").write_text(
            json.dumps(request_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        with self._lock:
            self._jobs[job_id] = job
            self._write_job(job)
        return self.public_view(job)

    def update_job(self, job_id: str, **updates: Any) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            job.update(updates)
            job["updated_at"] = utcnow_iso()
            self._write_job(job)
            return dict(job)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def mark_downloaded(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            now = utcnow_iso()
            job["download_count"] = int(job.get("download_count") or 0) + 1
            job["first_downloaded_at"] = job.get("first_downloaded_at") or now
            job["last_downloaded_at"] = now
            job["updated_at"] = now
            self._write_job(job)
            return dict(job)

    def public_view(self, job: dict[str, Any]) -> dict[str, Any]:
        public_job = dict(job)
        public_job.pop("audio_path", None)
        return public_job

    def audio_path(self, job_id: str) -> Path:
        return Path(self.job_dir(job_id) / DEFAULT_JOB_OUTPUT_NAME)

    def uploaded_reference_path(self, job_id: str) -> Path | None:
        job = self.get_job(job_id)
        if not job or not job.get("reference_filename"):
            return None
        return self.job_dir(job_id) / str(job["reference_filename"])

    def log_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.log"

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def cleanup_expired(
        self,
        *,
        job_retention: timedelta,
        downloaded_job_retention: timedelta,
    ) -> list[str]:
        expired: list[tuple[str, Path]] = []
        now = datetime.now(timezone.utc)

        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if not self._is_terminal(job):
                    continue
                if not self._is_expired(
                    job,
                    now=now,
                    job_retention=job_retention,
                    downloaded_job_retention=downloaded_job_retention,
                ):
                    continue
                expired.append((job_id, self.job_dir(job_id)))
                self._jobs.pop(job_id, None)

        removed_ids: list[str] = []
        for job_id, job_dir in expired:
            shutil.rmtree(job_dir, ignore_errors=True)
            removed_ids.append(job_id)
        return removed_ids

    def _write_job(self, job: dict[str, Any]) -> None:
        (self.job_dir(job["id"]) / "job.json").write_text(
            json.dumps(job, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _is_terminal(job: dict[str, Any]) -> bool:
        return str(job.get("status") or "") in {"completed", "failed"}

    def _is_expired(
        self,
        job: dict[str, Any],
        *,
        now: datetime,
        job_retention: timedelta,
        downloaded_job_retention: timedelta,
    ) -> bool:
        downloaded_at = parse_iso_datetime(job.get("last_downloaded_at"))
        if downloaded_at is not None:
            return now >= downloaded_at + downloaded_job_retention

        terminal_at = (
            parse_iso_datetime(job.get("completed_at"))
            or parse_iso_datetime(job.get("failed_at"))
            or parse_iso_datetime(job.get("updated_at"))
        )
        if terminal_at is None:
            return False
        return now >= terminal_at + job_retention


class JobGarbageCollector:
    def __init__(self, settings: ServerSettings, store: JobStore):
        self.settings = settings
        self.store = store
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="tts-job-cleaner", daemon=True)
        self._startup_lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        with self._startup_lock:
            if self._started:
                return
            self._thread.start()
            self._started = True

    def stop(self) -> None:
        self._stop_event.set()

    def sweep_now(self) -> list[str]:
        removed = self.store.cleanup_expired(
            job_retention=timedelta(hours=self.settings.job_retention_hours),
            downloaded_job_retention=timedelta(hours=self.settings.downloaded_job_retention_hours),
        )
        if removed:
            logger.info("Cleaned up %s expired TTS job(s): %s", len(removed), ", ".join(removed))
        return removed

    def _run(self) -> None:
        self.sweep_now()
        while not self._stop_event.wait(self.settings.cleanup_interval_seconds):
            self.sweep_now()


class SynthesisWorker:
    def __init__(self, settings: ServerSettings, store: JobStore):
        self.settings = settings
        self.store = store
        self._queue: queue.Queue[str] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="tts-job-worker", daemon=True)
        self._loaded_model: LoadedModel | None = None
        self._startup_lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        with self._startup_lock:
            if self._started:
                return
            self._thread.start()
            self._started = True

    def submit(self, job_id: str) -> None:
        self._queue.put(job_id)

    def _run(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._process_job(job_id)
            finally:
                self._queue.task_done()

    def _ensure_model(self) -> LoadedModel:
        if self._loaded_model is not None:
            return self._loaded_model

        started = time.perf_counter()
        device = select_device(self.settings.device)
        tts = load_tts(self.settings.model, device=device)
        self._loaded_model = LoadedModel(
            tts=tts,
            device=device,
            load_seconds=time.perf_counter() - started,
        )
        return self._loaded_model

    def _process_job(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if job is None:
            return

        log_path = self.store.log_path(job_id)
        with log_path.open("a", encoding="utf-8") as log_file:
            try:
                started_at = utcnow_iso()
                self.store.update_job(job_id, status="in_progress", started_at=started_at)
                log_line(log_file, f"Job {job_id} started at {started_at}")

                # Coqui/Torch may write progress noise to stdout/stderr while loading the model
                # or synthesizing. Keep that scoped to the per-job log instead of the server console.
                with redirect_stdout(log_file), redirect_stderr(log_file):
                    loaded_model = self._ensure_model()
                log_line(log_file, f"Model ready on device {loaded_model.device} in {loaded_model.load_seconds:.2f}s")

                reference_path = self._resolve_reference_path(job_id, job)
                reference_path = resolve_reference_path(reference_path)
                output_path = self.store.audio_path(job_id)
                ffmpeg_bin = self._resolve_ffmpeg(reference_path)
                request_payload = self._load_job_request_payload(job_id)
                text = str(request_payload["input"]).strip()
                options = self._build_job_inference_options(request_payload)

                log_line(log_file, f"Reference selected: {reference_path}")
                log_line(
                    log_file,
                    "XTTS options: "
                    f"language={options.language}, "
                    f"split_sentences={options.split_sentences}, "
                    f"enable_text_splitting={options.enable_text_splitting}, "
                    f"speed={options.speed}",
                )
                chunk_count = 0
                runtime_started = time.perf_counter()
                synthesis_started = time.perf_counter()

                with redirect_stdout(log_file), redirect_stderr(log_file):
                    chunk_count = self._run_synthesis(
                        loaded_model=loaded_model,
                        text=text,
                        reference_path=reference_path,
                        output_path=output_path,
                        ffmpeg_bin=ffmpeg_bin,
                        options=options,
                        log=lambda message: log_line(log_file, message),
                    )

                synthesis_seconds = time.perf_counter() - synthesis_started
                wall_seconds = time.perf_counter() - runtime_started

                completed_at = utcnow_iso()
                self.store.update_job(
                    job_id,
                    status="completed",
                    completed_at=completed_at,
                    audio_ready=output_path.is_file(),
                    runtime_seconds=wall_seconds,
                    model_load_seconds=loaded_model.load_seconds,
                    synthesis_seconds=synthesis_seconds,
                    external_chunks_used=chunk_count,
                )
                log_line(log_file, f"Job {job_id} completed at {completed_at}")
            except Exception as exc:
                failed_at = utcnow_iso()
                self.store.update_job(
                    job_id,
                    status="failed",
                    failed_at=failed_at,
                    error=str(exc),
                    audio_ready=False,
                )
                log_line(log_file, f"Job {job_id} failed at {failed_at}: {exc}")

    def _load_job_request_payload(self, job_id: str) -> dict[str, Any]:
        request_path = self.store.job_dir(job_id) / "request.json"
        return json.loads(request_path.read_text(encoding="utf-8"))

    def _build_job_inference_options(self, payload: dict[str, Any]):
        return build_inference_options(
            language=_pick_override(payload.get("language"), self.settings.language),
            split_sentences=_pick_override(payload.get("split_sentences"), self.settings.split_sentences),
            enable_text_splitting=_pick_override(
                payload.get("enable_text_splitting"),
                self.settings.enable_text_splitting,
            ),
            speed=float(_pick_override(payload.get("speed"), self.settings.speed)),
            temperature=_pick_override(payload.get("temperature"), self.settings.temperature),
            length_penalty=_pick_override(payload.get("length_penalty"), self.settings.length_penalty),
            repetition_penalty=_pick_override(
                payload.get("repetition_penalty"),
                self.settings.repetition_penalty,
            ),
            top_k=_pick_override(payload.get("top_k"), self.settings.top_k),
            top_p=_pick_override(payload.get("top_p"), self.settings.top_p),
            gpt_cond_len=_pick_override(payload.get("gpt_cond_len"), self.settings.gpt_cond_len),
            gpt_cond_chunk_len=_pick_override(
                payload.get("gpt_cond_chunk_len"),
                self.settings.gpt_cond_chunk_len,
            ),
            max_ref_len=_pick_override(payload.get("max_ref_len"), self.settings.max_ref_len),
            sound_norm_refs=_pick_override(payload.get("sound_norm_refs"), self.settings.sound_norm_refs),
        )

    def _resolve_reference_path(self, job_id: str, job: dict[str, Any]) -> Path:
        uploaded_reference = self.store.uploaded_reference_path(job_id)
        if uploaded_reference is not None:
            return uploaded_reference
        return find_reference_in_shared(self.settings.shared_dir, str(job["voice"]))

    def _resolve_ffmpeg(self, reference_path: Path) -> str | None:
        if reference_path.suffix.lower() != ".wav":
            return require_ffmpeg(self.settings.ffmpeg)
        return resolve_ffmpeg(self.settings.ffmpeg)

    def _run_synthesis(
        self,
        *,
        loaded_model: LoadedModel,
        text: str,
        reference_path: Path,
        output_path: Path,
        ffmpeg_bin: str | None,
        options,
        log: Callable[[str], None] | None = None,
    ) -> int:
        job_chunk_mode = self.settings.chunk_mode
        needs_ffmpeg_for_reference = reference_path.suffix.lower() != ".wav"
        work_dir = Path(tempfile.mkdtemp(prefix=f"{output_path.stem}_work_", dir=str(output_path.parent)))
        log_message = log or (lambda _message: None)

        try:
            reference_wav = (
                convert_reference_to_wav(reference_path, work_dir, ffmpeg_bin)
                if needs_ffmpeg_for_reference
                else reference_path
            )

            prefer_chunking = job_chunk_mode == "on"

            if not prefer_chunking:
                try:
                    log_message("Model-managed synthesis start")
                    synthesize_to_file(loaded_model.tts, text, reference_wav, output_path, options=options)
                    log_message("Model-managed synthesis done")
                    return 0
                except Exception as exc:
                    if job_chunk_mode != "auto":
                        raise
                    if ffmpeg_bin is None:
                        raise RuntimeError(
                            "Model-managed synthesis failed and ffmpeg is unavailable for external chunking fallback."
                        ) from exc
                    log_message(f"Model-managed synthesis failed, retrying with external chunking: {exc}")

            return synthesize_chunked(
                tts=loaded_model.tts,
                text=text,
                reference_wav=reference_wav,
                output_path=output_path,
                work_dir=work_dir,
                ffmpeg_bin=require_ffmpeg(self.settings.ffmpeg),
                max_chars=self.settings.max_chars,
                options=options,
                log=log_message,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


def parse_args(argv: list[str] | None = None) -> ServerSettings:
    parser = argparse.ArgumentParser(
        prog="tts-win-server",
        description="Native async TTS jobs server for the Windows XTTS port.",
    )
    parser.add_argument("--host", default=DEFAULT_SERVER_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_SERVER_PORT)
    parser.add_argument("--shared-dir", default=str(DEFAULT_SHARED_DIR))
    parser.add_argument("--jobs-dir", default=str(DEFAULT_JOBS_DIR))
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--model", default=XTTS_MODEL)
    parser.add_argument("--chunk-mode", choices=("auto", "on", "off"), default="auto")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--language", default=DEFAULT_LANGUAGE)
    parser.add_argument(
        "--split-sentences",
        choices=("on", "off"),
        default="on" if DEFAULT_SPLIT_SENTENCES else "off",
    )
    parser.add_argument(
        "--enable-text-splitting",
        choices=("on", "off"),
        default="on" if DEFAULT_ENABLE_TEXT_SPLITTING else "off",
    )
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--length-penalty", type=float, default=None)
    parser.add_argument("--repetition-penalty", type=float, default=None)
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--top-p", type=float, default=None)
    parser.add_argument("--gpt-cond-len", type=int, default=None)
    parser.add_argument("--gpt-cond-chunk-len", type=int, default=None)
    parser.add_argument("--max-ref-len", type=int, default=None)
    parser.add_argument("--sound-norm-refs", action="store_true")
    parser.add_argument("--job-retention-hours", type=int, default=DEFAULT_JOB_RETENTION_HOURS)
    parser.add_argument(
        "--downloaded-job-retention-hours",
        type=int,
        default=DEFAULT_DOWNLOADED_JOB_RETENTION_HOURS,
    )
    parser.add_argument(
        "--cleanup-interval-seconds",
        type=int,
        default=DEFAULT_CLEANUP_INTERVAL_SECONDS,
    )
    args = parser.parse_args(argv)
    return ServerSettings(
        host=args.host,
        port=args.port,
        shared_dir=resolve_shared_dir(args.shared_dir),
        jobs_dir=Path(args.jobs_dir).expanduser().resolve(),
        device=args.device,
        ffmpeg=args.ffmpeg,
        model=args.model,
        chunk_mode=args.chunk_mode,
        max_chars=args.max_chars,
        language=str(args.language).strip() or DEFAULT_LANGUAGE,
        split_sentences=args.split_sentences == "on",
        enable_text_splitting=args.enable_text_splitting == "on",
        speed=args.speed,
        temperature=args.temperature,
        length_penalty=args.length_penalty,
        repetition_penalty=args.repetition_penalty,
        top_k=args.top_k,
        top_p=args.top_p,
        gpt_cond_len=args.gpt_cond_len,
        gpt_cond_chunk_len=args.gpt_cond_chunk_len,
        max_ref_len=args.max_ref_len,
        sound_norm_refs=args.sound_norm_refs,
        job_retention_hours=max(args.job_retention_hours, 0),
        downloaded_job_retention_hours=max(args.downloaded_job_retention_hours, 0),
        cleanup_interval_seconds=max(args.cleanup_interval_seconds, 1),
    )


def create_app(settings: ServerSettings | None = None) -> FastAPI:
    server_settings = settings or ServerSettings(
        shared_dir=resolve_shared_dir(str(DEFAULT_SHARED_DIR)),
        jobs_dir=DEFAULT_JOBS_DIR,
    )
    store = JobStore(server_settings.jobs_dir)
    worker = SynthesisWorker(server_settings, store)
    gc = JobGarbageCollector(server_settings, store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        worker.start()
        gc.start()
        yield
        gc.stop()

    app = FastAPI(title="tts-win", version="0.3.0", lifespan=lifespan)
    app.state.settings = server_settings
    app.state.store = store
    app.state.worker = worker
    app.state.gc = gc

    @app.get("/health")
    def health() -> dict[str, Any]:
        gc.sweep_now()
        return {
            "status": "ok",
            "model": server_settings.model,
            "shared_dir": str(server_settings.shared_dir),
            "jobs_dir": str(server_settings.jobs_dir),
            "language": server_settings.language,
            "split_sentences": server_settings.split_sentences,
            "enable_text_splitting": server_settings.enable_text_splitting,
            "speed": server_settings.speed,
            "job_retention_hours": server_settings.job_retention_hours,
            "downloaded_job_retention_hours": server_settings.downloaded_job_retention_hours,
        }

    @app.post("/v1/tts/jobs", status_code=status.HTTP_202_ACCEPTED)
    def create_tts_job(request: CreateTTSJobRequest) -> dict[str, Any]:
        gc.sweep_now()
        if request.model != server_settings.model:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Only model '{server_settings.model}' is currently supported.",
            )

        if request.response_format.lower() not in SUPPORTED_RESPONSE_FORMATS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only response_format='wav' is currently supported.",
            )

        normalized_request = CreateTTSJobRequest(
            input=request.input.strip(),
            model=request.model,
            voice=(request.voice or "reference").strip() or "reference",
            response_format=request.response_format.lower(),
            language=(request.language or "").strip() or None,
            split_sentences=request.split_sentences,
            enable_text_splitting=request.enable_text_splitting,
            speed=request.speed,
            temperature=request.temperature,
            length_penalty=request.length_penalty,
            repetition_penalty=request.repetition_penalty,
            top_k=request.top_k,
            top_p=request.top_p,
            gpt_cond_len=request.gpt_cond_len,
            gpt_cond_chunk_len=request.gpt_cond_chunk_len,
            max_ref_len=request.max_ref_len,
            sound_norm_refs=request.sound_norm_refs,
            reference_audio_base64=request.reference_audio_base64,
            reference_audio_filename=request.reference_audio_filename,
            metadata=request.metadata,
        )

        if not normalized_request.input:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="input must not be empty.",
            )

        if not normalized_request.reference_audio_base64:
            try:
                find_reference_in_shared(server_settings.shared_dir, normalized_request.voice)
            except FileNotFoundError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        try:
            job = store.create_job(normalized_request)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        worker.submit(job["id"])
        return job

    @app.get("/v1/tts/jobs/{job_id}")
    def get_tts_job(job_id: str) -> dict[str, Any]:
        gc.sweep_now()
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
        return store.public_view(job)

    @app.get("/v1/tts/jobs/{job_id}/audio")
    def get_tts_job_audio(job_id: str) -> FileResponse:
        gc.sweep_now()
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

        audio_path = store.audio_path(job_id)
        if audio_path.is_file():
            store.mark_downloaded(job_id)
            return FileResponse(path=audio_path, media_type="audio/wav", filename=f"{job_id}.wav")

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Audio is not ready yet.",
                "status": job["status"],
                "error": job.get("error"),
            },
        )

    return app


def decode_reference_audio_to_file(encoded: str, filename: str | None, job_dir: Path) -> Path:
    mime_type, payload = split_data_uri(encoded)
    try:
        decoded = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("reference_audio_base64 is not valid base64.") from exc

    suffix = infer_reference_suffix(filename, mime_type)
    reference_path = job_dir / f"reference{suffix}"
    reference_path.write_bytes(decoded)
    return reference_path


def split_data_uri(value: str) -> tuple[str | None, str]:
    normalized = value.strip()
    if normalized.startswith("data:"):
        if "," not in normalized:
            raise ValueError("Invalid data URI for reference audio.")
        header, payload = normalized.split(",", 1)
        mime_type = header[5:].split(";", 1)[0] or None
        return mime_type, payload.strip()
    return None, normalized


def infer_reference_suffix(filename: str | None, mime_type: str | None) -> str:
    if filename:
        suffix = Path(filename).suffix.lower()
        if suffix:
            return suffix
    if mime_type and mime_type in AUDIO_MIME_TO_EXT:
        return AUDIO_MIME_TO_EXT[mime_type]
    return ".wav"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def log_line(handle, message: str) -> None:
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    handle.write(f"[{timestamp}] {message}\n")
    handle.flush()


def _pick_override(value: Any, default: Any) -> Any:
    return default if value is None else value


def main(argv: list[str] | None = None) -> int:
    settings = parse_args(argv)
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, access_log=False, log_level="warning")
    return 0


app = create_app()


if __name__ == "__main__":
    raise SystemExit(main())
