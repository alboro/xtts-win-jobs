# xtts-win-jobs

Minimal Windows-first XTTS v2 toolkit for local voice cloning with CUDA.

This is a vibecoding project for personal use. It is intentionally pragmatic and narrow: a small local XTTS toolchain that works on Windows with CUDA, shared voice references, and a simple async jobs API.

The repo has two layers:

- a local CLI for direct synthesis
- a native async FastAPI jobs server for polling-based workflows

## Features

- Windows-native, no Docker required
- XTTS v2 voice cloning
- Russian-first workflow
- file-first CLI input
- automatic reference discovery from `shared/`
- newest matching reference wins by prefix, regardless of format
- ffmpeg-based reference conversion when needed
- model-managed sentence splitting by default, with external chunking only as fallback or on demand
- XTTS tuning knobs exposed in both CLI and async jobs API
- async `/v1/tts/jobs` API with polling and audio download
- start/end timing, estimated speech duration, and throughput logs

## Requirements

- Windows 10 or 11
- NVIDIA GPU for the intended fast path
- Python 3.11 recommended
- ffmpeg
- acceptance of the XTTS CPML terms for first model download

Tested stack in this repo:

- Python `3.11`
- `torch 2.8.0+cu128`
- `torchaudio 2.8.0+cu128`
- `coqui-tts 0.27.5`
- `spacy 3.8+`

## Quick Start

```cmd
scripts\bootstrap_windows.cmd
tts-win.cmd --doctor
```

Put your files into `shared/`:

- `text.txt`
- `reference.wav`

Run the CLI:

```cmd
tts-win.cmd text.txt .\output\speech.wav
```

Russian audiobook preset to try first:

```cmd
tts-win.cmd text.txt .\output\speech.wav ^
  --language ru ^
  --split-sentences on ^
  --enable-text-splitting on ^
  --speed 1.0 ^
  --temperature 0.6 ^
  --top-p 0.8 ^
  --repetition-penalty 2.2
```

## CLI Reference Lookup

By default the CLI searches inside `shared/` for files whose name starts with `reference`.

Examples:

- `reference.wav`
- `reference_2026-04-04.m4a`
- `reference_take2.flac`

If several files match, the newest file is chosen.

You can also pass a different prefix:

```cmd
tts-win.cmd text.txt .\output\speech.wav alla
```

Or an explicit file:

```cmd
tts-win.cmd text.txt .\output\speech.wav .\shared\alla_2026-04-04.m4a
```

## Async Server

Start the server:

```cmd
tts-win-server.cmd --host 127.0.0.1 --port 8020
```

This `127.0.0.1:8020` endpoint is the default target expected by the local `epub_to_audiobook` helper script.

Health check:

```cmd
curl http://127.0.0.1:8020/health
```

Create a job with a shared voice prefix:

```json
POST /v1/tts/jobs
{
  "input": "Привет. Это тест.",
  "voice": "reference",
  "response_format": "wav"
}
```

Create a job with a per-request uploaded reference:

```json
POST /v1/tts/jobs
{
  "input": "Привет. Это тест.",
  "voice": "reference",
  "response_format": "wav",
  "reference_audio_base64": "<base64-or-data-uri>",
  "reference_audio_filename": "sample.m4a"
}
```

Create a job with XTTS tuning overrides:

```json
POST /v1/tts/jobs
{
  "input": "Привет. Это тест длинной озвучки.",
  "voice": "reference",
  "response_format": "wav",
  "language": "ru",
  "split_sentences": true,
  "enable_text_splitting": true,
  "speed": 1.0,
  "temperature": 0.6,
  "top_p": 0.8,
  "repetition_penalty": 2.2,
  "sound_norm_refs": true
}
```

Poll status:

```cmd
curl http://127.0.0.1:8020/v1/tts/jobs/<job_id>
```

Download audio when ready:

```cmd
curl http://127.0.0.1:8020/v1/tts/jobs/<job_id>/audio --output result.wav
```

## API Notes

- `voice` is a local voice identifier or shared reference prefix, not an OpenAI-hosted voice.
- `reference_audio_base64` overrides `voice` for that request.
- `reference_audio_base64` may be raw base64 or a `data:` URI.
- `/v1/tts/jobs/{id}/audio` returns `409` until the final file exists.
- `response_format` is currently `wav` only.
- the server uses one in-process worker, which keeps GPU generation serialized and simple.
- jobs are persisted under `.data/jobs/`.
- completed jobs are cleaned up automatically.
- by default, terminal jobs are kept for `24` hours, and already-downloaded jobs are kept for `6` hours after the latest download.
- cleanup runs on a background interval and also opportunistically on API requests.
- the first request after server start may spend noticeable time on model warm-up.

Server-level defaults can also be tuned at startup:

```cmd
tts-win-server.cmd ^
  --host 127.0.0.1 ^
  --port 8020 ^
  --language ru ^
  --split-sentences on ^
  --enable-text-splitting on ^
  --speed 1.0 ^
  --temperature 0.6 ^
  --top-p 0.8 ^
  --repetition-penalty 2.2
```

## Chunking Strategy

`tts-win` prefers the model's own sentence splitting first and lets XTTS split overlong sentences internally when needed.

That usually sounds better than manual chunk boundaries.

`--chunk-mode` means:

- `auto`: try model-managed synthesis first, fall back to external manual chunking only if needed
- `on`: force external manual chunking
- `off`: never use external manual chunking

## XTTS Quality Notes

The best quality improvements usually come from three places:

- a cleaner and steadier reference voice
- keeping XTTS close to its natural defaults instead of pushing speed too hard
- reducing randomness a bit for long-form narration

Practical notes for Russian long-form speech:

- Keep `speed` close to `1.0`. Bigger deviations can add artifacts.
- If the model sounds too jumpy or inconsistent, try lowering `temperature` first.
- If you hear long silences or filler-like tails, try raising `repetition_penalty` slightly.
- Keep `split_sentences` and `enable_text_splitting` on until you have a concrete reason to disable them.
- `sound_norm_refs` can help when the reference clip is noticeably uneven in loudness.

One important improvement still worth considering later: XTTS v2 supports multiple reference files for better cloning, while this repo currently chooses the newest matching reference file only.

## Project Layout

- `src/tts_win/`: CLI and server implementation
- `scripts/`: Windows bootstrap helpers
- `shared/`: your local text and reference files
- `output/`: generated audio
- `.data/jobs/`: persisted async jobs

The contents of `shared/`, `output/`, and local research folders are intentionally ignored by git.

## Responsible Use

Use only voices and source material you have the right to use.

XTTS models are subject to the Coqui Public Model License. Setting `COQUI_TOS_AGREED=1` means you agree to those terms.

## License

MIT for the code in this repository. Third-party models and dependencies keep their own licenses.
