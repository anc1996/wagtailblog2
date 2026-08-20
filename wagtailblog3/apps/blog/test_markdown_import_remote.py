import socket
import tempfile
from pathlib import Path

from django.test import SimpleTestCase
from PIL import Image

from blog.services.markdown_import_remote import (
    RemoteFetchResponse,
    RemoteImageDownloadError,
    RemoteImagePolicy,
    download_remote_image,
    normalize_remote_image_url,
    resolve_public_addresses,
)


def _address_info(address: str, port: int = 443):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]


class MarkdownImportRemoteTests(SimpleTestCase):
    def test_normalizes_only_safe_url_components(self):
        self.assertEqual(
            normalize_remote_image_url(
                "HTTPS://CDN.Example.COM:443/images/pic.png?size=2#preview"
            ),
            "https://cdn.example.com/images/pic.png?size=2",
        )

    def test_rejects_http_credentials_and_non_https_schemes(self):
        for url in (
            "http://example.com/pic.png",
            "https://user:secret@example.com/pic.png",
            "file:///tmp/pic.png",
            "data:image/png;base64,AA==",
        ):
            with self.subTest(url=url):
                with self.assertRaises(RemoteImageDownloadError):
                    normalize_remote_image_url(url)

    def test_resolver_rejects_private_metadata_and_mixed_answers(self):
        for address in ("127.0.0.1", "10.0.0.2", "169.254.169.254", "::1"):
            with self.subTest(address=address):
                with self.assertRaisesMessage(
                    RemoteImageDownloadError, "remote_address_forbidden"
                ):
                    resolve_public_addresses(
                        "images.example.com",
                        443,
                        resolver=lambda *args, address=address: _address_info(address),
                    )

        def mixed_resolver(*args):
            return _address_info("93.184.216.34") + _address_info("192.168.1.10")

        with self.assertRaisesMessage(
            RemoteImageDownloadError, "remote_address_forbidden"
        ):
            resolve_public_addresses(
                "images.example.com", 443, resolver=mixed_resolver
            )

    def test_redirect_is_revalidated_before_private_target_is_fetched(self):
        fetched: list[tuple[str, str]] = []

        def resolver(host: str, *args):
            address = "93.184.216.34" if host == "public.example" else "10.0.0.8"
            return _address_info(address)

        def fetcher(url: str, address: str, timeout: float):
            fetched.append((url, address))
            return RemoteFetchResponse(
                status=302, headers={"location": "https://private.example/a.png"}, chunks=()
            )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesMessage(
                RemoteImageDownloadError, "remote_address_forbidden"
            ):
                download_remote_image(
                    "https://public.example/a.png",
                    Path(directory),
                    allow_external_images=True,
                    resolver=resolver,
                    fetcher=fetcher,
                )

        self.assertEqual(len(fetched), 1)
        self.assertEqual(fetched[0][1], "93.184.216.34")

    def test_requires_explicit_consent_and_removes_partial_download(self):
        calls = 0

        def resolver(*args):
            return _address_info("93.184.216.34")

        def fetcher(url: str, address: str, timeout: float):
            nonlocal calls
            calls += 1
            return RemoteFetchResponse(
                status=200, headers={}, chunks=(b"1234", b"5678")
            )

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            with self.assertRaisesMessage(
                RemoteImageDownloadError, "external_images_not_allowed"
            ):
                download_remote_image(
                    "https://images.example.com/a.png",
                    target,
                    allow_external_images=False,
                    resolver=resolver,
                    fetcher=fetcher,
                )
            self.assertEqual(calls, 0)

            with self.assertRaisesMessage(
                RemoteImageDownloadError, "remote_response_too_large"
            ):
                download_remote_image(
                    "https://images.example.com/a.png",
                    target,
                    allow_external_images=True,
                    policy=RemoteImagePolicy(max_bytes=6),
                    resolver=resolver,
                    fetcher=fetcher,
                )
            self.assertEqual(list(target.iterdir()), [])

    def test_valid_image_is_decoded_and_given_server_safe_name(self):
        with tempfile.TemporaryDirectory() as source_directory:
            source = Path(source_directory) / "source.png"
            Image.new("RGB", (3, 2), (10, 20, 30)).save(source)
            payload = source.read_bytes()

        def resolver(*args):
            return _address_info("93.184.216.34")

        def fetcher(url: str, address: str, timeout: float):
            return RemoteFetchResponse(
                status=200,
                headers={"content-length": str(len(payload))},
                chunks=(payload,),
            )

        with tempfile.TemporaryDirectory() as directory:
            downloaded = download_remote_image(
                "https://images.example.com/not-trusted.bin#fragment",
                Path(directory),
                allow_external_images=True,
                resolver=resolver,
                fetcher=fetcher,
            )

            self.assertTrue(downloaded.path.is_file())
            self.assertEqual(downloaded.safe_filename, "remote-image.png")
            self.assertEqual(downloaded.image_format, "PNG")
            self.assertEqual(downloaded.width, 3)
            self.assertEqual(downloaded.height, 2)
            self.assertEqual(
                downloaded.source_url,
                "https://images.example.com/not-trusted.bin",
            )
