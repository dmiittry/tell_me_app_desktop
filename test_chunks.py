import queue
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

from audio_chunks import append_chunk, open_recording, split_text, wave_duration
from narration import NarrationEngine


class FakeControls:
    def __init__(self, player):
        self.player = player
        self.currentPosition = 0.0

    def play(self):
        self.player.playState = 3

    def pause(self):
        self.player.playState = 2

    def stop(self):
        self.player.playState = 1
        self.currentPosition = 0.0


class FakePlayer:
    def __init__(self):
        self.settings = type('Settings', (), {})()
        self.controls = FakeControls(self)
        self.playState = 1
        self.openState = 13

    def close(self):
        pass


class ChunkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.events = queue.Queue()
        with patch('narration.win32com.client.Dispatch', return_value=FakePlayer()):
            self.engine = NarrationEngine(self.events, self.root / 'saved')

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def chunk(self, index, duration=2):
        self.engine.handle_event(dict(event='chunk', job_id=self.engine.generation,
            index=index, path=f'{index}.wav', duration=duration, total=3))

    def end_audio(self):
        self.engine.poll()
        self.engine.poll()
        self.engine.player.controls.stop()
        self.engine.poll()

    def test_split_keeps_every_word_in_order(self):
        text = 'Первая короткая фраза.\n' + ' '.join(f'слово{i}' for i in range(92)) + ' Конец!'
        chunks = split_text(text)
        self.assertEqual(' '.join(chunks).split(), text.split())
        self.assertTrue(all(len(c.split()) <= 28 and len(c) <= 240 for c in chunks))
        self.assertEqual(chunks[0], 'Первая короткая фраза.')
        self.assertEqual(split_text('  \n '), [])

    def test_join_has_exact_frame_order_and_duration(self):
        paths = []
        expected = b''
        for index in (1, 2, 3):
            samples = index.to_bytes(2, 'little') * (240 * index)
            path = self.root / f'{index}.wav'
            with open_recording(path) as audio:
                audio.writeframes(samples)
            paths.append(path)
            expected += samples
        target = self.root / 'merged.wav'
        with open_recording(target) as output:
            for path in paths:
                append_chunk(output, path)
        with wave.open(str(target), 'rb') as result:
            self.assertEqual(result.readframes(result.getnframes()), expected)
        self.assertAlmostEqual(wave_duration(target), .06)

    def test_sequential_playback_and_waiting(self):
        self.chunk(0)
        self.assertEqual(self.engine.index, 0)
        self.end_audio()
        self.assertEqual(self.engine.state, 'waiting')
        self.assertEqual(self.engine.position(), 2)
        self.chunk(1)
        self.assertEqual(self.engine.index, 1)
        self.assertEqual(self.engine.position(), 2)
        self.chunk(2)
        self.end_audio()
        self.assertEqual(self.engine.index, 2)
        self.assertEqual(self.engine.position(), 4)

    def test_complete_handoff_keeps_position_and_pause(self):
        self.chunk(0)
        self.end_audio()
        self.engine.pause()
        self.chunk(1)
        self.engine.poll()
        self.engine.player.controls.currentPosition = .7
        self.engine.handle_event(dict(event='complete', job_id=0, path='complete.wav', duration=6))
        self.assertEqual(self.engine.state, 'paused')
        self.assertAlmostEqual(self.engine.position(), 2.7)
        self.engine.poll()
        self.assertAlmostEqual(self.engine.player.controls.currentPosition, 2.7)
        self.engine.set_rate(1.5)
        self.assertEqual(self.engine.player.settings.rate, 1.5)

    def test_speed_is_locked_until_complete_and_stale_events_ignored(self):
        self.chunk(0)
        self.engine.set_rate(1.7)
        self.assertEqual(self.engine.player.settings.rate, 1.0)
        self.engine.stop()
        self.engine.handle_event(dict(event='complete', job_id=0, path='old.wav', duration=6))
        self.assertIsNone(self.engine.recording)
        self.assertEqual(self.engine.state, 'stopped')

    def test_render_publishes_chunks_then_saved_file_and_reuses_it(self):
        def synth(text, voice, raw, cancel):
            with open_recording(raw) as audio:
                audio.writeframes(b'\x01\x00' * 2400)
        self.engine._synthesize = synth
        text = ' '.join('слово' + str(i) for i in range(65))
        self.engine.request(text, 'test')
        events = []
        while not events or events[-1]['event'] != 'complete':
            events.append(self.events.get(timeout=5))
        self.assertEqual([e['index'] for e in events if e['event'] == 'chunk'], [0, 1, 2])
        path = Path(events[-1]['path'])
        self.assertAlmostEqual(wave_duration(path), .3)
        self.engine._synthesize = lambda *args: self.fail('Cached audio was rendered again')
        self.engine.request(text, 'test')
        cached = self.events.get(timeout=5)
        self.assertTrue(cached['cached'])
        self.assertEqual(Path(cached['path']), path)

    def test_cancel_does_not_publish_partial_recording(self):
        entered = threading.Event()
        def synth(text, voice, raw, cancel):
            entered.set()
            cancel.wait(3)
        self.engine._synthesize = synth
        self.engine.request('Отменяемая запись.', 'test')
        self.assertTrue(entered.wait(2))
        self.engine.stop()
        for worker in self.engine.workers:
            worker.join(2)
        self.assertTrue(self.events.empty())
        self.assertEqual(list(self.engine.audio_dir.glob('*')), [])


if __name__ == '__main__':
    unittest.main()
