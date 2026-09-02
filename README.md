# Narranova

Narranova is a self-hostable EPUB-to-audiobook application. It creates a
reviewable narration plan, generates audio through external TTS providers, and
produces chapter audio and a chapterized M4B.

The production implementation lives under `src/narranova`. MOSS/OpenMOSS is an
external service; its runtime, models, and Windows dependencies are not part of
this repository.

## Development

Narranova currently requires Python 3.10 or newer. Initialize a local data
directory without installing the package:

```console
PYTHONPATH=src python -m narranova init --data-dir ./data
```

Import and inspect a DRM-free EPUB:

```console
PYTHONPATH=src python -m narranova import ./book.epub --data-dir ./data
PYTHONPATH=src python -m narranova books --data-dir ./data
PYTHONPATH=src python -m narranova plan BOOK_ID --data-dir ./data
```

Run the foundation tests:

```console
PYTHONPATH=src python -m unittest discover -s tests/unit -v
```

## Implemented production slices

- SQLite-backed application initialization and versioned migrations
- Safe artifact paths and atomic writes
- Defensive EPUB ZIP/XML parsing
- Metadata, cover, spine, and readable-element extraction
- Stable, source-mapped narration plans
- Provider-sized chunk planning with content-loss verification
- Dedicated external OpenMOSS streaming-PCM adapter
- WAV validation before atomic artifact promotion

The OpenMOSS adapter deliberately preserves `stream=true`, PCM output,
`stream_chunk_frames=16`, and the 6,000-token default. Reference cloning never
sends `ref_text`. Narranova does not contain or launch the MOSS runtime.

The disposable audiobook and OpenMOSS WebUI prototypes have been removed.
Verified lessons were reimplemented behind Narranova's production boundaries;
the external MOSS runtime remains a separate service.

## External OpenMOSS generation

With OpenMOSS already running separately, register its endpoint. Each command
prints the ID needed by the next command:

```console
PYTHONPATH=src python -m narranova provider-add-openmoss "Local MOSS" \
  http://127.0.0.1:8000/tts --data-dir ./data

PYTHONPATH=src python -m narranova voice-create-openmoss BOOK_ID PROVIDER_ID \
  --reference ./approved-reference.wav \
  --instruction "A warm, restrained fiction audiobook narrator." \
  --data-dir ./data

PYTHONPATH=src python -m narranova job-create BOOK_ID VOICE_PROFILE_ID \
  --data-dir ./data
PYTHONPATH=src python -m narranova job-run JOB_ID --data-dir ./data
PYTHONPATH=src python -m narranova job-status JOB_ID --data-dir ./data
```

Running `job-run` again resumes pending or failed work and skips completed WAVs
whose hashes and audio frames still validate. From another shell,
`job-pause JOB_ID` requests a pause after the current provider request finishes;
running `job-run JOB_ID` resumes it.
