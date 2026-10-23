import pytest

from qrobot.images import ImageDownloader, ImageDownloadError, _detect_image_mime


def test_detect_image_signatures():
    assert _detect_image_mime(b"\x89PNG\r\n\x1a\n" + b"x" * 20, "") == "image/png"
    assert _detect_image_mime(b"not an image", "image/png") is None


async def test_private_image_url_is_rejected_before_request(tmp_path):
    downloader = ImageDownloader(temp_root=tmp_path, allow_private_hosts=False)
    with pytest.raises(ImageDownloadError, match="内网或本机"):
        await downloader.download(("http://127.0.0.1/image.png",))
