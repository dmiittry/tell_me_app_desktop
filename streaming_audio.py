"""Incremental MP3 decoding, disk-backed PCM storage, and bounded live playback."""
from __future__ import annotations

import array
from contextlib import suppress
from pathlib import Path
import tempfile
import threading
import uuid

import av
import miniaudio

from audio_chunks import SAMPLE_RATE, open_recording


class StreamCancelled(Exception):
    pass


class IncomingAudio:
    """A short read is available data; an empty read means genuine end of stream."""

    def __init__(self, cancel: threading.Event):
        self.cancel = cancel
        self.condition = threading.Condition()
        self.buffer = bytearray()
        self.finished = False
        self.aborted = False
        self.received = 0
        self.limit = 256 * 1024

    def feed(self, data: bytes) -> None:
        # Split even unusually large packets, keeping memory bounded.
        for offset in range(0, len(data), self.limit):
            part = data[offset:offset + self.limit]
            with self.condition:
                while len(self.buffer) + len(part) > self.limit:
                    self._check_cancel()
                    self.condition.wait(.05)
                self._check_cancel()
                self.buffer.extend(part)
                self.received += len(part)
                self.condition.notify_all()

    def _check_cancel(self) -> None:
        if self.cancel.is_set() or self.aborted:
            raise StreamCancelled()

    def read(self, num_bytes: int) -> bytes:
        with self.condition:
            while not self.buffer and not self.finished:
                self._check_cancel()
                self.condition.wait(.05)
            self._check_cancel()
            result = bytes(self.buffer[:num_bytes])
            del self.buffer[:len(result)]
            self.condition.notify_all()
            return result

    def finish(self) -> None:
        with self.condition:
            self.finished = True
            self.condition.notify_all()

    def abort(self) -> None:
        with self.condition:
            self.aborted = True
            self.condition.notify_all()


class PCMSpool:
    """Keep long recordings on disk. Network/decoder never wait for the listener."""

    def __init__(self, directory: str | Path):
        self.condition = threading.Condition()
        self.file = tempfile.TemporaryFile(dir=directory, prefix='pcm_', suffix='.part')
        self.frames = 0
        self.finished = False
        self.closed = False

    def append(self, pcm: bytes) -> None:
        if len(pcm) % 2:
            raise ValueError('Incomplete PCM sample')
        with self.condition:
            if self.closed:
                raise StreamCancelled()
            self.file.seek(0, 2)
            self.file.write(pcm)
            self.frames += len(pcm) // 2
            self.condition.notify_all()

    def read(self, offset: int, count: int) -> bytes:
        with self.condition:
            if self.closed:
                raise StreamCancelled()
            self.file.seek(offset * 2)
            return self.file.read(max(0, min(count, self.frames - offset)) * 2)

    def finish(self) -> None:
        with self.condition:
            self.finished = True
            self.file.flush()
            self.condition.notify_all()

    def save(self, path: Path, cancel: threading.Event) -> float:
        if not self.finished or not self.frames:
            raise ValueError('Сервис не прислал звук.')
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + '.' + uuid.uuid4().hex + '.part')
        try:
            with open_recording(partial) as output:
                for offset in range(0, self.frames, 65536):
                    if cancel.is_set():
                        raise StreamCancelled()
                    output.writeframesraw(self.read(offset, 65536))
            if cancel.is_set():
                raise StreamCancelled()
            partial.replace(path)
            return self.frames / SAMPLE_RATE
        finally:
            partial.unlink(missing_ok=True)

    def close(self) -> None:
        with self.condition:
            self.closed = True
            self.file.close()
            self.condition.notify_all()


def decode_incoming(source: IncomingAudio, spool: PCMSpool) -> None:
    # A packet parser retains incomplete MP3 frames. Unlike a file decoder it
    # neither probes the end nor waits for unrelated future bytes to fill reads.
    codec = av.CodecContext.create('mp3float', 'r')
    resampler = av.AudioResampler(format='s16', layout='mono', rate=SAMPLE_RATE)

    def append_frames(frames):
        for frame in frames:
            for pcm in resampler.resample(frame):
                # Planes can have aligned padding beyond their actual samples.
                spool.append(bytes(pcm.planes[0])[:pcm.samples * 2])

    while data := source.read(16384):
        for packet in codec.parse(data):
            append_frames(codec.decode(packet))
    for packet in codec.parse(b''):
        append_frames(codec.decode(packet))
    append_frames(codec.decode(None))
    for pcm in resampler.resample(None):
        spool.append(bytes(pcm.planes[0])[:pcm.samples * 2])
    spool.finish()


class LivePlayback:
    """The device callback only touches a small RAM buffer, never disk or network."""

    def __init__(self, spool: PCMSpool):
        self.spool = spool
        self.lock = threading.Condition()
        self.buffer = bytearray()
        self.max_frames = SAMPLE_RATE * 2
        self.start_frames = int(SAMPLE_RATE * .15)
        self.consumed = 0
        self.paused = False
        self.volume = 100
        self.buffering = True
        self.eof = False
        self.error = ''
        self.closed = threading.Event()
        self.device = None
        self.feeder = threading.Thread(target=self._prefetch, daemon=True)
        self.feeder.start()

    def start(self) -> None:
        self.device = miniaudio.PlaybackDevice(nchannels=1, sample_rate=SAMPLE_RATE,
            output_format=miniaudio.SampleFormat.SIGNED16, buffersize_msec=40,
            backends=[miniaudio.Backend.WASAPI, miniaudio.Backend.DSOUND,
                      miniaudio.Backend.WINMM])
        callback = self._callback()
        next(callback)
        self.device.start(callback)

    def _prefetch(self) -> None:
        offset = 0
        try:
            while not self.closed.is_set():
                with self.lock:
                    capacity = self.max_frames - len(self.buffer) // 2
                    if capacity <= 0:
                        self.lock.wait(.05)
                        continue
                with self.spool.condition:
                    if offset >= self.spool.frames and not self.spool.finished:
                        self.spool.condition.wait(.05)
                        continue
                    data = self.spool.read(offset, min(capacity, 4096))
                    end = self.spool.finished and offset + len(data) // 2 >= self.spool.frames
                with self.lock:
                    self.buffer.extend(data)
                    offset += len(data) // 2
                    self.eof = end
                if end:
                    return
        except StreamCancelled:
            pass
        except Exception as exc:
            self.error = str(exc)

    def take(self, frames: int) -> bytes:
        """Silence during starvation does not consume or discard speech samples."""
        with self.lock:
            if self.closed.is_set() or self.paused:
                return bytes(frames * 2)
            if self.buffering and len(self.buffer) // 2 < self.start_frames and not self.eof:
                return bytes(frames * 2)
            count = min(frames, len(self.buffer) // 2)
            data = bytes(self.buffer[:count * 2])
            del self.buffer[:count * 2]
            self.consumed += count
            self.buffering = count < frames
            volume = self.volume
            self.lock.notify_all()
        if volume != 100:
            samples = array.array('h')
            samples.frombytes(data)
            data = array.array('h', (round(value * volume / 100) for value in samples)).tobytes()
        return data + bytes((frames - count) * 2)

    def _callback(self):
        frames = yield b''
        while not self.closed.is_set():
            try:
                data = self.take(frames)
            except Exception as exc:
                self.error = str(exc)
                data = bytes(frames * 2)
            frames = yield data

    @property
    def position(self) -> float:
        with self.lock:
            return self.consumed / SAMPLE_RATE

    @property
    def done(self) -> bool:
        with self.lock:
            return self.eof and not self.buffer

    def close(self) -> None:
        self.closed.set()
        with self.lock:
            self.lock.notify_all()
        with self.spool.condition:
            self.spool.condition.notify_all()
        if self.device:
            with suppress(Exception):
                self.device.close()
            self.device = None
        self.feeder.join(timeout=1)
        self.spool.close()
