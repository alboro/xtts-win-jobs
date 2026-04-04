from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

XTTS_MODEL = "tts_models/multilingual/multi-dataset/xtts_v2"
DEFAULT_LANGUAGE = "ru"
DEFAULT_MAX_CHARS = 180
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SHARED_DIR = PROJECT_ROOT / "shared"
DEFAULT_REFERENCE_PREFIX = "reference"
warnings.filterwarnings(
    "ignore",
    message=r"In 2\.9, this function's implementation will be changed to use torchaudio\.load_with_torchcodec",
    category=UserWarning,
)

REFERENCE_EXTENSIONS = {
    ".wav",
    ".mp3",
    ".m4a",
    ".flac",
    ".ogg",
    ".opus",
    ".aac",
    ".wma",
    ".mp4",
    ".mkv",
    ".webm",
}


@dataclass
class ResolvedRun:
    text: str
    output_path: Path
    reference_path: Path
    text_source_label: str
    reference_source_label: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tts-win",
        description="Windows-first XTTS v2 CLI for Russian voice cloning.",
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help=(
            "Default mode: INPUT_FILE OUTPUT [REFERENCE_OR_PREFIX]. "
            "With --text: TEXT OUTPUT [REFERENCE_OR_PREFIX]. "
            "With --file: OUTPUT [REFERENCE_OR_PREFIX]."
        ),
    )
    parser.add_argument(
        "--file",
        dest="input_file",
        help="Read text from a UTF-8 text file instead of the first positional argument.",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="Treat the first positional input as literal text instead of a file path.",
    )
    parser.add_argument(
        "--shared-dir",
        default=str(DEFAULT_SHARED_DIR),
        help=f"Shared directory for auto-discovery. Default: {DEFAULT_SHARED_DIR}.",
    )
    parser.add_argument(
        "--reference-prefix",
        default=DEFAULT_REFERENCE_PREFIX,
        help=f"Default reference prefix inside shared dir. Default: {DEFAULT_REFERENCE_PREFIX}.",
    )
    parser.add_argument(
        "--device",
        choices=("auto", "cuda", "cpu"),
        default="auto",
        help="Execution device. Default: auto.",
    )
    parser.add_argument(
        "--chunk-mode",
        choices=("auto", "on", "off"),
        default="auto",
        help="External manual chunking. auto=try model sentence splitting first and fall back on failure; on=force manual chunking; off=never use manual chunking. Default: auto.",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help="Soft chunk size limit used by external manual chunking. Default: 180.",
    )
    parser.add_argument(
        "--ffmpeg",
        default="ffmpeg",
        help="Path to ffmpeg executable or command name. Default: ffmpeg.",
    )
    parser.add_argument(
        "--model",
        default=XTTS_MODEL,
        help=f"Coqui model name. Default: {XTTS_MODEL}.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep temporary chunk and conversion files for debugging.",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Print runtime diagnostics and exit.",
    )
    return parser


def resolve_shared_dir(path_value: str) -> Path:
    shared_dir = Path(path_value).expanduser()
    if not shared_dir.is_absolute():
        shared_dir = (Path.cwd() / shared_dir).resolve()
    else:
        shared_dir = shared_dir.resolve()
    shared_dir.mkdir(parents=True, exist_ok=True)
    return shared_dir


def resolve_existing_file(value: str, shared_dir: Path) -> Path | None:
    raw_path = Path(value).expanduser()
    candidates = [raw_path] if raw_path.is_absolute() else [Path.cwd() / raw_path, shared_dir / raw_path]

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    return None


def resolve_text_file_path(path_value: str, shared_dir: Path) -> Path:
    resolved = resolve_existing_file(path_value, shared_dir)
    if resolved is None:
        raise FileNotFoundError(
            f"Input text file not found: {path_value}. Checked current directory and {shared_dir}."
        )
    return resolved


def resolve_reference_arg(
    reference_value: str | None,
    shared_dir: Path,
    default_prefix: str,
) -> tuple[Path, str]:
    if reference_value:
        resolved_file = resolve_existing_file(reference_value, shared_dir)
        if resolved_file is not None:
            return resolved_file, "explicit file"
        prefix = Path(reference_value).stem or reference_value
    else:
        prefix = default_prefix

    reference_path = find_reference_in_shared(shared_dir, prefix)
    return reference_path, f"newest shared match for prefix '{prefix}'"


def read_text_file(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"Input text file not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Input text file is empty: {path}")
    return text


def resolve_cli_inputs(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    shared_dir: Path,
) -> ResolvedRun:
    if args.input_file and args.text:
        parser.error("Use either --file or --text, not both.")

    if args.input_file:
        if len(args.inputs) not in (1, 2):
            parser.error("With --file you must pass: OUTPUT [REFERENCE_OR_PREFIX]")
        input_path = resolve_text_file_path(args.input_file, shared_dir)
        text = read_text_file(input_path)
        reference_value = args.inputs[1] if len(args.inputs) == 2 else None
        reference_path, reference_source_label = resolve_reference_arg(
            reference_value=reference_value,
            shared_dir=shared_dir,
            default_prefix=args.reference_prefix,
        )
        return ResolvedRun(
            text=text,
            output_path=Path(args.inputs[0]),
            reference_path=reference_path,
            text_source_label=str(input_path),
            reference_source_label=reference_source_label,
        )

    if args.text:
        if len(args.inputs) not in (2, 3):
            parser.error("With --text you must pass: TEXT OUTPUT [REFERENCE_OR_PREFIX]")
        text = args.inputs[0].strip()
        if not text:
            parser.error("Text must not be empty.")
        reference_value = args.inputs[2] if len(args.inputs) == 3 else None
        reference_path, reference_source_label = resolve_reference_arg(
            reference_value=reference_value,
            shared_dir=shared_dir,
            default_prefix=args.reference_prefix,
        )
        return ResolvedRun(
            text=text,
            output_path=Path(args.inputs[1]),
            reference_path=reference_path,
            text_source_label="<inline text>",
            reference_source_label=reference_source_label,
        )

    if len(args.inputs) not in (2, 3):
        parser.error("Usage: INPUT_FILE OUTPUT [REFERENCE_OR_PREFIX]")

    input_path = resolve_text_file_path(args.inputs[0], shared_dir)
    text = read_text_file(input_path)
    reference_value = args.inputs[2] if len(args.inputs) == 3 else None
    reference_path, reference_source_label = resolve_reference_arg(
        reference_value=reference_value,
        shared_dir=shared_dir,
        default_prefix=args.reference_prefix,
    )
    return ResolvedRun(
        text=text,
        output_path=Path(args.inputs[1]),
        reference_path=reference_path,
        text_source_label=str(input_path),
        reference_source_label=reference_source_label,
    )


def resolve_output_path(path: Path, overwrite: bool) -> Path:
    output_path = path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"Output already exists: {output_path}. Use --overwrite to replace it."
        )

    return output_path


def resolve_reference_path(path: Path) -> Path:
    reference_path = path.expanduser().resolve()
    if not reference_path.is_file():
        raise FileNotFoundError(f"Reference audio not found: {reference_path}")
    return reference_path


def is_supported_reference_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in REFERENCE_EXTENSIONS


def find_reference_in_shared(shared_dir: Path, prefix: str) -> Path:
    prefix_normalized = prefix.lower()
    candidates = [
        candidate.resolve()
        for candidate in shared_dir.rglob("*")
        if is_supported_reference_file(candidate) and candidate.stem.lower().startswith(prefix_normalized)
    ]

    if not candidates:
        raise FileNotFoundError(
            f"No reference files found in {shared_dir} for prefix '{prefix}'."
        )

    return max(candidates, key=lambda item: (item.stat().st_mtime, item.name.lower()))


def resolve_ffmpeg(ffmpeg_value: str) -> str | None:
    candidate = Path(ffmpeg_value)
    if candidate.is_file():
        return str(candidate.resolve())

    discovered = shutil.which(ffmpeg_value)
    if discovered:
        return discovered

    for fallback in iter_ffmpeg_fallbacks():
        if fallback.is_file():
            return str(fallback.resolve())

    return None


def iter_ffmpeg_fallbacks():
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        winget_packages = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        if winget_packages.is_dir():
            yield from winget_packages.glob("*FFmpeg*/*/bin/ffmpeg.exe")
        yield Path(local_app_data) / "Programs" / "ffmpeg" / "bin" / "ffmpeg.exe"

    yield Path("C:/ffmpeg/bin/ffmpeg.exe")
    yield Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe")


def require_ffmpeg(ffmpeg_value: str) -> str:
    ffmpeg_bin = resolve_ffmpeg(ffmpeg_value)
    if ffmpeg_bin:
        return ffmpeg_bin
    raise RuntimeError(
        "ffmpeg was not found. Install ffmpeg and add it to PATH, or pass --ffmpeg C:\\path\\to\\ffmpeg.exe."
    )


def run_command(command: list[str], description: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode == 0:
        return

    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    details = stderr or stdout or "No details returned."
    raise RuntimeError(f"{description} failed.\n{details}")


def convert_reference_to_wav(reference_path: Path, work_dir: Path, ffmpeg_bin: str) -> Path:
    if reference_path.suffix.lower() == ".wav":
        return reference_path

    converted_path = work_dir / f"{reference_path.stem}_reference.wav"
    command = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(reference_path),
        "-vn",
        "-ar",
        "22050",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(converted_path),
    ]
    run_command(command, "Reference conversion")
    return converted_path


def estimate_audio_duration_seconds(text: str) -> float:
    words = len(text.split())
    return (words / 150.0) * 60.0


def split_text_ru(text: str, max_chars: int) -> list[str]:
    parts: list[str] = []
    current = ""
    tokens = re.split(r"(\s*[.!?]+(?:\s+|$))", text)
    sentences: list[str] = []

    for index in range(0, len(tokens), 2):
        sentence = tokens[index] + (tokens[index + 1] if index + 1 < len(tokens) else "")
        if sentence.strip():
            sentences.append(sentence.strip())

    for sentence in sentences:
        if len(sentence) > max_chars:
            start = 0
            while start < len(sentence):
                cut = min(start + max_chars, len(sentence))
                soft_cut = sentence.rfind(",", start, cut)
                if soft_cut == -1:
                    soft_cut = sentence.rfind(" ", start, cut)
                cut = soft_cut if soft_cut != -1 and soft_cut > start else cut
                parts.append(sentence[start:cut].strip())
                start = cut
        elif len(current) + len(sentence) <= max_chars:
            current = f"{current} {sentence}".strip()
        else:
            if current:
                parts.append(current)
            current = sentence

    if current:
        parts.append(current)

    return [part for part in parts if part]



def split_text_for_xtts(text: str, max_chars: int) -> list[str]:
    return split_text_ru(text, max_chars=max_chars)


def select_device(requested_device: str) -> str:
    import torch

    if requested_device == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"

    if requested_device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested, but torch.cuda.is_available() returned False.")

    return requested_device


def load_tts(model_name: str, device: str):
    from TTS.api import TTS

    return TTS(model_name).to(device)


def synthesize_to_file(tts, text: str, reference_wav: Path, output_path: Path) -> None:
    tts.tts_to_file(
        text=text,
        speaker_wav=str(reference_wav),
        language=DEFAULT_LANGUAGE,
        file_path=str(output_path),
        split_sentences=True,
        enable_text_splitting=True,
    )


def write_concat_manifest(chunk_paths: list[Path], manifest_path: Path) -> None:
    lines = []
    for chunk_path in chunk_paths:
        normalized = chunk_path.resolve().as_posix()
        lines.append(f"file '{normalized}'")
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def concatenate_chunks(chunk_paths: list[Path], output_path: Path, ffmpeg_bin: str) -> None:
    manifest_path = output_path.with_name(f"{output_path.stem}_concat.txt")
    write_concat_manifest(chunk_paths, manifest_path)
    try:
        command = [
            ffmpeg_bin,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(manifest_path),
            "-ar",
            "24000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
        run_command(command, "Chunk concatenation")
    finally:
        manifest_path.unlink(missing_ok=True)


def format_timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S %Z")


def format_duration(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []

    if hours >= 1:
        parts.append(f"{int(hours)}h")
    if minutes >= 1 or hours >= 1:
        parts.append(f"{int(minutes)}m")
    parts.append(f"{secs:.1f}s")
    return " ".join(parts)


def print_run_summary(
    *,
    start_at: datetime,
    shared_dir: Path,
    text_source_label: str,
    output_path: Path,
    reference_path: Path,
    reference_source_label: str,
    text: str,
    chunk_mode: str,
) -> None:
    word_count = len(text.split())
    estimated_audio = estimate_audio_duration_seconds(text)
    print(f"Start: {format_timestamp(start_at)}")
    print(f"Shared dir: {shared_dir}")
    print(f"Input: {text_source_label}")
    print(f"Output: {output_path}")
    print(f"Reference: {reference_path}")
    print(f"Reference source: {reference_source_label}")
    print(f"Text size: {len(text)} chars, {word_count} words")
    print(f"Estimated speech: {format_duration(estimated_audio)}")
    print(f"Requested chunk mode: {chunk_mode}")
    print("Model sentence splitting: enabled")
    print("XTTS long-sentence splitting: enabled")


def print_finish_summary(
    *,
    started_at: datetime,
    finished_at: datetime,
    wall_seconds: float,
    model_load_seconds: float,
    synthesis_seconds: float,
    text: str,
    chunk_count: int,
) -> None:
    words = len(text.split())
    estimated_audio = estimate_audio_duration_seconds(text)
    chars_per_second = len(text) / wall_seconds if wall_seconds > 0 else 0.0
    words_per_second = words / wall_seconds if wall_seconds > 0 else 0.0
    estimated_realtime_x = estimated_audio / wall_seconds if wall_seconds > 0 else 0.0

    print(f"Finish: {format_timestamp(finished_at)}")
    print(f"Wall time: {format_duration(wall_seconds)}")
    print(f"Model load: {format_duration(model_load_seconds)}")
    print(f"Synthesis: {format_duration(synthesis_seconds)}")
    if chunk_count > 0:
        print(f"External chunks used: {chunk_count}")
    else:
        print("External chunks used: no (model sentence splitting only)")
    print(f"Throughput: {chars_per_second:.1f} chars/s, {words_per_second:.1f} words/s")
    print(f"Estimated speech/runtime: {estimated_realtime_x:.2f}x")
    print(
        f"Started at {format_timestamp(started_at)}, finished at {format_timestamp(finished_at)}."
    )


def synthesize_chunked(
    tts,
    text: str,
    reference_wav: Path,
    output_path: Path,
    work_dir: Path,
    ffmpeg_bin: str,
    max_chars: int,
) -> int:
    chunks = split_text_for_xtts(text, max_chars=max_chars)
    if not chunks:
        raise RuntimeError("Chunking produced no text chunks.")

    if len(chunks) == 1:
        chunk_started = time.perf_counter()
        print(
            f"[chunk 1/1] start | {len(chunks[0])} chars | "
            f"est. {format_duration(estimate_audio_duration_seconds(chunks[0]))}"
        )
        synthesize_to_file(tts, chunks[0], reference_wav, output_path)
        print(f"[chunk 1/1] done | {format_duration(time.perf_counter() - chunk_started)}")
        return 1

    chunk_dir = work_dir / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunk_paths: list[Path] = []

    for index, chunk_text in enumerate(chunks, start=1):
        chunk_path = chunk_dir / f"chunk_{index:03d}.wav"
        chunk_started = time.perf_counter()
        print(
            f"[chunk {index}/{len(chunks)}] start | {len(chunk_text)} chars | "
            f"est. {format_duration(estimate_audio_duration_seconds(chunk_text))}"
        )
        synthesize_to_file(tts, chunk_text, reference_wav, chunk_path)
        chunk_elapsed = time.perf_counter() - chunk_started
        print(f"[chunk {index}/{len(chunks)}] done | {format_duration(chunk_elapsed)}")
        chunk_paths.append(chunk_path)

    concat_started = time.perf_counter()
    print("Concatenating chunks...")
    concatenate_chunks(chunk_paths, output_path, ffmpeg_bin)
    print(f"Concatenation done | {format_duration(time.perf_counter() - concat_started)}")
    return len(chunk_paths)


def run_doctor(ffmpeg_value: str, shared_dir: Path, reference_prefix: str) -> int:
    print(f"Python: {sys.version.split()[0]}")
    print(f"shared_dir: {shared_dir}")

    ffmpeg_bin = resolve_ffmpeg(ffmpeg_value)
    if ffmpeg_bin:
        print(f"ffmpeg: OK ({ffmpeg_bin})")
    else:
        print("ffmpeg: MISSING")

    try:
        default_reference = find_reference_in_shared(shared_dir, reference_prefix)
        print(f"default_reference: {default_reference}")
    except Exception as exc:
        print(f"default_reference: ERROR ({exc})")

    try:
        import torch

        print(f"torch: {torch.__version__}")
        print(f"cuda_available: {torch.cuda.is_available()}")
        if torch.cuda.is_available():
            print(f"cuda_device_count: {torch.cuda.device_count()}")
            print(f"cuda_device_0: {torch.cuda.get_device_name(0)}")
    except Exception as exc:
        print(f"torch: ERROR ({exc})")

    try:
        from TTS.api import TTS

        print("coqui_tts: OK")
        print(f"default_model: {XTTS_MODEL}")
        del TTS
    except Exception as exc:
        print(f"coqui_tts: ERROR ({exc})")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    shared_dir = resolve_shared_dir(args.shared_dir)

    if args.doctor:
        return run_doctor(args.ffmpeg, shared_dir, args.reference_prefix)

    try:
        resolved = resolve_cli_inputs(args, parser, shared_dir)
        text = resolved.text
        output_path = resolve_output_path(resolved.output_path, overwrite=args.overwrite)
        reference_path = resolve_reference_path(resolved.reference_path)
        started_at = datetime.now().astimezone()
        run_started = time.perf_counter()

        print_run_summary(
            start_at=started_at,
            shared_dir=shared_dir,
            text_source_label=resolved.text_source_label,
            output_path=output_path,
            reference_path=reference_path,
            reference_source_label=resolved.reference_source_label,
            text=text,
            chunk_mode=args.chunk_mode,
        )

        needs_ffmpeg_for_reference = reference_path.suffix.lower() != ".wav"
        ffmpeg_bin: str | None = (
            require_ffmpeg(args.ffmpeg)
            if needs_ffmpeg_for_reference
            else resolve_ffmpeg(args.ffmpeg)
        )

        if args.chunk_mode == "on" and ffmpeg_bin is None:
            ffmpeg_bin = require_ffmpeg(args.ffmpeg)

        prefer_chunking = args.chunk_mode == "on"

        if prefer_chunking:
            print("Synthesis mode: external manual chunking")
        else:
            print("Synthesis mode: model sentence splitting")

        work_dir = Path(
            tempfile.mkdtemp(prefix=f"{output_path.stem}_work_", dir=str(output_path.parent))
        )

        try:
            reference_wav = (
                convert_reference_to_wav(reference_path, work_dir, ffmpeg_bin)
                if needs_ffmpeg_for_reference
                else reference_path
            )

            device = select_device(args.device)
            print(f"Using device: {device}")
            print(f"Loading model: {args.model}")
            model_load_started = time.perf_counter()
            tts = load_tts(args.model, device=device)
            model_load_seconds = time.perf_counter() - model_load_started
            print(f"Model ready in {format_duration(model_load_seconds)}")

            synthesis_started = time.perf_counter()
            chunk_count = 0

            if not prefer_chunking:
                try:
                    print("Model-managed synthesis start")
                    synthesize_to_file(tts, text, reference_wav, output_path)
                    print("Model-managed synthesis done")
                except Exception as exc:
                    if args.chunk_mode != "auto":
                        raise
                    if ffmpeg_bin is None:
                        raise RuntimeError(
                            "Model-managed synthesis failed and ffmpeg is unavailable for external chunking fallback."
                        ) from exc
                    print(f"Model-managed synthesis failed, retrying with external chunking: {exc}")
                    chunk_count = synthesize_chunked(
                        tts=tts,
                        text=text,
                        reference_wav=reference_wav,
                        output_path=output_path,
                        work_dir=work_dir,
                        ffmpeg_bin=ffmpeg_bin,
                        max_chars=args.max_chars,
                    )
            else:
                chunk_count = synthesize_chunked(
                    tts=tts,
                    text=text,
                    reference_wav=reference_wav,
                    output_path=output_path,
                    work_dir=work_dir,
                    ffmpeg_bin=ffmpeg_bin,
                    max_chars=args.max_chars,
                )

            synthesis_seconds = time.perf_counter() - synthesis_started
            wall_seconds = time.perf_counter() - run_started
            finished_at = datetime.now().astimezone()

            print(f"Saved: {output_path}")
            print_finish_summary(
                started_at=started_at,
                finished_at=finished_at,
                wall_seconds=wall_seconds,
                model_load_seconds=model_load_seconds,
                synthesis_seconds=synthesis_seconds,
                text=text,
                chunk_count=chunk_count,
            )
            return 0
        finally:
            if args.keep_temp:
                print(f"Temporary files kept at: {work_dir}")
            else:
                shutil.rmtree(work_dir, ignore_errors=True)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1







