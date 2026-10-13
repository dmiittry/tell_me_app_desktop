"""Map speech timing to the exact text shown by the karaoke view."""
from bisect import bisect_right
import math
import re


class KaraokeTimeline:
    def __init__(self, text: str = ''):
        self.text = text
        self.cues: list[dict] = []
        self.times: list[float] = []
        self.cursor = 0
        self.mode = 'pending'

    def add(self, word: dict, estimated: bool = False) -> None:
        try:
            start = float(word['time'])
            duration = float(word['duration'])
            label = str(word['text']).strip()
        except (KeyError, TypeError, ValueError):
            return
        if not label or not math.isfinite(start + duration) or start < 0 or duration <= 0:
            return
        if self.times and start < self.times[-1]:
            return
        match = re.compile(re.escape(label), flags=re.IGNORECASE).search(self.text, self.cursor)
        if not match:
            return
        begin, end = match.start(), match.end()
        self.cues.append({'time': start, 'end_time': start + duration,
                         'start': begin, 'end': end})
        self.times.append(start)
        self.cursor = end
        self.mode = 'estimated' if estimated else 'exact'

    def estimate_segment(self, text: str, duration: float, offset: float = 0) -> None:
        words = re.findall(r'\S+', text)
        weights = [max(2, len(word)) + (3 if word[-1:] in '.!?;:,' else 0) for word in words]
        total = sum(weights)
        position = offset
        for word, weight in zip(words, weights):
            length = duration * weight / total
            self.add({'time': position, 'duration': length, 'text': word}, estimated=True)
            position += length

    def at(self, seconds: float) -> dict | None:
        index = bisect_right(self.times, seconds) - 1
        if index >= 0 and seconds < self.cues[index]['end_time']:
            return self.cues[index]
        return None

    def spoken_end(self, seconds: float) -> int:
        index = bisect_right(self.times, seconds) - 1
        if index < 0:
            return 0
        cue = self.cues[index]
        return cue['start'] if seconds < cue['end_time'] else cue['end']
