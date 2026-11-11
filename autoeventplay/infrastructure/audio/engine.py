"""Real-time audio engine.

Signal flow per audio block::

    voices ──► source buses (media L/R, tts, recording)
           ──► input faders/mutes  ──► input meters
           ──► routing matrix (inputs × outputs, on/off)
           ──► output faders/mutes ──► output meters ──► sound devices

Several sound devices (e.g. the Pi headphone jack, USB adapters, a Dante
AVIO adapter) can be used at the same time. The first device used by the
output list is the clock master: its callback renders one block for *all*
outputs, the other devices are fed through small ring buffers. Without any
usable device (or with backend ``null``) a timer thread clocks the engine,
so routing, meters and the web UI keep working for testing.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from ...domain.dynamics import is_active as dyn_active
from ...domain.eq import design_sos
from ...domain.levels import MIN_DB, db_to_gain, gain_to_db
from ...domain.model import INPUT_DEFS, RoutingState, SourceBus
from .dyn_processor import DynState
from .eq_processor import EqState
from ...i18n import Msg

log = logging.getLogger("aep.audio")

try:  # optional at import time so tests and dev machines work without PortAudio
    import sounddevice as sd  # type: ignore
except Exception:  # pragma: no cover - depends on the machine
    sd = None


BUS_CHANNELS = {SourceBus.MEDIA: 2, SourceBus.TTS: 1, SourceBus.RECORDING: 1}
TEST_TONE_DB = -18.0  # pink noise test signal, RMS level in dBFS
PINK_LOOP_SAMPLES = 1 << 19  # ~10.9 s at 48 kHz, seamless loop


def make_pink_noise(n: int, sample_rate: int, rms_db: float = TEST_TONE_DB, seed: int = 1) -> np.ndarray:
    """Pink noise (−3 dB/octave, band-limited 20 Hz – 20 kHz) as a seamless loop, normalised to an RMS level.

    Synthesised in the frequency domain: an inverse FFT is periodic, so the loop has no click at the seam.
    """
    rng = np.random.default_rng(seed)
    freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)
    spectrum = rng.standard_normal(len(freqs)) + 1j * rng.standard_normal(len(freqs))
    mag = np.zeros_like(freqs)
    band = (freqs >= 20.0) & (freqs <= min(20000.0, sample_rate / 2))
    mag[band] = 1.0 / np.sqrt(freqs[band])
    noise = np.fft.irfft(spectrum * mag, n)
    noise *= db_to_gain(rms_db) / np.sqrt(np.mean(noise ** 2))
    return noise.astype(np.float32)


class Voice:
    """One playing buffer on a source bus."""

    def __init__(self, data: np.ndarray, label: str = "", gain: float = 1.0):
        self.data = data.astype(np.float32, copy=False)
        self.label = label
        self.gain = gain
        self.pos = 0
        self.fade_left: int | None = None  # set when stopping with a fade
        self.fade_total = 1
        self.finished = False
        self.stopped = False
        self._done_callbacks: list[Callable[["Voice"], None]] = []

    @property
    def frames(self) -> int:
        return int(self.data.shape[0])

    @property
    def duration(self) -> float:
        return self.frames

    def add_done_callback(self, cb: Callable[["Voice"], None]) -> None:
        self._done_callbacks.append(cb)

    def render_into(self, out: np.ndarray) -> None:
        n = out.shape[0]
        remaining = self.frames - self.pos
        take = min(n, remaining)
        if take > 0:
            chunk = self.data[self.pos:self.pos + take]
            if self.fade_left is not None:
                start = self.fade_left
                ramp = np.clip((start - np.arange(take)) / max(1, self.fade_total), 0.0, 1.0).astype(np.float32)
                out[:take] += chunk * (ramp[:, None] * self.gain)
                self.fade_left -= take
                if self.fade_left <= 0:
                    self.pos = self.frames
                    return
            else:
                out[:take] += chunk * self.gain
            self.pos += take

    @property
    def is_done(self) -> bool:
        return self.pos >= self.frames

    def stop(self, fade_frames: int) -> None:
        self.stopped = True
        if fade_frames <= 0:
            self.pos = self.frames
        elif self.fade_left is None:
            self.fade_left = fade_frames
            self.fade_total = fade_frames

    def _fire_done(self) -> None:
        if self.finished:
            return
        self.finished = True
        for cb in self._done_callbacks:
            try:
                cb(self)
            except Exception:  # pragma: no cover
                log.exception("voice done callback failed")


@dataclass
class _Snapshot:
    """Immutable routing snapshot used by the audio thread."""

    input_ids: list[str]
    output_ids: list[str]
    input_gains: np.ndarray
    output_gains: np.ndarray
    matrix: np.ndarray
    # device name -> list of (output column index, device channel)
    device_map: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    # EQ states per column (None = bypass)
    input_eq: list[EqState | None] = field(default_factory=list)
    output_eq: list[EqState | None] = field(default_factory=list)
    input_dyn: list[DynState | None] = field(default_factory=list)
    output_dyn: list[DynState | None] = field(default_factory=list)
    # input trim gains (applied first) and per column: True = compressor before EQ
    input_trims: np.ndarray | None = None
    input_dyn_first: list[bool] = field(default_factory=list)
    output_dyn_first: list[bool] = field(default_factory=list)


def _process_channel(sig: np.ndarray, eq: EqState | None, dyn: DynState | None, dyn_first: bool) -> np.ndarray:
    """Channel processing: EQ and compressor in the configured order (None = bypassed)."""
    if eq is None and dyn is None:
        return sig
    stages = (dyn, eq) if dyn_first else (eq, dyn)
    for stage in stages:
        if stage is not None:
            sig = stage.process(sig)
    return sig


class _DeviceStream:
    def __init__(self, name: str, channels: int, columns: list[tuple[int, int]]):
        self.name = name
        self.channels = channels
        self.columns = columns
        self.stream: Any = None
        self.buffer: collections.deque[np.ndarray] = collections.deque()
        self.buffered = 0
        self.lock = threading.Lock()
        self.underruns = 0
        self.error: str | None = None
        self.open = False


class AudioEngine:
    def __init__(self, backend: str = "sounddevice", sample_rate: int = 48000, blocksize: int = 1024):
        self.backend = backend
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self._lock = threading.RLock()
        self._voices: dict[SourceBus, list[Voice]] = {b: [] for b in SourceBus}
        self._snap: _Snapshot | None = None
        self._streams: list[_DeviceStream] = []
        self._master: _DeviceStream | None = None
        self._null_thread: threading.Thread | None = None
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._peak_in: np.ndarray = np.zeros(len(INPUT_DEFS), dtype=np.float32)
        self._peak_out: np.ndarray = np.zeros(0, dtype=np.float32)
        self._test_tones: set[str] = set()
        self._tone_phase = 0
        self._pink: np.ndarray | None = None  # generated on first use
        self.status_message = Msg("stopped")
        self.callback_errors = 0
        self._eq_states: dict[str, EqState] = {}
        self._dyn_states: dict[str, DynState] = {}
        self._last_state: RoutingState | None = None

    # ------------------------------------------------------------------ setup

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def apply_routing(self, state: RoutingState) -> None:
        """Rebuild the routing snapshot (cheap; safe to call on every fader move)."""
        self._last_state = state
        input_ids = [d.id for d in INPUT_DEFS]
        by_id = {i.id: i for i in state.inputs}
        in_gains = np.array([
            0.0 if (by_id.get(i) is None or by_id[i].muted) else db_to_gain(by_id[i].level_db) for i in input_ids
        ], dtype=np.float32)
        outputs = list(state.outputs)
        out_gains = np.array([0.0 if o.muted else db_to_gain(o.level_db) for o in outputs], dtype=np.float32)
        matrix = np.zeros((len(input_ids), len(outputs)), dtype=np.float32)
        out_index = {o.id: k for k, o in enumerate(outputs)}
        for inp, out in state.crosspoints:
            if inp in input_ids and out in out_index:
                matrix[input_ids.index(inp), out_index[out]] = 1.0
        device_map: dict[str, list[tuple[int, int]]] = {}
        for k, o in enumerate(outputs):
            device_map.setdefault(o.device, []).append((k, o.channel))
        in_eq = [self._eq_state("in:" + i, by_id[i].eq if i in by_id else None) for i in input_ids]
        out_eq = [self._eq_state("out:" + o.id, o.eq) for o in outputs]
        in_dyn = [self._dyn_state("in:" + i, by_id[i].dyn if i in by_id else None) for i in input_ids]
        out_dyn = [self._dyn_state("out:" + o.id, o.dyn) for o in outputs]
        in_trims = np.array([db_to_gain(by_id[i].trim_db) if i in by_id else 1.0 for i in input_ids], dtype=np.float32)
        in_first = [bool(i in by_id and by_id[i].order == "dyn_eq") for i in input_ids]
        out_first = [o.order == "dyn_eq" for o in outputs]
        snap = _Snapshot(input_ids, [o.id for o in outputs], in_gains, out_gains, matrix, device_map, in_eq, out_eq, in_dyn, out_dyn,
                         in_trims, in_first, out_first)
        with self._lock:
            layout_changed = self._snap is None or self._snap.device_map != snap.device_map
            self._snap = snap
            if self._peak_out.shape[0] != len(outputs):
                self._peak_out = np.zeros(len(outputs), dtype=np.float32)
        if layout_changed and self._running:
            log.info("Output layout changed, restarting audio devices")
            self.restart()

    def _eq_state(self, key: str, eq: dict | None) -> EqState | None:
        sos = design_sos(eq, self.sample_rate)
        if sos is None:
            self._eq_states.pop(key, None)
            return None
        state = self._eq_states.get(key)
        if state is None:
            state = self._eq_states[key] = EqState()
        state.update(sos)
        return state

    def _dyn_state(self, key: str, dyn: dict | None) -> DynState | None:
        if not dyn_active(dyn):
            self._dyn_states.pop(key, None)
            return None
        state = self._dyn_states.get(key)
        if state is None:
            state = self._dyn_states[key] = DynState(self.sample_rate)
        state.update(dyn, self.sample_rate)
        return state

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        self._open_devices()

    def stop(self) -> None:
        with self._lock:
            self._running = False
        self._close_devices()

    def restart(self) -> None:
        self._close_devices()
        self._open_devices()

    def set_format(self, sample_rate: int, blocksize: int) -> None:
        changed = (sample_rate, blocksize) != (self.sample_rate, self.blocksize)
        self.sample_rate, self.blocksize = sample_rate, blocksize
        if changed and self._last_state is not None:
            self._eq_states.clear()
            self._dyn_states.clear()
            self.apply_routing(self._last_state)
        if changed and self._running:
            self.restart()

    # ------------------------------------------------------------------ devices

    def _open_devices(self) -> None:
        snap = self._snap
        self._streams = []
        self._master = None
        if snap is None:
            self._start_null_clock(Msg("no outputs configured"))
            return
        if self.backend != "sounddevice" or sd is None:
            reason = Msg("audio backend 'null'") if self.backend != "sounddevice" else Msg("sounddevice/PortAudio not available")
            self._start_null_clock(reason)
            return
        for name, columns in snap.device_map.items():
            channels = max(ch for _, ch in columns) + 1
            ds = _DeviceStream(name, channels, columns)
            try:
                device = self._resolve_device(name)
                is_master = self._master is None
                ds.stream = sd.OutputStream(
                    device=device,
                    samplerate=self.sample_rate,
                    blocksize=self.blocksize,
                    channels=channels,
                    dtype="float32",
                    latency="high",
                    callback=self._make_callback(ds, is_master),
                )
                ds.stream.start()
                ds.open = True
                if is_master:
                    self._master = ds
                log.info("Audio device '%s' opened (%d ch, %d Hz, block %d)%s", name, channels, self.sample_rate,
                         self.blocksize, " [clock master]" if is_master else "")
            except Exception as exc:
                ds.error = str(exc)
                log.error("Cannot open audio device '%s': %s", name, exc)
            self._streams.append(ds)
        if self._master is None:
            self._start_null_clock(Msg("no audio device could be opened"))
        else:
            failed = [s.name for s in self._streams if not s.open]
            self.status_message = Msg("running") if not failed else Msg("running (errors: {devices})", devices=", ".join(failed))

    def _close_devices(self) -> None:
        self._stop_null_clock()
        for ds in self._streams:
            if ds.stream is not None:
                try:
                    ds.stream.stop()
                    ds.stream.close()
                except Exception:  # pragma: no cover
                    pass
            ds.open = False
        self._streams = []
        self._master = None
        self.status_message = Msg("stopped")

    @staticmethod
    def _resolve_device(name: str) -> int | None:
        if name in ("", "default", None):
            return None
        devices = sd.query_devices()
        for idx, dev in enumerate(devices):
            if dev["max_output_channels"] > 0 and dev["name"] == name:
                return idx
        for idx, dev in enumerate(devices):
            if dev["max_output_channels"] > 0 and name.lower() in dev["name"].lower():
                return idx
        raise RuntimeError(Msg("Device '{name}' not found", name=name))

    @staticmethod
    def list_devices() -> list[dict[str, Any]]:
        if sd is None:
            return []
        try:
            out = []
            hostapis = sd.query_hostapis()
            for idx, dev in enumerate(sd.query_devices()):
                if dev["max_output_channels"] <= 0:
                    continue
                name = dev["name"]
                out.append({
                    "index": idx,
                    "name": name,
                    "channels": int(dev["max_output_channels"]),
                    "default_samplerate": int(dev["default_samplerate"]),
                    "hostapi": hostapis[dev["hostapi"]]["name"] if dev["hostapi"] < len(hostapis) else "",
                    "dante": any(k in name.lower() for k in ("dante", "avio", "inferno", "aes67")),
                })
            return out
        except Exception as exc:  # pragma: no cover
            log.warning("Cannot list audio devices: %s", exc)
            return []

    def _make_callback(self, ds: _DeviceStream, is_master: bool) -> Callable:
        def callback(outdata, frames, time_info, status):  # noqa: ANN001 - PortAudio signature
            try:
                if status and status.output_underflow:
                    ds.underruns += 1
                if is_master:
                    block = self.render(frames)
                    self._write_device(ds, block, outdata)
                    for other in self._streams:
                        if other is not ds and other.open:
                            self._push_buffer(other, block)
                else:
                    self._pull_buffer(ds, outdata, frames)
            except Exception:  # pragma: no cover - never raise inside PortAudio
                self.callback_errors += 1
                outdata.fill(0)
                if self.callback_errors < 5:
                    log.exception("audio callback failed")
        return callback

    @staticmethod
    def _write_device(ds: _DeviceStream, block: np.ndarray, outdata: np.ndarray) -> None:
        outdata.fill(0)
        for col, ch in ds.columns:
            outdata[:, ch] += block[:, col]

    def _push_buffer(self, ds: _DeviceStream, block: np.ndarray) -> None:
        dev = np.zeros((block.shape[0], ds.channels), dtype=np.float32)
        for col, ch in ds.columns:
            dev[:, ch] += block[:, col]
        with ds.lock:
            ds.buffer.append(dev)
            ds.buffered += dev.shape[0]
            # keep latency bounded (clock drift between devices): max ~4 blocks
            while ds.buffered > self.blocksize * 4 and ds.buffer:
                ds.buffered -= ds.buffer.popleft().shape[0]

    @staticmethod
    def _pull_buffer(ds: _DeviceStream, outdata: np.ndarray, frames: int) -> None:
        outdata.fill(0)
        filled = 0
        with ds.lock:
            while filled < frames and ds.buffer:
                head = ds.buffer[0]
                take = min(frames - filled, head.shape[0])
                outdata[filled:filled + take] = head[:take]
                filled += take
                ds.buffered -= take
                if take == head.shape[0]:
                    ds.buffer.popleft()
                else:
                    ds.buffer[0] = head[take:]
        if filled < frames:
            ds.underruns += 1

    def _start_null_clock(self, reason: str) -> None:
        self.status_message = Msg("without audio output ({reason})", reason=reason)
        log.warning("Audio engine runs without output: %s", reason)
        self._stop_null_clock()
        stop_flag = threading.Event()

        def run() -> None:
            period = self.blocksize / self.sample_rate
            next_t = time.monotonic()
            while not stop_flag.is_set():
                self.render(self.blocksize)
                next_t += period
                delay = next_t - time.monotonic()
                if delay > 0:
                    stop_flag.wait(delay)
                else:
                    next_t = time.monotonic()

        self._null_stop = stop_flag
        self._null_thread = threading.Thread(target=run, name="aep-null-clock", daemon=True)
        self._null_thread.start()

    def _stop_null_clock(self) -> None:
        if self._null_thread is not None:
            self._null_stop.set()
            self._null_thread.join(timeout=1)
            self._null_thread = None

    # ------------------------------------------------------------------ rendering

    def render(self, frames: int) -> np.ndarray:
        snap = self._snap
        n_out = len(snap.output_ids) if snap else 0
        if snap is None:
            return np.zeros((frames, 0), dtype=np.float32)
        buses = {b: np.zeros((frames, c), dtype=np.float32) for b, c in BUS_CHANNELS.items()}
        finished: list[Voice] = []
        with self._lock:
            for bus, voices in self._voices.items():
                if not voices:
                    continue
                buf = buses[bus]
                for v in voices:
                    v.render_into(buf)
                    if v.is_done:
                        finished.append(v)
                if finished:
                    self._voices[bus] = [v for v in voices if not v.is_done]
        # inputs: media L, media R, tts, recording
        x = np.empty((frames, len(INPUT_DEFS)), dtype=np.float32)
        for k, d in enumerate(INPUT_DEFS):
            x[:, k] = buses[d.bus][:, d.bus_channel]
        if snap.input_trims is not None:
            x *= snap.input_trims
        for k in range(x.shape[1]):
            x[:, k] = _process_channel(x[:, k], snap.input_eq[k] if k < len(snap.input_eq) else None,
                                       snap.input_dyn[k] if k < len(snap.input_dyn) else None,
                                       k < len(snap.input_dyn_first) and snap.input_dyn_first[k])
        x *= snap.input_gains
        peak_in = np.abs(x).max(axis=0) if frames else np.zeros(len(INPUT_DEFS))
        y = x @ snap.matrix if n_out else np.zeros((frames, 0), dtype=np.float32)
        for k in range(n_out):
            y[:, k] = _process_channel(y[:, k], snap.output_eq[k] if k < len(snap.output_eq) else None,
                                       snap.output_dyn[k] if k < len(snap.output_dyn) else None,
                                       k < len(snap.output_dyn_first) and snap.output_dyn_first[k])
        y *= snap.output_gains
        if self._test_tones:
            tone = self._pink_block(frames)
            for k, oid in enumerate(snap.output_ids):
                if oid in self._test_tones:
                    y[:, k] += tone
        peak_out = np.abs(y).max(axis=0) if (frames and n_out) else np.zeros(n_out)
        np.clip(y, -1.0, 1.0, out=y)
        with self._lock:
            np.maximum(self._peak_in, peak_in, out=self._peak_in)
            if self._peak_out.shape[0] == n_out:
                np.maximum(self._peak_out, peak_out, out=self._peak_out)
        for v in finished:
            self._notify_done(v)
        return y

    def _notify_done(self, voice: Voice) -> None:
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(voice._fire_done)
                return
            except RuntimeError:
                pass
        voice._fire_done()

    # ------------------------------------------------------------------ control

    def play(self, bus: SourceBus, data: np.ndarray, label: str = "") -> Voice:
        channels = BUS_CHANNELS[bus]
        if data.ndim == 1:
            data = data[:, None]
        if data.shape[1] != channels:
            if channels == 1:
                data = data.mean(axis=1, keepdims=True)
            else:
                data = np.repeat(data[:, :1], channels, axis=1) if data.shape[1] == 1 else data[:, :channels]
        voice = Voice(np.ascontiguousarray(data, dtype=np.float32), label)
        with self._lock:
            self._voices[bus].append(voice)
        return voice

    async def wait(self, voice: Voice) -> None:
        if voice.finished:
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()

        def done(_v: Voice) -> None:
            if not fut.done():
                fut.set_result(None)

        voice.add_done_callback(done)
        if voice.finished and not fut.done():
            fut.set_result(None)
        await fut

    def stop_all(self, fade_ms: int = 40) -> int:
        fade = int(self.sample_rate * fade_ms / 1000)
        count = 0
        with self._lock:
            for voices in self._voices.values():
                for v in voices:
                    v.stop(fade)
                    count += 1
        return count

    def stop_voice(self, voice: Voice, fade_ms: int = 40) -> None:
        with self._lock:
            voice.stop(int(self.sample_rate * fade_ms / 1000))

    def active_labels(self) -> list[str]:
        with self._lock:
            return [v.label for voices in self._voices.values() for v in voices if v.label and not v.stopped]

    def _pink_block(self, frames: int) -> np.ndarray:
        if self._pink is None:
            self._pink = make_pink_noise(PINK_LOOP_SAMPLES, self.sample_rate)
        idx = (self._tone_phase + np.arange(frames)) % len(self._pink)
        self._tone_phase = int((self._tone_phase + frames) % len(self._pink))
        return self._pink[idx]

    def set_test_tone(self, output_id: str, on: bool) -> None:
        if on and self._pink is None:  # generate outside the audio callback (~50 ms on a Pi)
            self._pink = make_pink_noise(PINK_LOOP_SAMPLES, self.sample_rate)
        with self._lock:
            if on:
                self._test_tones.add(output_id)
            else:
                self._test_tones.discard(output_id)

    def test_tones(self) -> list[str]:
        return sorted(self._test_tones)

    def read_meters(self) -> dict[str, Any]:
        """Peak levels (dBFS) since the last call, then reset."""
        snap = self._snap
        with self._lock:
            pin = self._peak_in.copy()
            pout = self._peak_out.copy()
            self._peak_in.fill(0)
            self._peak_out.fill(0)
        inputs = {d.id: round(gain_to_db(float(pin[k])), 1) for k, d in enumerate(INPUT_DEFS)}
        outputs = {}
        if snap is not None:
            for k, oid in enumerate(snap.output_ids):
                outputs[oid] = round(gain_to_db(float(pout[k])), 1) if k < len(pout) else MIN_DB
        gr: dict[str, float] = {}
        if snap is not None:
            for k, d in enumerate(snap.input_dyn):
                if d is not None:
                    gr["in:" + snap.input_ids[k]] = d.read_gr()
            for k, d in enumerate(snap.output_dyn):
                if d is not None:
                    gr["out:" + snap.output_ids[k]] = d.read_gr()
        return {"inputs": inputs, "outputs": outputs, "gr": gr}

    def status(self) -> dict[str, Any]:
        return {
            "backend": self.backend if sd is not None else "null",
            "message": self.status_message,
            "sample_rate": self.sample_rate,
            "blocksize": self.blocksize,
            "devices": [
                {"name": s.name, "channels": s.channels, "open": s.open, "error": s.error, "underruns": s.underruns,
                 "master": s is self._master}
                for s in self._streams
            ],
            "test_tones": self.test_tones(),
        }
