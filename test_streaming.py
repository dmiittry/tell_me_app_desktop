import array
import asyncio
import io
from pathlib import Path
import queue
import random
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

import miniaudio
import av

from narration import NarrationEngine
from streaming_audio import IncomingAudio, LivePlayback, PCMSpool, StreamCancelled, decode_incoming
from test_chunks import FakePlayer


FIXTURE = Path(__file__).parent / 'tests_data' / 'speech.mp3'


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('Timed out')
        time.sleep(.005)


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_packet_boundaries_preserve_all_samples_and_decode_before_eof(self):
        original = FIXTURE.read_bytes()
        for max_packet, repeats in ((1, 1), (127, 1), (4096, 12)):
            with self.subTest(max_packet=max_packet):
                raw = original * repeats
                resampler = av.AudioResampler(format='s16', layout='mono', rate=24000)
                with av.open(io.BytesIO(raw), format='mp3') as audio:
                    expected = b''.join(bytes(pcm.planes[0])[:pcm.samples * 2]
                        for frame in audio.decode(audio=0) for pcm in resampler.resample(frame))
                expected += b''.join(bytes(pcm.planes[0])[:pcm.samples * 2]
                    for pcm in resampler.resample(None))
                self.assertEqual(len(expected), len(miniaudio.decode(raw, nchannels=1,
                    sample_rate=24000).samples) * 2)
                source = IncomingAudio(threading.Event())
                spool = PCMSpool(self.root)
                errors = []

                def decode():
                    try:
                        decode_incoming(source, spool)
                    except Exception as exc:
                        errors.append(exc)
                        source.abort()

                worker = threading.Thread(target=decode)
                worker.start()
                try:
                    source.feed(raw[:10000])
                    wait_for(lambda: spool.frames > 0 or errors)
                    self.assertFalse(source.finished)
                    self.assertTrue(worker.is_alive())
                    rng = random.Random(7)
                    offset = 10000
                    while offset < len(raw):
                        size = rng.randint(1, max_packet)
                        source.feed(raw[offset:offset + size])
                        offset += size
                    source.finish()
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual(spool.read(0, spool.frames), expected)
                    self.assertLessEqual(len(source.buffer), source.limit)
                finally:
                    source.abort()
                    worker.join(1)
                    spool.close()

    def test_starvation_pause_and_volume_never_skip_speech(self):
        spool = PCMSpool(self.root)
        live = LivePlayback(spool)
        try:
            self.assertEqual(live.take(200), bytes(400))
            self.assertEqual(live.position, 0)
            pcm = array.array('h', [1000] * 4800).tobytes()
            spool.append(pcm)
            wait_for(lambda: len(live.buffer) == len(pcm))
            live.paused = True
            self.assertEqual(live.take(200), bytes(400))
            self.assertEqual(live.position, 0)
            live.paused = False
            live.volume = 50
            self.assertEqual(live.take(4800), array.array('h', [500] * 4800).tobytes())
            position = live.position
            self.assertEqual(live.take(200), bytes(400))
            self.assertEqual(live.position, position)
            # A final buffer smaller than the prebuffer threshold must still play.
            final = array.array('h', [-1000] * 50).tobytes()
            spool.append(final)
            spool.finish()
            wait_for(lambda: live.eof)
            live.volume = 100
            self.assertEqual(live.take(100), final + bytes(100))
            self.assertTrue(live.done)
            self.assertAlmostEqual(live.position, 4850 / 24000)
        finally:
            live.close()

    def test_paused_long_recording_saves_without_filling_memory(self):
        spool = PCMSpool(self.root)
        live = LivePlayback(spool)
        try:
            live.paused = True
            pcm = b'\x05\x00' * (24000 * 20)
            spool.append(pcm)
            spool.finish()
            wait_for(lambda: len(live.buffer) >= live.max_frames * 2)
            self.assertLessEqual(len(live.buffer), live.max_frames * 2)
            self.assertEqual(live.position, 0)
            path = self.root / 'ready.wav'
            self.assertEqual(spool.save(path, threading.Event()), 20)
            with wave.open(str(path)) as audio:
                self.assertEqual(audio.readframes(audio.getnframes()), pcm)
            cancelled = threading.Event()
            cancelled.set()
            with self.assertRaises(StreamCancelled):
                spool.save(self.root / 'cancelled.wav', cancelled)
            self.assertFalse((self.root / 'cancelled.wav').exists())
            self.assertEqual(list(self.root.glob('cancelled*.part')), [])
        finally:
            live.close()

    def test_cancellation_unblocks_empty_read_and_full_producer(self):
        for full in (False, True):
            source = IncomingAudio(threading.Event())
            if full:
                source.feed(bytes(source.limit))
            errors = []

            def blocked():
                try:
                    source.feed(b'\x01') if full else source.read(1024)
                except StreamCancelled:
                    errors.append('cancelled')

            worker = threading.Thread(target=blocked)
            worker.start()
            source.cancel.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, ['cancelled'])

    def test_stream_completion_keeps_live_audio_then_seek_preserves_pause(self):
        with patch('narration.win32com.client.Dispatch', return_value=FakePlayer()):
            engine = NarrationEngine(queue.Queue(), self.root)
        try:
            spool = PCMSpool(self.root)
            engine.live = LivePlayback(spool)
            spool.append(b'\x01\x00' * (24000 * 5))
            spool.finish()
            wait_for(lambda: len(engine.live.buffer) >= 24000 * 2)
            engine.live.take(24000)
            live = engine.live
            engine.pause()
            engine.handle_event(dict(event='complete', job_id=engine.generation,
                path=str(self.root / 'saved.wav'), duration=5))
            self.assertIs(engine.live, live)
            self.assertEqual(engine.position(), 1)
            self.assertEqual(engine.state, 'paused')
            engine.seek(3)
            self.assertIsNone(engine.live)
            self.assertEqual(engine.position(), 3)
            engine.poll()
            self.assertEqual(engine.state, 'paused')
            engine.set_rate(1.5)
            self.assertEqual(engine.player.settings.rate, 1.5)
        finally:
            engine.close()

    def test_network_break_does_not_retry_or_publish_partial_audio(self):
        calls = []

        class BrokenService:
            def __init__(self, *args, **kwargs):
                calls.append(1)

            async def stream(self):
                yield {'type': 'audio', 'data': FIXTURE.read_bytes()[:20000]}
                await asyncio.sleep(.02)
                raise OSError('Connection lost')

        events = queue.Queue()
        with patch('narration.win32com.client.Dispatch', return_value=FakePlayer()):
            engine = NarrationEngine(events, self.root / 'saved')
        try:
            with patch('narration.edge_tts.Communicate', BrokenService), patch.object(LivePlayback, 'start'):
                engine.request('Тест', 'edge:voice')
                event = events.get(timeout=3)
                self.assertEqual(event['event'], 'error')
                engine.handle_event(event)
            self.assertEqual(calls, [1])
            self.assertEqual(engine.state, 'error')
            self.assertIsNone(engine.live)
            self.assertEqual(list(engine.audio_dir.glob('*.wav')), [])
        finally:
            engine.close()


if __name__ == '__main__':
    unittest.main()
