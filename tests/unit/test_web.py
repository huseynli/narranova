from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from narranova.web import create_web_app
from tests.unit.test_epub_ingest import make_epub
from tests.unit.test_generation_jobs import make_wave


def request(
    app,
    path: str = "/",
    *,
    method: str = "GET",
    body: bytes = b"",
    cookie: str = "",
    content_type: str = "application/x-www-form-urlencoded",
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
            self.assertIn(b"No books yet", body)
            self.assertNotIn(b"Prepare your studio", body)
            self.assertTrue(any(name == "Set-Cookie" for name, _ in headers))

    def test_static_stylesheet_is_packaged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = create_web_app(Path(temporary) / "data")

            status, headers, body = request(app, "/static/app.css")

            self.assertEqual(status, "200 OK")
            self.assertIn(b"--accent", body)
            self.assertIn(("Content-Type", "text/css; charset=utf-8"), headers)

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
            app = create_web_app(root / "data")
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

            status, _, connections = request(app, "/connections")
            self.assertEqual(status, "200 OK")
            self.assertIn(b"Connect your voice engine", connections)
            self.assertIn(b'action="/connections"', connections)
            self.assertIn(b'name="kind"', connections)

            _, headers, voices = request(app, "/voices")
            cookie = next(value for name, value in headers if name == "Set-Cookie")
            token = cookie.split(";", 1)[0].split("=", 1)[1]
            self.assertIn(b"Find a voice worth keeping", voices)

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
            self.assertIn(b"Shape the narrator", studio)
            self.assertIn(b"Warm literary", studio)
            self.assertIn(b"The rain had stopped", studio)
            self.assertIn(b"No reference", studio)
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
                    }
                ).encode(),
                cookie=cookie,
            )
            self.assertEqual(status, "303 See Other")
            self.assertEqual(
                app.generation.get_voice_and_provider(profile_id)["profile"]["name"],
                "Renamed voice",
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


if __name__ == "__main__":
    unittest.main()
