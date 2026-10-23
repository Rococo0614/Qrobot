from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from .models import ImageAttachment


class ImageDownloadError(RuntimeError):
    pass


_MIME_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


class DownloadedImages:
    def __init__(self, directory: tempfile.TemporaryDirectory[str], images: list[ImageAttachment]):
        self._directory = directory
        self.images = tuple(images)

    def close(self) -> None:
        self._directory.cleanup()

    def __enter__(self) -> DownloadedImages:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class ImageDownloader:
    def __init__(
        self,
        *,
        temp_root: str | Path,
        max_count: int = 4,
        max_bytes: int = 12 * 1024 * 1024,
        timeout_seconds: float = 20.0,
        allow_private_hosts: bool = False,
    ) -> None:
        self.temp_root = Path(temp_root)
        self.max_count = max_count
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds
        self.allow_private_hosts = allow_private_hosts

    async def download(self, urls: tuple[str, ...]) -> DownloadedImages:
        self.temp_root.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(prefix="qrobot-images-", dir=self.temp_root)
        images: list[ImageAttachment] = []
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds), follow_redirects=False
            ) as client:
                for index, url in enumerate(urls[: self.max_count], 1):
                    image = await self._download_one(client, url, Path(directory.name), index)
                    images.append(image)
        except Exception:
            directory.cleanup()
            raise
        return DownloadedImages(directory, images)

    async def _download_one(
        self, client: httpx.AsyncClient, url: str, directory: Path, index: int
    ) -> ImageAttachment:
        current = url
        content = b""
        content_type = ""
        for _ in range(4):
            await self._validate_url(current)
            try:
                async with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise ImageDownloadError("图片重定向缺少地址")
                        current = urljoin(current, location)
                        continue
                    if response.is_error:
                        raise ImageDownloadError(
                            f"图片服务器返回 HTTP {response.status_code}"
                        )
                    declared_size = response.headers.get("content-length")
                    if declared_size:
                        try:
                            if int(declared_size) > self.max_bytes:
                                raise ImageDownloadError("图片超过大小限制")
                        except ValueError:
                            pass
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise ImageDownloadError("图片超过大小限制")
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    content_type = response.headers.get("content-type", "")
                    break
            except httpx.TimeoutException as exc:
                raise ImageDownloadError("图片下载超时") from exc
            except httpx.HTTPError as exc:
                raise ImageDownloadError(f"图片下载失败: {exc}") from exc
        else:
            raise ImageDownloadError("图片重定向次数过多")
        mime = _detect_image_mime(content, content_type)
        if mime is None:
            raise ImageDownloadError("附件不是受支持的图片格式")
        suffix = _MIME_SUFFIXES[mime]
        target = directory / f"image-{index}{suffix}"
        target.write_bytes(content)
        os.chmod(target, 0o600)
        return ImageAttachment(path=target, mime_type=mime, name=target.name)

    async def _validate_url(self, value: str) -> None:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ImageDownloadError("只允许下载 HTTP(S) 图片 URL")
        if parsed.username or parsed.password:
            raise ImageDownloadError("图片 URL 不能包含登录凭据")
        if self.allow_private_hosts:
            return
        try:
            addresses = await asyncio.to_thread(_resolve_addresses, parsed.hostname)
        except OSError as exc:
            raise ImageDownloadError("无法解析图片服务器地址") from exc
        if not addresses:
            raise ImageDownloadError("无法解析图片服务器地址")
        for address in addresses:
            if not address.is_global:
                raise ImageDownloadError("出于安全考虑，默认不下载内网或本机图片 URL")


def _resolve_addresses(hostname: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    values: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM):
        values.add(ipaddress.ip_address(item[4][0]))
    return values


def _detect_image_mime(content: bytes, header: str) -> str | None:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"BM"):
        return "image/bmp"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    header_mime = header.split(";", 1)[0].strip().lower()
    # Never trust the header alone for arbitrary data. It only narrows a known signature.
    if header_mime in _MIME_SUFFIXES and len(content) >= 16:
        return None
    return None
