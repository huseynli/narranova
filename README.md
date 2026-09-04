# Narranova

Narranova is a self-hostable EPUB-to-audiobook application. It creates a
reviewable narration plan, generates audio through external TTS providers, and
produces a chapterized M4B without retaining duplicate chapter PCM files.

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
- Controlled OpenMOSS connection benchmarks with Auto-tune recommendations
- Temporary provider-WAV validation and 48 kHz mono FLAC normalization
- Direct FLAC-to-M4B assembly without persistent chapter WAVs
- Source-mapped narration-map export
- FFmpeg-backed chapterized M4B export with metadata and cover art

The OpenMOSS adapter deliberately preserves `stream=true`, PCM output,
`stream_chunk_frames=16`, and the 6,000-token default. Reference cloning never
sends `ref_text`. Narranova does not contain or launch the MOSS runtime.
Connections store performance settings only. VoiceLab stores optional explicit
quality/sampling overrides with a narrator profile; blank controls use the
OpenMOSS engine default and are omitted from requests.

The disposable audiobook and OpenMOSS WebUI prototypes have been removed.
Verified lessons were reimplemented behind Narranova's production boundaries;
the external MOSS runtime remains a separate service.

## Docker

Build and start Narranova with its persistent named volume:

```console
cp .env.example .env
docker compose up --detach --build
docker compose ps
```

Open `http://127.0.0.1:8787`. The image runs as an unprivileged user, includes
FFmpeg for FLAC normalization and M4B assembly, and stores the SQLite database
and all artifacts in the `narranova-data` volume mounted at `/data`.

OpenMOSS is not included in this Compose project. If it runs on the Docker
host, register `http://host.docker.internal:8000/tts` in Narranova rather than
`127.0.0.1`; a host-gateway alias is provided for Linux and Docker Desktop.
Use an ordinary service hostname instead when OpenMOSS runs elsewhere on a
trusted network.

Narranova has no built-in authentication yet. Keep port 8787 private or place
it behind an authenticating reverse proxy before exposing it beyond a trusted
network.

### Backup and upgrade

Stop the application before copying the SQLite database and artifacts so the
backup is consistent:

```console
docker compose stop
docker run --rm --volume narranova-data:/data:ro --volume "$PWD":/backup \
  alpine tar -czf /backup/narranova-backup.tgz -C /data .
docker compose start
```

Restore only into an empty `narranova-data` volume while the application is
stopped. To upgrade a source checkout, back up first, then rebuild and restart;
forward-only database migrations run automatically at startup:

```console
docker compose build --pull
docker compose up --detach
docker compose ps
```

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

Running `job-run` again resumes pending or failed work and skips completed FLAC
masters whose hashes and audio streams still validate. From another shell,
`job-pause JOB_ID` requests a pause after the current provider request finishes;
running `job-run JOB_ID` resumes it. `job-cancel JOB_ID` requests an immediate
stop, discards the active partial response, and keeps already verified chunks.

After every chunk is complete, build and inspect the final deliverables:

```console
PYTHONPATH=src python -m narranova job-assemble JOB_ID --data-dir ./data
PYTHONPATH=src python -m narranova job-artifacts JOB_ID --data-dir ./data
```

M4B export and FLAC normalization require `ffmpeg` and `ffprobe` on the
Narranova host. Verified FLAC masters and the narration map are retained if M4B
encoding fails, so the build can be retried after the tools are installed.

Jobs retain their lossless FLAC masters by default for individual chunk
regeneration. After approving the M4B, remove those editable sources while
keeping the final files and job history:

```console
PYTHONPATH=src python -m narranova job-compact JOB_ID --data-dir ./data
```

Running `job-run` on a compacted job regenerates its editable masters and
invalidates the old derived M4B so it can be rebuilt.

## Web interface

Run the local server against the same data directory used by the CLI:

```console
PYTHONPATH=src python -m narranova web --data-dir ./data
```

Open `http://127.0.0.1:8787`. The web interface supports EPUB upload, library
and narration-plan review, per-section narration inclusion, a dedicated TTS
connections page, two packaged OpenMOSS narrator pairs, and a book-independent
Voice Lab for comparing short custom auditions. The built-in pairs include their
exact instruction, reference text, and WAV; users can preview them in the voice
library and select one directly when creating a narration job. Voice Lab first
creates reference candidates from narration
direction alone or an optional uploaded/generated source, then pairs the chosen
reference with final instructions as a named reusable profile. Existing profiles
are not offered as the new profile's final pair. Connections and profiles can be
edited, renamed, or deleted. Each connection can be health-tested and benchmarked
with fixed text, narrator, instruction, seed, and engine-default sampling. Auto-tune
measures streaming decode batches 16 through 512, recommends the smallest result
within 3% of peak throughput, and can apply it to future jobs. Each generation job
owns immutable connection-performance and voice snapshots
and copied reference WAV. Profiles used by unfinished jobs are visibly marked and
protected from deletion until those jobs complete or are deleted; deleting the
profile afterward removes its own files without breaking completed jobs. Saving a
profile removes its discarded draft audio. The interface also supports durable job creation,
background generation, pause/resume, status, verified FLAC chunk playback,
pause-after-chapter, selective chunk regeneration, direct M4B assembly,
storage finalization, and
final artifact downloads.
Generation uses durable SQLite leases so separate CLI and web processes cannot
run the same job—or concurrent requests against the same TTS connection—at the
same time. Transient connection failures receive bounded retries, and each
attempt records timing and provider diagnostics. Voice Lab auditions run in the
background and report completion without blocking the HTTP request.
VoiceLab's collapsed advanced section exposes optional OpenMOSS sampling controls
and manual candidate seeds. Audiobook chunks use deterministic per-chunk seeds so
retries and regeneration remain reproducible without making every chunk identical.
Saving section choices creates an immutable plan revision; existing jobs retain
their original plan and new jobs use the latest revision. The server binds to
loopback by default; use `--host` deliberately when exposing it to a trusted
network.

Each book also has deterministic Narration Enhancement settings. New jobs can
add configurable chapter, section-heading, and scene-break pauses using native
OpenMOSS `[pause X.Ys]` controls, normalize common TTS typography, and apply a
book-specific `term = IPA` pronunciation dictionary. Narranova stores the
unchanged chunk text and a separate, hashed provider-input snapshot, so editing
book settings never changes an existing job or the author's extracted prose.
