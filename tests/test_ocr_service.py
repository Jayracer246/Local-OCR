"""Tests for ocr_service. No running Ollama server or real model required."""

import os
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

import config
import ocr_service
from ocr_service import OCRRequest, OCRServiceError


import base64

# Real 2x2 images. The pipeline now verifies input format by content, so
# fixtures must be genuine files of the allowed types, not placeholder bytes.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGNkZGJmYGBgYmBg"
    "YGBgAAAAcAAKM8X3iAAAAABJRU5ErkJggg=="
)
TINY_WEBP = base64.b64decode(
    "UklGRiQAAABXRUJQVlA4IBgAAAAwAQCdASoCAAIAAUAmJaQAA3AA/v0gUAA="
)


def fake_version_response(version="0.12.3-test", status=200, payload=None):
    """Shape of the /api/version probe response."""
    response = mock.MagicMock()
    response.status_code = status
    response.json.return_value = (
        {"version": version} if payload is None else payload
    )
    return response


def arm_version_probe(client_cls, **kwargs):
    """Make a mocked ollama.Client answer the endpoint check like Ollama."""
    client_cls.return_value._client.get.return_value = fake_version_response(
        **kwargs
    )
    return client_cls


def chat_response(content):
    """Shape of ollama.Client.chat() responses: response.message.content."""
    return SimpleNamespace(message=SimpleNamespace(content=content))


def stream_response(*deltas):
    """An iterator of stream chunks, the shape of chat(..., stream=True).

    Each delta becomes one chunk with ``message.content``; ``None``/empty
    deltas are kept to mimic real server output (the service skips them).
    """
    return iter([SimpleNamespace(message=SimpleNamespace(content=d)) for d in deltas])


def model_entry(tag):
    """Shape of ollama.Client.list() entries: item.model."""
    return SimpleNamespace(model=tag)


def make_fake_document(page_count, needs_pass=False, page_points=(595, 842)):
    """A PyMuPDF document mock usable as a context manager.

    page_points sizes each page in PDF user units (default A4) so the
    page-area cap has something real to measure.
    """
    document = mock.MagicMock()
    document.needs_pass = needs_pass
    document.page_count = page_count
    document.__enter__.return_value = document
    document.__exit__.return_value = False
    pages = []
    for index in range(page_count):
        page = mock.MagicMock()
        pixmap = mock.MagicMock()
        # tobytes("png") is what the renderer calls now; it must return real
        # bytes because they are handed to ollama.Image, which validates.
        pixmap.tobytes.return_value = f"png-bytes-{index + 1}".encode()
        page.get_pixmap.return_value = pixmap
        page.rect = SimpleNamespace(
            width=page_points[0], height=page_points[1]
        )
        pages.append(page)
    document.load_page.side_effect = lambda index: pages[index]
    document.fake_pages = pages
    return document


def pages(count):
    """A lazy sequence of in-memory PageImage fixtures, like the real pipeline."""
    return [
        ocr_service.PageImage(number=i, total=count, data=f"png-{i}".encode())
        for i in range(1, count + 1)
    ]


def lazy_pages(count, on_yield=None):
    """Same, but a generator — proves the consumer never materialises them all."""
    def gen():
        for i in range(1, count + 1):
            if on_yield is not None:
                on_yield(i)
            yield ocr_service.PageImage(
                number=i, total=count, data=f"png-{i}".encode()
            )
    return gen()


def drain(event_queue):
    events = []
    while True:
        try:
            events.append(event_queue.get_nowait())
        except queue.Empty:
            return events


class TestNormalizeOllamaUrl(unittest.TestCase):
    def test_default_url_unchanged(self):
        self.assertEqual(
            ocr_service.normalize_ollama_url("http://localhost:11434"),
            "http://localhost:11434",
        )

    def test_whitespace_and_trailing_slash_trimmed(self):
        self.assertEqual(
            ocr_service.normalize_ollama_url("  http://127.0.0.1:11434/  "),
            "http://127.0.0.1:11434",
        )

    def test_multiple_trailing_slashes_trimmed(self):
        self.assertEqual(
            ocr_service.normalize_ollama_url("http://localhost:11434//"),
            "http://localhost:11434",
        )

    def test_https_to_loopback_accepted(self):
        # A local reverse proxy terminating TLS on loopback is still local.
        self.assertEqual(
            ocr_service.normalize_ollama_url("https://127.0.0.1:11434"),
            "https://127.0.0.1:11434",
        )

    def test_reverse_proxy_path_prefix_preserved(self):
        self.assertEqual(
            ocr_service.normalize_ollama_url("http://localhost/ollama/"),
            "http://localhost/ollama",
        )

    def test_loopback_forms_accepted(self):
        for value in (
            "http://localhost:11434",
            "http://localhost.:11434",       # fully qualified, trailing dot
            "http://LocalHost:11434",        # case-insensitive
            "http://127.0.0.1:11434",
            "http://127.0.0.1",              # default port
            "http://127.1.2.3:11434",        # all of 127.0.0.0/8 is loopback
            "http://[::1]:11434",            # IPv6 loopback
            "http://[::ffff:127.0.0.1]:11434",  # IPv4-mapped loopback
        ):
            with self.subTest(value=value):
                self.assertEqual(ocr_service.normalize_ollama_url(value), value)

    def test_non_loopback_host_rejected(self):
        """The whole point of the lock: page images cannot leave the machine."""
        for value in (
            "http://192.168.1.20:11434",      # LAN
            "http://10.0.0.5:11434",          # LAN
            "https://ollama.example.com",     # public DNS name
            "http://ollama.local:11434",      # mDNS name
            "https://server.lan/ollama/",     # LAN reverse proxy
            "http://0.0.0.0:11434",           # wildcard, not a loopback address
            "http://[::]:11434",              # IPv6 wildcard
            "http://169.254.169.254/",        # cloud metadata endpoint
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as ctx:
                    ocr_service.normalize_ollama_url(value)
                self.assertIn("this machine", str(ctx.exception))

    def test_loopback_lookalike_hosts_rejected(self):
        """Names that merely start or end with a local-looking label."""
        for value in (
            "http://localhost.example.com:11434",
            "http://127.0.0.1.example.com:11434",
            "http://notlocalhost:11434",
            "http://localhost.evil.co/ollama",
            "http://xlocalhost:11434",
            "http://localhost..:11434",   # only one root label is valid
            "http://localhost%2e%65xample.com",  # percent-encoded lookalike
            "http://loc%61lhost",                # percent-encoded 'localhost'
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ocr_service.normalize_ollama_url(value)

    def test_obfuscated_ip_forms_rejected(self):
        """Integer/hex/octal IP spellings are not accepted even though some
        resolvers expand them to 127.0.0.1 — the app only takes canonical
        loopback spellings, so there is nothing to disagree about."""
        for value in (
            "http://2130706433:11434",     # 127.0.0.1 as an integer
            "http://0x7f000001:11434",     # as hex
            "http://0177.0.0.1:11434",     # with an octal first octet
            "http://127。0。0。1",  # ideographic full stops
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ocr_service.normalize_ollama_url(value)

    def test_parser_differential_rejected(self):
        """A URL both parsers read as loopback is fine; any disagreement is
        rejected rather than resolved in the HTTP client's favour."""
        # urlparse and httpx both read the host after the last '@'.
        self.assertEqual(
            ocr_service.normalize_ollama_url("http://example.com\\@localhost"),
            "http://example.com\\@localhost",
        )
        # ...and both read this one as the remote host.
        with self.assertRaises(ValueError):
            ocr_service.normalize_ollama_url("http://localhost\\@example.com")

    def test_userinfo_cannot_disguise_a_remote_host(self):
        """http://localhost@evil.com/ connects to evil.com, not localhost.

        This is why validation must read urlparse().hostname and never
        .netloc or a substring of the raw URL.
        """
        for value in (
            "http://localhost@example.com:11434",
            "http://127.0.0.1@example.com:11434",
            "http://localhost:pw@example.com/ollama",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ocr_service.normalize_ollama_url(value)

    def test_empty_rejected(self):
        for value in ("", "   ", "///"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ocr_service.normalize_ollama_url(value)

    def test_missing_host_rejected(self):
        for value in ("http://", "http:///path"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ocr_service.normalize_ollama_url(value)

    def test_non_http_scheme_rejected(self):
        for value in ("ftp://host:11434", "file:///tmp/x", "localhost:11434"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ocr_service.normalize_ollama_url(value)


class TestValidateInputPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def make_file(self, name, data=None):
        """Write a file whose CONTENT matches its extension by default.

        validate_input_path now sniffs the header, so a fixture claiming to
        be a .png has to actually be one.
        """
        path = self.dir / name
        if data is None:
            suffix = path.suffix.lower()
            if suffix == ".pdf":
                data = b"%PDF-1.7\n stub"
            elif suffix == ".webp":
                data = TINY_WEBP
            elif suffix in (".png", ".jpg", ".jpeg"):
                data = TINY_PNG
            else:
                data = b"data"
        path.write_bytes(data)
        return path

    def test_extension_lying_about_content_rejected(self):
        """The allowlist is content-based: Pillow picks a decoder by magic
        bytes, so a renamed file must not reach that decoder."""
        psd = b"8BPS\x00\x01" + b"\x00" * 20          # Photoshop
        gif = b"GIF89a" + b"\x00" * 20                 # not in our allowlist
        tga = bytes([0, 0, 2, 0, 0, 0, 0, 0]) + b"\x00" * 20
        for name, data, label in (
            ("evil.png", psd, "PSD"),
            ("evil.jpg", gif, "GIF"),
            ("evil.webp", tga, "TGA"),
            ("evil.png", b"%PDF-1.7 not an image", "PDF"),
        ):
            with self.subTest(disguised=label):
                path = self.make_file(name, data=data)
                with self.assertRaisesRegex(ValueError, "not a PNG, JPEG"):
                    ocr_service.validate_input_path(path)

    def test_pdf_extension_lying_about_content_rejected(self):
        path = self.make_file("evil.pdf", data=TINY_PNG)
        with self.assertRaisesRegex(ValueError, "PDF header"):
            ocr_service.validate_input_path(path)

    def test_real_formats_accepted_by_content(self):
        for name, data in (
            ("real.png", TINY_PNG),
            ("real.webp", TINY_WEBP),
            ("real.pdf", b"%PDF-1.4 stub"),
        ):
            with self.subTest(name=name):
                ocr_service.validate_input_path(self.make_file(name, data=data))

    def test_missing_file_rejected(self):
        with self.assertRaisesRegex(ValueError, "exist"):
            ocr_service.validate_input_path(self.dir / "missing.pdf")

    def test_directory_rejected(self):
        subdir = self.dir / "folder.pdf"
        subdir.mkdir()
        with self.assertRaises(ValueError):
            ocr_service.validate_input_path(subdir)

    def test_unsupported_extension_rejected(self):
        path = self.make_file("notes.txt")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            ocr_service.validate_input_path(path)

    def test_supported_extensions_accepted(self):
        for name in ("a.pdf", "b.png", "c.jpg", "d.jpeg", "e.webp"):
            with self.subTest(name=name):
                ocr_service.validate_input_path(self.make_file(name))

    def test_uppercase_extensions_accepted(self):
        for name in ("UPPER.PDF", "SHOUT.PNG", "MIXED.JpEg"):
            with self.subTest(name=name):
                ocr_service.validate_input_path(self.make_file(name))

    @unittest.skipIf(
        os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        "chmod-based unreadability is not enforced for root or on Windows",
    )
    def test_unreadable_file_rejected(self):
        path = self.make_file("locked.pdf")
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        with self.assertRaisesRegex(ValueError, "readable"):
            ocr_service.validate_input_path(path)


class TestBuildOutputPath(unittest.TestCase):
    def test_pdf(self):
        self.assertEqual(
            ocr_service.build_output_path(Path("/docs/report.pdf")),
            Path("/docs/report_extracted.md"),
        )

    def test_image(self):
        self.assertEqual(
            ocr_service.build_output_path(Path("/pics/scan.png")),
            Path("/pics/scan_extracted.md"),
        )

    def test_dotted_stem(self):
        self.assertEqual(
            ocr_service.build_output_path(Path("/docs/report.v2.pdf")),
            Path("/docs/report.v2_extracted.md"),
        )

    def test_custom_output_dir_used_when_given(self):
        """Lets the user keep results out of a synced folder."""
        self.assertEqual(
            ocr_service.build_output_path(
                Path("/sync/dropbox/report.pdf"), Path("/private/out")
            ),
            Path("/private/out/report_extracted.md"),
        )

    def test_custom_output_dir_keeps_the_derived_name(self):
        for name in ("a.pdf", "a.PNG", "two.dots.jpeg"):
            with self.subTest(name=name):
                result = ocr_service.build_output_path(
                    Path("/in") / name, Path("/out")
                )
                self.assertEqual(result.parent, Path("/out"))
                self.assertTrue(result.name.endswith("_extracted.md"))

    def test_none_output_dir_is_the_default_beside_the_input(self):
        self.assertEqual(
            ocr_service.build_output_path(Path("/docs/r.pdf"), None),
            ocr_service.build_output_path(Path("/docs/r.pdf")),
        )


class TestValidateOutputDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_writable_directory_accepted(self):
        ocr_service.validate_output_dir(self.dir)

    def test_missing_directory_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            ocr_service.validate_output_dir(self.dir / "nope")

    def test_file_rejected(self):
        path = self.dir / "a.txt"
        path.write_text("x")
        with self.assertRaisesRegex(ValueError, "Not a folder"):
            ocr_service.validate_output_dir(path)

    @unittest.skipIf(
        os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        "chmod-based unwritability is not enforced for root or on Windows",
    )
    def test_unwritable_directory_rejected(self):
        locked = self.dir / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        with self.assertRaisesRegex(ValueError, "not writable"):
            ocr_service.validate_output_dir(locked)

class TestBuildOllamaOptions(unittest.TestCase):
    def test_auto_sends_no_overrides(self):
        self.assertEqual(ocr_service.build_ollama_options("auto"), {})
        self.assertEqual(ocr_service.build_ollama_options("auto", 2), {})

    def test_cpu_forces_num_gpu_zero(self):
        self.assertEqual(ocr_service.build_ollama_options("cpu"), {"num_gpu": 0})
        # A GPU index is meaningless once every layer is forced to CPU.
        self.assertEqual(ocr_service.build_ollama_options("cpu", 3), {"num_gpu": 0})

    def test_gpu_without_index_requests_max_offload_only(self):
        self.assertEqual(ocr_service.build_ollama_options("gpu"), {"num_gpu": -1})

    def test_gpu_with_index_adds_main_gpu_hint(self):
        self.assertEqual(
            ocr_service.build_ollama_options("gpu", 1),
            {"num_gpu": -1, "main_gpu": 1},
        )

    def test_unknown_mode_is_treated_as_auto(self):
        """Anything unrecognised must fail safe to "don't override", not raise."""
        self.assertEqual(ocr_service.build_ollama_options("quantum"), {})


class TestMakeClient(unittest.TestCase):
    def test_redirects_disabled(self):
        """A loopback URL is not enough on its own.

        ollama.Client defaults to follow_redirects=True, so anything
        listening on the port could answer 3xx and have httpx re-send the
        page image to an external host. The lock only holds with redirects
        off, so assert it explicitly.
        """
        with mock.patch.object(ocr_service.ollama, "Client") as client_cls:
            ocr_service.make_client("http://localhost:11434", 30)
        client_cls.assert_called_once_with(
            host="http://localhost:11434",
            timeout=30,
            follow_redirects=False,
            trust_env=False,
        )

    def test_real_client_has_redirects_disabled(self):
        """Guard against the kwarg being silently dropped upstream."""
        client = ocr_service.make_client("http://localhost:11434", 30)
        self.assertFalse(client._client.follow_redirects)

    def test_proxy_env_cannot_route_loopback_traffic_off_box(self):
        """httpx proxy handling has no loopback exemption.

        With trust_env left on, HTTP_PROXY/ALL_PROXY route even
        http://localhost:11434 through a remote proxy, carrying the page
        image with it. No attacker input is required — one env var, or a
        system-wide proxy on macOS/Windows, is enough.
        """
        proxy_env = {
            "HTTP_PROXY": "http://198.51.100.7:3128",
            "ALL_PROXY": "http://198.51.100.7:3128",
        }
        for url in (
            "http://localhost:11434",
            "http://127.0.0.1:11434",
            "http://[::1]:11434",
        ):
            with self.subTest(url=url):
                with mock.patch.dict(os.environ, proxy_env, clear=False):
                    client = ocr_service.make_client(url, 30)
                    transport = client._client._transport_for_url(
                        httpx.URL(url)
                    )
                pool = getattr(transport, "_pool", None)
                self.assertIsNone(
                    getattr(pool, "_proxy_url", None),
                    f"{url} was routed through a proxy despite being loopback",
                )

    def test_non_loopback_url_rejected_at_client_construction(self):
        """The guarantee lives in make_client, not only at the UI call sites."""
        for url in ("http://evil.example.com:11434", "http://192.168.1.5:11434"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    ocr_service.make_client(url, 30)


class TestListModels(unittest.TestCase):
    URL = "http://localhost:11434"

    def test_extraction_dedup_and_case_insensitive_sort(self):
        response = SimpleNamespace(
            models=[
                model_entry("zeta:7b"),
                model_entry("Alpha:12b"),
                model_entry("  "),
                model_entry("zeta:7b"),
                model_entry(None),
                model_entry("beta:2b "),
            ]
        )
        with mock.patch.object(ocr_service.ollama, "Client") as client_cls:
            client_cls.return_value.list.return_value = response
            result = ocr_service.list_models(self.URL)
        self.assertEqual(result, ["Alpha:12b", "beta:2b", "zeta:7b"])
        client_cls.assert_called_once_with(
            host=self.URL,
            timeout=config.MODEL_LIST_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
        )

    def test_empty_server_list(self):
        with mock.patch.object(ocr_service.ollama, "Client") as client_cls:
            client_cls.return_value.list.return_value = SimpleNamespace(models=[])
            self.assertEqual(ocr_service.list_models(self.URL), [])

    def test_client_construction_error_propagates_with_context(self):
        with mock.patch.object(ocr_service.ollama, "Client") as client_cls:
            client_cls.side_effect = ConnectionError("connection refused")
            with self.assertRaises(OCRServiceError) as ctx:
                ocr_service.list_models(self.URL)
        self.assertIn(self.URL, str(ctx.exception))
        self.assertIn("connection refused", str(ctx.exception))

    def test_list_call_error_propagates_with_context(self):
        with mock.patch.object(ocr_service.ollama, "Client") as client_cls:
            client_cls.return_value.list.side_effect = TimeoutError("timed out")
            with self.assertRaises(OCRServiceError) as ctx:
                ocr_service.list_models(self.URL)
        self.assertIn("timed out", str(ctx.exception))

    def test_unexpected_response_shape_wrapped_with_context(self):
        # e.g. a proxy or an incompatible client version returning a plain
        # dict instead of an object with a .models attribute.
        with mock.patch.object(ocr_service.ollama, "Client") as client_cls:
            client_cls.return_value.list.return_value = {"models": []}
            with self.assertRaises(OCRServiceError) as ctx:
                ocr_service.list_models(self.URL)
        self.assertIn(self.URL, str(ctx.exception))


class TestOpenInDefaultApp(unittest.TestCase):
    PATH = Path("/docs/out_extracted.md")

    def test_macos_uses_open(self):
        with mock.patch.object(ocr_service.sys, "platform", "darwin"), \
                mock.patch.object(ocr_service.subprocess, "run") as run:
            ocr_service.open_in_default_app(self.PATH)
        run.assert_called_once_with(["open", str(self.PATH)], check=True)

    def test_linux_uses_xdg_open(self):
        with mock.patch.object(ocr_service.sys, "platform", "linux"), \
                mock.patch.object(ocr_service.subprocess, "run") as run:
            ocr_service.open_in_default_app(self.PATH)
        run.assert_called_once_with(["xdg-open", str(self.PATH)], check=True)

    def test_windows_uses_startfile(self):
        with mock.patch.object(ocr_service.sys, "platform", "win32"), \
                mock.patch.object(ocr_service.os, "startfile", create=True) as startfile:
            ocr_service.open_in_default_app(self.PATH)
        startfile.assert_called_once_with(str(self.PATH))

    def test_failure_wrapped(self):
        with mock.patch.object(ocr_service.sys, "platform", "darwin"), \
                mock.patch.object(
                    ocr_service.subprocess, "run",
                    side_effect=OSError("no such tool"),
                ):
            with self.assertRaises(OCRServiceError) as ctx:
                ocr_service.open_in_default_app(self.PATH)
        self.assertIn(str(self.PATH), str(ctx.exception))


class TestRevealInFileManager(unittest.TestCase):
    PATH = Path("/docs/out_extracted.md")

    def test_macos_selects_with_open_r(self):
        with mock.patch.object(ocr_service.sys, "platform", "darwin"), \
                mock.patch.object(ocr_service.subprocess, "run") as run:
            ocr_service.reveal_in_file_manager(self.PATH)
        run.assert_called_once_with(["open", "-R", str(self.PATH)], check=True)

    def test_windows_selects_with_explorer(self):
        with mock.patch.object(ocr_service.sys, "platform", "win32"), \
                mock.patch.object(ocr_service.subprocess, "run") as run:
            ocr_service.reveal_in_file_manager(self.PATH)
        run.assert_called_once_with(["explorer", f"/select,{self.PATH}"])

    def test_linux_opens_parent_directory(self):
        with mock.patch.object(ocr_service.sys, "platform", "linux"), \
                mock.patch.object(ocr_service.subprocess, "run") as run:
            ocr_service.reveal_in_file_manager(self.PATH)
        run.assert_called_once_with(["xdg-open", str(self.PATH.parent)], check=True)

    def test_failure_wrapped(self):
        with mock.patch.object(ocr_service.sys, "platform", "darwin"), \
                mock.patch.object(
                    ocr_service.subprocess, "run",
                    side_effect=OSError("boom"),
                ):
            with self.assertRaises(OCRServiceError) as ctx:
                ocr_service.reveal_in_file_manager(self.PATH)
        self.assertIn(str(self.PATH), str(ctx.exception))


class TestVerifyImageBytes(unittest.TestCase):
    def test_allowed_formats_pass_and_report_their_type(self):
        self.assertEqual(ocr_service.verify_image_bytes(TINY_PNG, "x.png"), "PNG")
        self.assertEqual(ocr_service.verify_image_bytes(TINY_WEBP, "x.webp"), "WEBP")

    def test_disallowed_real_formats_rejected(self):
        """Formats Pillow can decode but we do not accept."""
        import io as _io

        from PIL import Image

        for fmt in ("GIF", "BMP", "TIFF", "TGA", "PPM"):
            buffer = _io.BytesIO()
            Image.new("RGB", (2, 2), (9, 9, 9)).save(buffer, format=fmt)
            with self.subTest(format=fmt):
                with self.assertRaises(ValueError):
                    ocr_service.verify_image_bytes(buffer.getvalue(), "x.png")

    def test_sniff_runs_before_pillow(self):
        """A disguised file is rejected without Pillow being invoked at all.

        This is the property that keeps a malformed PSD/GD/JPEG2000 away from
        its decoder, rather than relying on that decoder being bug-free.
        """
        psd = b"8BPS\x00\x01" + b"\x00" * 64
        with mock.patch("PIL.Image.open") as pil_open:
            with self.assertRaises(ValueError):
                ocr_service.verify_image_bytes(psd, "evil.png")
        pil_open.assert_not_called()

    def test_pixel_ceiling_enforced(self):
        big = mock.MagicMock()
        big.format = "PNG"
        big.size = (40_000, 40_000)      # 1.6 gigapixels
        big.__enter__ = lambda self_: self_
        big.__exit__ = lambda *a: False
        with mock.patch("PIL.Image.open", return_value=big):
            with self.assertRaisesRegex(ValueError, "megapixel"):
                ocr_service.verify_image_bytes(TINY_PNG, "bomb.png")

    def test_truncated_and_empty_input_rejected(self):
        for data in (b"", b"\x89PNG", TINY_PNG[:10]):
            with self.subTest(length=len(data)):
                with self.assertRaises(ValueError):
                    ocr_service.verify_image_bytes(data, "x.png")


class TestNeutralizeRemoteMedia(unittest.TestCase):
    def test_remote_image_becomes_a_plain_link(self):
        text, count = ocr_service.neutralize_remote_media(
            "before ![leak](http://attacker.example/x.png?d=abc) after"
        )
        self.assertEqual(count, 1)
        self.assertNotIn("![", text)
        # The URL stays fully visible — only the auto-fetch is removed.
        self.assertIn("[leak](http://attacker.example/x.png?d=abc)", text)

    def test_https_and_protocol_relative_also_defused(self):
        for url in ("https://a.example/x.png", "//a.example/x.png"):
            with self.subTest(url=url):
                text, count = ocr_service.neutralize_remote_media(f"![]({url})")
                self.assertEqual(count, 1)
                self.assertFalse(text.startswith("!"))

    def test_reference_style_images_defused(self):
        text, count = ocr_service.neutralize_remote_media(
            "![alt][ref]\n\n[ref]: http://attacker.example/x.png"
        )
        self.assertEqual(count, 1)
        self.assertIn("[alt][ref]", text)
        self.assertNotIn("![alt][ref]", text)

    def test_html_embedding_tags_defused(self):
        for tag in (
            '<img src="http://a.example/x.png">',
            "<iframe src='http://a.example'></iframe>",
            '<script src="http://a.example/x.js"></script>',
            '<link rel=stylesheet href="http://a.example/x.css">',
            '<video src="http://a.example/v.mp4">',
        ):
            with self.subTest(tag=tag):
                text, count = ocr_service.neutralize_remote_media(tag)
                self.assertGreaterEqual(count, 1)
                self.assertNotIn("<img", text.lower())
                self.assertNotIn("<iframe", text.lower())
                self.assertNotIn("<script", text.lower())
                self.assertNotIn("<link", text.lower())
                self.assertNotIn("<video", text.lower())

    def test_ordinary_recognized_text_is_untouched(self):
        """The overwhelmingly common case must pass through byte-identical."""
        samples = [
            "# Invoice 2451\n\n| Item | Qty |\n| --- | --- |\n| Bolt | 12 |",
            "Contact us at https://example.com/support or call 555-0100.",
            "See [the appendix](#appendix) and [terms](https://example.com/t).",
            "Bullet list:\n- alpha\n- beta\n\n> quoted\n\n`code` and 5 < 6",
            "Local figure: ![diagram](figure-1.png)",
            "Escaped exclamation! [not an image](https://example.com)",
        ]
        for sample in samples:
            with self.subTest(sample=sample[:32]):
                text, count = ocr_service.neutralize_remote_media(sample)
                self.assertEqual(count, 0)
                self.assertEqual(text, sample)

    def test_repeated_bangs_cannot_smuggle_an_image_through(self):
        """Regression: stripping a single '!' from '!![](url)' leaves '![](url)'.

        Found by fuzzing the sanitiser — the substitution has to consume the
        whole run of exclamation marks, not just one.
        """
        for prefix in ("!!", "!!!", "!!!!!"):
            with self.subTest(prefix=prefix):
                text, _ = ocr_service.neutralize_remote_media(
                    f"{prefix}[](http://attacker.example/x)"
                )
                self.assertNotIn("![", text)

    def test_output_is_idempotent(self):
        """Sanitising an already-sanitised document changes nothing further."""
        sample = (
            "!![](http://a.example/x) <img src=http://a.example> ![alt][ref]"
        )
        once, first_count = ocr_service.neutralize_remote_media(sample)
        twice, second_count = ocr_service.neutralize_remote_media(once)
        self.assertEqual(once, twice)
        self.assertGreater(first_count, 0)
        self.assertEqual(second_count, 0)

    def test_data_uri_images_are_left_alone(self):
        """data: images make no network request, so there is nothing to fix."""
        sample = "![x](data:image/png;base64,iVBORw0KGgo=)"
        text, count = ocr_service.neutralize_remote_media(sample)
        self.assertEqual((text, count), (sample, 0))


class TestVerifyOllamaEndpoint(unittest.TestCase):
    def _client(self, response):
        client = mock.MagicMock()
        client._client.get.return_value = response
        return client

    def test_accepts_an_ollama_shaped_response(self):
        client = self._client(fake_version_response("0.12.3"))
        self.assertEqual(ocr_service.verify_ollama_endpoint(client), "0.12.3")
        client._client.get.assert_called_once_with(
            "/api/version", timeout=config.ENDPOINT_VERIFY_TIMEOUT
        )

    def test_rejects_a_squatter_on_the_port(self):
        cases = {
            "non-200": fake_version_response(status=404),
            "no version key": fake_version_response(payload={"hello": "world"}),
            "empty version": fake_version_response(version=""),
            "non-string version": fake_version_response(payload={"version": 3}),
        }
        for label, response in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(OCRServiceError):
                    ocr_service.verify_ollama_endpoint(self._client(response))

    def test_non_json_body_rejected(self):
        response = mock.MagicMock()
        response.status_code = 200
        response.json.side_effect = ValueError("not json")
        with self.assertRaisesRegex(OCRServiceError, "did not identify"):
            ocr_service.verify_ollama_endpoint(self._client(response))

    def test_connection_failure_explains_itself(self):
        client = mock.MagicMock()
        client._client.get.side_effect = ConnectionError("refused")
        with self.assertRaisesRegex(OCRServiceError, "No response"):
            ocr_service.verify_ollama_endpoint(client)


class TestCancellation(unittest.TestCase):
    def test_checkpoint_raises_only_when_set(self):
        event = threading.Event()
        ocr_service._raise_if_cancelled(None)      # no event at all
        ocr_service._raise_if_cancelled(event)     # not set
        event.set()
        with self.assertRaises(ocr_service.OCRCancelled):
            ocr_service._raise_if_cancelled(event)

    def test_cancelled_is_not_a_service_error(self):
        """So it never surfaces as a failure dialog or gets rewrapped."""
        self.assertFalse(issubclass(ocr_service.OCRCancelled, OCRServiceError))

    def test_stops_between_pages_and_writes_nothing(self):
        event = threading.Event()
        client = mock.MagicMock()
        seen = []

        def chat(*_a, **_kw):
            seen.append(1)
            if len(seen) == 2:
                event.set()          # user clicks Cancel during page 2
            return stream_response("text")

        client.chat.side_effect = chat
        with self.assertRaises(ocr_service.OCRCancelled):
            ocr_service.recognize_images(
                client, "m", pages(10), lambda _m: None, cancel_event=event
            )
        # Stopped promptly rather than finishing all ten pages.
        self.assertEqual(len(seen), 2)

    def test_stops_mid_stream(self):
        """Cancellation lands within one chunk, not at the end of the page."""
        event = threading.Event()
        chunks_seen = []

        def long_stream():
            for i in range(1000):
                chunks_seen.append(i)
                if i == 3:
                    event.set()
                yield SimpleNamespace(message=SimpleNamespace(content=f"c{i}"))

        client = mock.MagicMock()
        client.chat.return_value = long_stream()
        with self.assertRaises(ocr_service.OCRCancelled):
            ocr_service.recognize_images(
                client, "m", pages(1), lambda _m: None, cancel_event=event
            )
        self.assertLess(len(chunks_seen), 10)

    def test_renderer_stops_between_pages(self):
        event = threading.Event()
        document = make_fake_document(50)
        rendered = []

        def load_page(index):
            rendered.append(index)
            if len(rendered) == 3:
                event.set()
            return document.fake_pages[index]

        document.load_page.side_effect = load_page
        with mock.patch.object(ocr_service, "pymupdf") as fake_pymupdf:
            fake_pymupdf.open.return_value = document
            with self.assertRaises(ocr_service.OCRCancelled):
                list(ocr_service.iter_pdf_pages(
                    Path("/d.pdf"), 150, lambda _m: None, event
                ))
        self.assertEqual(len(rendered), 3)
        self.assertTrue(document.__exit__.called)


class TestPageAreaCap(unittest.TestCase):
    def test_oversized_page_rejected_before_rasterizing(self):
        # 200x200 inches, the PDF maximum, at 300 DPI = 3.6 gigapixels.
        document = make_fake_document(1, page_points=(14400, 14400))
        with mock.patch.object(ocr_service, "pymupdf") as fake_pymupdf:
            fake_pymupdf.open.return_value = document
            with self.assertRaises(OCRServiceError) as ctx:
                list(ocr_service.iter_pdf_pages(
                    Path("/big.pdf"), 300, lambda _m: None
                ))
        message = str(ctx.exception)
        self.assertIn("megapixel", message)
        # The allocation must never have been attempted.
        document.fake_pages[0].get_pixmap.assert_not_called()

    def test_message_suggests_a_workable_dpi(self):
        document = make_fake_document(1, page_points=(7000, 7000))
        with mock.patch.object(ocr_service, "pymupdf") as fake_pymupdf:
            fake_pymupdf.open.return_value = document
            with self.assertRaises(OCRServiceError) as ctx:
                list(ocr_service.iter_pdf_pages(
                    Path("/big.pdf"), 300, lambda _m: None
                ))
        self.assertRegex(str(ctx.exception), r"Try \d+ DPI")

    def test_normal_page_unaffected_at_every_offered_dpi(self):
        for dpi in config.DPI_OPTIONS:
            with self.subTest(dpi=dpi):
                document = make_fake_document(1)     # A4
                with mock.patch.object(ocr_service, "pymupdf") as fake_pymupdf:
                    fake_pymupdf.open.return_value = document
                    result = list(ocr_service.iter_pdf_pages(
                        Path("/ok.pdf"), dpi, lambda _m: None
                    ))
                self.assertEqual(len(result), 1)

    def test_page_count_cap(self):
        document = make_fake_document(0)
        document.page_count = config.MAX_PDF_PAGES + 1
        with mock.patch.object(ocr_service, "pymupdf") as fake_pymupdf:
            fake_pymupdf.open.return_value = document
            with self.assertRaisesRegex(OCRServiceError, "page limit"):
                list(ocr_service.iter_pdf_pages(
                    Path("/long.pdf"), 150, lambda _m: None
                ))
        document.load_page.assert_not_called()


class TestSaveMarkdownAtomic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.output = self.dir / "doc_extracted.md"

    def test_writes_utf8(self):
        content = "# Überschrift\n\nТекст — naïve café ✓"
        ocr_service.save_markdown_atomic(self.output, content)
        self.assertEqual(self.output.read_text(encoding="utf-8"), content)

    def test_normalizes_newlines(self):
        ocr_service.save_markdown_atomic(self.output, "a\r\nb\rc\n")
        self.assertEqual(self.output.read_bytes(), b"a\nb\nc\n")

    def test_replaces_existing_file(self):
        self.output.write_text("old", encoding="utf-8")
        ocr_service.save_markdown_atomic(self.output, "new")
        self.assertEqual(self.output.read_text(encoding="utf-8"), "new")

    def test_no_leftover_temp_file_on_success(self):
        ocr_service.save_markdown_atomic(self.output, "content")
        self.assertEqual(list(self.dir.iterdir()), [self.output])

    def test_replace_failure_removes_temp_and_keeps_existing(self):
        self.output.write_text("old", encoding="utf-8")
        with mock.patch.object(ocr_service.os, "replace") as replace:
            replace.side_effect = OSError("disk full")
            with self.assertRaises(OCRServiceError) as ctx:
                ocr_service.save_markdown_atomic(self.output, "new")
        self.assertIn("disk full", str(ctx.exception))
        self.assertEqual(self.output.read_text(encoding="utf-8"), "old")
        self.assertEqual(list(self.dir.iterdir()), [self.output])

    def test_temp_creation_failure_reports_error(self):
        with mock.patch.object(
            ocr_service.tempfile, "NamedTemporaryFile"
        ) as ntf:
            ntf.side_effect = OSError("permission denied")
            with self.assertRaises(OCRServiceError):
                ocr_service.save_markdown_atomic(self.output, "content")
        self.assertEqual(list(self.dir.iterdir()), [])


class TestRecognizeImages(unittest.TestCase):
    MODEL = "vision-model:latest"

    def test_one_independent_request_per_image_with_exact_prompt(self):
        client = mock.MagicMock()
        client.chat.side_effect = [stream_response(" one "), stream_response("two\n")]
        fixtures = pages(2)
        results = ocr_service.recognize_images(
            client, self.MODEL, fixtures, lambda _msg: None
        )
        self.assertEqual(results, ["one", "two"])
        self.assertEqual(client.chat.call_count, 2)
        for call, page in zip(client.chat.call_args_list, fixtures):
            self.assertTrue(call.kwargs["stream"])
            self.assertEqual(call.kwargs["model"], self.MODEL)
            self.assertEqual(
                call.kwargs["messages"],
                [
                    {"role": "system", "content": config.SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": "Recognize this document page.",
                        "images": [ocr_service.ollama.Image(value=page.data)],
                    },
                ],
            )
        first, second = (c.kwargs["messages"] for c in client.chat.call_args_list)
        self.assertIsNot(first, second)

    def test_options_forwarded_verbatim_to_every_chat_call(self):
        client = mock.MagicMock()
        client.chat.side_effect = [stream_response("a"), stream_response("b")]
        gpu_options = {"num_gpu": -1, "main_gpu": 1}
        ocr_service.recognize_images(
            client, self.MODEL, pages(2), lambda _m: None, options=gpu_options,
        )
        for call in client.chat.call_args_list:
            self.assertEqual(call.kwargs["options"], gpu_options)

    def test_no_options_sends_none_rather_than_an_empty_dict(self):
        """An empty override dict must not be sent as a literal no-op options={}."""
        client = mock.MagicMock()
        client.chat.side_effect = [stream_response("a")]
        ocr_service.recognize_images(client, self.MODEL, pages(1), lambda _m: None)
        self.assertIsNone(client.chat.call_args.kwargs["options"])

    def test_image_payload_is_bytes_not_a_path(self):
        """The page never exists as a file, so nothing path-shaped may be sent."""
        client = mock.MagicMock()
        client.chat.side_effect = [stream_response("x")]
        ocr_service.recognize_images(client, self.MODEL, pages(1), lambda _m: None)
        sent = client.chat.call_args.kwargs["messages"][1]["images"][0]
        self.assertIsInstance(sent, ocr_service.ollama.Image)
        self.assertIsInstance(sent.value, bytes)

    def test_wire_payload_identical_to_the_old_file_based_route(self):
        """Switching to bytes must not change a single byte Ollama receives."""
        import base64
        from ollama._types import Image as OImage

        png = Path("examples/sample-5-page-pdf-a4-size.pdf").read_bytes()[:512]
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            handle.write(png)
            temp_path = Path(handle.name)
        self.addCleanup(temp_path.unlink)
        self.assertEqual(
            OImage(value=png).model_dump(),
            OImage(value=temp_path).model_dump(),
        )
        self.assertEqual(base64.b64decode(OImage(value=png).model_dump()), png)

    def test_pages_consumed_lazily_one_at_a_time(self):
        """Page N+1 must not be rendered until page N has been recognized.

        This is what keeps peak memory at one page and stops a long document
        from rebuilding the old unbounded-growth problem in RAM.
        """
        rendered, order = [], []
        client = mock.MagicMock()

        def chat(*_a, **_kw):
            order.append(("ocr", len(rendered)))
            return stream_response("text")

        client.chat.side_effect = chat
        ocr_service.recognize_images(
            client,
            self.MODEL,
            lazy_pages(4, on_yield=lambda i: (rendered.append(i),
                                              order.append(("render", i)))),
            lambda _m: None,
        )
        self.assertEqual(rendered, [1, 2, 3, 4])
        # Strictly alternating: render 1, ocr 1, render 2, ocr 2, ...
        self.assertEqual(
            order,
            [("render", 1), ("ocr", 1), ("render", 2), ("ocr", 2),
             ("render", 3), ("ocr", 3), ("render", 4), ("ocr", 4)],
        )

    def test_progress_messages_in_order(self):
        client = mock.MagicMock()
        client.chat.side_effect = [stream_response(f"p{i}") for i in range(3)]
        logs = []
        ocr_service.recognize_images(
            client, self.MODEL, pages(3), logs.append
        )
        self.assertEqual(
            logs,
            [
                "Sending page 1/3 to Ollama...",
                "Sending page 2/3 to Ollama...",
                "Sending page 3/3 to Ollama...",
            ],
        )

    def test_progress_callback_emits_ocr_before_each_page(self):
        client = mock.MagicMock()
        client.chat.side_effect = [stream_response(f"p{i}") for i in range(3)]
        events = []
        ocr_service.recognize_images(
            client,
            self.MODEL,
            pages(3),
            lambda _msg: None,
            lambda phase, cur, tot: events.append((phase, cur, tot)),
        )
        self.assertEqual(
            events,
            [
                ("ocr", 1, 3),
                ("ocr", 2, 3),
                ("ocr", 3, 3),
            ],
        )
        # Each progress event fires before the corresponding chat call.
        self.assertEqual(client.chat.call_count, 3)

    def test_empty_content_fails_identifying_page(self):
        for empty in (None, "", "   \n\t"):
            with self.subTest(content=repr(empty)):
                client = mock.MagicMock()
                client.chat.side_effect = [
                    stream_response("fine"),
                    stream_response(empty),
                ]
                with self.assertRaises(OCRServiceError) as ctx:
                    ocr_service.recognize_images(
                        client,
                        self.MODEL,
                        pages(2),
                        lambda _msg: None,
                    )
                self.assertIn("page 2/2", str(ctx.exception))

    def test_empty_stream_fails_identifying_page(self):
        """A stream that yields zero chunks triggers 'returned no text'."""
        client = mock.MagicMock()
        client.chat.side_effect = [stream_response("fine"), stream_response()]
        with self.assertRaises(OCRServiceError) as ctx:
            ocr_service.recognize_images(
                client,
                self.MODEL,
                pages(2),
                lambda _msg: None,
            )
        self.assertIn("page 2/2", str(ctx.exception))

    def test_chat_failure_wrapped_with_page_and_model_context(self):
        client = mock.MagicMock()
        client.chat.side_effect = [
            stream_response("ok"),
            RuntimeError("model not found"),
        ]
        with self.assertRaises(OCRServiceError) as ctx:
            ocr_service.recognize_images(
                client,
                self.MODEL,
                pages(2),
                lambda _msg: None,
            )
        message = str(ctx.exception)
        self.assertIn("page 2/2", message)
        self.assertIn(self.MODEL, message)
        self.assertIn("model not found", message)

    def test_generator_exception_mid_stream_wrapped_with_context(self):
        """An exception raised while iterating the stream is wrapped."""

        def exploding_stream():
            yield SimpleNamespace(message=SimpleNamespace(content="part"))
            raise RuntimeError("connection dropped")

        client = mock.MagicMock()
        client.chat.side_effect = [stream_response("ok"), exploding_stream()]
        with self.assertRaises(OCRServiceError) as ctx:
            ocr_service.recognize_images(
                client,
                self.MODEL,
                pages(2),
                lambda _msg: None,
            )
        message = str(ctx.exception)
        self.assertIn("page 2/2", message)
        self.assertIn("connection dropped", message)

    def test_stream_chunk_events_emitted_in_order(self):
        """stream_chunk events carry each delta, in order, with page number."""
        client = mock.MagicMock()
        client.chat.side_effect = [
            stream_response("Hel", "lo", " world"),
            stream_response("foo"),
        ]
        events = []
        ocr_service.recognize_images(
            client,
            self.MODEL,
            pages(2),
            lambda _msg: None,
            event_callback=lambda kind, payload: events.append((kind, payload)),
        )
        stream_events = [(k, p) for k, p in events if k == "stream_chunk"]
        self.assertEqual(
            stream_events,
            [
                ("stream_chunk", {"page": 1, "text": "Hel"}),
                ("stream_chunk", {"page": 1, "text": "lo"}),
                ("stream_chunk", {"page": 1, "text": " world"}),
                ("stream_chunk", {"page": 2, "text": "foo"}),
            ],
        )

    def test_page_text_events_emitted_after_each_page(self):
        """page_text events carry the assembled (stripped) text per page."""
        client = mock.MagicMock()
        client.chat.side_effect = [
            stream_response("  Hello ", "world  "),
            stream_response("second"),
        ]
        events = []
        results = ocr_service.recognize_images(
            client,
            self.MODEL,
            pages(2),
            lambda _msg: None,
            event_callback=lambda kind, payload: events.append((kind, payload)),
        )
        page_events = [(k, p) for k, p in events if k == "page_text"]
        self.assertEqual(
            page_events,
            [
                ("page_text", {"page": 1, "total": 2, "text": "Hello world"}),
                ("page_text", {"page": 2, "total": 2, "text": "second"}),
            ],
        )
        # The returned texts match the page_text payloads (stripped).
        self.assertEqual(results, ["Hello world", "second"])

    def test_page_image_event_before_send_with_valid_png(self):
        """A page_image event with valid PNG bytes precedes the send log."""
        import io as _io

        from PIL import Image

        buffer = _io.BytesIO()
        Image.new("RGB", (400, 300), (10, 20, 30)).save(buffer, format="PNG")
        client = mock.MagicMock()
        client.chat.side_effect = [stream_response("text")]
        timeline = []
        ocr_service.recognize_images(
            client,
            self.MODEL,
            [ocr_service.PageImage(number=1, total=1, data=buffer.getvalue())],
            lambda msg: timeline.append(("log", msg)),
            event_callback=lambda kind, payload: timeline.append((kind, payload)),
        )
        kinds = [entry[0] for entry in timeline]
        self.assertIn("page_image", kinds)
        image_index = kinds.index("page_image")
        send_index = next(
            i for i, entry in enumerate(timeline)
            if entry[0] == "log" and "Sending page" in entry[1]
        )
        self.assertLess(image_index, send_index)
        payload = timeline[image_index][1]
        self.assertEqual(payload["page"], 1)
        self.assertEqual(payload["total"], 1)
        Image.open(_io.BytesIO(payload["png"])).verify()

    def test_thumbnail_failure_does_not_abort_ocr(self):
        """Undecodable page bytes skip the preview but still recognize."""
        client = mock.MagicMock()
        client.chat.side_effect = [stream_response("recognized")]
        events = []
        results = ocr_service.recognize_images(
            client,
            self.MODEL,
            [ocr_service.PageImage(number=1, total=1, data=b"not an image")],
            lambda _msg: None,
            event_callback=lambda kind, payload: events.append((kind, payload)),
        )
        self.assertEqual(results, ["recognized"])
        self.assertNotIn("page_image", [kind for kind, _ in events])


class TestMakeThumbnailPng(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _write_image(self, name, size, color=(120, 60, 200)):
        from PIL import Image

        path = self.dir / name
        Image.new("RGB", size, color).save(path)
        return path

    def _open(self, data):
        import io as _io

        from PIL import Image

        return Image.open(_io.BytesIO(data))

    def test_downscales_png_preserving_aspect_and_returns_png(self):
        path = self._write_image("big.png", (2000, 1000))
        data = ocr_service.make_thumbnail_png(path, 900)
        self.assertIsInstance(data, bytes)
        out = self._open(data)
        self.assertEqual(out.format, "PNG")
        self.assertEqual(out.size, (900, 450))  # 2:1 aspect preserved

    def test_jpeg_input_supported(self):
        path = self._write_image("photo.jpg", (1200, 800))
        out = self._open(ocr_service.make_thumbnail_png(path, 600))
        self.assertEqual(out.format, "PNG")
        self.assertLessEqual(max(out.size), 600)

    def test_small_image_not_upscaled(self):
        path = self._write_image("small.png", (100, 50))
        out = self._open(ocr_service.make_thumbnail_png(path, 900))
        self.assertEqual(out.size, (100, 50))

    def test_real_pdf_rendered_page(self):
        """The pipeline feeds PNGs rendered from PDF pages — thumbnail one."""
        document = ocr_service.pymupdf.open()
        document.new_page(width=1200, height=1600)
        pixmap = document.load_page(0).get_pixmap(dpi=150)
        path = self.dir / "page_0001.png"
        pixmap.save(str(path))
        document.close()
        out = self._open(ocr_service.make_thumbnail_png(path, 900))
        self.assertEqual(out.format, "PNG")
        self.assertLessEqual(max(out.size), 900)


class TestIterPdfPages(unittest.TestCase):
    """The renderer yields PNG bytes lazily and writes nothing to disk."""

    def setUp(self):
        self.pdf_path = Path("/docs/input.pdf")

    def _consume(self, document, dpi=200, log=None):
        with mock.patch.object(ocr_service, "pymupdf") as fake_pymupdf:
            fake_pymupdf.open.return_value = document
            pages = list(
                ocr_service.iter_pdf_pages(
                    self.pdf_path, dpi, log or (lambda _msg: None)
                )
            )
        return pages, fake_pymupdf

    def test_ordered_render_with_dpi_rgb_no_alpha(self):
        document = make_fake_document(3)
        logs = []
        pages_out, fake_pymupdf = self._consume(document, dpi=200, log=logs.append)
        fake_pymupdf.open.assert_called_once_with(self.pdf_path)
        self.assertEqual(
            document.load_page.call_args_list,
            [mock.call(0), mock.call(1), mock.call(2)],
        )
        for page in document.fake_pages:
            page.get_pixmap.assert_called_once_with(
                dpi=200, colorspace=fake_pymupdf.csRGB, alpha=False
            )
        self.assertEqual([p.number for p in pages_out], [1, 2, 3])
        self.assertEqual({p.total for p in pages_out}, {3})
        self.assertEqual(
            logs,
            [
                "Rendering page 1/3...",
                "Rendering page 2/3...",
                "Rendering page 3/3...",
            ],
        )
        self.assertTrue(document.__exit__.called)

    def test_encodes_to_png_bytes_and_never_saves_a_file(self):
        document = make_fake_document(2)
        pages_out, _ = self._consume(document)
        for page in document.fake_pages:
            pixmap = page.get_pixmap.return_value
            pixmap.tobytes.assert_called_once_with("png")
            pixmap.save.assert_not_called()   # the whole point of the change
        self.assertTrue(all(isinstance(p, ocr_service.PageImage) for p in pages_out))

    def test_is_lazy_nothing_rendered_before_first_next(self):
        document = make_fake_document(5)
        with mock.patch.object(ocr_service, "pymupdf") as fake_pymupdf:
            fake_pymupdf.open.return_value = document
            generator = ocr_service.iter_pdf_pages(
                self.pdf_path, 150, lambda _msg: None
            )
            # Creating the generator must not open or render anything yet.
            fake_pymupdf.open.assert_not_called()
            first = next(generator)
            self.assertEqual(first.number, 1)
            self.assertEqual(document.load_page.call_count, 1)
            next(generator)
            self.assertEqual(document.load_page.call_count, 2)
            generator.close()
        self.assertTrue(document.__exit__.called)

    def test_closing_early_releases_the_document(self):
        """An abandoned generator must not hold the PDF open."""
        document = make_fake_document(10)
        with mock.patch.object(ocr_service, "pymupdf") as fake_pymupdf:
            fake_pymupdf.open.return_value = document
            generator = ocr_service.iter_pdf_pages(
                self.pdf_path, 150, lambda _msg: None
            )
            next(generator)
            self.assertFalse(document.__exit__.called)
            generator.close()
        self.assertTrue(document.__exit__.called)
        self.assertEqual(document.load_page.call_count, 1)

    def test_password_protected_fails_before_rendering(self):
        document = make_fake_document(5, needs_pass=True)
        with mock.patch.object(ocr_service, "pymupdf") as fake_pymupdf:
            fake_pymupdf.open.return_value = document
            with self.assertRaisesRegex(OCRServiceError, "password"):
                list(ocr_service.iter_pdf_pages(
                    self.pdf_path, 150, lambda _msg: None
                ))
        document.load_page.assert_not_called()
        self.assertTrue(document.__exit__.called)

    def test_zero_page_pdf_fails(self):
        document = make_fake_document(0)
        with mock.patch.object(ocr_service, "pymupdf") as fake_pymupdf:
            fake_pymupdf.open.return_value = document
            with self.assertRaisesRegex(OCRServiceError, "no pages"):
                list(ocr_service.iter_pdf_pages(
                    self.pdf_path, 150, lambda _msg: None
                ))
        document.load_page.assert_not_called()
        self.assertTrue(document.__exit__.called)

    def test_open_failure_wrapped(self):
        with mock.patch.object(ocr_service, "pymupdf") as fake_pymupdf:
            fake_pymupdf.open.side_effect = RuntimeError("broken xref")
            with self.assertRaises(OCRServiceError) as ctx:
                list(ocr_service.iter_pdf_pages(
                    self.pdf_path, 150, lambda _msg: None
                ))
        self.assertIn("broken xref", str(ctx.exception))

    def test_page_render_failure_identifies_page_and_closes_document(self):
        document = make_fake_document(3)
        document.fake_pages[1].get_pixmap.return_value.tobytes.side_effect = (
            RuntimeError("Overly large image")
        )
        with mock.patch.object(ocr_service, "pymupdf") as fake_pymupdf:
            fake_pymupdf.open.return_value = document
            with self.assertRaises(OCRServiceError) as ctx:
                list(ocr_service.iter_pdf_pages(
                    self.pdf_path, 150, lambda _msg: None
                ))
        self.assertIn("page 2/3", str(ctx.exception))
        self.assertTrue(document.__exit__.called)

    def test_real_pdf_end_to_end_writes_nothing(self):
        """Against the real PyMuPDF, not a mock: bytes out, no files created."""
        sample = Path("examples/sample-5-page-pdf-a4-size.pdf")
        with tempfile.TemporaryDirectory() as scratch:
            before = set(Path(tempfile.gettempdir()).glob("local_ocr_*"))
            pages_out = list(
                ocr_service.iter_pdf_pages(sample, 72, lambda _m: None)
            )
            after = set(Path(tempfile.gettempdir()).glob("local_ocr_*"))
            self.assertEqual(before, after, "a render directory was created")
            self.assertEqual(list(Path(scratch).iterdir()), [])
        self.assertEqual(len(pages_out), 5)
        for index, page in enumerate(pages_out, start=1):
            self.assertEqual(page.number, index)
            self.assertEqual(page.total, 5)
            self.assertTrue(page.data.startswith(b"\x89PNG\r\n\x1a\n"))


class TestReadInputImage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_yields_one_page_with_the_file_bytes(self):
        path = self.dir / "scan.png"
        path.write_bytes(TINY_PNG)
        pages_out = list(ocr_service.read_input_image(path))
        self.assertEqual(len(pages_out), 1)
        self.assertEqual((pages_out[0].number, pages_out[0].total), (1, 1))
        self.assertEqual(pages_out[0].data, path.read_bytes())

    def test_empty_file_rejected(self):
        path = self.dir / "empty.png"
        path.write_bytes(b"")
        with self.assertRaisesRegex(OCRServiceError, "empty"):
            list(ocr_service.read_input_image(path))

    def test_unreadable_file_wrapped(self):
        with self.assertRaises(OCRServiceError):
            list(ocr_service.read_input_image(self.dir / "missing.png"))


class TestProcessOcr(unittest.TestCase):
    # Must be loopback: make_client re-validates, so a remote placeholder
    # would now (correctly) fail before the code under test is reached.
    URL = "http://localhost:11434"
    MODEL = "vision:7b"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def make_request(self, name, dpi=150):
        input_path = self.dir / name
        input_path.write_bytes(
            b"%PDF-1.7 stub" if name.endswith(".pdf") else TINY_PNG
        )
        return OCRRequest(
            input_path=input_path,
            output_path=ocr_service.build_output_path(input_path),
            ollama_url=self.URL,
            model=self.MODEL,
            dpi=dpi,
        )

    def run_pdf_pipeline(self, request, document, chat_side_effect,
                         replace_error=None):
        """Run process_ocr for a PDF with mocks; return (result, error, events,
        created_temp_dirs)."""
        events = queue.Queue()
        created = []

        def forbidden_mkdtemp(*args, **kwargs):
            created.append(args)
            raise AssertionError(
                "process_ocr created a temp directory; page images must stay "
                "in memory so a crash cannot leave them on disk"
            )

        patches = [
            mock.patch.object(ocr_service, "pymupdf"),
            mock.patch.object(ocr_service.ollama, "Client"),
            mock.patch.object(
                ocr_service.tempfile, "mkdtemp", side_effect=forbidden_mkdtemp
            ),
        ]
        result = error = None
        with patches[0] as fake_pymupdf, patches[1] as client_cls, patches[2]:
            fake_pymupdf.open.return_value = document
            arm_version_probe(client_cls)
            client_cls.return_value.chat.side_effect = chat_side_effect
            self.client_cls = client_cls
            try:
                if replace_error is not None:
                    with mock.patch.object(
                        ocr_service.os, "replace", side_effect=replace_error
                    ):
                        result = ocr_service.process_ocr(request, events)
                else:
                    result = ocr_service.process_ocr(request, events)
            except Exception as exc:
                error = exc
        return result, error, drain(events), created

    def test_pdf_progress_events_are_a_single_ocr_phase(self):
        """Rendering is now interleaved with recognition, one page at a time.

        There is no separate up-front render phase to report, so a 3-page PDF
        emits exactly 3 monotonically increasing ocr events. app.on_progress
        already handles the no-render-phase case (fraction = current/total).
        """
        request = self.make_request("doc.pdf")
        document = make_fake_document(3)
        _result, _error, events, _created = self.run_pdf_pipeline(
            request,
            document,
            [stream_response("p1"), stream_response("p2"), stream_response("p3")],
        )
        progress_events = [
            payload for kind, payload in events if kind == "progress"
        ]
        self.assertEqual(len(progress_events), 3)
        for i, payload in enumerate(progress_events):
            self.assertEqual(payload["phase"], "ocr")
            self.assertEqual(payload["current"], i + 1)
            self.assertEqual(payload["total"], 3)

    def test_image_progress_events_only_ocr(self):
        """An image input emits exactly one ocr progress event, no render."""
        request = self.make_request("photo.png")
        events = queue.Queue()
        with mock.patch.object(ocr_service.ollama, "Client") as client_cls:
            arm_version_probe(client_cls)
            client_cls.return_value.chat.return_value = stream_response("recognized")
            ocr_service.process_ocr(request, events)
        progress_events = [
            payload for kind, payload in drain(events) if kind == "progress"
        ]
        self.assertEqual(len(progress_events), 1)
        self.assertEqual(progress_events[0]["phase"], "ocr")
        self.assertEqual(progress_events[0]["current"], 1)
        self.assertEqual(progress_events[0]["total"], 1)

    def test_image_input_read_into_memory_no_render_dir(self):
        request = self.make_request("photo.png")
        events = queue.Queue()
        with mock.patch.object(ocr_service.ollama, "Client") as client_cls, \
                mock.patch.object(ocr_service.tempfile, "mkdtemp") as mkdtemp:
            arm_version_probe(client_cls)
            client_cls.return_value.chat.return_value = stream_response("recognized")
            result = ocr_service.process_ocr(request, events)
        mkdtemp.assert_not_called()
        self.assertEqual(result, request.output_path)
        self.assertEqual(
            request.output_path.read_text(encoding="utf-8"), "recognized"
        )
        client_cls.assert_called_once_with(
            host=self.URL,
            timeout=config.OCR_STREAM_IDLE_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
        )
        self.assertTrue(client_cls.return_value.chat.call_args.kwargs["stream"])
        images = client_cls.return_value.chat.call_args.kwargs["messages"][1]["images"]
        # The file's bytes are sent, not its path — one read, and the same
        # code path a rendered PDF page takes.
        self.assertEqual(images, [ocr_service.ollama.Image(value=TINY_PNG)])
        self.assertEqual(images[0].value, request.input_path.read_bytes())
        logs = [payload for kind, payload in drain(events) if kind == "log"]
        self.assertEqual(logs[0], "[1/3] Preparing image...")
        self.assertIn("[2/3] Sending page 1/1 to Ollama...", logs)
        self.assertIn("[3/3] Saving Markdown...", logs)

    def test_gpu_mode_on_the_request_reaches_the_chat_call(self):
        """End-to-end: OCRRequest.gpu_mode -> build_ollama_options -> chat()."""
        input_path = self.dir / "photo.png"
        input_path.write_bytes(TINY_PNG)
        request = OCRRequest(
            input_path=input_path,
            output_path=ocr_service.build_output_path(input_path),
            ollama_url=self.URL, model=self.MODEL, dpi=150,
            gpu_mode="gpu", gpu_index=1,
        )
        events = queue.Queue()
        with mock.patch.object(ocr_service.ollama, "Client") as client_cls:
            arm_version_probe(client_cls)
            client_cls.return_value.chat.return_value = stream_response("text")
            ocr_service.process_ocr(request, events)
        self.assertEqual(
            client_cls.return_value.chat.call_args.kwargs["options"],
            {"num_gpu": -1, "main_gpu": 1},
        )

    def test_pdf_pipeline_order_join_and_no_disk_residue(self):
        request = self.make_request("doc.pdf", dpi=300)
        document = make_fake_document(3)
        result, error, events, created = self.run_pdf_pipeline(
            request,
            document,
            [stream_response("p1"), stream_response("p2"), stream_response("p3")],
        )
        self.assertIsNone(error)
        self.assertEqual(result, request.output_path)
        self.assertEqual(
            request.output_path.read_text(encoding="utf-8"), "p1\n\np2\n\np3"
        )
        # No render directory was created at all — nothing for a crash to leave.
        self.assertEqual(created, [])
        # Rendering is now interleaved with recognition rather than a phase
        # that completes first: page 2 is rendered only after page 1 is sent.
        logs = [payload for kind, payload in events if kind == "log"]
        self.assertEqual(logs[0], "[1/3] Preparing document...")
        renders = [i for i, m in enumerate(logs) if m.startswith("[1/3] Rendering")]
        sends = [i for i, m in enumerate(logs) if m.startswith("[2/3] Sending")]
        self.assertEqual(len(renders), 3)
        self.assertEqual(len(sends), 3)
        for render_index, send_index in zip(renders, sends):
            self.assertLess(render_index, send_index)
        self.assertLess(sends[0], renders[1], "page 2 rendered before page 1 sent")
        # Pages were sent in numeric order, as in-memory bytes.
        calls = self.client_cls.return_value.chat.call_args_list
        sent = [c.kwargs["messages"][1]["images"][0] for c in calls]
        self.assertEqual(
            sent,
            [ocr_service.ollama.Image(value=f"png-bytes-{n}".encode())
             for n in (1, 2, 3)],
        )

    def test_render_failure_no_output_and_no_residue(self):
        request = self.make_request("doc.pdf")
        document = make_fake_document(2)
        document.fake_pages[0].get_pixmap.side_effect = RuntimeError("render boom")
        result, error, _events, created = self.run_pdf_pipeline(
            request, document, []
        )
        self.assertIsNone(result)
        self.assertIsInstance(error, OCRServiceError)
        self.assertEqual(created, [])
        self.assertFalse(request.output_path.exists())

    def test_client_construction_failure_cleans_temp_dir(self):
        request = self.make_request("doc.pdf")
        document = make_fake_document(1)
        events = queue.Queue()
        created = []
        real_mkdtemp = tempfile.mkdtemp

        def spy_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created.append(path)
            return path

        with mock.patch.object(ocr_service, "pymupdf") as fake_pymupdf, \
                mock.patch.object(ocr_service.ollama, "Client") as client_cls, \
                mock.patch.object(
                    ocr_service.tempfile, "mkdtemp", side_effect=spy_mkdtemp
                ):
            fake_pymupdf.open.return_value = document
            client_cls.side_effect = ConnectionError("no route to host")
            with self.assertRaises(OCRServiceError) as ctx:
                ocr_service.process_ocr(request, events)
        self.assertIn("no route to host", str(ctx.exception))
        self.assertEqual(created, [])
        self.assertFalse(request.output_path.exists())

    def test_real_pdf_run_writes_only_the_output_file(self):
        """Integration: real PyMuPDF, real Pillow, only the client mocked.

        Snapshots the system temp directory around a full run. The output
        Markdown must be the only file this application creates — no page
        images anywhere, because a SIGKILL (crash, force-quit, OOM, flat
        battery) does not run cleanup code and would otherwise strand
        readable images of the document on disk.
        """
        sample = Path("examples/sample-5-page-pdf-a4-size.pdf")
        input_path = self.dir / "real.pdf"
        input_path.write_bytes(sample.read_bytes())
        request = OCRRequest(
            input_path=input_path,
            output_path=ocr_service.build_output_path(input_path),
            ollama_url=self.URL,
            model=self.MODEL,
            dpi=72,
        )
        system_temp = Path(tempfile.gettempdir())
        before = set(system_temp.iterdir())
        events = queue.Queue()
        with mock.patch.object(ocr_service.ollama, "Client") as client_cls:
            arm_version_probe(client_cls)
            client_cls.return_value.chat.side_effect = [
                stream_response(f"page {n}") for n in range(1, 6)
            ]
            result = ocr_service.process_ocr(request, events)
        new_temp_entries = set(system_temp.iterdir()) - before
        self.assertEqual(new_temp_entries, set(), "run left files in temp")
        self.assertEqual(result, request.output_path)
        self.assertEqual(
            sorted(q.name for q in self.dir.iterdir()),
            ["real.pdf", "real_extracted.md"],
        )
        self.assertEqual(
            request.output_path.read_text(encoding="utf-8"),
            "page 1\n\npage 2\n\npage 3\n\npage 4\n\npage 5",
        )
        # And the bytes actually sent were real PNGs rendered in memory.
        sent = [
            c.kwargs["messages"][1]["images"][0]
            for c in client_cls.return_value.chat.call_args_list
        ]
        self.assertEqual(len(sent), 5)
        for image in sent:
            self.assertTrue(image.value.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_late_page_failure_leaves_no_new_output(self):
        request = self.make_request("doc.pdf")
        document = make_fake_document(2)
        result, error, _events, created = self.run_pdf_pipeline(
            request,
            document,
            [stream_response("p1"), RuntimeError("model exploded")],
        )
        self.assertIsNone(result)
        self.assertIsInstance(error, OCRServiceError)
        self.assertFalse(request.output_path.exists())
        self.assertEqual(created, [])

    def test_late_page_failure_preserves_existing_output(self):
        request = self.make_request("doc.pdf")
        request.output_path.write_text("previous run", encoding="utf-8")
        document = make_fake_document(2)
        result, error, _events, created = self.run_pdf_pipeline(
            request,
            document,
            [stream_response("p1"), RuntimeError("model exploded")],
        )
        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertEqual(
            request.output_path.read_text(encoding="utf-8"), "previous run"
        )
        self.assertEqual(created, [])

    def test_save_failure_preserves_existing_output(self):
        request = self.make_request("doc.pdf")
        request.output_path.write_text("previous run", encoding="utf-8")
        document = make_fake_document(1)
        result, error, _events, created = self.run_pdf_pipeline(
            request,
            document,
            [stream_response("p1")],
            replace_error=OSError("disk full"),
        )
        self.assertIsNone(result)
        self.assertIsInstance(error, OCRServiceError)
        self.assertEqual(
            request.output_path.read_text(encoding="utf-8"), "previous run"
        )
        self.assertEqual(created, [])
        # No stray temp output file remains next to the output either.
        leftovers = [p for p in self.dir.iterdir() if p.suffix == ".tmp"]
        self.assertEqual(leftovers, [])


    def test_page_text_events_through_pipeline(self):
        """process_ocr emits page_text events with assembled text per page."""
        request = self.make_request("doc.pdf")
        document = make_fake_document(2)
        _result, _error, events, _created = self.run_pdf_pipeline(
            request,
            document,
            [stream_response("  page one ", "text"), stream_response("two")],
        )
        page_events = [
            (kind, payload) for kind, payload in events if kind == "page_text"
        ]
        self.assertEqual(
            page_events,
            [
                ("page_text", {"page": 1, "total": 2, "text": "page one text"}),
                ("page_text", {"page": 2, "total": 2, "text": "two"}),
            ],
        )

    def test_stream_chunk_events_through_pipeline(self):
        """process_ocr emits stream_chunk events for each delta."""
        request = self.make_request("photo.png")
        events = queue.Queue()
        with mock.patch.object(ocr_service.ollama, "Client") as client_cls:
            arm_version_probe(client_cls)
            client_cls.return_value.chat.return_value = stream_response("A", "B", "C")
            ocr_service.process_ocr(request, events)
        chunk_events = [
            (kind, payload) for kind, payload in drain(events) if kind == "stream_chunk"
        ]
        self.assertEqual(
            chunk_events,
            [
                ("stream_chunk", {"page": 1, "text": "A"}),
                ("stream_chunk", {"page": 1, "text": "B"}),
                ("stream_chunk", {"page": 1, "text": "C"}),
            ],
        )

    def test_mid_stream_generator_exception_leaves_no_residue(self):
        """An exception while iterating a stream cleans the temp dir."""

        def exploding_stream():
            yield SimpleNamespace(message=SimpleNamespace(content="partial"))
            raise RuntimeError("connection dropped")

        request = self.make_request("doc.pdf")
        document = make_fake_document(2)
        result, error, _events, created = self.run_pdf_pipeline(
            request,
            document,
            [exploding_stream(), stream_response("p2")],
        )
        self.assertIsNone(result)
        self.assertIsInstance(error, OCRServiceError)
        self.assertIn("page 1/2", str(error))
        self.assertIn("connection dropped", str(error))
        self.assertEqual(created, [])
        self.assertFalse(request.output_path.exists())

    def test_empty_stream_through_pipeline_fails(self):
        """A stream with zero chunks triggers 'returned no text'."""
        request = self.make_request("photo.png")
        events = queue.Queue()
        with mock.patch.object(ocr_service.ollama, "Client") as client_cls:
            arm_version_probe(client_cls)
            client_cls.return_value.chat.return_value = stream_response()
            error = None
            try:
                ocr_service.process_ocr(request, events)
            except Exception as exc:
                error = exc
        self.assertIsInstance(error, OCRServiceError)
        self.assertIn("returned no text", str(error))
        self.assertIn("page 1/1", str(error))
        self.assertFalse(request.output_path.exists())


if __name__ == "__main__":
    unittest.main()
