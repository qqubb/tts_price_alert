import mmap
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
import sounddevice as sd
from kokoro import KPipeline

# ===================== Config =====================
SHM_PATH = "/dev/shm/eth_price_shm"
PIPE_PATH = "/tmp/eth_price_pipe"
BUFFER_SIZE = 32
THRESHOLD_VALUE = 12.5
SAMPLE_RATE = 24000
DEBOUNCE_SECONDS = 0.3
FADE_OUT_MS = 300

# ===================== Speech Engine =====================
@dataclass
class SpeechTask:
    text: str
    created_at: float

class SpeechEngine:
    def __init__(self):
        self.pipeline = KPipeline(lang_code='a', device='cpu')
        self._prefix_cache = self._build_prefix_cache([
            "Starting price checkpoint",
            "up to",
            "down to"
        ])
        self._lock = threading.Lock()
        self._current_thread: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        self._last_alert_time = 0.0

    def _build_prefix_cache(self, phrases):
        cache = {}
        for phrase in phrases:
            try:
                gen = self.pipeline(phrase, voice='af_heart')
                _, _, audio = next(gen)
                arr = audio.detach().cpu().numpy().astype(np.float32)
                cache[phrase] = arr[: min(len(arr), 2400)]
            except Exception as e:
                print(f"[speech] prefix cache error for '{phrase}': {e}")
        return cache

    def _match_leadin(self, text: str) -> Optional[np.ndarray]:
        for key, chunk in self._prefix_cache.items():
            if text.lower().startswith(key.lower()):
                return chunk
        return None

    def _fade_and_write(self, stream: sd.OutputStream, block: np.ndarray, cancel_triggered: bool):
        if cancel_triggered:
            fade_samples = min(len(block), int(SAMPLE_RATE * FADE_OUT_MS / 1000))
            if fade_samples > 0:
                fade_curve = np.linspace(1.0, 0.0, fade_samples, dtype=np.float32)
                block[:fade_samples] *= fade_curve
            stream.write(block)
            return True
        else:
            stream.write(block)
            return False

    def _stream_tts(self, text: str, cancel_event: threading.Event, leadin: Optional[np.ndarray] = None):
        try:
            with sd.OutputStream(samplerate=SAMPLE_RATE, channels=1, dtype='float32', blocksize=4096) as stream:
                if leadin is not None and leadin.size > 0:
                    self._fade_and_write(stream, leadin, cancel_triggered=False)
                for _, _, audio in self.pipeline(text, voice='af_heart'):
                    audio_np = audio.detach().cpu().numpy().astype(np.float32)
                    if audio_np.size == 0:
                        continue
                    if cancel_event.is_set():
                        self._fade_and_write(stream, audio_np, cancel_triggered=True)
                        break
                    self._fade_and_write(stream, audio_np, cancel_triggered=False)
        except Exception as e:
            print(f"[speech] playback error: {e}")

    def say(self, text: str, *, force: bool = False):
        now = time.time()
        if not force and (now - self._last_alert_time) < DEBOUNCE_SECONDS:
            return
        self._last_alert_time = now

        leadin = self._match_leadin(text)
        with self._lock:
            if self._current_thread and self._current_thread.is_alive():
                self._cancel_event.set()
            self._cancel_event = threading.Event()
            t = threading.Thread(target=self._stream_tts, args=(text, self._cancel_event, leadin), daemon=True)
            self._current_thread = t
            t.start()

# ===================== Main Loop =====================
def main():
    if not os.path.exists(SHM_PATH):
        raise FileNotFoundError(f"Shared memory not found: {SHM_PATH}")
    if not os.path.exists(PIPE_PATH):
        os.mkfifo(PIPE_PATH)

    with open(SHM_PATH, "r+b") as f, open(PIPE_PATH, "rb") as pipe:
        shm = mmap.mmap(f.fileno(), BUFFER_SIZE, access=mmap.ACCESS_READ)
        speech = SpeechEngine()
        checkpoint_price: Optional[float] = None

        while True:
            # Block until Go writes to pipe
            pipe.read(1)

            shm.seek(0)
            raw = shm.read(BUFFER_SIZE).split(b"\x00", 1)[0]
            try:
                price = float(raw.decode("utf-8"))
            except ValueError:
                continue

            if checkpoint_price is None:
                checkpoint_price = round(price / THRESHOLD_VALUE) * THRESHOLD_VALUE
                alert_text = f"Starting price checkpoint: {int(round(checkpoint_price))}"
                print("[ALERT]", alert_text)
                speech._stream_tts(alert_text, threading.Event(), speech._match_leadin(alert_text))
                continue

            change = price - checkpoint_price
            if change >= THRESHOLD_VALUE:
                alert_text = f"up to {int(round(price))}"
                print("[ALERT]", alert_text)
                speech.say(alert_text)
                checkpoint_price = price
            elif change <= -THRESHOLD_VALUE:
                alert_text = f"down to {int(round(price))}"
                print("[ALERT]", alert_text)
                speech.say(alert_text)
                checkpoint_price = price
            else:
                print(f"ETH {price:.2f} Δ {change:.2f}")

if __name__ == "__main__":
    main()
