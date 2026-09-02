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
