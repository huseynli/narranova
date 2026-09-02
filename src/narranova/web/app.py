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

from narranova.application.deletion import DeleteArtifacts
from narranova.application.generation import GenerationJobs, VoiceProfiles
from narranova.application.ingest import ImportBook
from narranova.application.revise_plan import ReviseNarrationPlan
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
        self.supervisor = JobSupervisor(jobs)

    def __call__(self, environ: dict[str, object], start_response: StartResponse) -> Iterable[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = unquote(str(environ.get("PATH_INFO", "/")))
        csrf, set_cookie = self._csrf_token(environ)
        try:
            if method == "GET" and path in {"/static/app.css", "/static/choices.css"}:
                asset_name = path.rsplit("/", 1)[-1]
                content = files("narranova.web.static").joinpath(asset_name).read_bytes()
                return self._respond(start_response, "200 OK", content, "text/css; charset=utf-8")
            if method == "GET" and path == "/":
                return self._html(start_response, self._dashboard(environ, csrf), set_cookie)
            parts = [part for part in path.split("/") if part]
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
                        warning="This permanently deletes the source EPUB, narration plans, voice profiles, every job, and all generated audio for this book.",
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
        if parts == ["actions", "providers"]:
            provider_id = self.profiles.add_openmoss_provider(
                fields.get("name", ""), fields.get("endpoint", "")
            )
            return self._redirect(start_response, f"/?notice=Provider+registered+{provider_id}")
        if len(parts) == 3 and parts[0] == "books" and parts[2] == "voices":
            upload = uploads.get("reference")
            if upload is None:
                raise ValueError("Choose an approved reference WAV")
            profile_id = self.profiles.create_openmoss_profile(
                book_id=parts[1],
                provider_id=fields.get("provider_id", ""),
                reference_audio=upload.path,
                instruction=fields.get("instruction", ""),
                language=fields.get("language", "English"),
            )
            return self._redirect(
                start_response, f"/books/{parts[1]}?notice=Voice+profile+created+{profile_id}"
            )
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
            job_id = self.jobs.create(parts[1], fields.get("voice_profile_id", ""))
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
            f'<a class="job-row" href="/jobs/{self._e(job.id)}"><span><strong>{self._e(job.book_title)}</strong><small>{self._e(job.id[:10])}</small></span><span class="status status-{self._e(job.status)}">{self._e(job.status)}</span></a>'
            for job in jobs
        ) or '<div class="empty">Generation jobs will appear here.</div>'
        provider_rows = "".join(
            f'<li><span class="signal"></span><span><strong>{self._e(provider.name)}</strong><small>{self._e(provider.endpoint_url)}</small></span></li>'
            for provider in providers
        ) or '<li class="empty">No provider configured.</li>'
        body = f"""
        <section class="welcome"><div><p class="eyebrow">Production workspace</p><h1>Turn a book into a voice.</h1><p>Import, review, and generate without losing your place.</p></div>
          <form class="upload-card" method="post" action="/actions/import" enctype="multipart/form-data">
            {self._csrf(csrf)}<label for="epub">Import a DRM-free EPUB</label><input id="epub" name="epub" type="file" accept=".epub,application/epub+zip" required><button class="primary">Import book</button>
          </form></section>
        <div class="dashboard-grid"><section class="panel library"><header><div><p class="eyebrow">Library</p><h2>Your books</h2></div><span class="count">{len(books):02d}</span></header>{book_cards}</section>
        <aside class="stack"><section class="panel"><header><div><p class="eyebrow">Activity</p><h2>Recent jobs</h2></div></header>{job_rows}</section>
        <section class="panel provider-panel"><header><div><p class="eyebrow">Connections</p><h2>OpenMOSS</h2></div></header><ul>{provider_rows}</ul>
          <details><summary>Add endpoint</summary><form method="post" action="/actions/providers">{self._csrf(csrf)}<label>Name<input name="name" placeholder="Studio MOSS" required></label><label>/tts endpoint<input name="endpoint" type="url" value="http://127.0.0.1:8000/tts" required></label><button>Add provider</button></form></details></section></aside></div>"""
        return self._layout("Workspace", body, environ)

    def _book(self, book_id: str, environ: dict[str, object], csrf: str) -> str:
        book = self.books.get_book(book_id)
        record = self.books.get_plan_record(book_id)
        plan_path = self._artifact(record["artifact_path"])
        if self.store.sha256(plan_path) != record["plan_sha256"]:
            raise RuntimeError("Narration plan failed hash validation")
        plan = NarrationPlan.from_json(plan_path.read_text(encoding="utf-8"))
        providers = self.generation.list_providers()
        voices = self.generation.list_voice_profiles(book_id)
        jobs = self.generation.list_jobs(book_id)
        provider_options = "".join(
            f'<option value="{self._e(item.id)}">{self._e(item.name)}</option>' for item in providers if item.enabled
        )
        voice_options = "".join(
            f'<option value="{self._e(item.id)}">{self._e(item.provider_name)} · {self._e(str(item.profile.get("instruction", ""))[:54])}</option>'
            for item in voices
        )
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
        voice_form = (
            f"""<form method="post" action="/books/{self._e(book_id)}/voices" enctype="multipart/form-data">{self._csrf(csrf)}
            <label>Provider<select name="provider_id" required>{provider_options}</select></label><label>Approved reference WAV<input type="file" name="reference" accept="audio/wav,.wav" required></label><label>Narrator instruction<textarea name="instruction" rows="3" required>A natural audiobook narrator with restrained emotion and thoughtful pacing.</textarea></label><label>Language<input name="language" value="English"></label><button>Create voice profile</button></form>"""
            if provider_options else '<p class="empty">Add an OpenMOSS endpoint from the workspace first.</p>'
        )
        job_form = (
            f'<form class="inline-form" method="post" action="/books/{self._e(book_id)}/jobs">{self._csrf(csrf)}<select name="voice_profile_id" required>{voice_options}</select><button class="primary">Create generation job</button></form>'
            if voice_options else '<p class="empty">Create an approved voice profile before generating.</p>'
        )
        body = f"""<a class="back" href="/">← Workspace</a><section class="book-head"><div><p class="eyebrow">Narration plan · revision {record['revision']}</p><h1>{self._e(book.title)}</h1><p>{self._e(book.author or 'Unknown author')} · {len(plan.chapters)} sections · {enabled_units} of {len(plan.units)} units included</p></div><div class="head-actions"><span class="status status-{self._e(book.status)}">{self._e(book.status)}</span><a class="danger-link" href="/books/{self._e(book_id)}/delete">Delete book</a></div></section>
        <div class="book-grid"><main><section class="panel"><form class="plan-form" method="post" action="/books/{self._e(book_id)}/plan">{self._csrf(csrf)}<header><div><p class="eyebrow">Source map</p><h2>Choose what to narrate</h2><p class="section-help">Turn off front matter, tables of contents, copyright pages, or any other section you do not want spoken.</p></div><button class="primary">Save narration choices</button></header>{chapters}<div class="plan-save"><span>New generation jobs use the latest saved revision. Existing jobs keep their original text.</span><button class="primary">Save narration choices</button></div></form></section></main>
        <aside class="stack"><section class="panel"><header><div><p class="eyebrow">Voice</p><h2>Approved profile</h2></div></header>{voice_form}</section><section class="panel"><header><div><p class="eyebrow">Generate</p><h2>Audio jobs</h2></div></header>{job_form}{job_rows}</section></aside></div>"""
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

    def _layout(self, title: str, body: str, environ: dict[str, object], refresh: bool = False) -> str:
        query = parse_qs(str(environ.get("QUERY_STRING", "")))
        notice = query.get("notice", [""])[0]
        refresh_tag = '<meta http-equiv="refresh" content="4">' if refresh else ""
        notice_html = f'<div class="notice">{self._e(notice)}</div>' if notice else ""
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{self._e(title)} · Narranova</title>{refresh_tag}<link rel="stylesheet" href="/static/app.css"><link rel="stylesheet" href="/static/choices.css"></head><body><header class="topbar"><a class="brand" href="/"><span>N</span><strong>Narranova</strong></a><div class="top-note">EPUB → spoken edition</div></header><div class="shell">{notice_html}{body}</div><footer>Local-first audiobook production · MOSS stays external</footer></body></html>"""

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
                ("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; media-src 'self'"),
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
    jobs = GenerationJobs(books, generation, layout, store)
    return NarranovaWebApp(
        settings,
        books,
        generation,
        layout,
        store,
        ImportBook(EpubParser(), books, layout, store),
        ReviseNarrationPlan(books, layout, store),
        VoiceProfiles(generation, layout, store),
        jobs,
        DeleteArtifacts(books, generation, layout),
    )
