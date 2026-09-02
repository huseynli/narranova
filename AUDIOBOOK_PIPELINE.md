# Narranova prototype: local MOSS-TTS audiobook pipeline

> Historical prototype note for Narranova (narranova.app). This document
> records verified behavior worth carrying into production; it is not the
> Narranova architecture or an installation guide for the final product.
> `PROJECT_PLAN.md` is authoritative when the documents differ.

The prototype implementation described below has been removed from the
repository. The workflow remains documented solely as historical context for
the verified behaviors listed at the end of this file.

This disposable prototype leaves the working MOSS server, DLLs, models,
`stream_test.py`, and the sibling OpenMOSS source tree untouched. It supports
EPUB and UTF-8 TXT inputs using only the Python standard library.

Narranova will call MOSS as an external TTS service through a dedicated provider
adapter. The MOSS Windows executable, DLLs, model files, source patches, and
server WebUI must not be copied into or distributed from this repository.

## Preserved prototype workflow

Start the already-tested server in a separate PowerShell window:

```powershell
.\moss-tts-server.exe `
  --model .\models\moss-tts-local-1.5-q8_0.gguf `
  --host 127.0.0.1 `
  --port 8000
```

Prepare a book first. This extracts and chunks text but sends nothing to MOSS:

```powershell
python .\audiobook.py prepare C:\books\novel.epub
```

The default project is `audiobooks\novel`. Review its `chapters` and `chunks`
text files, then generate all pending chunks sequentially:

```powershell
python .\audiobook.py generate .\audiobooks\novel
```

Generation always uses the configured `narrator_reference.wav`, saved narrator
instruction, `stream=true`, `response_format=pcm`, and
`stream_chunk_frames=16`. It never sends `ref_text`. The pipeline does not
change the server context size.

After every chunk is complete, concatenate each chapter without re-encoding:

```powershell
python .\audiobook.py assemble .\audiobooks\novel
```

Check resume state at any time:

```powershell
python .\audiobook.py status .\audiobooks\novel
```

An interrupted `generating` chunk and any `failed` chunk are retried by the
next ordinary `generate` command. Completed chunks with missing or corrupt WAVs
are also repaired. To regenerate one known-bad completed take:

```powershell
python .\audiobook.py generate .\audiobooks\novel --chunk c003-p002 --force
python .\audiobook.py assemble .\audiobooks\novel --chapter c003
```

## Project layout

```text
audiobooks/novel/
  manifest.json              resumable state, settings, hashes, and metrics
  chapters/c001.txt          cleaned extracted chapter/spine text
  chunks/c001-p001.txt       exact text sent for one generation
  chunks/c001-p001.wav       independently replaceable generated take
  chapter_audio/c001.wav     lossless concatenation of verified chunk WAVs
  logs/events.jsonl          append-only attempts, timings, and errors
```

The default target is about 6,000 characters, based on the measured local test
of 7,248 characters producing about 7:07 of audio. Chunk boundaries prefer
paragraphs, then sentences, then word boundaries. The preparer verifies that
chunking did not drop any non-whitespace character and stores hashes for the
source, narrator reference, extracted chapters, and every chunk.

Final M4B encoding and chapter markers were intentionally not part of this
prototype. Narranova's production pipeline includes them as described in the
authoritative project plan.

## Lessons to carry forward

- Stream MOSS PCM with `stream=true`, `response_format=pcm`, and
  `stream_chunk_frames=16`; keep `max_new_tokens=6000` as the known-safe
  default until deliberate provider testing supports a change.
- Send the saved instruction and selected reference WAV for each book chunk,
  and omit `ref_text` for ordinary reference-audio cloning.
- Chunk at paragraph, sentence, then word boundaries and verify that chunking
  loses no non-whitespace source content.
- Write generation output to a temporary file, validate its audio frames, and
  atomically promote it only after success.
- Keep completed chunks independently replaceable and record hashes, attempts,
  timings, metrics, and errors for recovery.

These behaviors should receive focused tests in the production package. The
prototype's flat manifest, plain-text EPUB extraction, root script, package
shape, TXT support, and browser-local OpenMOSS UI are not compatibility
requirements and should not determine Narranova's design.
