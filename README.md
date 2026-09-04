# Narranova

Narranova is a self-hosted service for turning DRM-free EPUB books into
chapterized audiobooks with an external text-to-speech engine. Its Web UI takes
you from book import and narration planning through voice selection, resumable
generation, audio review, and final M4B export.

Narranova is designed for long-running audiobook work on a home server. It
keeps generation state in SQLite, verifies generated audio before accepting it,
and lets you pause, resume, repair, or selectively regenerate work without
starting the entire book again.

Website: [narranova.app](https://narranova.app)

> [!IMPORTANT]
> Narranova does not include a TTS model or runtime. Run
> [pwilkin/OpenMOSS](https://github.com/pwilkin/openmoss) separately, with their
> [moss-tts-local-gguf](https://huggingface.co/ilintar/moss-tts-local-gguf)
> model, then connect Narranova to its `/tts` endpoint.

## What Narranova does

A typical audiobook moves through this workflow:

1. Import a DRM-free EPUB into the library.
2. Review the extracted sections and exclude front matter, tables of contents,
   copyright pages, or anything else you do not want narrated.
3. Connect an OpenMOSS server and benchmark it for the host hardware.
4. Choose one of the included narrator pairs or build a reusable custom voice
   profile in Voice Lab.
5. Configure optional pauses, text normalization, and book-specific IPA
   pronunciations.
6. Create a narration job and review generated chunks as they arrive.
7. Regenerate individual chunks when a take does not sound right.
8. Build and download a chapterized M4B with book metadata and cover art.

The imported book text and narration plan remain unchanged. Enhancements are
applied to a separate, hashed TTS-input snapshot belonging to the job.

## Features

- Import DRM-free EPUBs and choose exactly which sections to narrate
- Extract book metadata, navigation, and cover art automatically
- Add deterministic heading and scene-break pauses without rewriting the prose
- Normalize text and define per-book IPA pronunciations
- Save, monitor, benchmark, and tune OpenMOSS connections
- Use included narrator pairs or build reusable custom voice profiles
- Generate in the background with reliable pause, resume, retry, and recovery
- Listen to, download, delete, or regenerate individual audio chunks
- Keep compact lossless FLAC working files instead of large WAV collections
- Export a chapterized M4B with metadata, cover art, and chapter markers
- Delete books together with their jobs and generated audiobooks
- Finalize completed projects to remove working audio and reclaim storage

## Quick start with Docker

Docker Compose is the recommended way to run Narranova. You need:

- Docker Engine with the Compose plugin
- A separately running OpenMOSS server reachable from the container

Clone the repository and start the service:

```console
git clone https://github.com/huseynli/NarraNova.git
cd NarraNova
docker compose up --detach --build
docker compose ps
```

Open [http://127.0.0.1:8787](http://127.0.0.1:8787).

The image includes FFmpeg and FFprobe, runs as an unprivileged user, exposes a
health check, and stores all persistent state in the `narranova-data` volume at
`/data`.

### Connect OpenMOSS

Run [pwilkin/OpenMOSS](https://github.com/pwilkin/openmoss) wherever you want to
host inference and use a model from
[moss-tts-local-gguf](https://huggingface.co/ilintar/moss-tts-local-gguf). Then
open Narranova's **Connections** page and enter its reachable `/tts` endpoint.

The default Compose file builds `narranova:local`, publishes port `8787`, uses
the `UTC` timezone, and persists `/data` in the `narranova-data` volume. Edit
`compose.yaml` directly if those defaults do not fit your installation.

Useful commands:

```console
docker compose logs --follow app
docker compose restart app
docker compose stop
docker compose down
```

`docker compose down` does not remove the `narranova-data` volume. Do not add
`--volumes` unless you intentionally want to delete the library, profiles,
jobs, and generated audio.

## Run locally without Docker

Narranova requires:

- Python 3.10 or newer
- FFmpeg and FFprobe available on `PATH`
- A separately running OpenMOSS service

Create a virtual environment and install Narranova from the repository:

```console
git clone https://github.com/huseynli/NarraNova.git
cd NarraNova
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
narranova web --data-dir ./data
```

On Windows, activate the environment with `.venv\Scripts\activate` instead.
Then open [http://127.0.0.1:8787](http://127.0.0.1:8787) and register the local
OpenMOSS endpoint, commonly `http://127.0.0.1:8000/tts`.

The server binds to loopback by default. To listen on a trusted LAN interface:

```console
narranova web --host 0.0.0.0 --port 8787 --data-dir ./data
```

Narranova does not currently provide authentication. Do not expose this port to
the public internet without an authenticating reverse proxy and appropriate
TLS/network controls.

### Run from a source checkout

For development or a quick test, installation is optional:

```console
PYTHONPATH=src python -m narranova web --data-dir ./data
```

## Create your first audiobook

After opening the Web UI:

1. Go to **Connections**, add the OpenMOSS `/tts` endpoint, and confirm that its
   health indicator turns green. The **Benchmark** page can measure and save a
   suitable streaming setting for that hardware.
2. Go to **Voices** to preview the included pairs. Use **Voice Lab** if you want
   to generate or upload a reference and save a custom instruction/audio pair.
3. Return to **Library**, import an EPUB, and open its book page.
4. Turn off any sections you do not want spoken. Changes save automatically.
5. Review **Narration Enhancement** settings and add IPA entries for names or
   unusual words when needed.
6. Select **Set up narration**, choose the connection and narrator pair, and
   create the job.
7. Start generation from the job page. You can listen to completed chunks while
   later chunks continue, pause safely, or regenerate an unsatisfactory take.
8. When all chunks are complete, build the audiobook and download the M4B.
9. After approving the result, optionally finalize storage to remove the
   lossless working masters.

Only DRM-free `.epub` files are currently accepted.

## Persistent data, backup, and restore

Narranova keeps the SQLite database, imported EPUBs, narration plans, voice
references, job snapshots, generated chunks, and final artifacts together in
one data directory. Docker uses `/data`; a local installation uses the path
passed with `--data-dir` or the `NARRANOVA_DATA_DIR` environment variable.

Stop Narranova before backing up the data directory so the SQLite database and
artifacts are captured consistently.

For the default Docker volume:

```console
docker compose stop
docker run --rm --volume narranova-data:/data:ro --volume "$PWD":/backup \
  alpine tar -czf /backup/narranova-backup.tgz -C /data .
docker compose start
```

For a local installation, stop the process and archive or copy the complete
directory supplied through `--data-dir`.

Restore a backup only while Narranova is stopped, and restore the complete data
directory into an empty destination. Do not restore only the SQLite file or
only the artifact folders; their records and hashes belong together.

## Updating

Back up the data directory before upgrading. Database migrations are
forward-only and run automatically when Narranova starts.

For Docker:

```console
git pull --ff-only
docker compose build --pull
docker compose up --detach
docker compose ps
```

For a local installation:

```console
git pull --ff-only
source .venv/bin/activate
python -m pip install --upgrade .
```

Restart the local Narranova process after installation.

## Storage behavior

Long audiobooks can produce many gigabytes of lossless working audio. Narranova
stores each generated chunk as FLAC rather than retaining raw provider WAVs, and
it assembles the final M4B without permanent chapter WAV intermediates.

Completed jobs keep their FLAC chunks so individual takes can be regenerated.
After listening to and approving the final M4B, use the job's storage
finalization action to delete those editable masters while retaining the M4B,
narration report, source book, job history, and voice snapshot.

Deleting a chunk removes its corresponding audio file and returns it to pending.
Deleting a job or book also removes its corresponding artifacts from disk.

## Optional CLI

The Web UI is the primary interface. A small CLI is available for automation
and diagnostics:

```console
narranova --help
narranova books --data-dir ./data
narranova job-status JOB_ID --data-dir ./data
narranova job-pause JOB_ID --data-dir ./data
narranova job-cancel JOB_ID --data-dir ./data
```

The CLI and Web UI use the same database and artifact directory. Avoid running
multiple Narranova instances against the same local data directory unless they
share the same filesystem and you understand the operational implications.

## Troubleshooting

### The OpenMOSS connection is red

- Confirm that the URL ends in `/tts` and that OpenMOSS is running.
- Check host firewalls and whether OpenMOSS is listening on an address reachable
  from the container or Narranova machine.
- Open the connection's benchmark page and use **Test connection** for returned
  error details.

### M4B assembly fails

Docker already includes FFmpeg. For a local installation, verify both tools:

```console
ffmpeg -version
ffprobe -version
```

Narranova retains verified FLAC chunks if assembly fails, so the export can be
retried after fixing the local FFmpeg installation.

### Docker starts but cannot write `/data`

The supplied Compose file uses a managed named volume with the correct image
permissions. If you replace it with a bind mount, ensure host UID/GID `10001`
can write the mounted directory.

## Development

Run the test suite from the repository root:

```console
PYTHONPATH=src python -m unittest discover -s tests -v
```

Project decisions and planned work are tracked in `PROJECT_PLAN.md`.
