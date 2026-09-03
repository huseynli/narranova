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
- Lossless chapter-WAV assembly with selective rebuilds
- Source-mapped narration-map export
- FFmpeg-backed chapterized M4B export with metadata and cover art

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

PYTHONPATH=src python -m narranova voice-create-openmoss PROVIDER_ID \
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

After every chunk is complete, build and inspect the final deliverables:

```console
PYTHONPATH=src python -m narranova job-assemble JOB_ID --data-dir ./data
PYTHONPATH=src python -m narranova job-artifacts JOB_ID --data-dir ./data
```

M4B export requires `ffmpeg` and `ffprobe` on the Narranova host. Chapter WAVs
and the narration map are retained if M4B encoding fails, so the build can be
retried after the tools are installed.

## Web interface

Run the local server against the same data directory used by the CLI:

```console
PYTHONPATH=src python -m narranova web --data-dir ./data
```

Open `http://127.0.0.1:8787`. The web interface supports EPUB upload, library
and narration-plan review, per-section narration inclusion, a dedicated TTS
connections page, nine packaged OpenMOSS narrator pairs, and a book-independent
Voice Lab for comparing short custom auditions. The built-in pairs include their
exact instruction, reference text, and WAV; users can preview them in the voice
library and select one directly when creating a narration job. Voice Lab first
creates reference candidates from narration
direction alone or an optional uploaded/generated source, then pairs the chosen
reference with final instructions as a named reusable profile. Existing profiles
are not offered as the new profile's final pair. Connections and profiles can be
edited, renamed, or deleted. Each generation job owns an immutable voice snapshot
and copied reference WAV. Profiles used by unfinished jobs are visibly marked and
protected from deletion until those jobs complete or are deleted; deleting the
profile afterward removes its own files without breaking completed jobs. Saving a
profile removes its discarded draft audio. The interface also supports durable job creation,
background generation, pause/resume, status, verified chunk playback, selective
chunk regeneration, chapter assembly, and final artifact downloads.
Saving section choices creates an immutable plan revision; existing jobs retain
their original plan and new jobs use the latest revision. The server binds to
loopback by default; use `--host` deliberately when exposing it to a trusted
network.
