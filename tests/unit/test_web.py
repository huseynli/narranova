from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from narranova.cli.main import ThreadingWSGIServer
from narranova.web import create_web_app
from tests.unit.test_epub_ingest import make_epub
from tests.unit.test_generation_jobs import FakeAudioMasters, make_wave


def request(
    app,
    path: str = "/",
    *,
    method: str = "GET",
    body: bytes = b"",
    cookie: str = "",
    content_type: str = "application/x-www-form-urlencoded",
    range_header: str = "",
):
    captured: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = headers

    environ: dict[str, object] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "CONTENT_LENGTH": str(len(body)),
        "CONTENT_TYPE": content_type,
        "wsgi.input": io.BytesIO(body),
        "HTTP_COOKIE": cookie,
        "HTTP_RANGE": range_header,
    }
    content = b"".join(app(environ, start_response))
    return str(captured["status"]), list(captured["headers"]), content


class WebAppTests(unittest.TestCase):
    def test_dashboard_is_a_real_empty_application_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = create_web_app(Path(temporary) / "data")

            status, headers, body = request(app)

            self.assertEqual(status, "200 OK")
            self.assertIn(b"Your audiobook desk", body)
            self.assertIn(b"workspace-heading full-page-heading", body)
            self.assertIn(b"No books yet", body)
            self.assertNotIn(b"Prepare your studio", body)
            self.assertIn(b'<aside class="stack"><form class="panel import-card"', body)
            self.assertIn(b"New book", body)
            self.assertIn(b"data-theme-toggle", body)
            self.assertIn(b'<script src="/static/theme.js"></script>', body)
            self.assertNotIn(b"Add a DRM-free book to your production library", body)
            self.assertNotIn(b">Book file</label>", body)
            self.assertIn(b'aria-label="Choose an EPUB book"', body)
            self.assertLess(body.index(b"Import an EPUB"), body.index(b"Activity"))
            self.assertTrue(any(name == "Set-Cookie" for name, _ in headers))

    def test_jobs_workspace_has_requested_navigation_and_status_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = create_web_app(Path(temporary) / "data")

            status, _, body = request(app, "/jobs")

            self.assertEqual(status, "200 OK")
            self.assertIn(b"Every narration run in one place", body)
            self.assertIn(b"jobs-heading full-page-heading", body)
            self.assertNotIn(b"Back to library", body)
            self.assertEqual(body.count(b"Job state"), 4)
            for label in (b"Active", b"Recent", b"Finished", b"Stopped"):
                self.assertIn(label, body)
            self.assertNotIn(b"Local studio", body)
            self.assertNotIn(b"MOSS stays external", body)
            nav = [
                body.index(b">Library</a>"),
                body.index(b">Connections</a>"),
                body.index(b">Voices</a>"),
                body.index(b">Jobs</a>"),
            ]
            self.assertEqual(nav, sorted(nav))

    def test_static_stylesheet_is_packaged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = create_web_app(Path(temporary) / "data")

            status, headers, body = request(app, "/static/app.css")

            self.assertEqual(status, "200 OK")
            self.assertIn(b"--accent", body)
            self.assertIn(b':root[data-theme="dark"]', body)
            self.assertIn(("Content-Type", "text/css; charset=utf-8"), headers)

            status, headers, script = request(app, "/static/app.js")
            self.assertEqual(status, "200 OK")
            self.assertIn(b'localStorage.setItem("narranova-theme"', script)
            self.assertIn(b"narranova_theme=", script)
            self.assertIn(b"data-job-monitor", script)
            self.assertIn(b"window.setTimeout(pollJob", script)
            self.assertIn(b'Regenerate', script)
            self.assertNotIn(b"location.reload", script)
            self.assertIn(("Content-Type", "text/javascript; charset=utf-8"), headers)
            self.assertTrue(ThreadingWSGIServer.daemon_threads)

            status, headers, bootstrap = request(app, "/static/theme.js")
            self.assertEqual(status, "200 OK")
            self.assertIn(b'localStorage.getItem("narranova-theme"', bootstrap)
            self.assertIn(b"document.documentElement.dataset.theme", bootstrap)
            self.assertIn(("Content-Type", "text/javascript; charset=utf-8"), headers)

    def test_csrf_protected_provider_form_persists_to_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = create_web_app(Path(temporary) / "data")
            _, headers, _ = request(app)
            cookie = next(value for name, value in headers if name == "Set-Cookie")
            token = cookie.split(";", 1)[0].split("=", 1)[1]
            body = urlencode(
                {
                    "csrf": token,
                    "name": "Local MOSS",
                    "endpoint": "http://127.0.0.1:8000/tts",
                }
            ).encode()

            status, response_headers, _ = request(
                app,
                "/actions/providers",
                method="POST",
                body=body,
                cookie=cookie,
            )

            self.assertEqual(status, "303 See Other")
            self.assertTrue(any(name == "Location" for name, _ in response_headers))
            self.assertEqual(app.generation.list_providers()[0].name, "Local MOSS")

    def test_post_without_csrf_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = create_web_app(Path(temporary) / "data")

            status, _, body = request(
                app,
                "/actions/providers",
                method="POST",
                body=b"name=x&endpoint=http%3A%2F%2Fmoss%2Ftts",
            )

            self.assertEqual(status, "403 Forbidden")
            self.assertIn(b"form expired", body)

    def test_multipart_epub_upload_redirects_to_persisted_book(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            make_epub(source)
            app = create_web_app(root / "data", masters=FakeAudioMasters())
            _, headers, _ = request(app)
            cookie = next(value for name, value in headers if name == "Set-Cookie")
            token = cookie.split(";", 1)[0].split("=", 1)[1]
            boundary = "narranova-test-boundary"
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n"
                f"{token}\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"epub\"; "
                "filename=\"book.epub\"\r\nContent-Type: application/epub+zip\r\n\r\n"
            ).encode() + source.read_bytes() + f"\r\n--{boundary}--\r\n".encode()

            status, response_headers, _ = request(
                app,
                "/actions/import",
                method="POST",
                body=body,
                cookie=cookie,
                content_type=f"multipart/form-data; boundary={boundary}",
            )

            location = next(value for name, value in response_headers if name == "Location")
            self.assertEqual(status, "303 See Other")
            self.assertTrue(location.startswith("/books/"))
            self.assertEqual(app.books.list_books()[0].title, "The Example Book")

    def test_book_sections_can_be_excluded_through_a_new_plan_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            make_epub(source)
            app = create_web_app(root / "data")
            imported = app.import_book.execute(source)
            _, headers, page = request(app, f"/books/{imported.book_id}")
            cookie = next(value for name, value in headers if name == "Set-Cookie")
            token = cookie.split(";", 1)[0].split("=", 1)[1]
            self.assertIn(b"Choose what to narrate", page)
            self.assertIn(b'name="chapter_1" checked', page)
            self.assertEqual(page.count(b">Save choices</button>"), 1)
            self.assertEqual(page.count(b"/narrations/new"), 1)
            self.assertNotIn(b"Manage voice profiles", page)
            self.assertNotIn(b"available voices", page)
            self.assertNotIn(b"generation jobs</span>", page)
            self.assertIn(b"Turn this plan into audio", page)
            self.assertIn(b"Set up narration", page)
            body = urlencode({"csrf": token, "chapter_2": "on"}).encode()

            status, response_headers, _ = request(
                app,
                f"/books/{imported.book_id}/plan",
                method="POST",
                body=body,
                cookie=cookie,
            )

            self.assertEqual(status, "303 See Other")
            self.assertIn("revision+2", next(v for n, v in response_headers if n == "Location"))
            self.assertEqual(app.books.get_plan_record(imported.book_id)["revision"], 2)

    def test_book_deletion_requires_confirmation_and_removes_the_book(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            make_epub(source)
            app = create_web_app(root / "data")
            imported = app.import_book.execute(source)

            status, headers, page = request(app, f"/books/{imported.book_id}/delete")
            cookie = next(value for name, value in headers if name == "Set-Cookie")
            token = cookie.split(";", 1)[0].split("=", 1)[1]
            self.assertEqual(status, "200 OK")
            self.assertIn(b"Delete book permanently", page)
            self.assertIn(b"This permanently deletes", page)

            status, response_headers, _ = request(
                app,
                f"/books/{imported.book_id}/delete",
                method="POST",
                body=urlencode({"csrf": token}).encode(),
                cookie=cookie,
            )

            self.assertEqual(status, "303 See Other")
            self.assertEqual(
                next(value for name, value in response_headers if name == "Location"),
                "/?notice=Book+deleted",
            )
            self.assertEqual(app.books.list_books(), [])

    def test_connections_and_voice_studio_have_dedicated_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_web_app(root / "data")
            reference = root / "reference.wav"
            make_wave(reference)
            provider_id = app.profiles.add_openmoss_provider(
                "Lab MOSS", "http://moss.test:8000/tts"
            )
            app.profiles.create_openmoss_profile(
                provider_id=provider_id,
                reference_audio=reference,
                instruction="An existing narrator.",
                name="Existing saved voice",
            )

            status, _, connections = request(app, "/connections")
            self.assertEqual(status, "200 OK")
            self.assertIn(b"Connect your voice engine", connections)
            self.assertIn(b'class="page-heading full-page-heading"', connections)
            self.assertIn(b'class="connections-stack"', connections)
            self.assertIn(b"Configured engines", connections)
            self.assertIn(b'<span class="count">01</span>', connections)
            self.assertLess(
                connections.index(b"Add a TTS engine"),
                connections.index(b"Saved connections"),
            )
            self.assertIn(b'action="/connections"', connections)
            self.assertIn(b'name="kind"', connections)
            self.assertIn(f'/connections/{provider_id}/benchmark'.encode(), connections)
            self.assertIn(f'/connections/{provider_id}/test'.encode(), connections)

            status, _, benchmark = request(
                app, f"/connections/{provider_id}/benchmark"
            )
            self.assertEqual(status, "200 OK")
            self.assertIn(b"Streaming decode batch", benchmark)
            self.assertIn(b"Run Auto-tune", benchmark)
            for frames in (16, 32, 64, 128, 256, 512):
                self.assertIn(f'value="{frames}"'.encode(), benchmark)
            self.assertNotIn(b'text_temperature', benchmark)
            self.assertNotIn(b'audio_temperature', benchmark)
            self.assertNotIn(b'name="max_new_tokens"', benchmark)
            self.assertIn(b"Lower quantization reduces memory use", benchmark)
            self.assertIn(b"Keep enabled when supported", benchmark)

            _, headers, voices = request(app, "/voices")
            cookie = next(value for name, value in headers if name == "Set-Cookie")
            token = cookie.split(";", 1)[0].split("=", 1)[1]
            self.assertIn(b"Choose a voice or build your own", voices)
            self.assertIn(b'class="page-heading full-page-heading"', voices)
            self.assertIn(b"Built-in narrator pairs", voices)
            self.assertIn(b"01 female", voices)
            self.assertIn(b"Created by you", voices)
            self.assertIn(b'<span class="count">01</span>', voices)
            self.assertLess(voices.index(b"Build a custom pair"), voices.index(b"Your profiles"))
            self.assertLess(voices.index(b"Your profiles"), voices.index(b"Built-in narrator pairs"))
            self.assertIn(b'class="panel start-studio"', voices)
            self.assertIn(b'class="start-studio-actions"', voices)

            status, audio_headers, audio = request(app, "/default-voices/01/audio")
            self.assertEqual(status, "200 OK")
            self.assertIn(("Content-Type", "audio/wav"), audio_headers)
            self.assertGreater(len(audio), 1_000_000)

            status, response_headers, _ = request(
                app,
                "/voices/drafts",
                method="POST",
                body=urlencode({"csrf": token}).encode(),
                cookie=cookie,
            )
            location = next(value for name, value in response_headers if name == "Location")
            self.assertEqual(status, "303 See Other")
            self.assertTrue(location.startswith("/voices/drafts/"))

            status, _, studio = request(app, location)
            self.assertEqual(status, "200 OK")
            self.assertIn(b"Build a stable narrator", studio)
            self.assertIn(b"Create the reference audio", studio)
            self.assertIn(b"Pair and save the profile", studio)
            self.assertIn(b"Warm literary", studio)
            self.assertIn(b"The rain had stopped", studio)
            self.assertIn(b"No source reference", studio)
            self.assertIn(b"Advanced quality &amp; sampling", studio)
            self.assertIn(b'name="text_temperature"', studio)
            self.assertIn(b'name="audio_repetition_penalty"', studio)
            self.assertIn(b'name="seed"', studio)
            self.assertIn(b"Engine default", studio)
            self.assertNotIn(b"Existing saved reference", studio)
            self.assertNotIn(b"Existing saved voice", studio)
            self.assertNotIn(b'name="book_id"', studio)

    def test_connections_and_profiles_can_be_edited_and_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.wav"
            make_wave(reference)
            app = create_web_app(root / "data")
            provider_id = app.profiles.add_openmoss_provider(
                "Original MOSS", "http://moss.test:8000/tts"
            )
            profile_id = app.profiles.create_openmoss_profile(
                provider_id=provider_id,
                reference_audio=reference,
                instruction="A careful narrator.",
                name="Original voice",
            )
            _, headers, _ = request(app, "/voices")
            cookie = next(value for name, value in headers if name == "Set-Cookie")
            token = cookie.split(";", 1)[0].split("=", 1)[1]

            status, _, edit_page = request(app, f"/voices/{profile_id}/edit")
            self.assertEqual(status, "200 OK")
            self.assertIn(b"Original voice", edit_page)
            status, _, _ = request(
                app,
                f"/voices/{profile_id}",
                method="POST",
                body=urlencode(
                    {
                        "csrf": token,
                        "provider_id": provider_id,
                        "name": "Renamed voice",
                        "instruction": "A warmer narrator.",
                        "language": "English",
                        "audio_temperature": "0.7",
                        "seed": "42",
                    }
                ).encode(),
                cookie=cookie,
            )
            self.assertEqual(status, "303 See Other")
            self.assertEqual(
                app.generation.get_voice_and_provider(profile_id)["profile"]["name"],
                "Renamed voice",
            )
            self.assertEqual(
                app.generation.get_voice_and_provider(profile_id)["profile"]["sampling"],
                {"audio_temperature": 0.7, "seed": 42},
            )

            status, _, _ = request(
                app,
                f"/connections/{provider_id}",
                method="POST",
                body=urlencode(
                    {
                        "csrf": token,
                        "kind": "openmoss",
                        "name": "Renamed MOSS",
                        "endpoint": "http://moss.test:9000/tts",
                    }
                ).encode(),
                cookie=cookie,
            )
            self.assertEqual(status, "303 See Other")
            self.assertEqual(app.generation.get_provider(provider_id)["name"], "Renamed MOSS")

            status, _, _ = request(
                app,
                f"/voices/{profile_id}/delete",
                method="POST",
                body=urlencode({"csrf": token}).encode(),
                cookie=cookie,
            )
            self.assertEqual(status, "303 See Other")
            status, _, _ = request(
                app,
                f"/connections/{provider_id}/delete",
                method="POST",
                body=urlencode({"csrf": token}).encode(),
                cookie=cookie,
            )
            self.assertEqual(status, "303 See Other")
            self.assertEqual(app.generation.list_voice_profiles(), [])
            self.assertEqual(app.generation.list_providers(), [])

    def test_connection_auto_tune_route_creates_a_controlled_background_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = create_web_app(Path(temporary) / "data")
            provider_id = app.profiles.add_openmoss_provider(
                "Bench MOSS", "http://moss.test:8000/tts"
            )
            _, headers, _ = request(
                app, f"/connections/{provider_id}/benchmark"
            )
            cookie = next(value for name, value in headers if name == "Set-Cookie")
            token = cookie.split(";", 1)[0].split("=", 1)[1]
            started: list[str] = []
            app.benchmark_supervisor.start = started.append

            status, response_headers, _ = request(
                app,
                f"/connections/{provider_id}/benchmarks",
                method="POST",
                body=urlencode({"csrf": token, "mode": "auto"}).encode(),
                cookie=cookie,
            )

            self.assertEqual(status, "303 See Other")
            self.assertIn(
                "Benchmark+started",
                next(value for name, value in response_headers if name == "Location"),
            )
            run = app.benchmarks.repository.get_run(started[0])
            self.assertEqual(run.requested_frames, (16, 32, 64, 128, 256, 512))
            status, _, state_body = request(
                app,
                f"/connections/{provider_id}/benchmarks/{run.id}/status",
            )
            self.assertEqual(status, "200 OK")
            self.assertEqual(json.loads(state_body)["total"], 6)

    def test_voice_profile_marks_unfinished_job_usage_until_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            reference = root / "reference.wav"
            make_epub(source)
            make_wave(reference)
            app = create_web_app(root / "data")
            imported = app.import_book.execute(source)
            provider_id = app.profiles.add_openmoss_provider(
                "Job MOSS", "http://moss.test:8000/tts"
            )
            profile_id = app.profiles.create_openmoss_profile(
                provider_id=provider_id,
                reference_audio=reference,
                instruction="A stable narrator.",
                name="Working voice",
            )
            job_id = app.jobs.create(imported.book_id, profile_id)

            _, _, narration_page = request(
                app, f"/books/{imported.book_id}/narrations/new"
            )
            self.assertIn(b'value="builtin:01"', narration_page)
            self.assertIn(b"Listen to built-in pairs", narration_page)
            self.assertIn(b"Listen to custom pairs", narration_page)
            self.assertLess(
                narration_page.index(b">Custom " + bytes((194, 183)) + b" Working voice</option>"),
                narration_page.index(b">Built in " + bytes((194, 183)) + b" 01 female</option>"),
            )
            self.assertLess(
                narration_page.index(b"Listen to custom pairs"),
                narration_page.index(b"Listen to built-in pairs"),
            )
            self.assertIn(b"Working voice", narration_page)
            self.assertIn(f'data-custom-provider="{provider_id}"'.encode(), narration_page)
            self.assertIn(f'/voices/{profile_id}/reference/audio'.encode(), narration_page)

            _, _, in_use_page = request(app, "/voices")
            self.assertIn(b"In use \xc2\xb7 1", in_use_page)
            self.assertIn(b"Complete or delete the generation job first", in_use_page)
            self.assertNotIn(f'/voices/{profile_id}/delete'.encode(), in_use_page)

            app.generation.complete_job(job_id)
            _, _, completed_page = request(app, "/voices")
            self.assertNotIn(b"In use \xc2\xb7 1", completed_page)
            self.assertIn(f'/voices/{profile_id}/delete'.encode(), completed_page)

    def test_completed_chunk_has_a_download_action_and_attachment_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            make_epub(source)
            app = create_web_app(root / "data", masters=FakeAudioMasters())
            imported = app.import_book.execute(source)
            provider_id = app.profiles.add_openmoss_provider(
                "Job MOSS", "http://moss.test:8000/tts"
            )
            job_id = app.jobs.create(imported.book_id, "builtin:01", provider_id)
            chunk = app.generation.list_chunks(job_id)[0]
            audio_path = app.layout.job_chunk_master(imported.book_id, job_id, chunk.id)
            make_wave(audio_path)
            app.generation.complete_chunk(
                job_id,
                chunk.database_id,
                audio_path.relative_to(app.layout.root).as_posix(),
                app.store.sha256(audio_path),
                0.01,
            )
            app.generation.start_job(job_id)

            download_path = f"/jobs/{job_id}/chunks/{chunk.database_id}/download"
            status, _, job_page = request(app, f"/jobs/{job_id}")
            self.assertEqual(status, "200 OK")
            self.assertNotIn(b'http-equiv="refresh"', job_page)
            self.assertIn(b"data-job-monitor", job_page)
            self.assertIn(b"Generation in progress", job_page)
            self.assertIn(b"data-job-start hidden", job_page)
            self.assertIn(b"data-job-pause", job_page)
            self.assertIn(download_path.encode(), job_page)
            self.assertIn(b'class="chunk-action-links"', job_page)
            self.assertIn(
                f'/jobs/{job_id}/chunks/{chunk.database_id}/regenerate'.encode(),
                job_page,
            )
            self.assertIn(b"data-chunk-regenerate disabled", job_page)
            self.assertIn(b">Regenerate</button>", job_page)
            self.assertIn(b">Download</a>", job_page)
            self.assertIn(b">Delete</a>", job_page)

            status, headers, state_body = request(app, f"/jobs/{job_id}/status")
            self.assertEqual(status, "200 OK")
            self.assertIn(("Content-Type", "application/json; charset=utf-8"), headers)
            state = json.loads(state_body)
            self.assertEqual(state["status"], "generating")
            self.assertFalse(state["regenerating"])
            self.assertEqual(state["completed"], 1)
            self.assertEqual(state["percent"], 50)

            app.supervisor.is_regenerating = lambda active_job_id: True
            status, _, regenerating_page = request(app, f"/jobs/{job_id}")
            self.assertEqual(status, "200 OK")
            self.assertIn(b"Regenerating chunk", regenerating_page)
            self.assertIn(b"data-job-pause hidden", regenerating_page)
            status, _, regenerating_state_body = request(app, f"/jobs/{job_id}/status")
            self.assertEqual(status, "200 OK")
            self.assertTrue(json.loads(regenerating_state_body)["regenerating"])

            status, headers, audio = request(app, download_path)
            self.assertEqual(status, "200 OK")
            self.assertIn(("Content-Type", "audio/flac"), headers)
            self.assertIn(
                ("Content-Disposition", f'attachment; filename="{chunk.id}.flac"'),
                headers,
            )
            self.assertEqual(audio, audio_path.read_bytes())

            status, headers, partial = request(
                app,
                f"/jobs/{job_id}/chunks/{chunk.database_id}/audio",
                range_header="bytes=0-15",
            )
            self.assertEqual(status, "206 Partial Content")
            self.assertIn(("Accept-Ranges", "bytes"), headers)
            self.assertIn(
                ("Content-Range", f"bytes 0-15/{audio_path.stat().st_size}"),
                headers,
            )
            self.assertEqual(partial, audio_path.read_bytes()[:16])

    def test_completed_job_offers_assembly_and_downloadable_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            make_epub(source)
            app = create_web_app(root / "data")
            imported = app.import_book.execute(source)
            provider_id = app.profiles.add_openmoss_provider(
                "Job MOSS", "http://moss.test:8000/tts"
            )
            job_id = app.jobs.create(imported.book_id, "builtin:01", provider_id)
            for chunk in app.generation.list_chunks(job_id):
                audio_path = app.layout.job_chunk_master(
                    imported.book_id, job_id, chunk.id
                )
                make_wave(audio_path)
                app.generation.complete_chunk(
                    job_id,
                    chunk.database_id,
                    audio_path.relative_to(app.layout.root).as_posix(),
                    app.store.sha256(audio_path),
                    0.01,
                )
            app.generation.complete_job(job_id)
            audiobook = app.layout.job_audiobook(imported.book_id, job_id)
            audiobook.parent.mkdir(parents=True, exist_ok=True)
            audiobook.write_bytes(b"test m4b")
            artifact_id = app.generation.record_artifact(
                book_id=imported.book_id,
                job_id=job_id,
                kind="audiobook",
                relative_path=audiobook.relative_to(app.layout.root).as_posix(),
                sha256=app.store.sha256(audiobook),
                byte_size=audiobook.stat().st_size,
                metadata={"chapter_count": 2, "duration_seconds": 1.0},
            )

            status, page_headers, page = request(app, f"/jobs/{job_id}")

            self.assertEqual(status, "200 OK")
            self.assertIn(b"Audiobook files", page)
            self.assertIn(b"Chapterized audiobook", page)
            self.assertIn(b"Rebuild audiobook", page)
            self.assertIn(b"lossless chunk masters", page)
            self.assertIn(b"Finalize and free space", page)
            self.assertIn(f"/jobs/{job_id}/compact".encode(), page)
            self.assertIn(f"/jobs/{job_id}/assemble".encode(), page)
            self.assertIn(
                f"/jobs/{job_id}/artifacts/{artifact_id}/download".encode(), page
            )

            status, headers, content = request(
                app, f"/jobs/{job_id}/artifacts/{artifact_id}/download"
            )
            self.assertEqual(status, "200 OK")
            self.assertIn(("Content-Type", "audio/mp4"), headers)
            self.assertIn(
                (
                    "Content-Disposition",
                    'attachment; filename="The Example Book.m4b"',
                ),
                headers,
            )
            self.assertEqual(content, b"test m4b")

            status, _, state_body = request(app, f"/jobs/{job_id}/status")
            self.assertEqual(status, "200 OK")
            state = json.loads(state_body)
            self.assertTrue(state["can_assemble"])
            self.assertTrue(state["has_audiobook"])
            self.assertFalse(state["compacted"])
            self.assertGreater(state["editable_bytes"], 0)
            self.assertEqual(state["artifacts"][0]["kind"], "audiobook")

            cookie = next(
                value for name, value in page_headers if name == "Set-Cookie"
            )
            token = cookie.split(";", 1)[0].split("=", 1)[1]
            status, response_headers, _ = request(
                app,
                f"/jobs/{job_id}/compact",
                method="POST",
                body=urlencode({"csrf": token}).encode(),
                cookie=cookie,
            )
            self.assertEqual(status, "303 See Other")
            self.assertIn(
                "Finalized+and+freed",
                next(value for name, value in response_headers if name == "Location"),
            )
            self.assertTrue(audiobook.is_file())
            self.assertTrue(
                all(
                    chunk.audio_artifact_path is None
                    for chunk in app.generation.list_chunks(job_id)
                )
            )
            _, _, compact_page = request(app, f"/jobs/{job_id}")
            self.assertIn(b"Finished files only", compact_page)
            self.assertIn(b"Restore editable sources", compact_page)


if __name__ == "__main__":
    unittest.main()
