import tempfile
import unittest
import io
import struct
from pathlib import Path

from figma_understanding.download import DownloadError, StreamResponse, download_asset
from tests.helpers import png, stream


def webp(chunk_type, payload):
    return (
        b"RIFF"
        + (12 + len(payload)).to_bytes(4, "little")
        + b"WEBP"
        + chunk_type
        + len(payload).to_bytes(4, "little")
        + payload
    )


class DownloadTests(unittest.TestCase):
    def test_downloads_atomically_with_hash_and_dimensions(self):
        body = png(2, 3)
        opener = lambda url: StreamResponse(200, {"Content-Type": "image/png"}, stream(body))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "asset.png"
            result = download_asset("https://images.figma.com/a.png", target, expected_type="image/png", opener=opener)
            self.assertEqual((2, 3), (result["width"], result["height"]))
            self.assertEqual(body, target.read_bytes())

    def test_rejects_unknown_host_before_opening(self):
        called = False
        def opener(url):
            nonlocal called
            called = True
            raise AssertionError
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(DownloadError):
            download_asset("https://evil.example/a.png", Path(directory) / "a", opener=opener)
        self.assertFalse(called)

    def test_revalidates_redirect_destination(self):
        def opener(url):
            return StreamResponse(302, {"Location": "https://evil.example/a.png"}, stream(b""))
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(DownloadError):
            download_asset("https://images.figma.com/a.png", Path(directory) / "a", opener=opener)

    def test_rejects_signature_mismatch_without_partial_file(self):
        opener = lambda url: StreamResponse(200, {"Content-Type": "image/png"}, stream(b"not an image"))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "a.png"
            with self.assertRaises(DownloadError):
                download_asset("https://images.figma.com/a.png", target, opener=opener)
            self.assertFalse(target.exists())

    def test_rejects_destination_escape(self):
        body = png()
        opener = lambda url: StreamResponse(200, {"Content-Type": "image/png"}, stream(body))
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            with self.assertRaisesRegex(DownloadError, "escapes"):
                download_asset(
                    "https://images.figma.com/a.png",
                    Path(outside) / "a.png",
                    allowed_root=Path(directory),
                    opener=opener,
                )

    def test_403_and_429_are_explicit(self):
        for status in (403, 429):
            opener = lambda url, status=status: StreamResponse(status, {}, stream(b""))
            with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(DownloadError, str(status)):
                download_asset("https://images.figma.com/a.png", Path(directory) / "a", opener=opener)

    def test_headers_are_case_insensitive_and_stream_is_closed(self):
        body = png()
        response_stream = io.BytesIO(body)
        opener = lambda url: StreamResponse(200, {"content-type": "image/png", "content-length": str(len(body))}, response_stream)
        with tempfile.TemporaryDirectory() as directory:
            download_asset("https://images.figma.com/a.png", Path(directory) / "a.png", opener=opener)
        self.assertTrue(response_stream.closed)

    def test_rejects_byte_and_pixel_limits_without_final_file(self):
        body = png(10, 10)
        opener = lambda url: StreamResponse(200, {"Content-Type": "image/png"}, stream(body))
        with tempfile.TemporaryDirectory() as directory:
            byte_target = Path(directory) / "bytes.png"
            with self.assertRaisesRegex(DownloadError, "byte limit"):
                download_asset("https://images.figma.com/a.png", byte_target, max_bytes=len(body) - 1, opener=opener)
            self.assertFalse(byte_target.exists())
        opener = lambda url: StreamResponse(200, {"Content-Type": "image/png"}, stream(body))
        with tempfile.TemporaryDirectory() as directory:
            pixel_target = Path(directory) / "pixels.png"
            with self.assertRaisesRegex(DownloadError, "dimensions"):
                download_asset("https://images.figma.com/a.png", pixel_target, max_pixels=99, opener=opener)
            self.assertFalse(pixel_target.exists())

    def test_rejects_truncated_image_with_valid_header(self):
        body = png()[:24]
        opener = lambda url: StreamResponse(200, {"Content-Type": "image/png"}, stream(body))
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(DownloadError, "incomplete|truncated"):
            download_asset("https://images.figma.com/a.png", Path(directory) / "a.png", opener=opener)

    def test_accepts_supported_original_formats_with_dimensions(self):
        bits = 1 | (2 << 14)  # 2x3 pixels, stored as dimension minus one.
        samples = {
            "gif": (b"GIF89a" + struct.pack("<HH", 2, 3) + b"\x00;", "image/gif"),
            "jpeg": (b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x03\x00\x02\xff\xd9", "image/jpeg"),
            "vp8x": (webp(b"VP8X", b"\x00" * 4 + (1).to_bytes(3, "little") + (2).to_bytes(3, "little")), "image/webp"),
            "vp8l": (webp(b"VP8L", b"\x2f" + bits.to_bytes(4, "little") + b"\x00"), "image/webp"),
            "vp8": (webp(b"VP8 ", b"\x00" * 3 + b"\x9d\x01\x2a" + struct.pack("<HH", 2, 3)), "image/webp"),
        }
        with tempfile.TemporaryDirectory() as directory:
            for name, (body, content_type) in samples.items():
                with self.subTest(name=name):
                    target = Path(directory) / name
                    opener = lambda url, body=body, content_type=content_type: StreamResponse(
                        200, {"Content-Type": content_type}, stream(body)
                    )
                    result = download_asset(
                        f"https://images.figma.com/{name}", target, opener=opener
                    )
                    self.assertEqual((2, 3), (result["width"], result["height"]))
                    self.assertEqual(body, target.read_bytes())

    def test_rejects_incomplete_supported_original_formats(self):
        bits = 1 | (2 << 14)
        gif = b"GIF89a" + struct.pack("<HH", 2, 3) + b"\x00;"
        jpeg = b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x03\x00\x02\xff\xd9"
        webps = [
            webp(b"VP8X", b"\x00" * 4 + (1).to_bytes(3, "little") + (2).to_bytes(3, "little")),
            webp(b"VP8L", b"\x2f" + bits.to_bytes(4, "little") + b"\x00"),
            webp(b"VP8 ", b"\x00" * 3 + b"\x9d\x01\x2a" + struct.pack("<HH", 2, 3)),
        ]
        samples = [
            ("gif", gif[:-1], "image/gif"),
            ("jpeg", jpeg[:-1], "image/jpeg"),
            *(
                (f"webp-{index}", body[:4] + (len(body) - 9).to_bytes(4, "little") + body[8:], "image/webp")
                for index, body in enumerate(webps)
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for name, body, content_type in samples:
                with self.subTest(name=name), self.assertRaises(DownloadError):
                    opener = lambda url, body=body, content_type=content_type: StreamResponse(
                        200, {"Content-Type": content_type}, stream(body)
                    )
                    download_asset(
                        f"https://images.figma.com/{name}",
                        Path(directory) / name,
                        opener=opener,
                    )


if __name__ == "__main__":
    unittest.main()

