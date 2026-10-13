"""Streaming neural speech, offline synthesis, and persistent seekable recordings."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import uuid
import wave

import edge_tts
import miniaudio
import pythoncom
import win32com.client
from audio_chunks import SAMPLE_RATE, append_chunk, open_recording, split_text, wave_duration
from streaming_audio import IncomingAudio, LivePlayback, PCMSpool, StreamCancelled, decode_incoming
from karaoke import KaraokeTimeline

NEURAL_VOICES = [
    ('Дмитрий — естественный голос · интернет', 'edge:ru-RU-DmitryNeural'),
    ('Светлана — естественный голос · интернет', 'edge:ru-RU-SvetlanaNeural'),
]
AUDIO_DIR = Path(os.environ.get('APPDATA', str(Path.home()))) / 'TellMe' / 'audio'


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f'{seconds // 60:02d}:{seconds % 60:02d}'


class Cancelled(Exception):
    pass


class NarrationEngine:
    def __init__(self, events: queue.Queue[dict], audio_dir: Path | None = None):
        self.events = events
        self.player = win32com.client.Dispatch('WMPlayer.OCX')
        self.player.settings.autoStart = False
        self.player.settings.volume = 100
        self.player.settings.rate = 1.0
        self.audio_dir = audio_dir or AUDIO_DIR
        self.temp_dir = tempfile.TemporaryDirectory(prefix='tellme_chunks_', ignore_cleanup_errors=True)
        self.workers: list[threading.Thread] = []
        self.cancel = threading.Event()
        self.generation = 0
        self.rate = 1.0
        self.volume = 100
        self.duration = 0.0
        self.state = 'stopped'
        self.error = ''
        self.recording: Path | None = None
        self.chunks: list[dict] = []
        self.index = -1
        self.offset = 0.0
        self.total = 0
        self.rendered = 0
        self.local_duration = 0.0
        self.started = False
        self.paused = False
        self.pending_load: tuple[float, float] | None = None
        self.live: LivePlayback | None = None
        self.streaming = False
        self.background = False
        self.request_key: tuple[str, str] | None = None
        self.prepared_position = 0.0
        self.speech_text = ''
        self.karaoke = KaraokeTimeline()

    def voices(self) -> list[tuple[str, str]]:
        result = list(NEURAL_VOICES)
        try:
            voice = win32com.client.Dispatch('SAPI.SpVoice')
            tokens = voice.GetVoices()
            for index in range(tokens.Count):
                description = tokens.Item(index).GetDescription()
                result.append((f'{description} · без интернета', f'sapi:{description}'))
        except Exception:
            pass
        return result

    def request(self, text: str, voice_id: str, title: str = 'Заметка',
                autoplay: bool = True) -> int:
        key = (text, voice_id)
        if key == self.request_key and self.state not in ('stopped', 'error'):
            if not autoplay:
                return self.generation
            if self.background:
                self.start_prepared()
                return self.generation
        self.stop()
        self.cancel = threading.Event()
        self.request_key = key
        self.background = not autoplay
        self.prepared_position = 0.0
        self.speech_text = text
        self.karaoke = KaraokeTimeline(text)
        self.recording = None
        self.chunks = []
        self.index = -1
        self.offset = self.duration = self.local_duration = 0.0
        self.rendered = 0
        self.error = ''
        self.streaming = voice_id.startswith('edge:')
        parts = split_text(text)
        self.total = len(parts)
        self.state = 'preparing' if self.background else 'generating'
        key = hashlib.sha256(('chunks-v1\0' + voice_id + '\0' + text).encode('utf-8')).hexdigest()
        path = self.audio_dir / f'{key}.wav'
        try:
            duration = wave_duration(path)
        except (OSError, ValueError, EOFError, wave.Error):
            if self.streaming:
                try:
                    self.live = LivePlayback(PCMSpool(self.temp_dir.name))
                    self.live.volume = self.volume
                    if autoplay:
                        self.live.start()
                except Exception as exc:
                    if self.live:
                        self.live.close()
                        self.live = None
                    self.state = 'error'
                    self.error = 'Не удалось открыть устройство вывода звука: ' + str(exc)
                    return self.generation
                target = self._render_stream
                args = (self.generation, text, voice_id, title, path, self.cancel, self.live.spool)
            else:
                target = self._render
                args = (self.generation, parts, voice_id, title, path, self.cancel)
            worker = threading.Thread(target=target, args=args, daemon=True)
            self.workers = [w for w in self.workers if w.is_alive()]
            self.workers.append(worker)
            worker.start()
        else:
            words = []
            with suppress(OSError, ValueError, AttributeError):
                metadata = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
                words = metadata.get('words', [])
            self.events.put({'event': 'complete', 'job_id': self.generation,
                             'path': str(path), 'duration': duration, 'cached': True, 'words': words})
        return self.generation

    def start_prepared(self) -> None:
        """Attach playback to the existing job; never request the same speech twice."""
        if not self.background:
            return
        self.background = False
        self.paused = False
        if self.recording:
            self._load(str(self.recording), self.duration, self.prepared_position)
        elif self.live:
            try:
                self.live.start()
                self.poll()
            except Exception as exc:
                self.fail('Не удалось открыть устройство вывода звука: ' + str(exc))
        elif self.chunks:
            self._next_chunk()
        else:
            self.state = 'generating'

    @staticmethod
    async def _receive_stream(text: str, voice: str, source: IncomingAudio,
                              cancel: threading.Event, on_word=None) -> None:
        async def receive():
            for attempt in range(3):
                pending_words = []
                try:
                    async for packet in edge_tts.Communicate(text, voice,
                            boundary='WordBoundary', connect_timeout=10, receive_timeout=30).stream():
                        if packet['type'] == 'audio':
                            await asyncio.to_thread(source.feed, packet['data'])
                            if on_word:
                                for word in pending_words:
                                    on_word(word)
                                pending_words.clear()
                        elif packet['type'] == 'WordBoundary' and on_word:
                            if source.received:
                                on_word(packet)
                            else:
                                pending_words.append(packet)
                    return
                except (asyncio.CancelledError, StreamCancelled):
                    raise
                except Exception:
                    # Replaying an already partially received utterance duplicates speech.
                    if source.received or attempt == 2:
                        raise
                    await asyncio.sleep(attempt + 1)

        task = asyncio.create_task(receive())
        try:
            while not task.done():
                if cancel.is_set():
                    source.abort()
                    raise StreamCancelled()
                await asyncio.wait({task}, timeout=0.05)
            await task
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    def _render_stream(self, job_id: int, text: str, voice_id: str, title: str,
                       path: Path, cancel: threading.Event, spool: PCMSpool) -> None:
        source = IncomingAudio(cancel)
        errors = []
        words = []

        def on_word(packet):
            word = {'time': packet['offset'] / 10_000_000,
                    'duration': packet['duration'] / 10_000_000, 'text': packet['text']}
            words.append(word)
            self.events.put({'event': 'word', 'job_id': job_id, 'word': word})

        def decode():
            try:
                decode_incoming(source, spool)
            except Exception as exc:
                errors.append(exc)
                source.abort()

        decoder = threading.Thread(target=decode, daemon=True)
        decoder.start()
        try:
            asyncio.run(self._receive_stream(text, voice_id[5:], source, cancel, on_word))
            source.finish()
            decoder.join()
            if cancel.is_set():
                raise StreamCancelled()
            if errors:
                raise errors[0]
            duration = spool.save(path, cancel)
            metadata = {'title': title, 'voice': voice_id, 'duration': duration,
                        'mode': 'stream', 'words': words,
                        'created_at': time.strftime('%Y-%m-%d %H:%M:%S')}
            with suppress(OSError):
                path.with_suffix('.json').write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
            self.events.put({'event': 'complete', 'job_id': job_id,
                'path': str(path), 'duration': duration, 'cached': False, 'words': words})
        except Exception as exc:
            if not cancel.is_set():
                message = str(errors[0] if errors else exc)
                self.events.put({'event': 'error', 'job_id': job_id,
                    'message': message or 'Соединение со службой голоса прервалось. Повторите чтение.'})
        finally:
            source.abort()
            decoder.join(timeout=1)

    def _synthesize(self, text: str, voice_id: str, raw: Path, cancel: threading.Event) -> None:
        if voice_id.startswith('sapi:'):
            pythoncom.CoInitialize()
            try:
                voice = win32com.client.Dispatch('SAPI.SpVoice')
                tokens = voice.GetVoices()
                for index in range(tokens.Count):
                    token = tokens.Item(index)
                    if token.GetDescription() == voice_id[5:]:
                        voice.Voice = token
                        break
                else:
                    raise RuntimeError('Выбранный голос Windows больше не найден.')
                stream = win32com.client.Dispatch('SAPI.SpFileStream')
                stream.Open(str(raw), 3, False)
                try:
                    voice.AudioOutputStream = stream
                    voice.Speak(text)
                finally:
                    stream.Close()
            finally:
                pythoncom.CoUninitialize()
        else:
            raise ValueError('Неизвестный голос')

    def _render(self, job_id: int, parts: list[str], voice_id: str, title: str,
                path: Path, cancel: threading.Event) -> None:
        work = Path(self.temp_dir.name) / uuid.uuid4().hex
        partial = path.with_name(path.name + f'.{work.name}.part')
        try:
            work.mkdir(parents=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open_recording(partial) as merged:
                for index, text in enumerate(parts):
                    if cancel.is_set():
                        raise Cancelled()
                    raw = work / f'{index}.source'
                    chunk = work / f'{index}.wav'
                    self._synthesize(text, voice_id, raw, cancel)
                    if cancel.is_set():
                        raise Cancelled()
                    decoded = miniaudio.decode_file(str(raw), nchannels=1, sample_rate=SAMPLE_RATE,
                                                    output_format=miniaudio.SampleFormat.SIGNED16)
                    with open_recording(chunk) as audio:
                        audio.writeframes(decoded.samples.tobytes())
                    raw.unlink(missing_ok=True)
                    append_chunk(merged, chunk)
                    self.events.put({'event': 'chunk', 'job_id': job_id, 'index': index,
                        'total': len(parts), 'path': str(chunk), 'duration': wave_duration(chunk), 'text': text})
            if cancel.is_set():
                raise Cancelled()
            duration = wave_duration(partial)
            partial.replace(path)
            metadata = {'title': title, 'voice': voice_id, 'duration': duration,
                        'chunks': len(parts), 'created_at': time.strftime('%Y-%m-%d %H:%M:%S')}
            with suppress(OSError):
                path.with_suffix('.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
            self.events.put({'event': 'complete', 'job_id': job_id,
                             'path': str(path), 'duration': duration, 'cached': False})
        except Cancelled:
            pass
        except Exception as exc:
            if not cancel.is_set():
                self.events.put({'event': 'error', 'job_id': job_id, 'message': str(exc)})
        finally:
            partial.unlink(missing_ok=True)

    def _load(self, path: str, duration: float, position: float = 0.0) -> None:
        self.player.controls.stop()
        self.local_duration = duration
        self.started = False
        # WMP opens the media decoder only after play(). Keep it silent until seeking.
        self.player.settings.volume = 0
        self.player.settings.rate = self.rate if self.recording else 1.0
        self.player.URL = path
        self.pending_load = (position, time.monotonic())
        self.player.controls.play()
        self.state = 'paused' if self.paused else 'playing'

    def handle_event(self, event: dict) -> None:
        if event.get('job_id') != self.generation:
            return
        kind = event['event']
        if kind == 'word':
            self.karaoke.add(event['word'])
        elif kind == 'chunk':
            offset = sum(chunk['duration'] for chunk in self.chunks)
            self.chunks.append(event)
            self.rendered = len(self.chunks)
            self.karaoke.estimate_segment(event.get('text', ''), event['duration'], offset)
            if not self.background and (self.index < 0 or self.state == 'waiting'):
                self._next_chunk()
        elif kind == 'complete':
            position = self.position()
            self.recording = Path(event['path'])
            self.duration = float(event['duration'])
            self.rendered = self.total
            self.offset = 0.0
            words = event.get('words')
            if isinstance(words, list) and words:
                self.karaoke = KaraokeTimeline(self.speech_text)
                for word in words:
                    if isinstance(word, dict):
                        self.karaoke.add(word)
            if not self.karaoke.cues:
                self.karaoke.estimate_segment(self.speech_text, self.duration)
            if self.background:
                if self.live:
                    self.live.close()
                    self.live = None
                self.state = 'ready'
                return
            if self.live:
                # Continue from the same PCM buffer at 1x; no gap on file completion.
                # Open the seekable player only when its speed/seek features are needed.
                if self.rate != 1.0:
                    self._handoff_live()
                else:
                    self.poll()
                return
            if position >= self.duration - 0.01:
                self.player.controls.stop()
                self.pending_load = None
                self.state = 'finished'
            else:
                self._load(str(self.recording), self.duration, position)
        elif kind == 'error':
            self.fail(event['message'])

    def fail(self, message: str) -> None:
        self.stop()
        self.error = message
        self.state = 'error'

    def _handoff_live(self, position: float | None = None) -> None:
        if not self.live or not self.recording:
            return
        with self.live.lock:
            self.live.paused = True
            if position is None:
                position = self.live.position
        self.live.close()
        self.live = None
        self._load(str(self.recording), self.duration, position)

    def _next_chunk(self) -> None:
        next_index = self.index + 1
        if next_index >= len(self.chunks):
            self.state = 'waiting'
            return
        self.index = next_index
        chunk = self.chunks[self.index]
        self._load(chunk['path'], chunk['duration'])

    def poll(self) -> None:
        if self.background:
            if self.live and self.live.error:
                self.fail(self.live.error)
            return
        if self.live:
            if self.live.error:
                self.fail(self.live.error)
            elif self.live.device and not self.live.device.running:
                self.fail('Устройство вывода звука отключено. Подключите его и повторите чтение.')
            elif self.paused:
                self.state = 'paused'
            elif self.recording and self.live.done:
                self.state = 'finished'
            elif self.live.buffering:
                self.state = 'waiting' if self.live.position else 'generating'
            else:
                self.state = 'playing'
            return
        if self.pending_load:
            position, started_at = self.pending_load
            if int(self.player.openState) == 13 and int(self.player.playState) in (2, 3):
                self.player.controls.currentPosition = position
                if self.paused:
                    self.player.controls.pause()
                self.player.settings.volume = self.volume
                self.pending_load = None
            elif time.monotonic() - started_at > 15:
                self.fail('Не удалось открыть звуковой файл. Повторите чтение.')
            return
        if self.state != 'playing':
            return
        state = int(self.player.playState)
        if state == 3:
            self.started = True
        if state == 8 or (state == 1 and self.started):
            if self.recording:
                self.state = 'finished'
            else:
                self.offset += self.local_duration
                self.started = False
                self.local_duration = 0.0
                self._next_chunk()

    def position(self) -> float:
        if self.background:
            return self.prepared_position
        if self.state == 'finished':
            return self.duration
        if self.live:
            return self.live.position
        if self.pending_load:
            return self.offset + self.pending_load[0]
        if self.state in ('waiting', 'generating', 'stopped', 'error'):
            return self.offset
        state = int(self.player.playState)
        if state == 8 or (state == 1 and self.started):
            local = self.local_duration
        else:
            local = min(self.local_duration, float(self.player.controls.currentPosition))
        return self.offset + local

    def seek(self, seconds: float) -> None:
        if not self.recording:
            return
        position = max(0.0, min(self.duration, seconds))
        if self.background:
            self.prepared_position = position
            return
        if self.live:
            if self.state == 'finished':
                self.paused = True
            self._handoff_live(position)
            return
        if self.state == 'finished':
            self.paused = True
            self._load(str(self.recording), self.duration, position)
        elif self.pending_load:
            self.pending_load = (position, self.pending_load[1])
        else:
            self.player.controls.currentPosition = position

    def set_rate(self, rate: float) -> None:
        self.rate = rate
        if self.live and self.recording and rate != 1.0 and self.state != 'finished':
            self._handoff_live()
        self.player.settings.rate = rate if self.recording else 1.0

    def set_volume(self, volume: int) -> None:
        self.volume = volume
        if self.live:
            self.live.volume = volume
        if not self.pending_load:
            self.player.settings.volume = volume

    def pause(self) -> None:
        if self.background:
            return
        self.paused = True
        if self.live:
            self.live.paused = True
            self.state = 'paused'
            return
        if self.state == 'playing':
            self.player.controls.pause()
            self.state = 'paused'

    def resume(self) -> None:
        if self.background:
            return
        self.paused = False
        if self.live:
            self.live.paused = False
            self.poll()
            return
        if self.state == 'paused':
            if not self.pending_load:
                self.player.controls.play()
            self.state = 'playing'

    def stop(self) -> None:
        self.cancel.set()
        self.generation += 1
        self.background = False
        self.request_key = None
        if self.live:
            self.live.close()
            self.live = None
        self.player.controls.stop()
        self.pending_load = None
        self.paused = False
        self.started = False
        self.state = 'stopped'

    def close(self) -> None:
        self.stop()
        self.player.close()
        for worker in self.workers:
            worker.join(timeout=1)
        if not any(w.is_alive() for w in self.workers):
            self.temp_dir.cleanup()
