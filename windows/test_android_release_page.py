import io
import unittest
import urllib.error
from unittest.mock import Mock, patch

import segno

from desktop_app import DesktopApi
from update_check import ANDROID_FALLBACK_PAGE, ANDROID_MANIFEST, ANDROID_RELEASES, android_release_page


class AndroidReleasePageTests(unittest.TestCase):
    def test_valid_manifest_opens_versioned_github_release(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(b"16 0.5.5\n")
        with patch("update_check.urllib.request.build_opener", return_value=opener), \
                patch("desktop_app.os.startfile") as open_page:
            api = DesktopApi(Mock())
            guide = api.get_android_download_guide()
            self.assertEqual(api.open_android_download_page(), {"opened": True})
        opener.open.assert_called_once_with(ANDROID_MANIFEST, timeout=5)
        page = f"{ANDROID_RELEASES}/tag/android-v0.5.5"
        open_page.assert_called_once_with(page)
        self.assertEqual(guide["qr"], segno.make_qr(page, error="M").png_data_uri(scale=5, border=4))

    def test_missing_stable_channel_uses_the_published_fallback(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(ANDROID_MANIFEST, 404, "Not Found", {}, None)
        with patch("update_check.urllib.request.build_opener", return_value=opener), \
                patch("desktop_app.os.startfile") as open_page:
            api = DesktopApi(Mock())
            self.assertEqual(api.open_android_download_page(), {"opened": True})
        open_page.assert_called_once_with(ANDROID_FALLBACK_PAGE)

    def test_unavailable_or_invalid_stable_channel_still_returns_fallback_qr(self):
        for error in (TimeoutError(), ValueError("invalid manifest")):
            with self.subTest(error=error), patch("desktop_app.android_release_page", side_effect=error):
                guide = DesktopApi(Mock()).get_android_download_guide()
            expected = segno.make_qr(ANDROID_FALLBACK_PAGE, error="M").png_data_uri(scale=5, border=4)
            self.assertEqual(guide["qr"], expected)

    def test_rejects_invalid_or_oversized_manifest(self):
        for raw in (b"16 0.5.5 https://example.com", b"16 0.5.5-beta", b"2147483648 0.5.5", b" " * 256):
            with self.subTest(raw=raw):
                opener = Mock()
                opener.open.return_value = io.BytesIO(raw)
                with patch("update_check.urllib.request.build_opener", return_value=opener):
                    with self.assertRaises(ValueError):
                        android_release_page()
