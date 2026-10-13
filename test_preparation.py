import asyncio
import gc
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import app as app_module
from audio_chunks import open_recording
from karaoke import KaraokeTimeline
from narration import NarrationEngine
from streaming_audio import LivePlayback
from test_chunks import FakePlayer
from test_streaming import FIXTURE, wait_for


class FakeService:
    calls = []
    release = threading.Event()

    def __init__(self, text, voice, **kwargs):
        self.calls.append((text, voice))
        self.text = text

    async def stream(self):
        raw = FIXTURE.read_bytes()
        yield {'type': 'audio', 'data': raw[:20000]}
        offset = 1_000_000
        for word in self.text.split():
            yield {'type': 'WordBoundary', 'text': word, 'offset': offset, 'duration': 4_000_000}
            offset += 5_000_000
        while not self.release.is_set():
            await asyncio.sleep(.01)
        yield {'type': 'audio', 'data': raw[20000:]}


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.events = queue.Queue()
        self.player = FakePlayer()
        with patch('narration.win32com.client.Dispatch', return_value=self.player):
            self.engine = NarrationEngine(self.events, self.root)
        self.play_spy = patch.object(self.player.controls, 'play', wraps=self.player.controls.play).start()
        self.live_spy = patch.object(LivePlayback, 'start').start()
        self.service_patch = patch('narration.edge_tts.Communicate', FakeService)
        self.service_patch.start()
        FakeService.calls = []
        FakeService.release.clear()

    def tearDown(self):
        self.engine.close()
        self.service_patch.stop()
        patch.stopall()
        self.temp.cleanup()

    def complete(self):
        FakeService.release.set()
        deadline = time.monotonic() + 3
        while not self.engine.recording:
            self.engine.handle_event(self.events.get(timeout=max(.01, deadline - time.monotonic())))
        self.engine.poll()

    def test_preparation_is_silent_and_read_joins_the_same_stream(self):
        job = self.engine.request('First second third', 'edge:voice', autoplay=False)
        wait_for(lambda: self.engine.live.spool.frames > 0)
        self.engine.poll()
        self.assertEqual(self.engine.state, 'preparing')
        self.assertEqual(self.engine.position(), 0)
        self.live_spy.assert_not_called()
        self.play_spy.assert_not_called()
        self.assertEqual(self.engine.request('First second third', 'edge:voice'), job)
        self.assertEqual(len(FakeService.calls), 1)
        self.live_spy.assert_called_once()
        self.complete()
        self.assertEqual(self.engine.karaoke.mode, 'exact')
        self.assertEqual(len(self.engine.karaoke.cues), 3)
        self.assertEqual(len(FakeService.calls), 1)

    def test_completed_background_and_cache_stay_silent_until_read(self):
        text = 'First second third'
        self.engine.request(text, 'edge:voice', autoplay=False)
        self.complete()
        self.assertEqual(self.engine.state, 'ready')
        self.assertIsNone(self.engine.live)
        self.live_spy.assert_not_called()
        self.play_spy.assert_not_called()
        metadata = json.loads(self.engine.recording.with_suffix('.json').read_text(encoding='utf-8'))
        self.assertEqual(len(metadata['words']), 3)
        self.engine.stop()
        self.engine.request(text, 'edge:voice', autoplay=False)
        self.engine.handle_event(self.events.get(timeout=1))
        self.assertEqual(self.engine.karaoke.mode, 'exact')
        self.engine.seek(.7)
        self.engine.set_rate(1.5)
        self.play_spy.assert_not_called()
        self.engine.request(text, 'edge:voice')
        self.play_spy.assert_called_once()
        self.engine.poll()
        self.assertAlmostEqual(self.engine.position(), .7)
        self.assertEqual(self.player.settings.rate, 1.5)
        self.assertEqual(len(FakeService.calls), 1)

    def test_offline_fragments_wait_for_read(self):
        def synth(text, voice, raw, cancel):
            with open_recording(raw) as audio:
                audio.writeframes(b'\x01\x00' * 2400)
        self.engine._synthesize = synth
        self.engine.request('A short sentence.', 'test', autoplay=False)
        deadline = time.monotonic() + 3
        while not self.engine.recording:
            self.engine.handle_event(self.events.get(timeout=max(.01, deadline - time.monotonic())))
        self.assertEqual(self.engine.state, 'ready')
        self.play_spy.assert_not_called()
        self.assertEqual(self.engine.karaoke.mode, 'estimated')
        self.engine.request('A short sentence.', 'test')
        self.play_spy.assert_called_once()

    def test_replaced_preparation_ignores_old_events(self):
        old = self.engine.request('Old text', 'edge:voice', autoplay=False)
        wait_for(lambda: self.engine.live.spool.frames > 0)
        current = self.engine.request('New text', 'edge:other', autoplay=False)
        self.engine.handle_event({'event': 'complete', 'job_id': old, 'path': 'old.wav', 'duration': 3})
        self.engine.handle_event({'event': 'word', 'job_id': old,
                                 'word': {'time': .1, 'duration': 1, 'text': 'Old'}})
        self.assertNotEqual(old, current)
        self.assertIsNone(self.engine.recording)
        self.assertEqual(self.engine.karaoke.cues, [])
        self.assertEqual(self.engine.speech_text, 'New text')
        self.live_spy.assert_not_called()


class KaraokeTests(unittest.TestCase):
    def test_word_mapping_repeat_punctuation_and_backward_seek(self):
        text = 'Да, да! Это ссылка. Яркий день.'
        timeline = KaraokeTimeline(text)
        for index, word in enumerate(('Да', 'да', 'Это', 'ссылка', 'Яркий', 'день')):
            timeline.add({'time': index * .5 + .1, 'duration': .3, 'text': word})
        self.assertEqual(len(timeline.cues), 6)
        self.assertIsNone(timeline.at(0))
        self.assertIsNone(timeline.at(.45))
        self.assertEqual(timeline.spoken_end(.45), 2)
        self.assertEqual(timeline.spoken_end(0), 0)
        self.assertEqual(text[timeline.at(.7)['start']:timeline.at(.7)['end']], 'да')
        self.assertEqual(text[timeline.at(2.2)['start']:timeline.at(2.2)['end']], 'Яркий')
        self.assertEqual(text[timeline.at(.2)['start']:timeline.at(.2)['end']], 'Да')
        self.assertIsNone(timeline.at(9))


class AppPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patches = [
            patch.object(app_module, 'DATA_DIR', root),
            patch.object(app_module, 'DATA_FILE', root / 'notes.json'),
            patch('narration.win32com.client.Dispatch', return_value=FakePlayer()),
            patch('narration.edge_tts.Communicate', FakeService),
            patch.object(LivePlayback, 'start'),
        ]
        for item in self.patches:
            item.start()
        FakeService.calls = []
        FakeService.release.clear()
        self.app = app_module.TellMeApp()
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        # Tcl timers belong to the shared interpreter and must not leak to the next test.
        for timer in self.app.tk.call('after', 'info'):
            self.app.after_cancel(timer)
        self.app._close()
        self.app = None
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()
        gc.collect()

    def pump_until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.app.update()
            if time.monotonic() >= deadline:
                self.fail('UI timed out: ' + self.app.engine.state)
            time.sleep(.005)

    def test_edit_prepares_silently_and_read_shows_karaoke_without_restarting(self):
        editor = self.app.editors[self.app._active_id()]
        editor.insert('1.0', 'First second third')
        self.pump_until(lambda: self.app.engine.live is not None and self.app.engine.live.spool.frames > 0)
        self.assertTrue(self.app.engine.background)
        self.assertIsNone(self.app.engine.live.device)
        self.assertEqual(self.app.engine.position(), 0)
        self.assertEqual(str(self.app.pause_button.cget('state')), 'disabled')
        FakeService.release.set()
        self.pump_until(lambda: self.app.engine.state == 'ready')
        job = self.app.engine.generation
        self.app._play()
        self.assertEqual(self.app.engine.generation, job)
        self.assertEqual(len(FakeService.calls), 1)
        self.assertTrue(self.app.karaoke_var.get())
        self.assertEqual(self.app.karaoke_frame.winfo_manager(), 'place')
        self.app.engine.poll()
        self.app.engine.player.controls.currentPosition = .7
        self.app._refresh_player()
        ranges = self.app.karaoke_text.tag_ranges('current')
        self.assertEqual(self.app.karaoke_text.get(*ranges), 'second')
        self.app.engine.seek(.2)
        self.app._refresh_player()
        self.assertEqual(self.app.karaoke_text.get(*self.app.karaoke_text.tag_ranges('current')), 'First')

    def test_typing_is_coalesced_stop_and_disabled_auto_do_not_restart(self):
        editor = self.app.editors[self.app._active_id()]
        for letter in 'Typed text':
            editor.insert('end', letter)
            self.app.update()
        self.assertEqual(FakeService.calls, [])
        self.pump_until(lambda: bool(FakeService.calls))
        self.assertEqual(len(FakeService.calls), 1)
        self.app._stop()
        self.app.auto_prepare_var.set(False)
        self.app._on_auto_prepare()
        editor.insert('end', ' changed')
        self.app.update()
        self.assertIsNone(self.app.prepare_job)
        self.assertEqual(self.app.engine.state, 'stopped')
        self.app._save()
        saved = json.loads(app_module.DATA_FILE.read_text(encoding='utf-8'))
        self.assertFalse(saved['auto_prepare'])

    def test_tab_change_cancels_the_previous_preparation(self):
        self.app.editors[self.app._active_id()].insert('1.0', 'First text')
        self.pump_until(lambda: bool(FakeService.calls))
        job = self.app.engine.generation
        self.app._add_note()
        self.app.update()
        self.assertGreater(self.app.engine.generation, job)
        self.assertEqual(self.app.engine.state, 'stopped')
        self.app.editors[self.app._active_id()].insert('1.0', 'Second text')
        self.pump_until(lambda: len(FakeService.calls) == 2)
        self.assertEqual(FakeService.calls[-1][0], 'Second text')

    def test_paste_prepares_immediately_and_clear_stops_with_auto_disabled(self):
        with patch.object(self.app, 'clipboard_get', return_value='First second third'):
            self.app._paste_note()
        self.app.update()
        self.assertIsNone(self.app.prepare_job)
        self.pump_until(lambda: bool(FakeService.calls))
        self.assertEqual(len(FakeService.calls), 1)
        self.app.auto_prepare_var.set(False)
        self.app._clear_note()
        self.app.update()
        self.assertEqual(self.app.engine.state, 'stopped')
        self.assertIsNone(self.app.engine.recording)
        self.assertEqual(self.app.engine.speech_text, '')
        self.assertIsNone(self.app.prepare_job)


if __name__ == '__main__':
    unittest.main()
