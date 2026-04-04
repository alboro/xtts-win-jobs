# tts-win

Minimal Windows-first XTTS v2 CLI for local voice cloning with CUDA.

This project is intentionally small. Most XTTS repositories are web UIs, API servers, or training stacks. `tts-win` is the opposite: a local Windows CLI that reads a text file, finds a reference voice, runs XTTS on NVIDIA GPU, and writes a `.wav`.

## Features

- Windows-native CLI, no Docker required
- XTTS v2 voice cloning
- Russian-first workflow
- file-first input
- automatic reference discovery from `shared/`
- newest matching reference wins by prefix, regardless of format
- ffmpeg-based reference conversion when needed
- model-managed sentence splitting by default, plus XTTS internal long-sentence splitting
- external manual chunking only as fallback or on demand
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

## Quick Start

```cmd
scripts\bootstrap_windows.cmd
tts-win.cmd --doctor
```

Put your files into `shared/`:

- `text.txt`
- `reference.wav`

Then run:

```cmd
tts-win.cmd text.txt .\output\speech.wav
```

## How Reference Lookup Works

By default the tool searches inside `shared/` for files whose name starts with `reference`.

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

## Usage

Default mode uses a text file as the first positional argument:

```cmd
tts-win.cmd text.txt .\output\speech.wav
```

Inline text is still supported:

```cmd
tts-win.cmd --text "Привет. Это тест." .\output\hello.wav
```

Useful options:

```cmd
tts-win.cmd --overwrite text.txt .\output\speech.wav
tts-win.cmd --chunk-mode auto text.txt .\output\speech.wav
tts-win.cmd --chunk-mode on text.txt .\output\speech.wav
tts-win.cmd --chunk-mode off text.txt .\output\speech.wav
tts-win.cmd --reference-prefix reference text.txt .\output\speech.wav
```

## Chunking Strategy

`tts-win` now prefers the model's own sentence splitting first, and also lets XTTS split overlong sentences internally when needed.

That usually sounds better than manual chunk boundaries.

`--chunk-mode` means:

- `auto`: try model-managed synthesis first, fall back to external manual chunking only if needed
- `on`: force external manual chunking
- `off`: never use external manual chunking

## Project Layout

- `src/tts_win/`: CLI implementation
- `scripts/`: Windows bootstrap helpers
- `shared/`: your local text and reference files
- `output/`: generated audio

The contents of `shared/` and `output/` are intentionally ignored by git.

## Responsible Use

Use only voices and source material you have the right to use.

XTTS models are subject to the Coqui Public Model License. Setting `COQUI_TOS_AGREED=1` means you agree to those terms.

## License

MIT for the code in this repository. Third-party models and dependencies keep their own licenses.

