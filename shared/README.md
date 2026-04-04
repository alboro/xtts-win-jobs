# shared

Drop local input files here.

Typical workflow:

- `text.txt` for the text to read
- `reference.wav` for the default voice reference

Reference lookup rules:

- default prefix is `reference`
- supported formats include `wav`, `mp3`, `m4a`, `flac`, `ogg`, `opus`, `aac`, `wma`, `mp4`, `mkv`, `webm`
- if multiple files share the same prefix, the newest file wins

Examples:

- `reference.wav`
- `reference_2026-04-04.m4a`
- `alla_take3.flac`
