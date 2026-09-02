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

Run the foundation tests:

```console
PYTHONPATH=src python -m unittest discover -s tests/unit -v
```

The older root `audiobook.py`, `audiobook_pipeline/`, and `webui/` trees are
prototype inputs. New product code must not import them.
