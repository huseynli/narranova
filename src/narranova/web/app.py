"""Dependency-light, server-rendered web UI for the local Narranova service."""

from __future__ import annotations

import cgi
import html
import json
import secrets
import shutil
from dataclasses import dataclass
from http import HTTPStatus
from importlib.resources import files
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, Iterable, Mapping
from urllib.parse import parse_qs, quote, unquote

from narranova.application.assembly import AudioAssembler, M4BEncoder
from narranova.application.benchmarking import ConnectionBenchmarks
from narranova.application.default_voices import (
    default_voice_pair,
    default_voice_pairs,
)
from narranova.application.deletion import DeleteArtifacts
from narranova.application.generation import AudioMasters, GenerationJobs, VoiceProfiles
from narranova.application.ingest import ImportBook
from narranova.application.provider_catalog import PROVIDER_TYPES, provider_type
from narranova.application.revise_plan import ReviseNarrationPlan
from narranova.application.voice_studio import INSTRUCTION_PRESETS, VoiceStudio
from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.audio import FFmpegAudioMasters, validate_wave
from narranova.config import Settings
from narranova.domain.narration import NarrationPlan
from narranova.domain.enhancement import (
    NarrationEnhancementSettings,
    format_pronunciations,
    parse_pronunciations,
)
from narranova.epub import EpubParser
from narranova.persistence import Database
from narranova.persistence.benchmarks import BenchmarkRepository, StoredBenchmarkRun
from narranova.persistence.books import BookRepository
from narranova.persistence.generation import (
    GenerationRepository,
    StoredArtifact,
    StoredChunk,
    StoredProvider,
)
from narranova.providers import (
    OPENMOSS_SAMPLING_FIELDS,
    OPENMOSS_STREAM_FRAME_OPTIONS,
    OpenMossConfig,
    OpenMossProvider,
    openmoss_sampling_from_form,
)
from narranova.web.supervisors import (
    BenchmarkSupervisor,
    JobSupervisor,
    VoiceStudioSupervisor,
)


MAX_REQUEST_BYTES = 128 * 1024 * 1024
StartResponse = Callable[[str, list[tuple[str, str]]], None]


@dataclass(frozen=True)
class Upload:
    filename: str
    path: Path


class NarranovaWebApp:
    def __init__(
        self,
        settings: Settings,
        books: BookRepository,
        generation: GenerationRepository,
        layout: ArtifactLayout,
        store: ArtifactStore,
        import_book: ImportBook,
        revise_plan: ReviseNarrationPlan,
        profiles: VoiceProfiles,
        jobs: GenerationJobs,
        assembler: AudioAssembler,
        deletion: DeleteArtifacts,
        voice_studio: VoiceStudio,
        benchmarks: ConnectionBenchmarks,
    ) -> None:
        self.settings = settings
        self.books = books
        self.generation = generation
        self.layout = layout
        self.store = store
        self.import_book = import_book
        self.revise_plan = revise_plan
        self.profiles = profiles
        self.jobs = jobs
        self.assembler = assembler
        self.deletion = deletion
        self.voice_studio = voice_studio
        self.benchmarks = benchmarks
        self.default_voices = default_voice_pairs()
        self.supervisor = JobSupervisor(jobs, assembler)
        self.benchmark_supervisor = BenchmarkSupervisor(benchmarks)
        self.voice_supervisor = VoiceStudioSupervisor(voice_studio)

    def __call__(self, environ: dict[str, object], start_response: StartResponse) -> Iterable[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = unquote(str(environ.get("PATH_INFO", "/")))
        csrf, set_cookie = self._csrf_token(environ)
        try:
            if method == "GET" and path == "/healthz":
                return self._respond(
                    start_response,
                    "200 OK",
                    b'{"status":"ok"}',
                    "application/json; charset=utf-8",
                )
            if method == "GET" and path in {
                "/static/app.css",
                "/static/choices.css",
                "/static/app.js",
                "/static/theme.js",
                "/static/favicon.svg",
            }:
                asset_name = path.rsplit("/", 1)[-1]
                content = files("narranova.web.static").joinpath(asset_name).read_bytes()
                if asset_name.endswith(".js"):
                    content_type = "text/javascript; charset=utf-8"
                elif asset_name.endswith(".svg"):
                    content_type = "image/svg+xml"
                else:
                    content_type = "text/css; charset=utf-8"
                return self._respond(start_response, "200 OK", content, content_type)
            if method == "GET" and path == "/":
                return self._html(start_response, self._dashboard(environ, csrf), set_cookie)
            parts = [part for part in path.split("/") if part]
            if method == "GET" and parts == ["connections"]:
                return self._html(start_response, self._connections(environ, csrf), set_cookie)
            if (
                method == "GET"
                and len(parts) == 3
                and parts[0] == "connections"
                and parts[2] == "health"
            ):
                return self._connection_health(start_response, parts[1])
            if method == "GET" and parts == ["voices"]:
                return self._html(start_response, self._voices(environ, csrf), set_cookie)
            if method == "GET" and parts == ["jobs"]:
                return self._html(start_response, self._jobs(environ, csrf), set_cookie)
            if (
                method == "GET"
                and len(parts) == 3
                and parts[0] == "default-voices"
                and parts[2] == "audio"
            ):
                return self._default_voice_audio(start_response, parts[1])
            if method == "GET" and len(parts) == 3 and parts[0] == "connections" and parts[2] == "edit":
                return self._html(
                    start_response,
                    self._edit_connection(parts[1], environ, csrf),
                    set_cookie,
                )
            if (
                method == "GET"
                and len(parts) == 3
                and parts[0] == "connections"
                and parts[2] == "benchmark"
            ):
                return self._html(
                    start_response,
                    self._connection_benchmark(parts[1], environ, csrf),
                    set_cookie,
                )
            if (
                method == "GET"
                and len(parts) == 5
                and parts[0] == "connections"
                and parts[2] == "benchmarks"
                and parts[4] == "status"
            ):
                return self._benchmark_state(start_response, parts[1], parts[3])
            if (
                method == "GET"
                and len(parts) == 5
                and parts[0] == "connections"
                and parts[2] == "benchmarks"
                and parts[4] == "delete"
            ):
                run = self.benchmarks.repository.get_run(parts[3])
                if run.provider_id != parts[1]:
                    raise KeyError("Connection benchmark not found")
                return self._html(
                    start_response,
                    self._confirm(
                        environ,
                        csrf,
                        title="Delete benchmark run",
                        subject=f"{'Auto-tune' if run.mode == 'auto' else 'Single batch'} · {run.created_at}",
                        warning="This permanently deletes the measurements and generated benchmark audio samples.",
                        action=f"/connections/{self._e(parts[1])}/benchmarks/{self._e(parts[3])}/delete",
                        cancel=f"/connections/{self._e(parts[1])}/benchmark",
                        button="Delete benchmark",
                    ),
                    set_cookie,
                )
            if (
                method == "GET"
                and len(parts) == 7
                and parts[0] == "connections"
                and parts[2] == "benchmarks"
                and parts[4] == "samples"
                and parts[6] == "audio"
            ):
                return self._benchmark_audio(
                    start_response,
                    parts[1],
                    parts[3],
                    int(parts[5]),
                    str(environ.get("HTTP_RANGE", "")),
                )
            if method == "GET" and len(parts) == 3 and parts[0] == "connections" and parts[2] == "delete":
                provider = self.generation.get_provider(parts[1])
                return self._html(
                    start_response,
                    self._confirm(
                        environ,
                        csrf,
                        title="Delete TTS connection",
                        subject=str(provider["name"]),
                        warning="This permanently removes the connection. Profiles using it must be deleted first.",
                        action=f"/connections/{self._e(parts[1])}/delete",
                        cancel="/connections",
                        button="Delete connection",
                    ),
                    set_cookie,
                )
            if method == "GET" and len(parts) == 3 and parts[0] == "voices" and parts[2] == "edit":
                return self._html(
                    start_response,
                    self._edit_voice(parts[1], environ, csrf),
                    set_cookie,
                )
            if method == "GET" and len(parts) == 3 and parts[0] == "voices" and parts[2] == "delete":
                voice = self.generation.get_voice_and_provider(parts[1])
                return self._html(
                    start_response,
                    self._confirm(
                        environ,
                        csrf,
                        title="Delete voice profile",
                        subject=str(voice["profile"].get("name") or "Untitled voice"),
                        warning="This permanently deletes the saved instruction and reference audio. Existing generation jobs keep their own independent voice snapshot.",
                        action=f"/voices/{self._e(parts[1])}/delete",
                        cancel="/voices",
                        button="Delete voice profile",
                    ),
                    set_cookie,
                )
            if method == "GET" and len(parts) == 3 and parts[:2] == ["voices", "drafts"]:
                return self._html(
                    start_response,
                    self._voice_studio(parts[2], environ, csrf),
                    set_cookie,
                )
            if (
                method == "GET"
                and len(parts) == 4
                and parts[:2] == ["voices", "drafts"]
                and parts[3] == "status"
            ):
                draft = self.voice_studio.get(parts[2])
                return self._respond(
                    start_response,
                    "200 OK",
                    json.dumps(
                        {
                            "status": draft.get("audition_status", "idle"),
                            "error": draft.get("audition_error") or "",
                            "take_count": len(draft.get("takes", [])),
                        }
                    ).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
            if (
                method == "GET"
                and len(parts) == 6
                and parts[:2] == ["voices", "drafts"]
                and parts[3] == "takes"
                and parts[5] == "audio"
            ):
                return self._studio_audio(start_response, parts[2], parts[4])
            if (
                method == "GET"
                and len(parts) == 5
                and parts[:2] == ["voices", "drafts"]
                and parts[3:] == ["reference", "audio"]
            ):
                return self._studio_uploaded_audio(start_response, parts[2])
            if (
                method == "GET"
                and len(parts) == 4
                and parts[0] == "voices"
                and parts[2:] == ["reference", "audio"]
            ):
                return self._profile_audio(start_response, parts[1])
            if (
                method == "GET"
                and len(parts) == 4
                and parts[0] == "books"
                and parts[2:] == ["narrations", "new"]
            ):
                return self._html(
                    start_response,
                    self._new_narration(parts[1], environ, csrf),
                    set_cookie,
                )
            if method == "GET" and len(parts) == 2 and parts[0] == "books":
                return self._html(start_response, self._book(parts[1], environ, csrf), set_cookie)
            if method == "GET" and len(parts) == 2 and parts[0] == "jobs":
                return self._html(start_response, self._job(parts[1], environ, csrf), set_cookie)
            if method == "GET" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "status":
                return self._job_state(start_response, parts[1])
            if (
                method == "GET"
                and len(parts) == 5
                and parts[0] == "jobs"
                and parts[2] == "artifacts"
                and parts[4] == "download"
            ):
                return self._artifact_download(start_response, parts[1], parts[3])
            if method == "GET" and len(parts) == 3 and parts[0] == "books" and parts[2] == "delete":
                book = self.books.get_book(parts[1])
                book_jobs = self.generation.list_jobs(parts[1])
                audiobook_count = sum(
                    artifact.kind == "audiobook"
                    for job in book_jobs
                    for artifact in self.generation.list_job_artifacts(job.id)
                )
                audiobook_label = (
                    "1 generated audiobook"
                    if audiobook_count == 1
                    else f"{audiobook_count} generated audiobooks"
                )
                return self._html(
                    start_response,
                    self._confirm(
                        environ,
                        csrf,
                        title="Delete book",
                        subject=book.title,
                        warning=f"This permanently deletes the source EPUB, narration plans, every job, {audiobook_label}, and all generated chunk audio for this book. Reusable voice profiles remain available.",
                        action=f"/books/{self._e(parts[1])}/delete",
                        cancel=f"/books/{self._e(parts[1])}",
                        button="Delete book and audio",
                    ),
                    set_cookie,
                )
            if method == "GET" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "delete":
                job = self.generation.get_job(parts[1])
                return self._html(
                    start_response,
                    self._confirm(
                        environ,
                        csrf,
                        title="Delete generation job",
                        subject=f"{job['book_title']} · {parts[1][:12]}",
                        warning="This permanently deletes the job history, chunk text, and every audio file generated by this job. The book and voice profile remain available.",
                        action=f"/jobs/{self._e(parts[1])}/delete",
                        cancel=f"/jobs/{self._e(parts[1])}",
                        button="Delete job permanently",
                    ),
                    set_cookie,
                )
            if method == "GET" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "compact":
                job = self.generation.get_job(parts[1])
                return self._html(
                    start_response,
                    self._confirm(
                        environ,
                        csrf,
                        title="Finalize audiobook storage",
                        subject=f"{job['book_title']} · {parts[1][:12]}",
                        warning="This permanently removes the lossless FLAC chunk masters. The verified M4B, narration map, source EPUB, text, and voice snapshot remain. Restoring editable sources requires synthesizing the book again.",
                        action=f"/jobs/{self._e(parts[1])}/compact",
                        cancel=f"/jobs/{self._e(parts[1])}",
                        button="Finalize and free space",
                    ),
                    set_cookie,
                )
            if method == "GET" and len(parts) == 5 and parts[0] == "jobs" and parts[2] == "chunks" and parts[4] == "delete":
                chunk = self.generation.get_chunk(parts[1], parts[3])
                return self._html(
                    start_response,
                    self._confirm(
                        environ,
                        csrf,
                        title="Delete generated chunk",
                        subject=chunk.id,
                        warning="This deletes the lossless FLAC master and returns the chunk to pending. You can regenerate it by running the job again.",
                        action=f"/jobs/{self._e(parts[1])}/chunks/{self._e(parts[3])}/delete",
                        cancel=f"/jobs/{self._e(parts[1])}",
                        button="Delete generated audio",
                    ),
                    set_cookie,
                )
            if (
                method == "GET"
                and len(parts) == 5
                and parts[0] == "jobs"
                and parts[2] == "chunks"
                and parts[4] in {"audio", "download"}
            ):
                return self._audio(
                    start_response,
                    parts[1],
                    parts[3],
                    range_header=str(environ.get("HTTP_RANGE", "")),
                    download=parts[4] == "download",
                )
            if method == "POST":
                fields, uploads = self._parse_form(environ)
                try:
                    if not secrets.compare_digest(fields.get("csrf", ""), csrf):
                        raise PermissionError("The form expired. Refresh the page and try again.")
                    return self._post(
                        start_response,
                        parts,
                        fields,
                        uploads,
                        wants_json="application/json"
                        in str(environ.get("HTTP_ACCEPT", "")),
                    )
                finally:
                    for upload in uploads.values():
                        upload.path.unlink(missing_ok=True)
            return self._error(start_response, HTTPStatus.NOT_FOUND, "Page not found", set_cookie)
        except PermissionError as exc:
            return self._error(start_response, HTTPStatus.FORBIDDEN, str(exc), set_cookie)
        except KeyError as exc:
            return self._error(start_response, HTTPStatus.NOT_FOUND, str(exc).strip("'"), set_cookie)
        except Exception as exc:
            return self._error(start_response, HTTPStatus.BAD_REQUEST, str(exc), set_cookie)

    def _post(
        self,
        start_response: StartResponse,
        parts: list[str],
        fields: dict[str, str],
        uploads: dict[str, Upload],
        *,
        wants_json: bool = False,
    ) -> Iterable[bytes]:
        if parts == ["actions", "import"]:
            upload = uploads.get("epub")
            if upload is None:
                raise ValueError("Choose an EPUB to import")
            result = self.import_book.execute(upload.path)
            return self._redirect(start_response, f"/books/{result.book_id}?notice=Book+imported")
        if parts in (["actions", "providers"], ["connections"]):
            provider_id = self.profiles.add_provider(
                fields.get("kind", "openmoss"),
                fields.get("name", ""),
                fields.get("endpoint", ""),
            )
            return self._redirect(
                start_response,
                f"/connections?notice=Connection+saved+{provider_id}",
            )
        if len(parts) == 2 and parts[0] == "connections":
            self.profiles.update_provider(
                parts[1],
                kind=fields.get("kind", ""),
                name=fields.get("name", ""),
                endpoint_url=fields.get("endpoint", ""),
            )
            return self._redirect(start_response, "/connections?notice=Connection+updated")
        if len(parts) == 3 and parts[0] == "connections" and parts[2] == "test":
            provider = self.generation.get_provider(parts[1])
            if provider["kind"] != "openmoss":
                raise ValueError("Connection testing is not implemented for this engine")
            client = OpenMossProvider(
                OpenMossConfig.from_connection(
                    str(provider["endpoint_url"]),
                    provider["configuration"],
                    timeout_seconds=10,
                )
            )
            information = client.health()
            model = (
                information.get("architecture")
                or information.get("model")
                or information.get("model_name")
            )
            notice = "Connection healthy"
            if model:
                notice += f" · {model}"
            return self._redirect(
                start_response,
                f"/connections/{parts[1]}/benchmark?notice={quote(notice, safe='')}",
            )
        if (
            len(parts) == 3
            and parts[0] == "connections"
            and parts[2] == "benchmarks"
        ):
            mode = fields.get("mode", "single")
            if mode not in {"single", "auto"}:
                raise ValueError("Choose a valid benchmark mode")
            frames = None
            if mode != "auto":
                try:
                    frames = int(fields.get("stream_chunk_frames", ""))
                except ValueError as exc:
                    raise ValueError("Choose a streaming decode batch") from exc
            benchmark_id = self.benchmarks.create(parts[1], frames)
            self.benchmark_supervisor.start(benchmark_id)
            return self._redirect(
                start_response,
                f"/connections/{parts[1]}/benchmark?notice=Benchmark+started",
            )
        if (
            len(parts) == 5
            and parts[0] == "connections"
            and parts[2] == "benchmarks"
            and parts[4] == "apply"
        ):
            run = self.benchmarks.repository.get_run(parts[3])
            if run.provider_id != parts[1]:
                raise KeyError("Connection benchmark not found")
            selected_text = fields.get("stream_chunk_frames", "").strip()
            selected = int(selected_text) if selected_text else None
            frames = self.benchmarks.apply(parts[3], selected)
            return self._redirect(
                start_response,
                f"/connections/{parts[1]}/benchmark?notice="
                f"Streaming+decode+batch+set+to+{frames}",
            )
        if (
            len(parts) == 5
            and parts[0] == "connections"
            and parts[2] == "benchmarks"
            and parts[4] == "delete"
        ):
            self.benchmarks.delete(parts[1], parts[3])
            return self._redirect(
                start_response,
                f"/connections/{parts[1]}/benchmark?notice=Benchmark+deleted",
            )
        if len(parts) == 3 and parts[0] == "connections" and parts[2] == "delete":
            self.deletion.connection(parts[1])
            return self._redirect(start_response, "/connections?notice=Connection+deleted")
        if parts == ["voices", "drafts"]:
            draft_id = self.voice_studio.start()
            return self._redirect(start_response, f"/voices/drafts/{draft_id}")
        if len(parts) == 4 and parts[:2] == ["voices", "drafts"] and parts[3] == "auditions":
            upload = uploads.get("reference")
            if upload:
                self.voice_studio.stage_uploaded_reference(parts[2], upload.path)
            started = self.voice_supervisor.start(
                parts[2],
                provider_id=fields.get("provider_id", ""),
                reference_choice=fields.get("reference_choice", ""),
                instruction=fields.get("instruction", ""),
                sample_text=fields.get("sample_text", ""),
                language=fields.get("language", "English"),
                profile_name=fields.get("name", ""),
                uploaded_reference=None,
                sampling=openmoss_sampling_from_form(fields),
            )
            workflow = (
                "uploaded"
                if fields.get("reference_choice") == "uploaded"
                else "generated"
            )
            return self._redirect(
                start_response,
                f"/voices/drafts/{parts[2]}?notice="
                f"{'Audition+generation+started' if started else 'Audition+already+running'}"
                f"&open={workflow}",
            )
        if len(parts) == 4 and parts[:2] == ["voices", "drafts"] and parts[3] == "save":
            profile_id = self.voice_studio.save_profile(
                parts[2],
                name=fields.get("name", ""),
                provider_id=fields.get("provider_id", ""),
                reference_choice=fields.get("reference_choice", ""),
                instruction=fields.get("instruction", ""),
                language=fields.get("language", "English"),
            )
            return self._redirect(
                start_response,
                f"/voices?notice=Voice+profile+saved+{profile_id}",
            )
        if len(parts) == 4 and parts[:2] == ["voices", "drafts"] and parts[3] == "discard":
            if self.voice_supervisor.is_active(parts[2]):
                raise ValueError("Wait for the active audition before discarding this draft")
            self.voice_studio.discard(parts[2])
            return self._redirect(start_response, "/voices?notice=Voice+draft+discarded")
        if len(parts) == 2 and parts[0] == "voices":
            upload = uploads.get("reference")
            self.profiles.update_openmoss_profile(
                parts[1],
                provider_id=fields.get("provider_id", ""),
                instruction=fields.get("instruction", ""),
                name=fields.get("name", ""),
                language=fields.get("language", "English"),
                reference_audio=upload.path if upload else None,
                sampling=openmoss_sampling_from_form(fields),
            )
            return self._redirect(start_response, "/voices?notice=Voice+profile+updated")
        if len(parts) == 3 and parts[0] == "voices" and parts[2] == "delete":
            self.deletion.voice_profile(parts[1])
            return self._redirect(start_response, "/voices?notice=Voice+profile+deleted")
        if len(parts) == 3 and parts[0] == "books" and parts[2] == "plan":
            enabled = {
                int(name.removeprefix("chapter_"))
                for name, value in fields.items()
                if name.startswith("chapter_") and value == "on"
            }
            result = self.revise_plan.execute(parts[1], enabled)
            if wants_json:
                content = json.dumps(
                    {
                        "revision": result.revision,
                        "enabled_chapters": result.enabled_chapters,
                        "enabled_units": result.enabled_units,
                        "changed": result.changed,
                    }
                ).encode("utf-8")
                return self._respond(
                    start_response,
                    "200 OK",
                    content,
                    "application/json; charset=utf-8",
                )
            notice = (
                f"Narration+choices+saved+as+revision+{result.revision}"
                if result.changed
                else "Narration+choices+unchanged"
            )
            return self._redirect(start_response, f"/books/{parts[1]}?notice={notice}")
        if len(parts) == 3 and parts[0] == "books" and parts[2] == "enhancement":
            settings = NarrationEnhancementSettings(
                enabled=fields.get("enabled") == "on",
                chapter_pause_seconds=float(
                    fields.get("chapter_pause_seconds", "1.8")
                ),
                section_pause_seconds=float(
                    fields.get("section_pause_seconds", "1.2")
                ),
                scene_break_pause_seconds=float(
                    fields.get("scene_break_pause_seconds", "1.5")
                ),
                normalize_text=fields.get("normalize_text") == "on",
                pronunciation_enabled=fields.get("pronunciation_enabled") == "on",
                pronunciations=parse_pronunciations(fields.get("pronunciations", "")),
            )
            self.books.save_enhancement_settings(parts[1], settings)
            return self._redirect(
                start_response,
                f"/books/{parts[1]}?notice=Narration+enhancement+saved",
            )
        if len(parts) == 3 and parts[0] == "books" and parts[2] == "jobs":
            job_id = self.jobs.create(
                parts[1],
                fields.get("voice_profile_id", ""),
                fields.get("provider_id") or None,
            )
            return self._redirect(start_response, f"/jobs/{job_id}?notice=Generation+job+created")
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "run":
            started = self.supervisor.start(parts[1])
            notice = "Generation+started" if started else "Generation+is+already+running"
            return self._redirect(start_response, f"/jobs/{parts[1]}?notice={notice}")
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "pause":
            self.generation.request_pause(parts[1])
            return self._redirect(start_response, f"/jobs/{parts[1]}?notice=Pause+requested")
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "pause-chapter":
            self.generation.request_pause_after_chapter(parts[1])
            return self._redirect(
                start_response,
                f"/jobs/{parts[1]}?notice=Pause+after+chapter+requested",
            )
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
            self.generation.request_cancel(parts[1])
            return self._redirect(
                start_response, f"/jobs/{parts[1]}?notice=Stop+requested"
            )
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "assemble":
            started = self.supervisor.assemble(parts[1])
            notice = "Audiobook+build+started" if started else "Job+is+already+busy"
            return self._redirect(start_response, f"/jobs/{parts[1]}?notice={notice}")
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "compact":
            if self.supervisor.is_active(parts[1]):
                raise ValueError("Wait for active generation or assembly before finalizing")
            freed = self.deletion.compact_job(parts[1])
            notice = self._format_bytes(freed).replace(" ", "+")
            return self._redirect(
                start_response,
                f"/jobs/{parts[1]}?notice=Finalized+and+freed+{notice}",
            )
        if (
            len(parts) == 5
            and parts[0] == "jobs"
            and parts[2] == "chunks"
            and parts[4] == "regenerate"
        ):
            started = self.supervisor.regenerate(parts[1], parts[3])
            notice = (
                "Chunk+regeneration+started"
                if started
                else "Generation+is+already+running"
            )
            return self._redirect(start_response, f"/jobs/{parts[1]}?notice={notice}")
        if len(parts) == 5 and parts[0] == "jobs" and parts[2] == "chunks" and parts[4] == "delete":
            if self.supervisor.is_active(parts[1]):
                raise ValueError("Pause the generation job before deleting its audio")
            self.deletion.generated_chunk(parts[1], parts[3])
            return self._redirect(start_response, f"/jobs/{parts[1]}?notice=Generated+chunk+deleted")
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "delete":
            if self.supervisor.is_active(parts[1]):
                raise ValueError("Pause the generation job before deleting it")
            book_id = self.deletion.job(parts[1])
            return self._redirect(start_response, f"/books/{book_id}?notice=Generation+job+deleted")
        if len(parts) == 3 and parts[0] == "books" and parts[2] == "delete":
            active_jobs = self.generation.list_jobs(parts[1])
            if any(self.supervisor.is_active(job.id) for job in active_jobs):
                raise ValueError("Pause active generation jobs before deleting this book")
            self.deletion.book(parts[1])
            return self._redirect(start_response, "/?notice=Book+deleted")
        raise KeyError("Action not found")

    def _dashboard(self, environ: dict[str, object], csrf: str) -> str:
        books = self.books.list_books()
        providers = self.generation.list_providers()
        profiles = self.generation.list_voice_profiles()
        all_jobs = self.generation.list_jobs()
        jobs = all_jobs[:8]
        book_cards = "".join(
            f"""
            <a class="book-row" href="/books/{self._e(book.id)}">
              <span class="book-mark">{self._e((book.title or 'U')[:1].upper())}</span>
              <span><strong>{self._e(book.title or 'Untitled')}</strong>
              <small>{self._e(book.author or 'Unknown author')} · {self._e(book.language or '—')}</small></span>
              <span class="pill">{self._e(book.status)}</span>
            </a>"""
            for book in books
        ) or '<div class="empty">No books yet. Import an EPUB to begin.</div>'
        job_rows = "".join(
            f'<a class="job-row" href="/jobs/{self._e(job.id)}"><span><strong>{self._e(job.book_title)}</strong><small>Job {self._e(job.id[:10])}</small></span><span class="status status-{self._e(job.status)}">{self._e(job.status)}</span></a>'
            for job in jobs
        ) or '<div class="empty">Generation jobs will appear here.</div>'
        body = f"""
        <section class="page-heading workspace-heading full-page-heading"><div><p class="eyebrow">Production workspace</p><h1>Your audiobook desk</h1><p>Bring in a book, shape its narration, then generate at your own pace.</p></div></section>
        <section class="stat-strip"><a href="/"><strong>{len(books)}</strong><span>Books</span></a><a href="/connections"><strong>{len(providers)}</strong><span>TTS connections</span></a><a href="/voices"><strong>{len(profiles)}</strong><span>Voice profiles</span></a><a href="/jobs"><strong>{len(all_jobs)}</strong><span>Recent jobs</span></a></section>
        <div class="dashboard-grid"><section class="panel library"><header><div><p class="eyebrow">Library</p><h2>Books in progress</h2></div><span class="count">{len(books):02d}</span></header>{book_cards}</section>
        <aside class="stack"><form class="panel import-card" method="post" action="/actions/import" enctype="multipart/form-data">{self._csrf(csrf)}<header><div><p class="eyebrow">New book</p><h2>Import an EPUB</h2></div></header><div class="import-actions"><input id="epub" name="epub" type="file" accept=".epub,application/epub+zip" aria-label="Choose an EPUB book" required><button class="primary">Import book</button></div></form><section class="panel" id="recent-jobs"><header><div><p class="eyebrow">Activity</p><h2>Recent jobs</h2></div><span class="count">{len(all_jobs):02d}</span></header>{job_rows}</section></aside></div>"""
        return self._layout("Workspace", body, environ)

    def _connections(self, environ: dict[str, object], csrf: str) -> str:
        providers = self.generation.list_providers()

        def connection_card(provider: StoredProvider) -> str:
            details = self.generation.get_provider(provider.id)
            if provider.kind == "openmoss":
                performance = OpenMossConfig.from_connection(
                    str(details["endpoint_url"]), details["configuration"]
                )
                performance_copy = (
                    f"Streaming decode batch {performance.stream_chunk_frames}"
                )
                performance_actions = f"""<a href="/connections/{self._e(provider.id)}/benchmark">Benchmark</a>"""
            else:
                performance_copy = provider_type(provider.kind).description
                performance_actions = ""
            return f"""<article class="connection-card"><div class="connection-icon">{self._e(provider_type(provider.kind).label[:1])}</div><div><span class="connection-health checking" data-connection-health data-health-url="/connections/{self._e(provider.id)}/health"><i></i><b>Checking connection</b></span><h3>{self._e(provider.name)}</h3><p>{self._e(provider.endpoint_url)}</p><small>{self._e(provider_type(provider.kind).label)} · {self._e(performance_copy)}</small><div class="card-actions">{performance_actions}<a href="/connections/{self._e(provider.id)}/edit">Edit</a><a class="danger-link" href="/connections/{self._e(provider.id)}/delete">Delete</a></div></div></article>"""

        cards = "".join(
            connection_card(provider)
            for provider in providers
        ) or """<div class="empty-state"><span>M</span><h3>No TTS connection yet</h3><p>Add the /tts URL from your separately running OpenMOSS server.</p></div>"""
        type_options = "".join(
            f'<option value="{self._e(item.id)}">{self._e(item.label)}</option>'
            for item in PROVIDER_TYPES
        )
        body = f"""<section class="page-heading full-page-heading"><div><p class="eyebrow">TTS connections</p><h1>Connect your voice engine</h1><p>Narranova sends text and approved voice references to services you operate separately.</p></div></section>
        <div class="connections-stack"><section class="panel connection-builder"><header><div><p class="eyebrow">New connection</p><h2>Add a TTS engine</h2></div></header><form method="post" action="/connections">{self._csrf(csrf)}<label>Connection type<small>Each engine exposes its own Voice Lab controls.</small><select name="kind" required>{type_options}</select></label><label>Connection name<small>Use a name that identifies the machine or model.</small><input name="name" placeholder="Studio MOSS" required></label><label>Service endpoint<small>For OpenMOSS, enter its external /tts URL.</small><input name="endpoint" type="url" value="http://127.0.0.1:8000/tts" required></label><button class="primary">Save connection</button></form></section>
        <section class="saved-connections"><div class="section-heading"><div><p class="eyebrow">Configured engines</p><h2>Saved connections</h2><p>{len(providers)} ready for narration</p></div><span class="count">{len(providers):02d}</span></div><div class="connection-list">{cards}</div></section></div>"""
        return self._layout("Connections", body, environ)

    def _jobs(self, environ: dict[str, object], csrf: str) -> str:
        """Render the cross-book generation job workspace."""
        jobs = self.generation.list_jobs()
        active_statuses = {
            "generating", "pause_requested", "cancel_requested", "assembling"
        }
        recent_statuses = {"uploaded", "planned", "choosing_voice", "ready"}
        finished_statuses = {"completed"}
        stopped_statuses = {"paused", "failed", "cancelled"}

        def rows(items: list[object], empty: str) -> str:
            if not items:
                return f'<div class="empty">{self._e(empty)}</div>'
            return "".join(
                f'<a class="job-row" href="/jobs/{self._e(job.id)}"><span><strong>{self._e(job.book_title)}</strong><small>Job {self._e(job.id[:12])} · {self._e(job.created_at)}</small></span><span class="status status-{self._e(job.status)}">{self._e(job.status)}</span></a>'
                for job in items
            )

        groups = (
            ("active", "Active", "Currently running or processing", active_statuses, "No active jobs."),
            ("recent", "Recent", "Ready to start or awaiting work", recent_statuses, "No recent jobs."),
            ("finished", "Finished", "Completed narration jobs", finished_statuses, "No finished jobs yet."),
            ("stopped", "Stopped", "Paused, failed, or cancelled jobs", stopped_statuses, "No stopped jobs."),
        )
        sections = "".join(
            f'<section class="panel jobs-section" id="jobs-{key}"><header><div><p class="eyebrow">Job state</p><h2>{label}</h2><p>{description}</p></div><span class="count">{len([job for job in jobs if job.status in statuses]):02d}</span></header>{rows([job for job in jobs if job.status in statuses], empty)}</section>'
            for key, label, description, statuses, empty in groups
        )
        body = f'''<section class="page-heading jobs-heading full-page-heading"><div><p class="eyebrow">Production jobs</p><h1>Every narration run in one place</h1><p>Track work across your library, return to a run, or review the audio it produced.</p></div></section><section class="jobs-grid">{sections}</section>'''
        return self._layout("Jobs", body, environ)

    def _edit_connection(self, provider_id: str, environ: dict[str, object], csrf: str) -> str:
        provider = self.generation.get_provider(provider_id)
        performance_note = ""
        if provider["kind"] == "openmoss":
            performance = OpenMossConfig.from_connection(
                str(provider["endpoint_url"]), provider["configuration"]
            )
            performance_note = f"""<div class="connection-performance-note"><span>Performance setting</span><strong>Streaming decode batch {performance.stream_chunk_frames}</strong><p>Measure and change this on the connection benchmark page.</p><a href="/connections/{self._e(provider_id)}/benchmark">Open benchmark →</a></div>"""
        type_options = "".join(
            f'<option value="{self._e(item.id)}"{" selected" if item.id == provider["kind"] else ""}>{self._e(item.label)}</option>'
            for item in PROVIDER_TYPES
        )
        body = f"""<a class="back" href="/connections">← TTS connections</a><section class="page-heading full-page-heading"><div><p class="eyebrow">Edit connection</p><h1>{self._e(provider['name'])}</h1><p>Update the engine type, display name, or service endpoint.</p></div></section><section class="panel edit-panel"><form method="post" action="/connections/{self._e(provider_id)}">{self._csrf(csrf)}<label>Connection type<select name="kind" required>{type_options}</select></label><label>Connection name<input name="name" value="{self._e(provider['name'])}" required></label><label>Service endpoint<input name="endpoint" type="url" value="{self._e(provider['endpoint_url'])}" required></label>{performance_note}<div class="form-actions"><a class="button" href="/connections">Cancel</a><button class="primary">Update connection</button></div></form></section>"""
        return self._layout("Edit connection", body, environ)

    def _connection_benchmark(
        self, provider_id: str, environ: dict[str, object], csrf: str
    ) -> str:
        provider = self.generation.get_provider(provider_id)
        if provider["kind"] != "openmoss":
            raise ValueError("Benchmarking is not implemented for this connection type")
        performance = OpenMossConfig.from_connection(
            str(provider["endpoint_url"]), provider["configuration"]
        )
        runs = self.benchmarks.repository.list_runs(provider_id, limit=6)
        active = next((run for run in runs if run.status == "running"), None)
        options = "".join(
            f'<option value="{frames}"{" selected" if frames == performance.stream_chunk_frames else ""}>{frames} frames · ~{frames * 0.08:.2f}s audio</option>'
            for frames in OPENMOSS_STREAM_FRAME_OPTIONS
        )
        disabled = " disabled" if active else ""
        monitor = ""
        if active:
            current = active.active_stream_chunk_frames
            monitor = f"""<section class="panel benchmark-monitor" data-benchmark-monitor data-status-url="/connections/{self._e(provider_id)}/benchmarks/{self._e(active.id)}/status" data-results-url="/connections/{self._e(provider_id)}/benchmark"><div class="benchmark-pulse"><i></i><div><strong>Benchmark running</strong><span data-benchmark-progress>{len(active.results)} of {len(active.requested_frames)} measurements complete{f' · testing {current} frames' if current else ''}</span></div></div></section>"""
        history = "".join(
            self._benchmark_run_card(provider_id, run, csrf)
            for run in runs
            if run.status != "running"
        ) or '<div class="empty-state compact"><span>↗</span><h3>No benchmark results yet</h3><p>Run one batch or Auto-tune all six supported values.</p></div>'
        body = f"""<a class="back" href="/connections">← TTS connections</a><section class="page-heading full-page-heading"><div><p class="eyebrow">Connection performance</p><h1>{self._e(provider['name'])}</h1><p>Measure this OpenMOSS server with controlled, book-independent narration.</p></div><div class="heading-actions"><form method="post" action="/connections/{self._e(provider_id)}/test">{self._csrf(csrf)}<button>Test connection</button></form><a class="button" href="/connections/{self._e(provider_id)}/edit">Edit connection</a></div></section>
        <section class="benchmark-summary-strip"><div><span>Endpoint</span><strong>{self._e(provider['endpoint_url'])}</strong></div><div><span>Current streaming decode batch</span><strong>{performance.stream_chunk_frames} frames · ~{performance.stream_chunk_frames * 0.08:.2f}s audio</strong></div><div><span>Sampling</span><strong>Engine default</strong></div></section>{monitor}
        <div class="benchmark-layout"><main class="stack"><section class="panel benchmark-builder"><header><div><p class="eyebrow">Performance test</p><h2>Streaming decode batch</h2><p>Larger batches reduce codec overhead and usually improve offline generation throughput. Smaller batches return audio sooner.</p></div></header><div class="benchmark-actions"><form method="post" action="/connections/{self._e(provider_id)}/benchmarks">{self._csrf(csrf)}<input type="hidden" name="mode" value="single"><label>Test one batch<select name="stream_chunk_frames">{options}</select></label><button class="primary"{disabled}>Run benchmark</button></form><form class="auto-tune" method="post" action="/connections/{self._e(provider_id)}/benchmarks">{self._csrf(csrf)}<input type="hidden" name="mode" value="auto"><div><strong>Auto-tune all six values</strong><p>Tests 16 → 32 → 64 → 128 → 256 → 512, then recommends the smallest batch within 3% of the fastest result.</p></div><button{disabled}>Run Auto-tune</button></form></div><div class="controlled-inputs"><span>Controlled inputs</span><p>Fixed original passage · Built-in narrator 04 · fixed instruction · fixed seed · engine-default sampling · safe automatic output ceiling</p></div></section><section class="benchmark-history"><div class="section-heading"><div><p class="eyebrow">Measurements</p><h2>Recent benchmark runs</h2><p>Listen to samples, apply a measured batch, or remove results you no longer need.</p></div><span class="count">{len([run for run in runs if run.status != 'running']):02d}</span></div>{history}</section></main>
        <aside class="stack"><section class="panel metric-guide"><header><div><p class="eyebrow">Reading results</p><h2>Realtime speed</h2></div></header><p><strong>1.0×</strong> means one minute of computation generates one minute of audio.</p><p><strong>2.0×</strong> means one minute of computation generates two minutes of audio.</p><p>Estimated audiobook time is labelled <strong>TTS generation only</strong>; it excludes queueing, retries, normalization, and M4B assembly.</p></section><section class="panel hardware-tuning"><header><div><p class="eyebrow">OpenMOSS server</p><h2>Hardware-side tuning</h2></div></header><p>Narranova cannot change launch flags on the external server. Compare these separately when configuring OpenMOSS:</p><ul><li><strong>Model quantization</strong> — Lower quantization reduces memory use and memory-bandwidth requirements and may improve throughput depending on the model, hardware, and backend.</li><li><strong>GPU offload</strong> — Confirm the intended device and layers are actually in use.</li><li><strong>Flash attention</strong> — Keep enabled when supported unless benchmarking shows otherwise.</li><li><strong>Context and launch configuration</strong> — Server-side batch and context choices can matter more than request controls.</li></ul></section></aside></div>"""
        return self._layout("Connection benchmark", body, environ)

    def _benchmark_run_card(
        self, provider_id: str, run: StoredBenchmarkRun, csrf: str
    ) -> str:
        measurements: list[str] = []
        for result in run.results:
            frames = int(result["stream_chunk_frames"])
            first_audio = result.get("first_audio_seconds")
            first_audio_text = (
                f"{float(first_audio):.2f}s" if first_audio is not None else "Not reported"
            )
            recommended = frames == run.recommended_stream_chunk_frames
            action = ""
            if run.status == "completed":
                action = f"""<form method="post" action="/connections/{self._e(provider_id)}/benchmarks/{self._e(run.id)}/apply">{self._csrf(csrf)}<input type="hidden" name="stream_chunk_frames" value="{frames}"><button class="{'primary' if recommended else ''}">{'Apply recommendation' if recommended else 'Use this batch'}</button></form>"""
            measurements.append(
                f"""<article class="benchmark-result{' recommended' if recommended else ''}"><header><div><span>Streaming decode batch</span><h3>{frames} frames <small>~{frames * 0.08:.2f}s audio</small></h3></div>{'<b>Recommended</b>' if recommended else ''}</header><dl><div><dt>Audio generated</dt><dd>{float(result['audio_duration_seconds']):.1f}s</dd></div><div><dt>Wall time</dt><dd>{float(result['wall_seconds']):.1f}s</dd></div><div><dt>Realtime speed</dt><dd>{float(result['realtime_speed']):.2f}×</dd></div><div><dt>Realtime factor</dt><dd>{float(result['realtime_factor']):.3f}</dd></div><div><dt>First audio</dt><dd>{first_audio_text}</dd></div><div><dt>40h estimate</dt><dd>{float(result['estimated_40h_tts_hours']):.1f}h <small>TTS only</small></dd></div></dl><audio controls preload="none" src="/connections/{self._e(provider_id)}/benchmarks/{self._e(run.id)}/samples/{frames}/audio"></audio>{action}</article>"""
            )
        error = (
            f'<div class="alert">{self._e(run.error_message)}</div>'
            if run.error_message
            else ""
        )
        recommendation = (
            f'<p>Recommended <strong>{run.recommended_stream_chunk_frames} frames</strong>: the smallest measured batch within 3% of peak speed.</p>'
            if run.recommended_stream_chunk_frames
            else ""
        )
        return f"""<section class="benchmark-run"><div class="benchmark-run-heading"><div><span class="status status-{self._e(run.status)}">{self._e(run.status)}</span><strong>{'Auto-tune' if run.mode == 'auto' else 'Single batch'} · {self._e(run.created_at)}</strong><small>Run {self._e(run.id[:10])} · fixed seed {run.seed}</small></div><div class="benchmark-run-actions">{recommendation}<a class="danger-link" href="/connections/{self._e(provider_id)}/benchmarks/{self._e(run.id)}/delete">Delete</a></div></div>{error}<div class="benchmark-results">{''.join(measurements)}</div></section>"""

    def _voices(self, environ: dict[str, object], csrf: str) -> str:
        profiles = self.generation.list_voice_profiles()
        builtin_cards = "".join(
            f"""<article class="builtin-voice-card"><div class="builtin-voice-head"><span>{self._e(pair.id)}</span><div><h3>{self._e(pair.name)}</h3><p>{self._e(pair.gender)} · Built-in OpenMOSS pair</p></div><b>Built in</b></div><audio controls preload="none" src="/default-voices/{self._e(pair.id)}/audio"></audio><details><summary>Narration instruction</summary><p>{self._e(pair.instruction)}</p><small>Reference text: {self._e(pair.sample_text)}</small></details></article>"""
            for pair in self.default_voices
        )
        profile_cards = "".join(
            f"""<article class="voice-card"><header><span class="voice-avatar">{self._e(profile.name[:1].upper())}</span><div><div class="voice-title-line"><h3>{self._e(profile.name)}</h3>{f'<span class="in-use-badge">In use · {profile.in_use_job_count}</span>' if profile.in_use_job_count else ''}</div><p>{self._e(profile.provider_name)} · {self._e(provider_type(profile.provider_kind).label)}</p></div><div class="card-actions"><a href="/voices/{self._e(profile.id)}/edit">Edit</a>{f'<span class="disabled-action" title="Complete or delete the generation job first">Delete</span>' if profile.in_use_job_count else f'<a class="danger-link" href="/voices/{self._e(profile.id)}/delete">Delete</a>'}</div></header><blockquote>{self._e(profile.profile.get('instruction', ''))}</blockquote><audio controls preload="none" src="/voices/{self._e(profile.id)}/reference/audio"></audio></article>"""
            for profile in profiles
        ) or """<div class="empty-state"><span>V</span><h3>No saved voices</h3><p>Create an audition workspace to find your first reference and instruction pair.</p></div>"""
        start = f"""<form method="post" action="/voices/drafts">{self._csrf(csrf)}<button class="primary">Open Voice Lab</button></form>"""
        body = f"""<section class="page-heading full-page-heading"><div><p class="eyebrow">Narrator pairs</p><h1>Choose a voice or build your own</h1><p>Built-in pairs are ready for any OpenMOSS connection. Custom profiles remain yours to edit and reuse.</p></div></section><div class="voice-library-stack"><section class="panel start-studio"><header><div><p class="eyebrow">Voice Lab</p><h2>Build a custom pair</h2></div></header><div class="start-studio-actions"><p>Create reference audio, pair it with precise narration instructions, and save it for any book.</p>{start}</div></section><section class="custom-voice-library"><div class="section-heading"><div><p class="eyebrow">Created by you</p><h2>Your profiles</h2><p>{len(profiles)} custom profiles ready for narration</p></div><span class="count">{len(profiles):02d}</span></div><div class="voice-list">{profile_cards}</div></section><section class="builtin-library"><div class="section-heading"><div><p class="eyebrow">Included with Narranova</p><h2>Built-in narrator pairs</h2><p>Preview the reference audio and its matching instruction.</p></div><span class="count">{len(self.default_voices):02d}</span></div><div class="builtin-voice-grid">{builtin_cards}</div></section></div>"""
        return self._layout("Voices", body, environ)

    def _quality_sampling_controls(
        self, sampling: Mapping[str, object] | None
    ) -> str:
        values = sampling or {}
        fields = "".join(
            f"""<label>{self._e(field.label)}<small>{self._e(field.help_text)}</small><input name="{self._e(field.name)}" type="number" min="{field.minimum:g}" max="{field.maximum:g}" step="{'1' if field.integer else 'any'}" value="{self._e(values.get(field.name, ''))}" placeholder="Engine default"></label>"""
            for field in OPENMOSS_SAMPLING_FIELDS
        )
        return f"""<details class="quality-settings" data-studio-module="sampling"><summary>Advanced quality &amp; sampling</summary><div class="quality-settings-body"><p>These controls affect quality and variation, not generation speed. Leave a field blank to use the OpenMOSS engine default and omit it from the request.</p><div class="quality-field-grid">{fields}</div></div></details>"""

    @staticmethod
    def _sampling_summary(sampling: Mapping[str, object]) -> str:
        if not sampling:
            return "Quality and sampling: Engine default"
        labels = {field.name: field.label for field in OPENMOSS_SAMPLING_FIELDS}
        return "Quality overrides: " + " · ".join(
            f"{labels.get(name, name)} {value}" for name, value in sampling.items()
        )

    def _edit_voice(self, profile_id: str, environ: dict[str, object], csrf: str) -> str:
        voice = self.generation.get_voice_and_provider(profile_id)
        profile = voice["profile"]
        definition = provider_type(str(voice["provider_kind"]))
        providers = [
            item for item in self.generation.list_providers()
            if item.enabled and item.kind == definition.id
        ]
        provider_options = "".join(
            f'<option value="{self._e(item.id)}"{" selected" if item.id == voice["provider_id"] else ""}>{self._e(item.name)}</option>'
            for item in providers
        )
        reference_field = (
            f"""<label>Replace reference WAV<small>Leave empty to keep the current approved sample.</small><input type="file" name="reference" accept="audio/wav,.wav"></label><audio controls preload="none" src="/voices/{self._e(profile_id)}/reference/audio"></audio>"""
            if definition.supports_reference_audio else ""
        )
        instruction_field = (
            f"""<label>Narration instruction<textarea name="instruction" rows="6" required>{self._e(profile.get('instruction', ''))}</textarea></label>"""
            if definition.supports_instructions else ""
        )
        quality_controls = (
            self._quality_sampling_controls(profile.get("sampling"))
            if definition.supports_sampling
            else ""
        )
        body = f"""<a class="back" href="/voices">← Voice profiles</a><section class="page-heading full-page-heading"><div><p class="eyebrow">Edit voice profile</p><h1>{self._e(profile.get('name', 'Untitled voice'))}</h1><p>Rename this profile or update its reusable voice settings.</p></div></section><section class="panel edit-panel"><form method="post" action="/voices/{self._e(profile_id)}" enctype="multipart/form-data">{self._csrf(csrf)}<label>Profile name<input name="name" value="{self._e(profile.get('name', ''))}" required></label><label>Connection<select name="provider_id" required>{provider_options}</select></label>{instruction_field}<label>Language<input name="language" value="{self._e(profile.get('language', 'English'))}"></label>{quality_controls}{reference_field}<div class="form-actions"><a class="button" href="/voices">Cancel</a><button class="primary">Update voice profile</button></div></form></section>"""
        return self._layout("Edit voice", body, environ)

    def _voice_studio(self, draft_id: str, environ: dict[str, object], csrf: str) -> str:
        draft = self.voice_studio.get(draft_id)
        open_workflow = parse_qs(str(environ.get("QUERY_STRING", ""))).get(
            "open", [""]
        )[0]
        providers = [item for item in self.generation.list_providers() if item.enabled]
        selected_provider = str(draft.get("provider_id") or "")
        provider_options = "".join(
            f'<option value="{self._e(item.id)}" data-instructions="{str(provider_type(item.kind).supports_instructions).lower()}" data-reference="{str(provider_type(item.kind).supports_reference_audio).lower()}" data-sampling="{str(provider_type(item.kind).supports_sampling).lower()}"{" selected" if item.id == selected_provider else ""}>{self._e(item.name)} · {self._e(provider_type(item.kind).label)}</option>'
            for item in providers
        )
        preset_buttons = "".join(
            f'<button type="button" class="prompt-chip" data-instruction="{self._e(instruction)}"><strong>{self._e(name)}</strong><span>{self._e(instruction)}</span></button>'
            for name, instruction in INSTRUCTION_PRESETS
        )
        generated_takes = [
            take for take in draft["takes"] if take.get("reference_choice") != "uploaded"
        ]
        uploaded_takes = [
            take for take in draft["takes"] if take.get("reference_choice") == "uploaded"
        ]

        def take_cards(takes: list[dict[str, object]], label: str, empty: str) -> str:
            return "".join(
                f"""<article class="take-card"><div class="take-index">{len(takes) - index:02d}</div><div class="take-main"><div><strong>{self._e(label)}</strong><span>{float(take['duration_seconds']):.1f} seconds</span></div><audio controls preload="none" src="/voices/drafts/{self._e(draft_id)}/takes/{self._e(str(take['id']))}/audio"></audio><details><summary>Instruction, text, and quality settings</summary><p>{self._e(str(take['instruction']))}</p><small>Reference text: {self._e(str(take['sample_text']))}</small><small>{self._e(self._sampling_summary(take.get('sampling') or {}))}</small></details></div></article>"""
                for index, take in enumerate(reversed(takes))
            ) or f'<div class="empty-state compact"><span>♪</span><h3>{self._e(empty)}</h3><p>New audio will appear here without leaving this workflow.</p></div>'

        generated_cards = take_cards(
            generated_takes, "Reference candidate", "No reference candidates yet"
        )
        uploaded_cards = take_cards(
            uploaded_takes, "Test sample", "No test samples yet"
        )
        can_audition = bool(providers)
        connection_warning = (
            ""
            if can_audition
            else '<div class="studio-warning"><strong>Connection needed</strong><span>Add OpenMOSS before generating your first take.</span><a href="/connections">Set up connection →</a></div>'
        )
        sampling_controls = self._quality_sampling_controls(draft.get("sampling"))
        instruction = self._e(draft.get("instruction", ""))
        language = self._e(draft.get("language", "English"))
        sample_text = self._e(draft.get("sample_text", ""))
        generated_choices = "".join(
            f'<label class="reference-radio"><input type="radio" name="reference_choice" value="take:{self._e(str(take["id"]))}"{" checked" if index == 0 else ""}><span><strong>Reference candidate {len(generated_takes) - index:02d}</strong><small>{float(take["duration_seconds"]):.1f}s · instruction saved exactly as tested</small><em>{self._e(str(take["instruction"]))}</em></span></label>'
            for index, take in enumerate(reversed(generated_takes))
        )
        generated_save = (
            f"""<form class="pair-save-form" method="post" action="/voices/drafts/{self._e(draft_id)}/save">{self._csrf(csrf)}<fieldset class="reference-list"><legend>Save a generated pair</legend><p class="field-help">Choose a candidate. Its generated audio, exact text, tested instruction, language, connection, and quality settings stay together.</p>{generated_choices}</fieldset><label>Profile name<input name="name" value="{self._e(draft.get('name', ''))}" placeholder="My fiction narrator" required></label><button class="primary wide-button">Create selected pair</button><p class="cleanup-note">All other candidate audio and draft files are deleted automatically.</p></form>"""
            if generated_takes
            else ""
        )
        uploaded_reference = (
            f'<div class="uploaded-reference"><div><strong>Uploaded reference</strong><small>Ready to use for generating a reference candidate</small></div><audio controls preload="none" src="/voices/drafts/{self._e(draft_id)}/reference/audio"></audio></div>'
            if draft.get("uploaded_reference_path")
            else ""
        )
        uploaded_choices = "".join(
            f'<label class="reference-radio"><input type="radio" name="reference_choice" value="uploaded:{self._e(str(take["id"]))}"{" checked" if index == 0 else ""}><span><strong>Reference candidate {len(uploaded_takes) - index:02d}</strong><small>{float(take["duration_seconds"]):.1f}s · generated audio and its exact text will be saved</small><em>{self._e(str(take["instruction"]))}</em></span></label>'
            for index, take in enumerate(reversed(uploaded_takes))
        )
        uploaded_save = (
            f"""<form class="pair-save-form" method="post" action="/voices/drafts/{self._e(draft_id)}/save">{self._csrf(csrf)}<fieldset class="reference-list"><legend>Save a generated pair</legend><p class="field-help">Choose a candidate. Narranova saves its generated audio, exact test text, and tested instruction. Your uploaded recording remains temporary.</p>{uploaded_choices}</fieldset><label>Profile name<input name="name" value="{self._e(draft.get('name', ''))}" placeholder="My recorded narrator" required></label><button class="primary wide-button">Create selected pair</button><p class="cleanup-note">The uploaded recording, unselected candidates, and other draft files are deleted automatically.</p></form>"""
            if draft.get("uploaded_reference_path") and uploaded_takes
            else ""
        )
        generated_workflow = f"""<details class="panel lab-workflow"><summary><span>Workflow one</span><div><h2>Design a narrator</h2><p>Generate a short reference voice from written direction, review it, then save the best result as a reusable pair.</p></div><b aria-hidden="true">+</b></summary><div class="lab-workflow-body">{connection_warning}<form class="lab-form" method="post" action="/voices/drafts/{self._e(draft_id)}/auditions" enctype="multipart/form-data">{self._csrf(csrf)}<div class="lab-field-row"><label>Connection<select name="provider_id" data-studio-provider required><option value="">Choose a connection</option>{provider_options}</select></label><label>Language<input name="language" value="{language}"></label></div><fieldset data-studio-module="instructions"><legend>Narration direction</legend><p class="field-help">Choose a simple starting point or write your own direction.</p><div class="prompt-grid">{preset_buttons}</div><label>Instruction<textarea name="instruction" data-instruction-field rows="5" required>{instruction}</textarea></label></fieldset><fieldset><legend>Reference passage</legend><p class="field-help">Aim for about 10–15 seconds of generated speech.</p><label>Text to generate<textarea name="sample_text" rows="5" maxlength="2000" required>{sample_text}</textarea></label></fieldset><input type="hidden" name="reference_choice" value="none">{sampling_controls}<input type="hidden" name="name" value="{self._e(draft.get('name', ''))}"><button class="primary wide-button"{"" if can_audition else " disabled"}>Generate reference audio</button></form><section class="lab-results"><div class="section-heading"><div><p class="eyebrow">Generated references</p><h3>Choose the voice you want to keep</h3></div><span class="count">{len(generated_takes):02d}</span></div><div class="take-list">{generated_cards}</div>{generated_save}</section></div></details>"""
        uploaded_workflow = f"""<details class="panel lab-workflow"><summary><span>Workflow two</span><div><h2>Bring your own reference</h2><p>Upload clean speech, use it to generate candidates, then save the generated result you prefer.</p></div><b aria-hidden="true">+</b></summary><div class="lab-workflow-body">{connection_warning}<form class="lab-form" method="post" action="/voices/drafts/{self._e(draft_id)}/auditions" enctype="multipart/form-data">{self._csrf(csrf)}<div class="lab-field-row"><label>Connection<select name="provider_id" data-studio-provider required><option value="">Choose a connection</option>{provider_options}</select></label><label>Language<input name="language" value="{language}"></label></div><fieldset data-studio-module="reference"><legend>Your reference recording</legend><p class="field-help">Use about 10–15 seconds of clean speech containing only the narrator’s voice.</p><label class="file-drop">{"Replace the uploaded WAV" if draft.get('uploaded_reference_path') else "Choose a WAV"}<input type="file" name="reference" accept="audio/wav,.wav"{"" if draft.get('uploaded_reference_path') else " required"}><span>Speech without music, effects, or background noise works best</span></label>{uploaded_reference}</fieldset><fieldset data-studio-module="instructions"><legend>Matching narration direction</legend><p class="field-help">Describe how the generated narrator should sound.</p><div class="prompt-grid">{preset_buttons}</div><label>Instruction<textarea name="instruction" data-instruction-field rows="5" required>{instruction}</textarea></label></fieldset><fieldset><legend>Test passage</legend><p class="field-help">Aim for about 10–15 seconds. Longer audio can reduce quality, use more memory, or crash the TTS server.</p><label>Text to generate<textarea name="sample_text" rows="5" maxlength="2000" required>{sample_text}</textarea></label></fieldset><input type="hidden" name="reference_choice" value="uploaded">{sampling_controls}<input type="hidden" name="name" value="{self._e(draft.get('name', ''))}"><button class="primary wide-button"{"" if can_audition else " disabled"}>Generate reference audio</button></form><section class="lab-results"><div class="section-heading"><div><p class="eyebrow">Generated references</p><h3>Choose the voice you want to keep</h3></div><span class="count">{len(uploaded_takes):02d}</span></div><div class="take-list">{uploaded_cards}</div>{uploaded_save}</section></div></details>"""
        if open_workflow == "generated":
            generated_workflow = generated_workflow.replace(
                '<details class="panel lab-workflow">',
                '<details class="panel lab-workflow" open>',
                1,
            )
        elif open_workflow == "uploaded":
            uploaded_workflow = uploaded_workflow.replace(
                '<details class="panel lab-workflow">',
                '<details class="panel lab-workflow" open>',
                1,
            )
        audition_status = str(draft.get("audition_status") or "idle")
        audition_error = str(draft.get("audition_error") or "")
        audition_notice = (
            '<div class="selection-note"><span>●</span><p>Generating your audition in the background. You can keep this page open.</p></div>'
            if audition_status in {"queued", "generating"}
            else f'<div class="alert">{self._e(audition_error)}</div>'
            if audition_error
            else ""
        )
        body = f"""<a class="back" href="/voices">← Voice profiles</a><section class="studio-heading full-page-heading"><div><p class="eyebrow">Voice Lab</p><h1>Build a stable narrator</h1><p>Choose one path: create a reference voice from direction, or test a recording of your own.</p></div><form method="post" action="/voices/drafts/{self._e(draft_id)}/discard">{self._csrf(csrf)}<button class="quiet-danger">Discard draft</button></form></section><div class="selection-note voice-lab-guidance"><span>i</span><p><strong>Keep reference audio short.</strong> Aim for 10–15 seconds. Longer audio can reduce quality, use more memory, or crash the TTS server.</p></div><div data-voice-audition-monitor data-status-url="/voices/drafts/{self._e(draft_id)}/status" data-status="{self._e(audition_status)}">{audition_notice}</div><div class="voice-lab-stack">{generated_workflow}{uploaded_workflow}</div>"""
        return self._layout("Voice Lab", body, environ)

    def _new_narration(self, book_id: str, environ: dict[str, object], csrf: str) -> str:
        book = self.books.get_book(book_id)
        record = self.books.get_plan_record(book_id)
        plan_path = self._artifact(record["artifact_path"])
        if self.store.sha256(plan_path) != record["plan_sha256"]:
            raise RuntimeError("Narration plan failed hash validation")
        plan = NarrationPlan.from_json(plan_path.read_text(encoding="utf-8"))
        providers = [item for item in self.generation.list_providers() if item.enabled]
        provider_ids = {item.id for item in providers}
        profiles = [
            item
            for item in self.generation.list_voice_profiles()
            if item.provider_id in provider_ids
        ]
        preferred_provider_id = profiles[0].provider_id if profiles else ""
        if not preferred_provider_id and providers:
            preferred_provider_id = providers[0].id
        enabled_units = sum(unit.enabled for unit in plan.units)
        provider_options = "".join(
            f'<option value="{self._e(item.id)}" data-kind="{self._e(item.kind)}"{" selected" if item.id == preferred_provider_id else ""}>{self._e(item.name)}</option>'
            for item in providers
        )
        profile_options = "".join(
            f'<option value="{self._e(item.id)}" data-provider="{self._e(item.provider_id)}" data-kind="{self._e(item.provider_kind)}">Custom · {self._e(item.name)}</option>'
            for item in profiles
        )
        builtin_options = "".join(
            f'<option value="{self._e(pair.selector_id)}" data-kind="{self._e(pair.provider_kind)}">Built in · {self._e(pair.name)}</option>'
            for pair in self.default_voices
        )
        compatible_builtin = any(
            pair.provider_kind in {provider.kind for provider in providers}
            for pair in self.default_voices
        )
        builtin_previews = "".join(
            f"""<article data-default-kind="{self._e(pair.provider_kind)}"><div><span>{self._e(pair.id)}</span><strong>{self._e(pair.name)}</strong><small>{self._e(pair.gender)}</small></div><audio controls preload="none" src="/default-voices/{self._e(pair.id)}/audio"></audio><p>{self._e(pair.instruction)}</p></article>"""
            for pair in self.default_voices
        )
        custom_previews = "".join(
            f"""<article data-custom-provider="{self._e(profile.provider_id)}"><div><span>{index:02d}</span><strong>{self._e(profile.name)}</strong><small>{self._e(profile.provider_name)}</small></div><audio controls preload="none" src="/voices/{self._e(profile.id)}/reference/audio"></audio><p>{self._e(profile.profile.get('instruction', ''))}</p></article>"""
            for index, profile in enumerate(profiles, start=1)
        )
        custom_preview = (
            f'<details class="pair-preview custom-preview" data-custom-preview><summary>Listen to custom pairs</summary><div>{custom_previews}</div></details>'
            if custom_previews else ""
        )
        if providers and (profiles or compatible_builtin):
            setup = f"""<form class="narration-form" method="post" action="/books/{self._e(book_id)}/jobs">{self._csrf(csrf)}<label>TTS connection<small>The service that will generate every chunk.</small><select name="provider_id" data-provider-select required>{provider_options}</select></label><label>Narrator pair<small>Choose an included pair or one of your custom profiles.</small><select name="voice_profile_id" data-profile-select required>{profile_options}{builtin_options}</select></label>{custom_preview}<details class="pair-preview builtin-preview"><summary>Listen to built-in pairs</summary><div>{builtin_previews}</div></details><div class="selection-note"><span>✓</span><p>The selected pair's reference audio and matching instruction will be copied into this job.</p></div><button class="primary wide-button">Create narration job</button></form>"""
        else:
            needs = []
            if not providers:
                needs.append('<a class="setup-missing" href="/connections"><span>01</span><div><strong>Add a TTS connection</strong><small>Connect your external OpenMOSS service.</small></div><b>→</b></a>')
            if not profiles and not compatible_builtin:
                needs.append(f'<form method="post" action="/voices/drafts" class="setup-missing">{self._csrf(csrf)}<span>02</span><div><strong>Create a voice profile</strong><small>Audition a reusable instruction and reference pair.</small></div><button aria-label="Open Voice Lab">→</button></form>')
            setup = f'<div class="missing-stack">{"".join(needs)}</div>'
        jobs = self.generation.list_jobs(book_id)
        enhancement = self.books.get_enhancement_settings(book_id)
        body = f"""<a class="back" href="/books/{self._e(book_id)}">← Back to book</a><section class="page-heading narration-heading full-page-heading"><div><p class="eyebrow">New narration</p><h1>{self._e(book.title)}</h1><p>Choose the engine and the approved voice pair for this run.</p></div></section><div class="narration-grid"><main class="panel narration-setup"><header><div><p class="eyebrow">Generation setup</p><h2>How should this book sound?</h2></div></header>{setup}</main><aside class="run-summary"><section class="panel"><header><div><p class="eyebrow">Plan summary</p><h2>Ready to generate</h2></div></header><dl><div><dt>Plan revision</dt><dd>{record['revision']}</dd></div><div><dt>Sections</dt><dd>{len(plan.chapters)}</dd></div><div><dt>Included units</dt><dd>{enabled_units}</dd></div><div><dt>Enhancement</dt><dd>{'On' if enhancement.enabled else 'Off'}</dd></div><div><dt>Previous jobs</dt><dd>{len(jobs)}</dd></div></dl><a class="button" href="/books/{self._e(book_id)}">Review plan &amp; enhancement</a></section></aside></div>"""
        return self._layout("New narration", body, environ)

    def _book(self, book_id: str, environ: dict[str, object], csrf: str) -> str:
        book = self.books.get_book(book_id)
        record = self.books.get_plan_record(book_id)
        plan_path = self._artifact(record["artifact_path"])
        if self.store.sha256(plan_path) != record["plan_sha256"]:
            raise RuntimeError("Narration plan failed hash validation")
        plan = NarrationPlan.from_json(plan_path.read_text(encoding="utf-8"))
        jobs = self.generation.list_jobs(book_id)
        enhancement = self.books.get_enhancement_settings(book_id)
        units_by_id = {unit.id: unit for unit in plan.units}
        chapter_markup: list[str] = []
        for index, chapter in enumerate(plan.chapters):
            chapter_units = [units_by_id[unit_id] for unit_id in chapter.unit_ids]
            chapter_enabled = all(unit.enabled for unit in chapter_units)
            checked = " checked" if chapter_enabled else ""
            units_markup = "".join(
                f'<p><span>{self._e(unit.id)}</span>{self._e(unit.display_text) if unit.display_text else "Scene break"}</p>'
                for unit in chapter_units
            )
            chapter_markup.append(
                f"""<details class="chapter" {'open' if index == 0 else ''}>
                <summary><span>{index + 1:02d}</span><strong>{self._e(chapter.title)}</strong>
                <small>{len(chapter.unit_ids)} units</small><label class="chapter-choice" title="Include this section in narration"><input type="checkbox" name="chapter_{chapter.spine_index}"{checked}><i></i><b></b></label></summary>
                <div class="units">{units_markup}</div></details>"""
            )
        chapters = "".join(chapter_markup)
        enabled_units = sum(unit.enabled for unit in plan.units)
        job_rows = "".join(
            f'<a class="job-row" href="/jobs/{self._e(job.id)}"><span><strong>Narration {self._e(job.id[:8])}</strong><small>{self._e(job.created_at)}</small></span><span class="status status-{self._e(job.status)}">{self._e(job.status)}</span></a>'
            for job in jobs
        ) or '<div class="empty">No generation job yet.</div>'
        enhancement_form = f"""<section class="panel enhancement-card"><header><div><p class="eyebrow">Narration enhancement</p><h2>Shape the spoken text</h2><p class="section-help">Deterministic controls applied only to new TTS jobs. Your extracted book text stays unchanged.</p></div></header><form method="post" action="/books/{self._e(book_id)}/enhancement" class="enhancement-form">{self._csrf(csrf)}<label class="setting-switch"><span><strong>Use narration enhancement</strong><small>Add structural pauses and optional pronunciation controls.</small></span><input type="checkbox" name="enabled"{' checked' if enhancement.enabled else ''}></label><div class="pause-grid"><label>Chapter heading pause<input type="number" name="chapter_pause_seconds" min="0.1" max="10" step="0.1" value="{enhancement.chapter_pause_seconds:.1f}"><small>seconds</small></label><label>Section heading pause<input type="number" name="section_pause_seconds" min="0.1" max="10" step="0.1" value="{enhancement.section_pause_seconds:.1f}"><small>seconds</small></label><label>Scene break pause<input type="number" name="scene_break_pause_seconds" min="0.1" max="10" step="0.1" value="{enhancement.scene_break_pause_seconds:.1f}"><small>seconds</small></label></div><label class="setting-switch"><span><strong>Normalize text for TTS</strong><small>Standardize whitespace, quotes, ellipses, and dash spacing.</small></span><input type="checkbox" name="normalize_text"{' checked' if enhancement.normalize_text else ''}></label><label class="setting-switch"><span><strong>Use pronunciation dictionary</strong><small>Replace matching terms with OpenMOSS IPA before synthesis.</small></span><input type="checkbox" name="pronunciation_enabled"{' checked' if enhancement.pronunciation_enabled else ''}></label><label>Pronunciation dictionary<small>One entry per line using <code>term = IPA</code>. Slashes are optional.</small><textarea name="pronunciations" rows="5" placeholder="Narranova = næɹəˈnoʊvə">{self._e(format_pronunciations(enhancement.pronunciations))}</textarea></label><button class="primary wide-button">Save enhancement settings</button></form></section>"""
        body = f"""<a class="back" href="/">← Workspace</a><section class="book-head full-page-heading"><div><p class="eyebrow">Book workspace</p><h1>{self._e(book.title)}</h1><p>{self._e(book.author or 'Unknown author')} · {len(plan.chapters)} sections · <span data-plan-enabled-units>{enabled_units}</span> of {len(plan.units)} units included</p></div><div class="head-actions"><a class="danger-link" href="/books/{self._e(book_id)}/delete">Delete book</a></div></section>
        <div class="book-grid"><main><section class="panel"><form class="plan-form" method="post" action="/books/{self._e(book_id)}/plan" data-plan-form>{self._csrf(csrf)}<header><div><p class="eyebrow">Narration plan · revision <span data-plan-revision>{record['revision']}</span></p><h2>Choose what to narrate</h2><p class="section-help">Turn off front matter, tables of contents, copyright pages, or any other section you do not want spoken.</p></div><span class="auto-save-status" data-plan-status>Changes save automatically</span></header>{chapters}<div class="plan-note"><span>Every toggle is saved automatically. The current choices will be used by the next narration job; existing jobs keep their original text.</span></div></form></section></main>
        <aside class="stack"><section class="panel book-workflow"><header><div><p class="eyebrow">New narration</p><h2>Turn this plan into audio</h2></div></header><div class="book-workflow-body"><p>Choose a TTS connection and narrator pair for a new job using plan revision <span data-plan-workflow-revision>{record['revision']}</span>.</p><a class="button primary" data-plan-next href="/books/{self._e(book_id)}/narrations/new">Set up narration</a></div></section>{enhancement_form}<section class="panel"><header><div><p class="eyebrow">Activity</p><h2>Generation jobs</h2></div><span class="count">{len(jobs):02d}</span></header>{job_rows}</section></aside></div>"""
        return self._layout(book.title, body, environ)

    def _job(self, job_id: str, environ: dict[str, object], csrf: str) -> str:
        job = self.generation.get_job(job_id)
        chunks = self.generation.list_chunks(job_id)
        completed = sum(chunk.status == "completed" for chunk in chunks)
        percent = round((completed / len(chunks)) * 100) if chunks else 0
        status = str(job["status"])
        chapter_pause_requested = job.get("pause_after_chapter_index") is not None
        regenerating = self.supervisor.is_regenerating(job_id)
        assembling = status == "assembling" or self.supervisor.is_assembling(job_id)
        all_completed = bool(chunks) and completed == len(chunks)
        compacted = bool(job.get("compacted_at"))
        masters_available = bool(chunks) and all(
            chunk.audio_artifact_path and chunk.audio_sha256 for chunk in chunks
        )
        editable_bytes = self._chunk_master_bytes(chunks)
        artifacts = self._visible_outputs(job_id)
        has_audiobook = any(artifact.kind == "audiobook" for artifact in artifacts)
        regeneration_disabled = status in {
            "generating", "pause_requested", "cancel_requested", "assembling"
        }
        chunk_rows = "".join(
            f"""<article class="chunk-row" data-chunk-id="{self._e(chunk.database_id)}"><div><span class="mono">{self._e(chunk.id)}</span><strong data-chunk-status>{self._e(chunk.status)}</strong><small data-chunk-meta>{chunk.attempts} attempt{'s' if chunk.attempts != 1 else ''}{f' · {chunk.duration_seconds:.1f}s' if chunk.duration_seconds else ''}{' · lossless FLAC' if chunk.audio_artifact_path else ''}</small></div><div class="chunk-actions" data-chunk-actions>{self._chunk_actions(job_id, chunk.database_id, csrf, regeneration_disabled) if chunk.status == 'completed' and chunk.audio_artifact_path else '<span class="source-removed">Master removed after finalization</span>' if compacted else ''}</div></article>"""
            for chunk in chunks
        )
        startable = (
            status in {"ready", "failed", "paused"} and not all_completed
        ) or (compacted and status == "completed")
        start_label = (
            "Restore editable sources"
            if compacted
            else "Resume generation"
            if status in {"failed", "paused"}
            else "Start generation"
        )
        running_label = (
            "Building audiobook"
            if assembling
            else "Regenerating chunk"
            if regenerating
            else "Generation in progress"
        )
        stop_control = f'''<form method="post" action="/jobs/{self._e(job_id)}/cancel" data-job-cancel{' hidden' if status not in {'generating', 'pause_requested', 'cancel_requested'} or regenerating else ''}>{self._csrf(csrf)}<button class="danger-button">Stop now</button></form><span class="pause-mark" data-job-cancel-requested{' hidden' if status != 'cancel_requested' else ''}>Stopping · discarding the active partial chunk</span>'''
        controls = f"""<form method="post" action="/jobs/{self._e(job_id)}/run" data-job-start{' hidden' if not startable else ''}>{self._csrf(csrf)}<button class="primary" data-job-start-label>{start_label}</button></form><span class="running-mark" data-job-running{' hidden' if status not in {'generating', 'assembling'} else ''}><i></i><b data-job-running-label>{running_label}</b></span><form method="post" action="/jobs/{self._e(job_id)}/pause" data-job-pause{' hidden' if status != 'generating' or regenerating or chapter_pause_requested else ''}>{self._csrf(csrf)}<button>Pause after chunk</button></form><form method="post" action="/jobs/{self._e(job_id)}/pause-chapter" data-job-pause-chapter{' hidden' if status != 'generating' or regenerating or chapter_pause_requested else ''}>{self._csrf(csrf)}<button>Pause after chapter</button></form>{stop_control}<span class="pause-mark" data-job-chapter-pause-requested{' hidden' if not chapter_pause_requested else ''}>Pause requested · finishing current chapter</span><span class="pause-mark" data-job-pause-requested{' hidden' if status != 'pause_requested' else ''}>Pause requested · finishing current chunk</span><span class="complete-mark" data-job-complete{' hidden' if status != 'completed' else ''}>✓ Generation complete</span><a class="danger-link job-delete" href="/jobs/{self._e(job_id)}/delete">Delete job</a>"""
        error_message = str(job.get("error_message") or "")
        error = f'<div class="alert" data-job-error{"" if error_message else " hidden"}>{self._e(error_message)}</div>'
        output_rows = self._output_rows(job_id, artifacts)
        assemble_allowed = (
            all_completed
            and masters_available
            and status in {"completed", "failed"}
        )
        assemble_label = "Rebuild audiobook" if has_audiobook else "Build audiobook"
        empty_output = (
            "No deliverables yet. Build the audiobook to create them."
            if all_completed
            else "Complete every chunk, then build the audiobook."
        )
        storage = self._storage_controls(
            job_id, has_audiobook, compacted, editable_bytes
        )
        output_panel = f"""<section class="panel outputs"><header><div><p class="eyebrow">Deliverables</p><h2>Audiobook files</h2></div><span class="count" data-output-count>{len(artifacts):02d}</span></header><div data-output-artifacts>{output_rows or f'<div class="empty">{empty_output}</div>'}</div><div class="output-build"><form method="post" action="/jobs/{self._e(job_id)}/assemble" data-job-assemble{' hidden' if not assemble_allowed else ''}>{self._csrf(csrf)}<button class="primary" data-job-assemble-label>{assemble_label}</button></form><p>Encodes the lossless FLAC masters once into a source-mapped, chapterized M4B.</p></div><div class="storage-policy" data-storage-policy>{storage}</div></section>"""
        body = f"""<a class="back" href="/books/{self._e(job['book_id'])}">← Back to book</a><section class="job-head full-page-heading"><div><p class="eyebrow">Generation job</p><h1>{self._e(job_id[:12])}</h1><p data-job-summary>{completed} of {len(chunks)} chunks complete</p></div><span class="status status-{self._e(status)}" data-job-status>{self._e(status)}</span></section>{error}<section class="panel progress-panel" data-job-monitor data-job-id="{self._e(job_id)}" data-csrf="{self._e(csrf)}"><div class="progress-copy"><strong data-job-percent>{percent}%</strong><span>verified audio</span></div><div class="progress"><i data-job-progress-bar style="width:{percent}%"></i></div><div class="job-actions">{controls}</div></section>{output_panel}<section class="panel chunks"><header><div><p class="eyebrow">Artifacts</p><h2>Audio chunks</h2></div><span class="count">{len(chunks):02d}</span></header>{chunk_rows}</section>"""
        return self._layout("Generation", body, environ)

    def _storage_controls(
        self,
        job_id: str,
        has_audiobook: bool,
        compacted: bool,
        editable_bytes: int,
    ) -> str:
        if compacted:
            return """<div><span class="status status-completed">Compact</span><strong>Finished files only</strong><p>Lossless chunk masters were removed. The M4B and narration map remain.</p></div>"""
        action = (
            f'<a class="button" href="/jobs/{self._e(job_id)}/compact">Finalize and free space</a>'
            if has_audiobook and editable_bytes
            else ""
        )
        return f"""<div><span class="status">Editable</span><strong>{self._format_bytes(editable_bytes)} of lossless chunk masters</strong><p>Keep these FLAC files for individual regeneration, or remove them after approving the M4B.</p></div>{action}"""

    def _chunk_master_bytes(self, chunks: list[StoredChunk]) -> int:
        total = 0
        for chunk in chunks:
            if not chunk.audio_artifact_path:
                continue
            try:
                path = self._artifact(str(chunk.audio_artifact_path))
                if path.is_file():
                    total += path.stat().st_size
            except (OSError, RuntimeError):
                continue
        return total

    def _visible_outputs(self, job_id: str) -> list[StoredArtifact]:
        return [
            artifact
            for artifact in self.generation.list_job_artifacts(job_id)
            if artifact.kind in {"chapter_audio", "audiobook", "narration_map"}
        ]

    def _output_rows(self, job_id: str, artifacts: list[StoredArtifact]) -> str:
        rows = []
        for artifact in artifacts:
            if artifact.kind == "chapter_audio":
                label = str(
                    artifact.metadata.get("title")
                    or f"Chapter {artifact.chapter_index}"
                )
                kind = "Chapter audio"
            elif artifact.kind == "audiobook":
                label = "Chapterized audiobook"
                kind = "M4B"
            else:
                label = "Narration map"
                kind = "JSON"
            rows.append(
                f'<article class="output-row"><div><strong>{self._e(label)}</strong>'
                f'<small>{self._e(kind)} · {self._format_bytes(artifact.byte_size)}</small>'
                f'</div><a class="button" href="/jobs/{self._e(job_id)}/artifacts/'
                f'{self._e(artifact.id)}/download">Download</a></article>'
            )
        return "".join(rows)

    def _chunk_actions(
        self, job_id: str, chunk_id: str, csrf: str, regeneration_disabled: bool
    ) -> str:
        base = f"/jobs/{self._e(job_id)}/chunks/{self._e(chunk_id)}"
        disabled = " disabled" if regeneration_disabled else ""
        return f"""<audio controls preload="none" src="{base}/audio"></audio><span class="chunk-action-links"><form method="post" action="{base}/regenerate">{self._csrf(csrf)}<button class="chunk-regenerate" data-chunk-regenerate{disabled}>Regenerate</button></form><a class="chunk-download" href="{base}/download">Download</a><a class="danger-link" href="{base}/delete">Delete</a></span>"""

    def _job_state(self, start_response: StartResponse, job_id: str) -> Iterable[bytes]:
        job = self.generation.get_job(job_id)
        chunks = self.generation.list_chunks(job_id)
        completed = sum(chunk.status == "completed" for chunk in chunks)
        percent = round((completed / len(chunks)) * 100) if chunks else 0
        artifacts = self._visible_outputs(job_id)
        compacted = bool(job.get("compacted_at"))
        masters_available = bool(chunks) and all(
            chunk.audio_artifact_path and chunk.audio_sha256 for chunk in chunks
        )
        content = json.dumps(
            {
                "status": job["status"],
                "chapter_pause_requested": job.get("pause_after_chapter_index")
                is not None,
                "regenerating": self.supervisor.is_regenerating(job_id),
                "assembling": self.supervisor.is_assembling(job_id),
                "error": job.get("error_message") or "",
                "completed": completed,
                "total": len(chunks),
                "percent": percent,
                "can_assemble": bool(chunks)
                and completed == len(chunks)
                and masters_available
                and job["status"] in {"completed", "failed"},
                "compacted": compacted,
                "editable_bytes": self._chunk_master_bytes(chunks),
                "has_audiobook": any(
                    artifact.kind == "audiobook" for artifact in artifacts
                ),
                "artifacts": [
                    {
                        "id": artifact.id,
                        "kind": artifact.kind,
                        "title": artifact.metadata.get("title"),
                        "chapter_index": artifact.chapter_index,
                        "byte_size": artifact.byte_size,
                    }
                    for artifact in artifacts
                ],
                "chunks": [
                    {
                        "id": chunk.database_id,
                        "status": chunk.status,
                        "attempts": chunk.attempts,
                        "duration": chunk.duration_seconds,
                        "audio_available": bool(chunk.audio_artifact_path),
                    }
                    for chunk in chunks
                ],
            }
        ).encode("utf-8")
        return self._respond(
            start_response,
            "200 OK",
            content,
            "application/json; charset=utf-8",
        )

    def _benchmark_state(
        self, start_response: StartResponse, provider_id: str, benchmark_id: str
    ) -> Iterable[bytes]:
        run = self.benchmarks.repository.get_run(benchmark_id)
        if run.provider_id != provider_id:
            raise KeyError("Connection benchmark not found")
        content = json.dumps(
            {
                "status": run.status,
                "completed": len(run.results),
                "total": len(run.requested_frames),
                "active_stream_chunk_frames": run.active_stream_chunk_frames,
                "recommended_stream_chunk_frames": (
                    run.recommended_stream_chunk_frames
                ),
                "error": run.error_message or "",
            }
        ).encode("utf-8")
        return self._respond(
            start_response,
            "200 OK",
            content,
            "application/json; charset=utf-8",
        )

    def _connection_health(
        self, start_response: StartResponse, provider_id: str
    ) -> Iterable[bytes]:
        provider = self.generation.get_provider(provider_id)
        healthy = False
        model = ""
        error = ""
        try:
            if provider["kind"] != "openmoss":
                raise ValueError("Health checks are not implemented for this engine")
            information = OpenMossProvider(
                OpenMossConfig.from_connection(
                    str(provider["endpoint_url"]),
                    provider["configuration"],
                    timeout_seconds=3,
                )
            ).health()
            healthy = True
            model = str(
                information.get("architecture")
                or information.get("model")
                or information.get("model_name")
                or ""
            )
        except Exception as exc:
            error = str(exc)
        content = json.dumps(
            {"healthy": healthy, "model": model, "error": error}
        ).encode("utf-8")
        return self._respond(
            start_response,
            "200 OK",
            content,
            "application/json; charset=utf-8",
        )

    def _benchmark_audio(
        self,
        start_response: StartResponse,
        provider_id: str,
        benchmark_id: str,
        frames: int,
        range_header: str,
    ) -> Iterable[bytes]:
        run = self.benchmarks.repository.get_run(benchmark_id)
        if run.provider_id != provider_id:
            raise KeyError("Connection benchmark not found")
        path = self.benchmarks.verified_sample(benchmark_id, frames)
        size = path.stat().st_size
        headers = [("Accept-Ranges", "bytes")]
        byte_range = self._audio_range(range_header, size)
        if range_header and byte_range is None:
            return self._respond(
                start_response,
                "416 Range Not Satisfiable",
                b"",
                "audio/flac",
                headers=headers + [("Content-Range", f"bytes */{size}")],
            )
        if byte_range is not None:
            start, end = byte_range
            return self._stream_file(
                start_response,
                "206 Partial Content",
                path,
                "audio/flac",
                headers=headers + [("Content-Range", f"bytes {start}-{end}/{size}")],
                start=start,
                length=end - start + 1,
            )
        return self._stream_file(start_response, "200 OK", path, "audio/flac", headers=headers)

    def _confirm(
        self,
        environ: dict[str, object],
        csrf: str,
        *,
        title: str,
        subject: str,
        warning: str,
        action: str,
        cancel: str,
        button: str,
    ) -> str:
        body = f"""<section class="confirm-page"><p class="eyebrow">Confirmation required</p><h1>{self._e(title)}</h1><h2>{self._e(subject)}</h2><p>{self._e(warning)}</p><div class="confirm-actions"><a class="button" href="{cancel}">Cancel</a><form method="post" action="{action}">{self._csrf(csrf)}<button class="danger-button">{self._e(button)}</button></form></div></section>"""
        return self._layout(title, body, environ)

    def _audio(
        self,
        start_response: StartResponse,
        job_id: str,
        chunk_id: str,
        *,
        range_header: str = "",
        download: bool = False,
    ) -> Iterable[bytes]:
        chunk = self.generation.get_chunk(job_id, chunk_id)
        if chunk.status != "completed" or not chunk.audio_artifact_path:
            raise KeyError("Audio is not available")
        path = self.jobs.verified_chunk_path(job_id, chunk_id)
        headers: list[tuple[str, str]] = [("Accept-Ranges", "bytes")]
        if download:
            safe_name = "".join(
                character
                for character in chunk.id
                if character.isascii()
                and (character.isalnum() or character in {"-", "_"})
            ) or "chunk"
            headers.append(
                ("Content-Disposition", f'attachment; filename="{safe_name}.flac"')
            )
        size = path.stat().st_size
        byte_range = self._audio_range(range_header, size) if not download else None
        if range_header and not download and byte_range is None:
            return self._respond(
                start_response,
                "416 Range Not Satisfiable",
                b"",
                "audio/flac",
                headers=headers + [("Content-Range", f"bytes */{size}")],
            )
        if byte_range is not None:
            start, end = byte_range
            return self._stream_file(
                start_response,
                "206 Partial Content",
                path,
                "audio/flac",
                headers=headers + [("Content-Range", f"bytes {start}-{end}/{size}")],
                start=start,
                length=end - start + 1,
            )
        return self._stream_file(start_response, "200 OK", path, "audio/flac", headers=headers)

    def _artifact_download(
        self,
        start_response: StartResponse,
        job_id: str,
        artifact_id: str,
    ) -> Iterable[bytes]:
        artifact = self.generation.get_job_artifact(job_id, artifact_id)
        path = self._artifact(artifact.relative_path)
        if not path.is_file() or self.store.sha256(path) != artifact.sha256:
            raise RuntimeError("Output artifact failed hash validation")
        job = self.generation.get_job(job_id)
        stem = self._safe_filename(str(job["book_title"]))
        if artifact.kind == "chapter_audio":
            validate_wave(path)
            title = str(
                artifact.metadata.get("title")
                or f"Chapter {artifact.chapter_index}"
            )
            filename = f"{self._safe_filename(title)}.wav"
            content_type = "audio/wav"
        elif artifact.kind == "audiobook":
            filename = f"{stem}.m4b"
            content_type = "audio/mp4"
        elif artifact.kind == "narration_map":
            filename = f"{stem}-narration-map.json"
            content_type = "application/json; charset=utf-8"
        else:
            raise KeyError("Output artifact is not downloadable")
        headers = [
            ("Content-Type", content_type),
            ("Content-Disposition", f'attachment; filename="{filename}"'),
            ("Content-Length", str(path.stat().st_size)),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "same-origin"),
            (
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self'; media-src 'self'",
            ),
        ]
        start_response("200 OK", headers)

        def stream() -> Iterable[bytes]:
            with path.open("rb") as source:
                while block := source.read(1024 * 1024):
                    yield block

        return stream()

    @staticmethod
    def _safe_filename(value: str) -> str:
        safe = "".join(
            character
            for character in value
            if character.isascii()
            and (character.isalnum() or character in {" ", "-", "_", "."})
        ).strip(" .")
        return safe or "Narranova audiobook"

    @staticmethod
    def _audio_range(header: str, size: int) -> tuple[int, int] | None:
        if not header.startswith("bytes=") or "," in header or size <= 0:
            return None
        value = header.removeprefix("bytes=").strip()
        if "-" not in value:
            return None
        first, last = value.split("-", 1)
        try:
            if not first:
                suffix = int(last)
                if suffix <= 0:
                    return None
                return max(0, size - suffix), size - 1
            start = int(first)
            end = int(last) if last else size - 1
        except ValueError:
            return None
        if start < 0 or start >= size or end < start:
            return None
        return start, min(end, size - 1)

    def _studio_audio(
        self,
        start_response: StartResponse,
        draft_id: str,
        take_id: str,
    ) -> Iterable[bytes]:
        path, _ = self.voice_studio.take_audio(draft_id, take_id)
        return self._stream_file(start_response, "200 OK", path, "audio/wav")

    def _studio_uploaded_audio(
        self,
        start_response: StartResponse,
        draft_id: str,
    ) -> Iterable[bytes]:
        path, _ = self.voice_studio.uploaded_audio(draft_id)
        return self._stream_file(start_response, "200 OK", path, "audio/wav")

    def _profile_audio(
        self,
        start_response: StartResponse,
        profile_id: str,
    ) -> Iterable[bytes]:
        voice = self.generation.get_voice_and_provider(profile_id)
        profile = voice["profile"]
        path = self._artifact(profile["reference_artifact_path"])
        validate_wave(path)
        if self.store.sha256(path) != profile["reference_sha256"]:
            raise RuntimeError("Voice reference failed hash validation")
        return self._stream_file(start_response, "200 OK", path, "audio/wav")

    def _default_voice_audio(
        self,
        start_response: StartResponse,
        voice_id: str,
    ) -> Iterable[bytes]:
        pair = default_voice_pair(f"builtin:{voice_id}")
        validate_wave(pair.audio_path)
        if self.store.sha256(pair.audio_path) != pair.audio_sha256:
            raise RuntimeError("Built-in narrator audio failed hash validation")
        return self._stream_file(start_response, "200 OK", pair.audio_path, "audio/wav")

    def _layout(self, title: str, body: str, environ: dict[str, object], refresh: bool = False) -> str:
        query = parse_qs(str(environ.get("QUERY_STRING", "")))
        notice = query.get("notice", [""])[0]
        path = str(environ.get("PATH_INFO", "/"))
        refresh_tag = '<meta http-equiv="refresh" content="4">' if refresh else ""
        notice_html = f'<div class="notice">{self._e(notice)}</div>' if notice else ""
        nav = (
            ("/", "Library", path == "/" or path.startswith("/books/")),
            ("/connections", "Connections", path.startswith("/connections")),
            ("/voices", "Voices", path.startswith("/voices")),
            ("/jobs", "Jobs", path == "/jobs" or path.startswith("/jobs/")),
        )
        nav_html = "".join(
            f'<a href="{href}" class="{"active" if active else ""}">{label}</a>'
            for href, label, active in nav
        )
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark"><title>{self._e(title)} · Narranova</title><link rel="icon" href="/static/favicon.svg" type="image/svg+xml">{refresh_tag}<script src="/static/theme.js"></script><link rel="stylesheet" href="/static/app.css"><link rel="stylesheet" href="/static/choices.css"><script defer src="/static/app.js"></script></head><body><header class="topbar"><div class="topbar-inner"><a class="brand" href="/"><span>N</span><strong>Narranova</strong></a><nav aria-label="Primary navigation">{nav_html}</nav><div class="topbar-actions"><button class="theme-toggle" type="button" data-theme-toggle aria-pressed="false"><span class="theme-icon" aria-hidden="true"></span><span data-theme-label>Dark</span></button></div></div></header><div class="shell">{notice_html}{body}</div><footer>Narranova · Local-first audiobook production</footer></body></html>"""

    def _parse_form(self, environ: dict[str, object]) -> tuple[dict[str, str], dict[str, Upload]]:
        try:
            length = int(str(environ.get("CONTENT_LENGTH", "0") or "0"))
        except ValueError as exc:
            raise ValueError("Invalid request length") from exc
        if length > MAX_REQUEST_BYTES:
            raise ValueError("Upload exceeds the 128 MB request limit")
        content_type = str(environ.get("CONTENT_TYPE", ""))
        fields: dict[str, str] = {}
        uploads: dict[str, Upload] = {}
        if content_type.startswith("multipart/form-data"):
            form = cgi.FieldStorage(fp=environ["wsgi.input"], environ=environ, keep_blank_values=True)
            items = form.list or []
            for item in items:
                if item.filename:
                    suffix = Path(item.filename).suffix[:12]
                    with NamedTemporaryFile(
                        dir=self.layout.temporary_root, suffix=suffix, delete=False
                    ) as temporary:
                        shutil.copyfileobj(item.file, temporary, 1024 * 1024)
                    uploads[item.name] = Upload(Path(item.filename).name, Path(temporary.name))
                else:
                    fields[item.name] = str(item.value)
        else:
            body = environ["wsgi.input"].read(length).decode("utf-8")
            fields = {key: values[-1] for key, values in parse_qs(body, keep_blank_values=True).items()}
        return fields, uploads

    def _csrf_token(self, environ: dict[str, object]) -> tuple[str, str | None]:
        cookies = str(environ.get("HTTP_COOKIE", ""))
        token = next(
            (part.split("=", 1)[1] for part in cookies.split(";") if part.strip().startswith("narranova_csrf=")),
            None,
        )
        if token and len(token) == 43:
            return token, None
        token = secrets.token_urlsafe(32)
        return token, f"narranova_csrf={token}; Path=/; HttpOnly; SameSite=Strict"

    def _artifact(self, relative: str) -> Path:
        path = (self.layout.root / relative).resolve()
        if not path.is_relative_to(self.layout.root):
            raise RuntimeError("Artifact path escapes the data directory")
        return path

    @staticmethod
    def _csrf(token: str) -> str:
        return f'<input type="hidden" name="csrf" value="{html.escape(token, quote=True)}">'

    @staticmethod
    def _e(value: object) -> str:
        return html.escape(str(value), quote=True)

    @staticmethod
    def _format_bytes(size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return f"{size} B"

    def _html(self, start_response: StartResponse, content: str, cookie: str | None) -> Iterable[bytes]:
        headers = [("Content-Type", "text/html; charset=utf-8")]
        if cookie:
            headers.append(("Set-Cookie", cookie))
        return self._respond(start_response, "200 OK", content.encode(), headers=headers)

    def _error(self, start_response: StartResponse, status: HTTPStatus, message: str, cookie: str | None) -> Iterable[bytes]:
        body = self._layout(
            status.phrase,
            f'<section class="error-page"><p class="eyebrow">{status.value}</p><h1>{self._e(status.phrase)}</h1><p>{self._e(message)}</p><a class="button" href="/">Return to workspace</a></section>',
            {},
        )
        headers = [("Content-Type", "text/html; charset=utf-8")]
        if cookie:
            headers.append(("Set-Cookie", cookie))
        return self._respond(start_response, f"{status.value} {status.phrase}", body.encode(), headers=headers)

    @staticmethod
    def _redirect(start_response: StartResponse, location: str) -> Iterable[bytes]:
        start_response("303 See Other", [("Location", location), ("Content-Length", "0")])
        return [b""]

    @staticmethod
    def _respond(
        start_response: StartResponse,
        status: str,
        content: bytes,
        content_type: str | None = None,
        headers: list[tuple[str, str]] | None = None,
    ) -> Iterable[bytes]:
        response_headers = list(headers or [])
        if content_type:
            response_headers.append(("Content-Type", content_type))
        response_headers.extend(
            [
                ("Content-Length", str(len(content))),
                ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "same-origin"),
                (
                    "Content-Security-Policy",
                    "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "script-src 'self'; media-src 'self'",
                ),
            ]
        )
        start_response(status, response_headers)
        return [content]

    @staticmethod
    def _stream_file(
        start_response: StartResponse,
        status: str,
        path: Path,
        content_type: str,
        *,
        headers: list[tuple[str, str]] | None = None,
        start: int = 0,
        length: int | None = None,
    ) -> Iterable[bytes]:
        remaining = path.stat().st_size - start if length is None else length
        response_headers = list(headers or [])
        response_headers.extend(
            [
                ("Content-Type", content_type),
                ("Content-Length", str(remaining)),
                ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "same-origin"),
            ]
        )
        start_response(status, response_headers)

        def stream() -> Iterable[bytes]:
            left = remaining
            with path.open("rb") as source:
                source.seek(start)
                while left > 0:
                    block = source.read(min(1024 * 1024, left))
                    if not block:
                        break
                    left -= len(block)
                    yield block

        return stream()


def create_web_app(
    data_dir: str | Path | None = None,
    *,
    masters: AudioMasters | None = None,
    encoder: M4BEncoder | None = None,
) -> NarranovaWebApp:
    settings = Settings.load(data_dir)
    layout = ArtifactLayout.at(settings.data_dir)
    layout.initialize()
    database = Database(settings.database_path)
    database.initialize()
    books = BookRepository(database)
    generation = GenerationRepository(database)
    generation.recover_interrupted_jobs()
    generation.recover_interrupted_assemblies()
    benchmark_repository = BenchmarkRepository(database)
    benchmark_repository.recover_interrupted()
    store = ArtifactStore(settings.data_dir)
    store.cleanup_abandoned_partials()
    audio_masters = masters or FFmpegAudioMasters()
    profiles = VoiceProfiles(generation, layout, store)
    jobs = GenerationJobs(books, generation, layout, store, masters=audio_masters)
    assembler = AudioAssembler(
        generation, layout, store, encoder=encoder, masters=audio_masters
    )
    benchmarks = ConnectionBenchmarks(
        benchmark_repository,
        generation,
        layout,
        store,
        masters=audio_masters,
    )
    return NarranovaWebApp(
        settings,
        books,
        generation,
        layout,
        store,
        ImportBook(EpubParser(), books, layout, store),
        ReviseNarrationPlan(books, layout, store),
        profiles,
        jobs,
        assembler,
        DeleteArtifacts(books, generation, layout),
        VoiceStudio(generation, profiles, layout, store),
        benchmarks,
    )
