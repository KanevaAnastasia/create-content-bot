import asyncio
from types import SimpleNamespace

from app import syntax_telethon_fallback as syntax


def test_download_resumes_saved_chunks_after_timeouts_and_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(syntax, '__file__', str(tmp_path / 'app' / 'syntax.py'))
    monkeypatch.setattr(syntax, '_syntax_media_download_timeout_sec', lambda: .02)
    payload = b'a' * 4096 + b'b' * 4096
    message = SimpleNamespace(id=12, document=SimpleNamespace(id=34, size=len(payload)), media=object())
    offsets = []
    healthy = False

    class Stream:
        def __init__(self, offset):
            self.offset = offset
        async def __anext__(self):
            if self.offset and not healthy:
                await asyncio.sleep(1)
            chunk = payload[self.offset:self.offset + 4096]
            self.offset += len(chunk)
            return chunk
        async def close(self):
            pass

    class Client:
        def iter_download(self, media, *, offset, request_size):
            offsets.append(offset)
            return Stream(offset)

    assert asyncio.run(syntax._download_telegram_media_bytes(Client(), message)) is None
    assert offsets == [0, 4096, 4096]
    healthy = True
    assert asyncio.run(syntax._download_telegram_media_bytes(Client(), message)) == payload
    assert offsets[-1] == 4096
    count = len(offsets)
    assert asyncio.run(syntax._download_telegram_media_bytes(Client(), message)) == payload
    assert len(offsets) == count


def test_active_download_can_exceed_total_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(syntax, '__file__', str(tmp_path / 'app' / 'syntax.py'))
    monkeypatch.setattr(syntax, '_syntax_media_download_timeout_sec', lambda: .1)
    payload = b'x' * (4096 * 4)
    message = SimpleNamespace(id=12, document=SimpleNamespace(id=35, size=len(payload)), media=object())

    class Stream:
        async def __anext__(self):
            await asyncio.sleep(.04)
            return payload[:4096]
        async def close(self):
            pass
    class Client:
        def iter_download(self, *args, **kwargs):
            return Stream()
    assert asyncio.run(syntax._download_telegram_media_bytes(Client(), message)) == payload
