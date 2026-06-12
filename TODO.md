# TODO

- Add first-class multi-language support instead of the current Russian-first defaults.
- Separate voice presets and reference discovery rules by language.
- Expose language-aware defaults through both CLI and async API.
- Add voice listing and metadata endpoints so clients can discover available local voices safely.
- Add optional callback/webhook delivery on top of the current polling jobs flow.
- Evaluate XTTS for **Latvian** as a self-hosted alternative to Google TTS in
  light_tts (`gtts_lv`). XTTS is multilingual voice-cloning but Latvian quality
  is reportedly mediocre — test with a good `lv` reference clip and compare
  against gtts_lv / Azure (Everita/Nils) before wiring it in as a `light_tts`
  engine. Context: contabo/projects/light_tts, light-jobs routing (engine_for_voice).
