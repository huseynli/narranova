"""Dependency-light, server-rendered web UI for the local Narranova service."""

from __future__ import annotations

import cgi
import html
import json
import secrets
import shutil
import threading
from dataclasses import dataclass
from http import HTTPStatus
from importlib.resources import files
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, Iterable
from urllib.parse import parse_qs, quote, unquote

from narranova.application.default_voices import (
    default_voice_pair,
    default_voice_pairs,
)
from narranova.application.deletion import DeleteArtifacts
from narranova.application.generation import GenerationJobs, VoiceProfiles
from narranova.application.ingest import ImportBook
from narranova.application.provider_catalog import PROVIDER_TYPES, provider_type
from narranova.application.revise_plan import ReviseNarrationPlan
from narranova.application.voice_studio import INSTRUCTION_PRESETS, VoiceStudio
from narranova.artifacts import ArtifactLayout, ArtifactStore
from narranova.audio import validate_wave
from narranova.config import Settings
from narranova.domain.narration import NarrationPlan
from narranova.epub import EpubParser
from narranova.persistence import Database
from narranova.persistence.books import BookRepository
from narranova.persistence.generation import GenerationRepository


MAX_REQUEST_BYTES = 128 * 1024 * 1024
StartResponse = Callable[[str, list[tuple[str, str]]], None]


@dataclass(frozen=True)
class Upload:
    filename: str
    path: Path


class JobSupervisor:
    def __init__(self, jobs: GenerationJobs) -> None:
        self.jobs = jobs
        self._lock = threading.Lock()
        self._active: set[str] = set()

    def start(self, job_id: str) -> bool:
        with self._lock:
            if job_id in self._active:
                return False
            self._active.add(job_id)
        thread = threading.Thread(target=self._run, args=(job_id,), daemon=True)
        thread.start()
        return True

    def is_active(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._active

    def _run(self, job_id: str) -> None:
        try:
            self.jobs.run(job_id)
        except Exception:
            # The job engine records provider failures durably for the UI.
            pass
        finally:
            with self._lock:
                self._active.discard(job_id)


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
        deletion: DeleteArtifacts,
        voice_studio: VoiceStudio,
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
        self.deletion = deletion
        self.voice_studio = voice_studio
        self.default_voices = default_voice_pairs()
        self.supervisor = JobSupervisor(jobs)

    def __call__(self, environ: dict[str, object], start_response: StartResponse) -> Iterable[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = unquote(str(environ.get("PATH_INFO", "/")))
        csrf, set_cookie = self._csrf_token(environ)
        try:
            if method == "GET" and path in {
                "/static/app.css",
                "/static/choices.css",
                "/static/app.js",
            }:
                asset_name = path.rsplit("/", 1)[-1]
                content = files("narranova.web.static").joinpath(asset_name).read_bytes()
                content_type = (
                    "text/javascript; charset=utf-8"
                    if asset_name.endswith(".js")
                    else "text/css; charset=utf-8"
                )
                return self._respond(start_response, "200 OK", content, content_type)
            if method == "GET" and path == "/":
                return self._html(start_response, self._dashboard(environ, csrf), set_cookie)
            parts = [part for part in path.split("/") if part]
            if method == "GET" and parts == ["connections"]:
                return self._html(start_response, self._connections(environ, csrf), set_cookie)
            if method == "GET" and parts == ["voices"]:
                return self._html(start_response, self._voices(environ, csrf), set_cookie)
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
                and len(parts) == 6
                and parts[:2] == ["voices", "drafts"]
                and parts[3] == "takes"
                and parts[5] == "audio"
            ):
                return self._studio_audio(start_response, parts[2], parts[4])
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
            if method == "GET" and len(parts) == 3 and parts[0] == "books" and parts[2] == "delete":
                book = self.books.get_book(parts[1])
                return self._html(
                    start_response,
                    self._confirm(
                        environ,
                        csrf,
                        title="Delete book",
                        subject=book.title,
                        warning="This permanently deletes the source EPUB, narration plans, every job, and all generated audio for this book. Reusable voice profiles remain available.",
                        action=f"/books/{self._e(parts[1])}/delete",
                        cancel=f"/books/{self._e(parts[1])}",
                        button="Delete book permanently",
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
            if method == "GET" and len(parts) == 5 and parts[0] == "jobs" and parts[2] == "chunks" and parts[4] == "delete":
                chunk = self.generation.get_chunk(parts[1], parts[3])
                return self._html(
                    start_response,
                    self._confirm(
                        environ,
                        csrf,
                        title="Delete generated chunk",
                        subject=chunk.id,
                        warning="This deletes the generated WAV and returns the chunk to pending. You can regenerate it by running the job again.",
                        action=f"/jobs/{self._e(parts[1])}/chunks/{self._e(parts[3])}/delete",
                        cancel=f"/jobs/{self._e(parts[1])}",
                        button="Delete generated audio",
                    ),
                    set_cookie,
                )
            if method == "GET" and len(parts) == 5 and parts[0] == "jobs" and parts[2] == "chunks" and parts[4] == "audio":
                return self._audio(start_response, parts[1], parts[3])
            if method == "POST":
                fields, uploads = self._parse_form(environ)
                try:
                    if not secrets.compare_digest(fields.get("csrf", ""), csrf):
                        raise PermissionError("The form expired. Refresh the page and try again.")
                    return self._post(start_response, parts, fields, uploads)
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
        if len(parts) == 3 and parts[0] == "connections" and parts[2] == "delete":
            self.generation.delete_provider(parts[1])
            return self._redirect(start_response, "/connections?notice=Connection+deleted")
        if parts == ["voices", "drafts"]:
            draft_id = self.voice_studio.start()
            return self._redirect(start_response, f"/voices/drafts/{draft_id}")
        if len(parts) == 4 and parts[:2] == ["voices", "drafts"] and parts[3] == "auditions":
            upload = uploads.get("reference")
            self.voice_studio.generate_take(
                parts[2],
                provider_id=fields.get("provider_id", ""),
                reference_choice=fields.get("reference_choice", ""),
                instruction=fields.get("instruction", ""),
                sample_text=fields.get("sample_text", ""),
                language=fields.get("language", "English"),
                profile_name=fields.get("name", ""),
                uploaded_reference=upload.path if upload else None,
            )
            return self._redirect(
                start_response,
                f"/voices/drafts/{parts[2]}?notice=New+audition+ready",
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
            notice = (
                f"Narration+choices+saved+as+revision+{result.revision}"
                if result.changed
                else "Narration+choices+unchanged"
            )
            return self._redirect(start_response, f"/books/{parts[1]}?notice={notice}")
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
        jobs = self.generation.list_jobs()[:8]
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
        <section class="page-heading workspace-heading"><div><p class="eyebrow">Production workspace</p><h1>Your audiobook desk</h1><p>Bring in a book, shape its narration, then generate at your own pace.</p></div>
          <form class="import-card" method="post" action="/actions/import" enctype="multipart/form-data">
            {self._csrf(csrf)}<label for="epub">Add a DRM-free EPUB</label><div><input id="epub" name="epub" type="file" accept=".epub,application/epub+zip" required><button class="primary">Import book</button></div>
          </form></section>
        <section class="stat-strip"><a href="/"><strong>{len(books)}</strong><span>Books</span></a><a href="/connections"><strong>{len(providers)}</strong><span>TTS connections</span></a><a href="/voices"><strong>{len(profiles)}</strong><span>Voice profiles</span></a><a href="#recent-jobs"><strong>{len(jobs)}</strong><span>Recent jobs</span></a></section>
        <div class="dashboard-grid"><section class="panel library"><header><div><p class="eyebrow">Library</p><h2>Books in progress</h2></div><span class="count">{len(books):02d}</span></header>{book_cards}</section>
        <aside class="stack"><section class="panel" id="recent-jobs"><header><div><p class="eyebrow">Activity</p><h2>Recent jobs</h2></div></header>{job_rows}</section></aside></div>"""
        return self._layout("Workspace", body, environ)

    def _connections(self, environ: dict[str, object], csrf: str) -> str:
        providers = self.generation.list_providers()
        cards = "".join(
            f"""<article class="connection-card"><div class="connection-icon">{self._e(provider_type(provider.kind).label[:1])}</div><div><span class="status status-completed">Ready</span><h3>{self._e(provider.name)}</h3><p>{self._e(provider.endpoint_url)}</p><small>{self._e(provider_type(provider.kind).label)} · {self._e(provider_type(provider.kind).description)}</small><div class="card-actions"><a href="/connections/{self._e(provider.id)}/edit">Edit</a><a class="danger-link" href="/connections/{self._e(provider.id)}/delete">Delete</a></div></div></article>"""
            for provider in providers
        ) or """<div class="empty-state"><span>M</span><h3>No TTS connection yet</h3><p>Add the /tts URL from your separately running OpenMOSS server.</p></div>"""
        type_options = "".join(
            f'<option value="{self._e(item.id)}">{self._e(item.label)}</option>'
            for item in PROVIDER_TYPES
        )
        body = f"""<section class="page-heading"><div><p class="eyebrow">TTS connections</p><h1>Connect your voice engine</h1><p>Narranova sends text and approved voice references to services you operate separately.</p></div></section>
        <div class="settings-grid"><main><div class="section-heading"><div><h2>Saved connections</h2><p>{len(providers)} configured</p></div></div><div class="connection-list">{cards}</div></main>
        <aside class="panel form-card"><header><div><p class="eyebrow">New connection</p><h2>Add a TTS engine</h2></div></header><form method="post" action="/connections">{self._csrf(csrf)}<label>Connection type<small>Each engine exposes its own Voice Lab controls.</small><select name="kind" required>{type_options}</select></label><label>Connection name<small>Use a name that identifies the machine or model.</small><input name="name" placeholder="Studio MOSS" required></label><label>Service endpoint<small>For OpenMOSS, enter its external /tts URL.</small><input name="endpoint" type="url" value="http://127.0.0.1:8000/tts" required></label><button class="primary">Save connection</button></form></aside></div>"""
        return self._layout("Connections", body, environ)

    def _edit_connection(self, provider_id: str, environ: dict[str, object], csrf: str) -> str:
        provider = self.generation.get_provider(provider_id)
        type_options = "".join(
            f'<option value="{self._e(item.id)}"{" selected" if item.id == provider["kind"] else ""}>{self._e(item.label)}</option>'
            for item in PROVIDER_TYPES
        )
        body = f"""<a class="back" href="/connections">← TTS connections</a><section class="page-heading"><div><p class="eyebrow">Edit connection</p><h1>{self._e(provider['name'])}</h1><p>Update the engine type, display name, or service endpoint.</p></div></section><section class="panel edit-panel"><form method="post" action="/connections/{self._e(provider_id)}">{self._csrf(csrf)}<label>Connection type<select name="kind" required>{type_options}</select></label><label>Connection name<input name="name" value="{self._e(provider['name'])}" required></label><label>Service endpoint<input name="endpoint" type="url" value="{self._e(provider['endpoint_url'])}" required></label><div class="form-actions"><a class="button" href="/connections">Cancel</a><button class="primary">Update connection</button></div></form></section>"""
        return self._layout("Edit connection", body, environ)

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
        body = f"""<section class="page-heading"><div><p class="eyebrow">Narrator pairs</p><h1>Choose a voice or build your own</h1><p>Built-in pairs are ready for any OpenMOSS connection. Custom profiles remain yours to edit and reuse.</p></div></section><section class="builtin-library"><div class="section-heading"><div><p class="eyebrow">Included with Narranova</p><h2>Built-in narrator pairs</h2><p>Preview the reference audio and its matching instruction.</p></div><span class="count">{len(self.default_voices):02d}</span></div><div class="builtin-voice-grid">{builtin_cards}</div></section><div class="voice-library-grid"><main><div class="section-heading"><div><h2>Your profiles</h2><p>{len(profiles)} custom profiles ready for narration</p></div></div><div class="voice-list">{profile_cards}</div></main><aside class="panel start-studio"><div class="studio-glyph">♪</div><p class="eyebrow">Voice Lab</p><h2>Build a custom pair</h2><p>Create reference audio, pair it with precise narration instructions, and save it for any book.</p>{start}</aside></div>"""
        return self._layout("Voices", body, environ)

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
        body = f"""<a class="back" href="/voices">← Voice profiles</a><section class="page-heading"><div><p class="eyebrow">Edit voice profile</p><h1>{self._e(profile.get('name', 'Untitled voice'))}</h1><p>Rename this profile or update its reusable voice settings.</p></div></section><section class="panel edit-panel"><form method="post" action="/voices/{self._e(profile_id)}" enctype="multipart/form-data">{self._csrf(csrf)}<label>Profile name<input name="name" value="{self._e(profile.get('name', ''))}" required></label><label>Connection<select name="provider_id" required>{provider_options}</select></label>{instruction_field}<label>Language<input name="language" value="{self._e(profile.get('language', 'English'))}"></label>{reference_field}<div class="form-actions"><a class="button" href="/voices">Cancel</a><button class="primary">Update voice profile</button></div></form></section>"""
        return self._layout("Edit voice", body, environ)

    def _voice_studio(self, draft_id: str, environ: dict[str, object], csrf: str) -> str:
        draft = self.voice_studio.get(draft_id)
        providers = [item for item in self.generation.list_providers() if item.enabled]
        selected_provider = str(draft.get("provider_id") or "")
        provider_options = "".join(
            f'<option value="{self._e(item.id)}" data-instructions="{str(provider_type(item.kind).supports_instructions).lower()}" data-reference="{str(provider_type(item.kind).supports_reference_audio).lower()}"{" selected" if item.id == selected_provider else ""}>{self._e(item.name)} · {self._e(provider_type(item.kind).label)}</option>'
            for item in providers
        )
        preset_buttons = "".join(
            f'<button type="button" class="prompt-chip" data-instruction="{self._e(instruction)}"><strong>{self._e(name)}</strong><span>{self._e(instruction)}</span></button>'
            for name, instruction in INSTRUCTION_PRESETS
        )
        reference_options = ['<option value="none">No source reference — instruction only</option>']
        if draft.get("uploaded_reference_path"):
            reference_options.append('<option value="uploaded">Uploaded reference WAV</option>')
        reference_options.extend(
            f'<option value="take:{self._e(take["id"])}">Generated candidate · {len(draft["takes"]) - index:02d}</option>'
            for index, take in enumerate(reversed(draft["takes"]))
        )
        takes = "".join(
            f"""<article class="take-card"><div class="take-index">{len(draft['takes']) - index:02d}</div><div class="take-main"><div><strong>Reference candidate</strong><span>{float(take['duration_seconds']):.1f} seconds</span></div><audio controls preload="none" src="/voices/drafts/{self._e(draft_id)}/takes/{self._e(take['id'])}/audio"></audio><details><summary>Instruction used</summary><p>{self._e(take['instruction'])}</p></details></div></article>"""
            for index, take in enumerate(reversed(draft["takes"]))
        ) or '<div class="empty-state compact"><span>♪</span><h3>Your reference candidates will appear here</h3><p>Generate from the instruction alone, or provide a clean source sample to guide the voice.</p></div>'
        save_references: list[str] = []
        if draft.get("uploaded_reference_path"):
            save_references.append('<label class="reference-radio"><input type="radio" name="reference_choice" value="uploaded"><span><strong>Uploaded WAV</strong><small>Keep the original reference</small></span></label>')
        save_references.extend(
            f'<label class="reference-radio"><input type="radio" name="reference_choice" value="take:{self._e(take["id"])}"{" checked" if index == 0 else ""}><span><strong>Audition take {len(draft["takes"]) - index:02d}</strong><small>{float(take["duration_seconds"]):.1f}s generated sample</small></span></label>'
            for index, take in enumerate(reversed(draft["takes"]))
        )
        can_audition = bool(providers)
        can_save = bool(save_references and providers)
        connection_warning = (
            ""
            if can_audition
            else '<div class="studio-warning"><strong>Connection needed</strong><span>Add OpenMOSS before generating your first take.</span><a href="/connections">Set up connection →</a></div>'
        )
        audition_form = f"""<form class="audition-form" method="post" action="/voices/drafts/{self._e(draft_id)}/auditions" enctype="multipart/form-data">{self._csrf(csrf)}<div class="phase-title"><span>Step 1</span><div><h2>Create the reference audio</h2><p>Shape a short, repeatable voice sample before saving a reusable profile.</p></div></div>{connection_warning}<div class="form-row"><label>Connection<select name="provider_id" data-studio-provider required><option value="">Choose a connection</option>{provider_options}</select></label><label>Language<input name="language" value="{self._e(draft.get('language', 'English'))}"></label></div><fieldset data-studio-module="instructions"><legend>Narration instruction</legend><p class="field-help">Start with an example, then make it your own. Specific pacing and emotional guidance works best.</p><div class="prompt-grid">{preset_buttons}</div><label for="instruction">Instruction for the candidate<textarea id="instruction" name="instruction" rows="5" required>{self._e(draft.get('instruction', ''))}</textarea></label></fieldset><fieldset><legend>Short test passage</legend><p class="field-help">Edit these lines to test names, dialogue, punctuation, or a particular mood.</p><label for="sample_text">Text to generate<textarea id="sample_text" name="sample_text" rows="5" maxlength="2000" required>{self._e(draft.get('sample_text', ''))}</textarea></label></fieldset><fieldset data-studio-module="reference"><legend>Optional source sample</legend><p class="field-help">Leave this empty to create a candidate from instruction alone. You can also guide it with an uploaded WAV or an earlier candidate.</p><label>Source reference<select name="reference_choice">{''.join(reference_options)}</select></label><label class="file-drop">Upload a source WAV<input type="file" name="reference" accept="audio/wav,.wav"><span>Optional · short, clean speech works best</span></label></fieldset><input type="hidden" name="name" value="{self._e(draft.get('name', ''))}"><button class="primary wide-button"{"" if can_audition else " disabled"}>Generate reference candidate</button></form>"""
        save_form = (
            f"""<form method="post" action="/voices/drafts/{self._e(draft_id)}/save">{self._csrf(csrf)}<input type="hidden" name="provider_id" value="{self._e(selected_provider or (providers[0].id if providers else ''))}"><label>Profile name<input name="name" value="{self._e(draft.get('name', ''))}" placeholder="Warm literary narrator" required></label><fieldset class="reference-list"><legend>Reference audio</legend><p class="field-help">Choose only from audio created or uploaded in this draft.</p>{''.join(save_references)}</fieldset><label>Instruction to pair with it<textarea name="instruction" rows="5" required>{self._e(draft.get('instruction', ''))}</textarea></label><label>Language<input name="language" value="{self._e(draft.get('language', 'English'))}"></label><button class="primary wide-button">Save instruction + reference profile</button><p class="cleanup-note">Saving keeps the selected pair. Other candidate audio and draft files are deleted automatically.</p></form>"""
            if can_save
            else '<div class="empty-state compact"><h3>Complete step 1 first</h3><p>Upload a WAV or generate a reference candidate before pairing and saving.</p></div>'
        )
        body = f"""<a class="back" href="/voices">← Voice profiles</a><section class="studio-heading"><div><p class="eyebrow">Voice Lab</p><h1>Build a stable narrator</h1><p>First create the voice reference. Then pair it with precise instructions for consistent book-length narration.</p></div><form method="post" action="/voices/drafts/{self._e(draft_id)}/discard">{self._csrf(csrf)}<button class="quiet-danger">Discard draft</button></form></section><ol class="lab-progress"><li class="active"><span>1</span><div><strong>Create reference</strong><small>Generate and review short candidates</small></div></li><li class="{'active' if can_save else ''}"><span>2</span><div><strong>Pair and save</strong><small>Lock reference audio with instructions</small></div></li></ol><div class="studio-grid"><main class="panel studio-builder">{audition_form}</main><aside class="studio-results"><section><div class="section-heading"><div><p class="eyebrow">Step 1 results</p><h2>Reference candidates</h2></div><span class="count">{len(draft['takes']):02d}</span></div><div class="take-list">{takes}</div></section><section class="panel save-profile"><header><div><p class="eyebrow">Step 2</p><h2>Pair and save the profile</h2></div></header>{save_form}</section></aside></div>"""
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
        if providers and (profiles or compatible_builtin):
            setup = f"""<form class="narration-form" method="post" action="/books/{self._e(book_id)}/jobs">{self._csrf(csrf)}<label>TTS connection<small>The service that will generate every chunk.</small><select name="provider_id" data-provider-select required>{provider_options}</select></label><label>Narrator pair<small>Choose an included pair or one of your custom profiles.</small><select name="voice_profile_id" data-profile-select required>{builtin_options}{profile_options}</select></label><details class="builtin-preview"><summary>Listen to built-in pairs</summary><div>{builtin_previews}</div></details><div class="selection-note"><span>✓</span><p>The selected pair's reference audio and matching instruction will be copied into this job.</p></div><button class="primary wide-button">Create narration job</button></form>"""
        else:
            needs = []
            if not providers:
                needs.append('<a class="setup-missing" href="/connections"><span>01</span><div><strong>Add a TTS connection</strong><small>Connect your external OpenMOSS service.</small></div><b>→</b></a>')
            if not profiles and not compatible_builtin:
                needs.append(f'<form method="post" action="/voices/drafts" class="setup-missing">{self._csrf(csrf)}<span>02</span><div><strong>Create a voice profile</strong><small>Audition a reusable instruction and reference pair.</small></div><button aria-label="Open Voice Lab">→</button></form>')
            setup = f'<div class="missing-stack">{"".join(needs)}</div>'
        jobs = self.generation.list_jobs(book_id)
        body = f"""<a class="back" href="/books/{self._e(book_id)}">← Back to book</a><section class="page-heading narration-heading"><div><p class="eyebrow">New narration</p><h1>{self._e(book.title)}</h1><p>Choose the engine and the approved voice pair for this run.</p></div></section><div class="narration-grid"><main class="panel narration-setup"><header><div><p class="eyebrow">Generation setup</p><h2>How should this book sound?</h2></div></header>{setup}</main><aside class="run-summary"><section class="panel"><header><div><p class="eyebrow">Plan summary</p><h2>Ready to generate</h2></div></header><dl><div><dt>Plan revision</dt><dd>{record['revision']}</dd></div><div><dt>Sections</dt><dd>{len(plan.chapters)}</dd></div><div><dt>Included units</dt><dd>{enabled_units}</dd></div><div><dt>Previous jobs</dt><dd>{len(jobs)}</dd></div></dl><a class="button" href="/books/{self._e(book_id)}">Review narration sections</a></section></aside></div>"""
        return self._layout("New narration", body, environ)

    def _book(self, book_id: str, environ: dict[str, object], csrf: str) -> str:
        book = self.books.get_book(book_id)
        record = self.books.get_plan_record(book_id)
        plan_path = self._artifact(record["artifact_path"])
        if self.store.sha256(plan_path) != record["plan_sha256"]:
            raise RuntimeError("Narration plan failed hash validation")
        plan = NarrationPlan.from_json(plan_path.read_text(encoding="utf-8"))
        voices = self.generation.list_voice_profiles()
        jobs = self.generation.list_jobs(book_id)
        units_by_id = {unit.id: unit for unit in plan.units}
        chapter_markup: list[str] = []
        for index, chapter in enumerate(plan.chapters):
            chapter_units = [units_by_id[unit_id] for unit_id in chapter.unit_ids]
            chapter_enabled = all(unit.enabled for unit in chapter_units)
            checked = " checked" if chapter_enabled else ""
            units_markup = "".join(
                f'<p><span>{self._e(unit.id)}</span>{self._e(unit.display_text)}</p>'
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
            f'<a class="job-row" href="/jobs/{self._e(job.id)}"><span><strong>{self._e(job.id[:10])}</strong><small>{self._e(job.created_at)}</small></span><span class="status status-{self._e(job.status)}">{self._e(job.status)}</span></a>'
            for job in jobs
        ) or '<div class="empty">No generation job yet.</div>'
        body = f"""<a class="back" href="/">← Workspace</a><section class="book-head"><div><p class="eyebrow">Narration plan · revision {record['revision']}</p><h1>{self._e(book.title)}</h1><p>{self._e(book.author or 'Unknown author')} · {len(plan.chapters)} sections · {enabled_units} of {len(plan.units)} units included</p></div><div class="head-actions"><a class="button primary" href="/books/{self._e(book_id)}/narrations/new">Create narration</a><a class="danger-link" href="/books/{self._e(book_id)}/delete">Delete book</a></div></section>
        <div class="book-grid"><main><section class="panel"><form class="plan-form" method="post" action="/books/{self._e(book_id)}/plan">{self._csrf(csrf)}<header><div><p class="eyebrow">Source map</p><h2>Choose what to narrate</h2><p class="section-help">Turn off front matter, tables of contents, copyright pages, or any other section you do not want spoken.</p></div><button class="primary">Save choices</button></header>{chapters}<div class="plan-save"><span>New jobs use this revision. Existing jobs keep their original text.</span><button class="primary">Save narration choices</button></div></form></section></main>
        <aside class="stack"><section class="panel book-workflow"><header><div><p class="eyebrow">Production</p><h2>Ready when you are</h2></div></header><div class="workflow-counts"><div><strong>{len(voices)}</strong><span>available voices</span></div><div><strong>{len(jobs)}</strong><span>generation jobs</span></div></div><a class="button" href="/voices">Manage voice profiles</a><a class="button primary" href="/books/{self._e(book_id)}/narrations/new">Create narration job</a></section><section class="panel"><header><div><p class="eyebrow">Activity</p><h2>Generation jobs</h2></div></header>{job_rows}</section></aside></div>"""
        return self._layout(book.title, body, environ)

    def _job(self, job_id: str, environ: dict[str, object], csrf: str) -> str:
        job = self.generation.get_job(job_id)
        chunks = self.generation.list_chunks(job_id)
        completed = sum(chunk.status == "completed" for chunk in chunks)
        percent = round((completed / len(chunks)) * 100) if chunks else 0
        chunk_rows = "".join(
            f"""<article class="chunk-row"><div><span class="mono">{self._e(chunk.id)}</span><strong>{self._e(chunk.status)}</strong><small>{chunk.attempts} attempt{'s' if chunk.attempts != 1 else ''}{f' · {chunk.duration_seconds:.1f}s' if chunk.duration_seconds else ''}</small></div><div class="chunk-actions">{f'<audio controls preload="none" src="/jobs/{self._e(job_id)}/chunks/{self._e(chunk.database_id)}/audio"></audio><a class="danger-link" href="/jobs/{self._e(job_id)}/chunks/{self._e(chunk.database_id)}/delete">Delete audio</a>' if chunk.status == 'completed' else ''}</div></article>"""
            for chunk in chunks
        )
        error = f'<div class="alert">{self._e(job.get("error_message") or "")}</div>' if job.get("error_message") else ""
        controls = f"""<form method="post" action="/jobs/{self._e(job_id)}/run">{self._csrf(csrf)}<button class="primary">{'Resume' if job['status'] in {'failed', 'paused'} else 'Start generation'}</button></form><form method="post" action="/jobs/{self._e(job_id)}/pause">{self._csrf(csrf)}<button>Pause after chunk</button></form>""" if job["status"] != "completed" else '<span class="complete-mark">✓ Generation complete</span>'
        controls += f'<a class="danger-link job-delete" href="/jobs/{self._e(job_id)}/delete">Delete job</a>'
        body = f"""<a class="back" href="/books/{self._e(job['book_id'])}">← Back to book</a><section class="job-head"><div><p class="eyebrow">Generation job</p><h1>{self._e(job_id[:12])}</h1><p>{completed} of {len(chunks)} chunks complete</p></div><span class="status status-{self._e(job['status'])}">{self._e(job['status'])}</span></section>{error}<section class="panel progress-panel"><div class="progress-copy"><strong>{percent}%</strong><span>verified audio</span></div><div class="progress"><i style="width:{percent}%"></i></div><div class="job-actions">{controls}</div></section><section class="panel chunks"><header><div><p class="eyebrow">Artifacts</p><h2>Audio chunks</h2></div></header>{chunk_rows}</section>"""
        return self._layout("Generation", body, environ, refresh=job["status"] in {"generating", "pause_requested"})

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

    def _audio(self, start_response: StartResponse, job_id: str, chunk_id: str) -> Iterable[bytes]:
        chunk = self.generation.get_chunk(job_id, chunk_id)
        if chunk.status != "completed" or not chunk.audio_artifact_path:
            raise KeyError("Audio is not available")
        path = self._artifact(chunk.audio_artifact_path)
        validate_wave(path)
        if self.store.sha256(path) != chunk.audio_sha256:
            raise RuntimeError("Audio failed hash validation")
        return self._respond(start_response, "200 OK", path.read_bytes(), "audio/wav")

    def _studio_audio(
        self,
        start_response: StartResponse,
        draft_id: str,
        take_id: str,
    ) -> Iterable[bytes]:
        path, _ = self.voice_studio.take_audio(draft_id, take_id)
        return self._respond(start_response, "200 OK", path.read_bytes(), "audio/wav")

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
        return self._respond(start_response, "200 OK", path.read_bytes(), "audio/wav")

    def _default_voice_audio(
        self,
        start_response: StartResponse,
        voice_id: str,
    ) -> Iterable[bytes]:
        pair = default_voice_pair(f"builtin:{voice_id}")
        validate_wave(pair.audio_path)
        if self.store.sha256(pair.audio_path) != pair.audio_sha256:
            raise RuntimeError("Built-in narrator audio failed hash validation")
        return self._respond(
            start_response, "200 OK", pair.audio_path.read_bytes(), "audio/wav"
        )

    def _layout(self, title: str, body: str, environ: dict[str, object], refresh: bool = False) -> str:
        query = parse_qs(str(environ.get("QUERY_STRING", "")))
        notice = query.get("notice", [""])[0]
        path = str(environ.get("PATH_INFO", "/"))
        refresh_tag = '<meta http-equiv="refresh" content="4">' if refresh else ""
        notice_html = f'<div class="notice">{self._e(notice)}</div>' if notice else ""
        nav = (
            ("/", "Library", path == "/" or path.startswith("/books/") or path.startswith("/jobs/")),
            ("/voices", "Voices", path.startswith("/voices")),
            ("/connections", "Connections", path.startswith("/connections")),
        )
        nav_html = "".join(
            f'<a href="{href}" class="{"active" if active else ""}">{label}</a>'
            for href, label, active in nav
        )
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{self._e(title)} · Narranova</title>{refresh_tag}<link rel="stylesheet" href="/static/app.css"><link rel="stylesheet" href="/static/choices.css"><script defer src="/static/app.js"></script></head><body><header class="topbar"><div class="topbar-inner"><a class="brand" href="/"><span>N</span><strong>Narranova</strong></a><nav aria-label="Primary navigation">{nav_html}</nav><div class="top-note"><i></i>Local studio</div></div></header><div class="shell">{notice_html}{body}</div><footer>Narranova · Local-first audiobook production · MOSS stays external</footer></body></html>"""

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


def create_web_app(data_dir: str | Path | None = None) -> NarranovaWebApp:
    settings = Settings.load(data_dir)
    layout = ArtifactLayout.at(settings.data_dir)
    layout.initialize()
    database = Database(settings.database_path)
    database.initialize()
    books = BookRepository(database)
    generation = GenerationRepository(database)
    store = ArtifactStore(settings.data_dir)
    profiles = VoiceProfiles(generation, layout, store)
    jobs = GenerationJobs(books, generation, layout, store)
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
        DeleteArtifacts(books, generation, layout),
        VoiceStudio(generation, profiles, layout, store),
    )
