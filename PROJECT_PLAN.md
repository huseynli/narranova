# Narranova — Project Plan

Last updated: 2026-09-03

Product name: **Narranova**  
Canonical domain: **narranova.app**  
Canonical repository: this repository

## Purpose

Build Narranova, a lightweight, self-hostable application and CLI that accepts
an EPUB, creates a reviewable narration plan, generates a resumable audiobook
through a selected TTS provider, and produces a metadata-rich, chapterized M4B
without retaining duplicate chapter PCM files.

The first release supports:

- An external OpenMOSS server using the working OpenMOSS-specific HTTP API.
- An optional, separately deployed Kokoro service.
- A small Dockerized application that does not bundle TTS models.

The second major iteration will generate EPUB 3 readaloud books containing
audio and SMIL Media Overlays. These files can be imported into Storyteller as
already-aligned readaloud books.

This document is the source of truth for implementation decisions and scope.
Update it when a decision changes.

## Product principles

1. Never silently lose readable source text.
2. Never generate an entire book or long chapter as one TTS request.
3. Every expensive operation must be resumable and independently retryable.
4. Completed audio is immutable unless the user explicitly regenerates it.
5. Keep the application image small; inference engines and models are separate.
6. Preserve EPUB structure and source mappings from the beginning so readaloud
   support does not require redesigning stored books later.
7. Treat provider capabilities explicitly; do not pretend every TTS engine
   supports voice descriptions, reference cloning, seeds, or timestamps.
8. Use safe temporary output, validate it, and atomically promote it only after
   successful completion.
9. Default to one active generation request per worker/provider to avoid
   overloading homelab inference servers.
10. Preserve the proven OpenMOSS streaming path and GPU-selection workaround.
11. Benchmark provider instances with fixed, book-independent material before
    asking users to commit hours of generation time.

## Deployment architecture

### Core application

The primary image contains:

- Python application and CLI.
- HTTP API and server-rendered web UI.
- SQLite support.
- EPUB parsing and validation code.
- FFmpeg and ffprobe for audio inspection, encoding, metadata, and M4B output.
- A persistent, resumable job worker.

The primary image does not contain:

- Kokoro weights or inference dependencies.
- MOSS/OpenMOSS binaries or weights.
- Whisper or forced-alignment models in v1.
- Commercial-provider SDKs unless a provider cannot use ordinary HTTP.

Persistent application data is mounted at `/data`.

### Kokoro

Kokoro runs as an optional service in Docker Compose, not inside the core image.
This allows users who do not use Kokoro to avoid its dependencies and supports
different CPU/GPU images independently.

Model weights live in a persistent model volume and are downloaded at first
setup or installed by an administrator. They are not baked into our image.

Quantization and model version are properties of a configured provider
instance. An administrator can register multiple endpoints, for example:

- `Kokoro CPU Q8`
- `Kokoro CPU Q4`
- `Kokoro GPU FP16`

A regular user chooses one of the configured provider instances. The application
does not unload and reload arbitrary model variants for each book request.

The first Kokoro integration may target an existing OpenAI-compatible Kokoro
HTTP service. The adapter must query or configure its available voices and
capabilities rather than hard-coding assumptions into the core pipeline.

### MOSS

MOSS is always external to this project. Users operate OpenMOSS themselves and
register its endpoint in the application.

The current OpenMOSS endpoint is not equivalent to the standard OpenAI
`/v1/audio/speech` API because it supports fields such as `instruction`,
`reference_wav_b64`, `stream_chunk_frames`, and streamed PCM. It therefore uses
a dedicated `OpenMossProvider` adapter.

The adapter must preserve the known-safe defaults:

- `stream=true`
- `response_format=pcm`
- `stream_chunk_frames=16`
- `max_new_tokens=6000` as an automatic safety/output ceiling
- No `ref_text` during ordinary reference-audio voice cloning
- The saved instruction and selected reference audio for every book chunk

An OpenMOSS connection stores request-level performance settings, principally
`stream_chunk_frames`. Narrator sampling and quality settings belong to Voice
Lab profiles and are not connection benchmark controls.

Do not change the working OpenMOSS model context or replace its runtime DLLs as
part of this application.

## Application architecture

The Python core is shared by the CLI, web application, and worker.

Major components:

- `EpubParser`: validates the upload and reads OPF metadata, manifest, spine,
  navigation, cover, XHTML, and readable elements.
- `NarrationPlanner`: cleans text, marks optional content, creates stable
  narration units, and packs those units into provider-appropriate chunks.
- `TTSProvider`: common provider interface and capability discovery.
- `VoiceStudio`: excerpt selection, preview generation, take comparison, and
  saved voice profiles.
- `JobEngine`: persistent pause/resume/retry/cancel state machine.
- `ArtifactStore`: paths, hashes, atomic writes, validation, retention, and
  download artifacts.
- `AudioAssembler`: FLAC-master validation, ordered direct assembly,
  AAC encoding, chapter markers, metadata, and M4B packaging.
- `WebApp`: upload, plan review, provider selection, voice studio, progress,
  error recovery, and downloads.
- `CLI`: automation and headless access to the same core operations.

V1 should use SQLite and a small persistent worker. It should not require
PostgreSQL, Redis, Celery, or a separate message broker.

## Initial production repository structure

Narranova starts as one installable Python application with a shared domain
core. The HTTP application, CLI, and worker are entry points into that core,
not separate implementations. Use a `src` layout so tests exercise the
installed package rather than importing accidentally from the repository root.

```text
narranova/
  pyproject.toml
  README.md
  LICENSE
  .env.example
  src/
    narranova/
      __init__.py
      config.py
      domain/
        books.py
        narration.py
        voices.py
        jobs.py
        artifacts.py
      application/
        ingest.py
        planning.py
        voice_studio.py
        generation.py
        assembly.py
        export.py
      epub/
        parser.py
        safety.py
        metadata.py
        narration_units.py
      providers/
        base.py
        openmoss.py
        kokoro.py
        openai_compatible.py
      audio/
        validation.py
        masters.py
        chapters.py
        m4b.py
      persistence/
        database.py
        repositories.py
        migrations/
      jobs/
        engine.py
        worker.py
        recovery.py
      artifacts/
        store.py
        layout.py
      web/
        app.py
        routes/
        templates/
        static/
      cli/
        main.py
        commands/
  tests/
    unit/
    integration/
    recovery/
    fixtures/
  deploy/
    Dockerfile
    compose.yaml
    compose.kokoro.yaml
  docs/
    architecture/
    operations/
    decisions/
  scripts/
```

The boundaries have the following intent:

- `domain/` contains provider- and interface-independent state and rules.
- `application/` coordinates use cases and transaction boundaries.
- `epub/`, `providers/`, `audio/`, `persistence/`, and `artifacts/` are adapters
  around external formats, services, and storage.
- `jobs/` owns durable execution and recovery; web requests do not perform
  long-running synthesis inline.
- `web/` contains Narranova's server-rendered UI. The imported OpenMOSS WebUI
  is server tooling and is not a foundation for the product UI.
- `deploy/` packages only Narranova and FFmpeg/ffprobe. It does not contain
  MOSS executables, DLLs, models, patches, or other Windows runtime files.

SQLite is the authoritative store for product and job state. The artifact tree
under `/data` holds immutable or reproducible files; it is not a second job
database. Database records refer to artifacts by stable IDs, relative paths,
hashes, and schema versions.

The imported `audiobook_pipeline` package, root `audiobook.py`, and OpenMOSS
`webui/` were migration inputs only and have been removed. Production modules
are implemented against the boundaries above, with focused behavior ported
when tests demonstrate its value. Do not add compatibility shims for the
prototype's module names, manifest schema, directory layout, TXT support, or
browser-local history.

## Provider interface

Every provider implements the conceptual interface:

```text
health()
list_models()
list_voices()
capabilities()
synthesize(request) -> streamed or complete audio result
```

Provider capabilities include at least:

```json
{
  "voice_presets": false,
  "voice_description": false,
  "reference_audio": false,
  "stochastic_variants": false,
  "seed": false,
  "timestamps": false,
  "streaming": false,
  "supported_languages": [],
  "supported_audio_formats": []
}
```

Initial providers:

1. `OpenMossProvider`
2. `KokoroProvider`
3. A generic `OpenAICompatibleSpeechProvider` foundation for future engines

Commercial providers are deferred, but the contract must allow API keys,
request costs, rate limits, provider request IDs, and usage metadata later.

Secrets are stored only by the backend and are never returned to the browser.
Provider configuration must be administrator-only in a multi-user deployment.

## Connection benchmarking and tuning

The Connections page owns endpoint configuration, health testing, available
OpenMOSS/model information, and performance benchmarking. Its OpenMOSS
benchmark uses a fixed original passage of roughly one printed page, built-in
narrator pair 04, its matching instruction, a fixed seed, and engine-default
sampling. It never reads text from an imported book.

The user-facing performance control is `stream_chunk_frames`, labelled
**Streaming decode batch**, with supported benchmark values `16`, `32`, `64`,
`128`, `256`, and `512`. One MOSS-TTS Local frame is approximately 80 ms, so
the UI may show the approximate audio represented by each batch. Long-form
generation continues to use streamed PCM. `max_new_tokens` is a safe automatic
output ceiling, not a speed control, and does not appear among the ordinary
benchmark controls.

A single run may measure one selected batch. Auto-tune measures all six in
ascending order, determines the best realtime throughput, and recommends the
smallest batch within 3% of that peak. Applying a result persists the selected
batch and recommendation with the connection. New generation jobs snapshot
the connection configuration so later tuning cannot change an in-progress
book. Voice Lab auditions use the connection's currently applied performance
settings.

Each result reports generated audio duration, TTS wall time, time to first
audio when available, realtime speed, conventional real-time factor, and the
tested batch. Any 40-hour projection is labelled **TTS generation only** and
excludes queueing, retries, normalization, and M4B assembly.

Hardware-side guidance remains informational because Narranova does not manage
the external OpenMOSS process. It may discuss quantization, GPU offload, flash
attention, context, and server launch configuration. Lower quantization reduces
memory use and memory-bandwidth requirements and may improve throughput
depending on the model, hardware, and backend. Keep flash attention enabled
when supported unless benchmarking shows otherwise.

## EPUB ingestion and narration plan

V1 supports DRM-free EPUB only.

Unlike the initial prototype, ingestion must preserve the connection between
spoken text and the original XHTML. A narration unit contains at least:

```json
{
  "id": "c003-p012-s004",
  "spine_index": 3,
  "document": "Text/chapter03.xhtml",
  "element_id": "narration-c003-p012-s004",
  "display_text": "The door opened slowly.",
  "spoken_text": "The door opened slowly.",
  "enabled": true
}
```

The application must:

- Preserve spine order.
- Preserve title, author, language, identifiers, cover, series metadata when
  available, and source XHTML locations.
- Assign stable IDs to readable elements or sentence spans.
- Separate display text from normalized spoken text.
- Record every cleaning or exclusion decision.
- Allow the user to review chapters and disable front matter, navigation,
  copyright pages, footers, promotional back matter, or other unwanted text.
- Verify that enabled readable text is represented exactly once in the
  narration plan.
- Store source, plan, chapter, and chunk hashes.

The narration plan is immutable after generation begins. Editing it creates a
new plan revision and invalidates only affected downstream chunks.

## Voice Lab

Narrator profiles are workspace-wide resources, independent of books. A saved
profile can be selected for any book when creating a narration job, provided it
is compatible with the chosen TTS connection. The UI changes according to
provider capabilities: instruction, reference-audio, voice-picker, language,
speed, and other controls appear only when the selected provider supports them.

Narranova also packages a small immutable catalog of built-in OpenMOSS narrator
pairs. Each pair contains a validated reference WAV, the exact text read in that
sample, and its matching narration instruction. Users can preview these pairs
in the voice library and select one directly when creating a book-generation
job. Built-in pairs are compatible with any configured OpenMOSS connection and
remain separate from editable, deletable user profiles. A job copies its chosen
built-in reference and instruction into its own immutable snapshot.

### MOSS workflow

1. User opens the dedicated Voice Lab without selecting or importing a book.
2. User selects an editable example narration instruction or writes one.
3. The application supplies a few short, editable test sentences that cover
   pacing, punctuation, and emotional movement.
4. Step one creates the reference audio. The user can generate from the
   instruction alone. Optionally, the user supplies a clean source WAV or uses
   a previously generated candidate as the next source reference.
5. The application generates one audition take per request. The user can
   compare retained draft takes, change the instruction or test text, and
   regenerate until satisfied without automatically spending inference time on
   unwanted variants.
6. Step two pairs and saves the profile. The user names it, chooses a reference
   created or uploaded in the current draft, and confirms the final instruction.
   Existing saved profiles are not offered as the new profile's final reference.
   The selected pair and its hashes become a reusable workspace voice profile.
7. After promotion, all unselected audition takes and other scratch files for
   that draft are deleted. Abandoned drafts are also subject to automatic
   expiry.
8. Optional overrides for text/audio temperature, top-p, top-k, audio repetition
   penalty, and seed live in a collapsed **Advanced quality & sampling** section.
   Blank values mean Engine default and are omitted from the OpenMOSS request.
   Each generated take records the explicit settings and seed that produced it.
9. Subsequent audiobook chunks use the same instruction and selected audio as
   reference audio. They do not send `ref_text`.

The application ships several editable example descriptions for audiobook
narration. Examples must be presented as starting points, not guaranteed voices.

### Kokoro workflow

Kokoro uses predefined voice embeddings rather than MOSS-style natural-language
voice descriptions and reference-audio cloning.

1. User chooses an available voice, language, speed, and supported voice blend.
2. The application proposes an excerpt, which can be rerolled.
3. The application generates a preview.
4. The saved profile contains the provider instance, model/quant/version,
   voice ID or blend, language, speed, and other supported settings.

The preview is an approval artifact, not cloning reference audio. Subsequent
generation uses the same Kokoro settings.

The UI offers multiple takes only when the provider reports stochastic output
or seed support. It must not waste resources producing identical deterministic
Kokoro files.

## Generation and resumability

Books are never submitted as a single TTS request. Narration units are packed
into chunks at paragraph and sentence boundaries using provider-specific input
limits. Long-form MOSS generation continues to target roughly 5–7 minutes per
chunk using the known-safe streaming configuration.

Book/job states:

```text
uploaded
planned
choosing_voice
ready
generating
pause_requested
paused
assembling
completed
failed
cancelled
```

Chunk states:

```text
pending -> generating -> completed
                      -> failed -> retry
```

Rules:

- `Pause` lets the active request finish and pauses before the next chunk.
- `Pause after chapter` finishes every remaining chunk in the active chapter,
  then pauses before the first chunk of the next included chapter.
- `Stop now` attempts to abort the active request, removes only its incomplete
  temporary output, and returns that chunk to `pending`.
- Container shutdown leaves active work recoverable on restart.
- Completed chunks are skipped after their audio and hashes are validated, but
  only while resuming their owning job. Every new narration job starts with
  fresh pending, job-owned chunks and never adopts audio from another job.
- OpenMOSS audiobook chunks derive a deterministic seed from the book ID,
  chapter index, and chunk index. A retry or explicit regeneration reuses the
  same seed; different chunks receive different sampling sequences.
- The job page polls structured status updates and patches progress in place;
  it must never refresh the whole document or interrupt chunk playback.
- Chunk audio supports byte-range playback, and the local web server handles
  concurrent requests so one paused player cannot block another.
- Failed chunks retain error history and may be retried independently.
- A completed chunk can be explicitly regenerated without affecting unrelated
  chunks. Its verified FLAC master remains in place until a replacement succeeds, and
  this single-chunk operation does not expose the full-job pause control.
- If a chunk changes, the narration map and final M4B become stale and must be rebuilt.
- Default generation concurrency is one.
- Retry policy, backoff, provider request IDs, timings, audio duration, RTF,
  character count, and errors are logged.
- Every job snapshots the applied connection performance configuration and the
  selected voice profile at creation. Connection tuning and later voice edits
  therefore affect only new jobs.

## Audio and output policy

Generated audio is first normalized into a validated internal master format.
OpenMOSS PCM is written to a temporary WAV, validated, converted to 48 kHz mono
lossless FLAC, verified, and then atomically promoted. The temporary provider
WAV is deleted immediately. Provider responses never become durable artifacts
without verification.

After all job chunks complete, the user explicitly starts an assembly run.
Narranova passes the ordered FLAC masters directly to external FFmpeg, records
chapter and book offsets in the narration map, and performs one AAC encoding
pass to create the chapterized M4B. FFprobe then validates the result. Assembly
does not create persistent chapter WAVs. A failed M4B encode retains the
verified FLAC masters and narration map for a safe retry.

The application produces:

- Independently replaceable chunk masters.
- A metadata-rich, chapterized M4B.
- A machine-readable narration map for future alignment.

M4B metadata includes when available:

- Title and subtitle
- Author
- Narrator or selected voice name
- Language
- Cover
- Series and series index
- Identifiers
- Chapter titles and exact timestamps
- Encoding/tool information

The narration map records:

- Narration-unit and chunk IDs
- Source EPUB document and element IDs
- Display and spoken-text hashes
- Chunk start/end offsets in chapter and book audio
- Provider, endpoint alias, model, quant/version, voice, instruction, reference,
  seed, and synthesis parameters
- Audio hashes and validation results

Chunk-level offsets are alignment hints, not sentence-level readaloud timing.
Do not claim that a v1 M4B is already synchronized at sentence level.

Suggested persistent artifacts:

```text
voices/
  <profile-id>/
    reference.wav
books/
  <book-id>/
    source/
      original.epub
    plan/
      revision-1.json
    jobs/
      <job-id>/
        voice/
          reference.wav
        chunks/
          c001-p001.txt
          c001-p001.flac
    output/
      Book Title.m4b
      cover.jpg
      narration-map.json
```

Every generation job stores an immutable profile snapshot and its own reference
WAV. This makes jobs reproducible while allowing a reusable voice profile and
its profile-owned files to be edited independently. A profile used by an
unfinished generation job is marked **In use** and cannot be deleted until all
referencing jobs complete or are deleted. After that, deleting books, jobs,
generated chunks, voice profiles, or abandoned Voice Lab drafts removes their
respective files as well as their database records.

Intermediate-master retention is configurable after final artifacts have been
verified. Jobs are editable by default and retain lossless FLAC chunks for
selective regeneration. After approving a verified M4B, the user may finalize
the job to delete those FLAC masters while retaining the M4B, narration map,
source EPUB, chunk text, voice snapshot, and job history. Restoring editable
sources requires synthesizing the missing chunks again. Abandoned temporary
provider WAVs are removed before an interrupted job resumes.

## Web application and CLI

The same core supports both interfaces.

Web flow:

1. Upload EPUB.
2. Inspect metadata and narration plan.
3. Enable or disable unwanted sections.
4. Configure external engines on the dedicated TTS Connections page.
5. Optionally test connectivity and benchmark the selected connection, then
   apply a measured streaming decode batch.
6. Complete the provider-appropriate workflow on the dedicated Voice Lab page:
   create reference audio first, then pair it with instructions and save a named
   profile.
7. Open the narration creation page and select both the TTS connection and the
   compatible saved voice profile.
8. Start generation.
9. Monitor, pause, resume, retry, or regenerate individual chunks.
10. Review chunk artifacts, storage use, and errors.
11. Download the M4B, narration map, and project metadata, then optionally
    finalize the job to remove editable FLAC masters.

The first UI should use server-rendered pages and limited JavaScript rather than
a large client framework unless later requirements justify one.

The CLI exposes equivalent commands suitable for automation. Exact command
names will be finalized during implementation, but it must cover upload/import,
planning, voice sampling/selection, generation, pause/resume, status, selective
regeneration, assembly, validation, and export.

V1 may be single-user or rely on an authenticating reverse proxy. Multi-user
quotas, billing, and complex permissions are deferred. Uploaded EPUBs must still
be treated as untrusted ZIP/XML/HTML input with size, path-traversal, entity,
and archive-expansion protections.

## Docker distribution

Provide:

- A versioned core application image.
- A minimal default Compose file for the core application.
- Optional Kokoro CPU and supported GPU profiles or example overrides.
- Persistent `/data` and model-cache volumes.
- Health checks.
- Documented backup and upgrade procedures for SQLite and artifacts.
- Configuration through environment variables or mounted secret files.
- No requirement for privileged mode.

The application must work without Kokoro when only an external provider is
configured.

## Iteration 2: native readaloud EPUB generation

Storyteller can import an already-created readaloud EPUB as a single asset. It
recognizes readaloud EPUBs separately from plain EPUBs and matched EPUB/audio
pairs, so it does not need to run alignment again on a correctly packaged file.

To generate a standards-compliant EPUB 3 readaloud book, the application will:

1. Obtain sentence- or phrase-level audio timestamps.
2. Preserve or inject stable XHTML IDs for each timed text range.
3. Package chapter audio assets into a copy of the EPUB.
4. Create SMIL Media Overlay files mapping XHTML IDs to audio clip ranges.
5. Add audio and SMIL resources to the OPF manifest.
6. Add `media-overlay` associations and duration metadata.
7. Repackage EPUB correctly, including its uncompressed leading `mimetype`.
8. Validate the result with EPUBCheck and internal consistency checks.
9. Test import and playback in Storyteller and at least one other compatible
   reading system.

Timestamp sources are provider-dependent:

- Direct provider word/sentence timestamps: no Whisper required.
- Sentence-per-file synthesis: duration-derived boundaries, with possible
  prosody and throughput tradeoffs.
- MOSS long-form chunks: a known-text forced aligner is likely required because
  current OpenMOSS output has no sentence timestamps.
- Storyteller delegation: export original EPUB plus M4B and allow Storyteller to
  transcribe/align, then optionally retrieve the readaloud EPUB.

Whisper is therefore an optional alignment implementation, not an inherent
requirement of TTS generation.

## V1 scope

In scope:

- DRM-free EPUB upload and validation
- DOM-aware, reviewable narration plans
- Metadata and cover extraction
- Dedicated OpenMOSS provider
- Optional external Kokoro provider
- Provider capability discovery
- Controlled OpenMOSS connection benchmarking and Auto-tune recommendation
- MOSS instruction/reference workflow with iterative one-take auditions
- Kokoro voice-selection and preview workflow
- Persistent pause/resume/retry/cancel
- Provider-WAV and lossless FLAC-master validation
- Chapterized M4B
- Narration map and project metadata
- CLI
- Lightweight self-hosted web UI
- Core Docker image and Compose configuration
- Focused automated tests and recovery tests

Deferred:

- Commercial TTS providers
- Direct readaloud EPUB generation
- Forced alignment
- Storyteller REST API automation
- PDF/TXT ingestion
- Multi-user quotas, billing, and advanced permissions
- Distributed workers and external databases/queues

## Existing implementation disposition

The imported audiobook implementation was a disposable prototype, not a
compatibility target, and has been removed. Preserve lessons and verified
behavior, but do not restore its architecture, public module names, manifest
format, or artifact layout merely to recreate prototype workflows.

Reimplement and test the useful parts behind Narranova's production boundaries:

- Known-safe OpenMOSS streaming request behavior
- Narrator instruction and reference-audio handling
- Paragraph/sentence chunking and loss checks
- Manifest/state concepts
- Atomic temporary output
- Provider-WAV validation and mono FLAC normalization
- Retry/error recording
- Ordered direct FLAC-to-M4B assembly
- Existing focused tests

The EPUB extractor requires the largest redesign because the initial version
reduces the book to cleaned plain text and does not retain sufficient XHTML
mapping for iteration two.

Do not modify or bundle the existing MOSS runtime, model files, DLLs, sibling
OpenMOSS GPU-selection patch, or imported OpenMOSS WebUI as part of Narranova.
MOSS remains a separately operated external service on every supported host.

## V1 acceptance criteria

V1 is complete when all of the following are true:

1. A user can upload a valid DRM-free EPUB through the web UI or import it via
   CLI.
2. The user can inspect extracted metadata, ordered chapters, and all enabled
   narration text before generation.
3. Every enabled readable character is accounted for in the narration plan.
4. The user can configure an external OpenMOSS endpoint, repeatedly audition
   from instruction alone or with optional reference audio, promote a generated
   take, and save a named workspace voice profile without retaining discarded
   draft audio.
5. The user can preview the packaged narrator pairs and use one directly for a
   book without first creating a custom profile.
6. The user can optionally use a configured Kokoro endpoint, browse available
   voices, preview one, and save its settings.
7. A generation job survives application/container restart without regenerating
   verified completed chunks.
8. The user can pause, resume, retry, and regenerate a selected chunk.
9. Failed or zero-length audio never becomes a completed artifact.
10. FLAC masters are assembled in source order without persistent chapter WAVs,
    and a changed chunk invalidates only derived outputs.
11. The final M4B contains the EPUB cover and metadata plus working chapter
    markers with verified timestamps.
12. The output includes a narration map connecting audio chunks to EPUB source
    locations and text hashes.
13. The default Docker deployment requires only the core container and one
   persistent volume; Kokoro remains optional.
14. No workflow depends on the bundled local MOSS runtime in this development
   folder.
15. Automated tests cover EPUB safety, plan integrity, provider payloads,
    pause/resume recovery, retry behavior, audio validation, selective rebuilds,
    metadata, and M4B chapter output.
16. A user can test and benchmark an OpenMOSS connection without importing a
    book, compare all six supported streaming batches using controlled inputs,
    understand throughput metrics, and apply a recommendation to future jobs.

## Reference documentation

- Storyteller adding and recognizing ebook, audiobook, and readaloud assets:
  https://storyteller-platform.dev/docs/managing/adding/
- Storyteller alignment requirements and supported audiobook inputs:
  https://storyteller-platform.dev/docs/managing/aligning/
- Storyteller v2 API documentation location (`/api/openapi`):
  https://storyteller-platform.dev/docs/migrations/from-v1-to-v2/
- EPUB Media Overlays specification:
  https://www.w3.org/publishing/epub32/epub-mediaoverlays.html
- Official Kokoro model and voice catalogue:
  https://huggingface.co/hexgrad/Kokoro-82M
  https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md
- Kokoro ONNX model variants:
  https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX/tree/main/onnx
