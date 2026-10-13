"""Manual Windows integration check: real service/device, temporary recordings only."""
import queue
from pathlib import Path
import tempfile
import time
import pythoncom

from audio_chunks import wave_duration
from narration import NarrationEngine


def verify():
    text = ('Это проверка потокового чтения. Программа получает звук небольшими частями '
            'и начинает воспроизведение до завершения загрузки всей записи. '
            'Во время паузы подготовка продолжается. Затем запись сохраняется на компьютере.')
    with tempfile.TemporaryDirectory(prefix='tellme_verify_') as directory:
        events = queue.Queue()
        engine = NarrationEngine(events, Path(directory) / 'audio')
        engine.set_volume(0)
        started = time.monotonic()
        first = None
        try:
            engine.request(text, 'edge:ru-RU-DmitryNeural')
            deadline = started + 90
            pause_checked = False
            while time.monotonic() < deadline and not engine.recording:
                pythoncom.PumpWaitingMessages()
                while not events.empty():
                    engine.handle_event(events.get_nowait())
                engine.poll()
                if engine.state == 'error':
                    raise RuntimeError(engine.error)
                if engine.position() > 0 and first is None:
                    first = time.monotonic() - started
                    print(f'First playback: {first:.3f}s', flush=True)
                if first and not pause_checked and not engine.recording:
                    engine.pause()
                    position = engine.position()
                    before = engine.live.spool.frames
                    time.sleep(.3)
                    assert engine.position() == position, 'Pause consumed audio'
                    assert engine.live.spool.frames >= before
                    engine.resume()
                    pause_checked = True
                time.sleep(.01)
            assert engine.recording, 'Recording did not complete'
            complete = time.monotonic() - started
            assert first is not None and first < complete
            assert wave_duration(engine.recording) == engine.duration
            assert engine.karaoke.mode == 'exact', engine.karaoke.mode
            assert len(engine.karaoke.cues) > 15, len(engine.karaoke.cues)
            print(f'Complete: {complete:.3f}s; audio: {engine.duration:.3f}s', flush=True)
            assert engine.live is not None, 'Unnecessary player switch at 1x'
            engine.pause()
            position = engine.position()
            engine.set_rate(1.4)
            for _ in range(200):
                pythoncom.PumpWaitingMessages()
                engine.poll()
                if not engine.pending_load:
                    break
                time.sleep(.02)
            assert engine.state == 'paused', engine.state
            assert abs(engine.position() - position) < .1
            engine.seek(2)
            engine.resume()
            deadline = time.monotonic() + 3
            while engine.position() <= 2.2 and time.monotonic() < deadline:
                pythoncom.PumpWaitingMessages()
                engine.poll()
                time.sleep(.02)
            assert engine.position() > 2.2, engine.position()
            engine.request(text, 'edge:ru-RU-DmitryNeural')
            cached = events.get(timeout=1)
            assert cached.get('cached'), cached
            engine.handle_event(cached)
            assert engine.karaoke.mode == 'exact', 'Cached word timings were lost'
            print('PASS: early playback, pause, WAV, speed, seek, cache, karaoke words', flush=True)
        finally:
            engine.close()


if __name__ == '__main__':
    verify()
