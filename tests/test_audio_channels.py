"""Audio channel regressions; ffmpeg is real, VAD/ASR models are not loaded."""

import contextlib
import functools
import io
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock

import numpy as np

from src.ai.transcriber import IncompleteAudioError, SAMPLE_RATE, Transcriber
from src.runtime.scheduler import AudioDownloader


def collecting_transcriber():
    transcriber = Transcriber()
    waveforms = []
    transcriber._init = Mock()
    transcriber._reset_vad = Mock()
    transcriber._drain_segments = Mock()
    transcriber._vad = Mock()
    transcriber._vad.accept_waveform.side_effect = lambda x: waveforms.append(x.copy())
    return transcriber, waveforms


class PCMFramingTests(unittest.TestCase):
    def consume(self, raw, *, channels=2, fragment_sizes=None, duration=1):
        transcriber, waveforms = collecting_transcriber()
        source = io.BytesIO(raw)
        sizes = iter(fragment_sizes or [])
        with contextlib.redirect_stdout(io.StringIO()):
            result = transcriber._consume_pcm_stream(
                read_fn=lambda n: source.read(min(n, next(sizes, n))),
                is_eof_fn=lambda: source.tell() == len(raw),
                stderr_provider=lambda: f"Duration: 00:00:{duration:05.2f}".encode(),
                return_code_fn=lambda: 0,
                timeout=10,
                input_channels=channels,
            )
        return transcriber, np.concatenate(waveforms), result

    def test_partial_frames_and_temporary_empty_reads_preserve_audio(self):
        # Non-frame-aligned reads and an empty read while the writer is alive.
        signal = np.linspace(-0.2, 0.2, SAMPLE_RATE + 137, dtype=np.float32)
        raw = np.column_stack((signal, np.zeros_like(signal))).tobytes()
        transcriber, received, _ = self.consume(
            raw, fragment_sizes=[1, 3, 0, 7, 11, 65535, 2, 5], duration=1.01,
        )
        self.assertEqual(len(received), len(signal))
        np.testing.assert_allclose(received, signal / np.sqrt(2), atol=1e-7)
        self.assertAlmostEqual(transcriber._last_duration, len(signal) / SAMPLE_RATE)

    def test_completeness_uses_frames_not_interleaved_channel_samples(self):
        for fraction in (0.89, 0.90):
            with self.subTest(fraction=fraction):
                stereo = np.full((int(SAMPLE_RATE * fraction), 2), 0.1, np.float32)
                transcriber, _, result = self.consume(stereo.tobytes())
                self.assertAlmostEqual(transcriber._last_duration, fraction)
                if fraction < 0.9:
                    with self.assertRaises(IncompleteAudioError):
                        transcriber._check_completeness(*result)
                else:
                    transcriber._check_completeness(*result)

    def test_default_mono_consumer_remains_compatible(self):
        signal = np.linspace(-0.1, 0.1, 1003, dtype=np.float32)
        transcriber, waveforms = collecting_transcriber()
        source = io.BytesIO(signal.tobytes())
        with contextlib.redirect_stdout(io.StringIO()):
            transcriber._consume_pcm_stream(
                source.read, lambda: True, lambda: b"", lambda: 0, timeout=10,
            )
        np.testing.assert_array_equal(np.concatenate(waveforms), signal)
        self.assertAlmostEqual(transcriber._last_duration, len(signal) / SAMPLE_RATE)

    def test_incomplete_stereo_frame_raises_runtime_error(self):
        raw = np.zeros((10, 2), dtype=np.float32).tobytes() + b"\x00"
        with self.assertRaisesRegex(RuntimeError, "incomplete PCM frame"):
            self.consume(raw)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for audio integration tests")
class FFmpegChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        # Deliberately include a last block shorter than one second / VAD window.
        self.signal = (0.2 * np.sin(
            2 * np.pi * 317 * np.arange(SAMPLE_RATE + 137) / SAMPLE_RATE
        )).astype(np.float32)

    def fixture(self, samples, name="input.wav"):
        path = Path(self.temp) / name
        channels = 1 if samples.ndim == 1 else samples.shape[1]
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-f", "f32le",
            "-ar", str(SAMPLE_RATE), "-ac", str(channels), "-i", "pipe:0",
            "-c:a", "pcm_f32le", str(path),
        ], input=samples.astype(np.float32).tobytes(), check=True, capture_output=True)
        return path

    def old_mono(self, path):
        raw = subprocess.run([
            "ffmpeg", "-v", "error", "-i", str(path), "-vn",
            "-ar", str(SAMPLE_RATE), "-ac", "1", "-f", "f32le", "pipe:1",
        ], check=True, capture_output=True).stdout
        return np.frombuffer(raw, dtype=np.float32)

    def assert_preserved(self, transcriber, waveforms):
        received = np.concatenate(waveforms)
        self.assertEqual(len(received), len(self.signal))
        self.assertAlmostEqual(transcriber._last_duration, len(self.signal) / SAMPLE_RATE)
        self.assertGreater(np.sqrt(np.mean(received ** 2)), 0.9 * np.sqrt(np.mean(self.signal ** 2)))
        self.assertGreater(abs(np.corrcoef(received, self.signal)[0, 1]), 0.99)

    def test_opposite_channels_do_not_cancel_before_vad(self):
        for scale in (1.0, 0.8):
            with self.subTest(right_scale=scale):
                path = self.fixture(np.column_stack((self.signal, -scale * self.signal)))
                transcriber, waveforms = collecting_transcriber()
                transcriber.transcribe_video(str(path))
                self.assert_preserved(transcriber, waveforms)

    def test_opposite_speech_with_common_noise_keeps_the_speech(self):
        # The old mix retains common noise at ~26% of the strongest input RMS.
        # This models the less perfectly opposed analog-electronics recording.
        noise = (0.038 * np.sin(
            2 * np.pi * 719 * np.arange(len(self.signal)) / SAMPLE_RATE
        )).astype(np.float32)
        path = self.fixture(np.column_stack((self.signal + noise, -self.signal + noise)))
        old = self.old_mono(path)
        ratio = np.sqrt(np.mean(old ** 2) / np.mean((self.signal + noise) ** 2))
        self.assertGreater(ratio, 0.25)
        self.assertLess(ratio, 0.28)
        transcriber, waveforms = collecting_transcriber()
        transcriber.transcribe_video(str(path))
        self.assert_preserved(transcriber, waveforms)

    def test_changing_channel_strength_does_not_flip_speech_polarity(self):
        signal = np.tile(self.signal[:SAMPLE_RATE], 2)
        left_gain = np.repeat([1.0, 0.9], SAMPLE_RATE)
        right_gain = np.repeat([0.9, 1.0], SAMPLE_RATE)
        path = self.fixture(np.column_stack((signal * left_gain, -signal * right_gain)))
        transcriber, waveforms = collecting_transcriber()
        transcriber.transcribe_video(str(path))
        received = np.concatenate(waveforms)
        self.assertEqual(len(received), len(signal))
        self.assertGreater(abs(np.corrcoef(received, signal)[0, 1]), 0.99)

    def test_ordinary_inputs_match_previous_ffmpeg_downmix(self):
        zero = np.zeros_like(self.signal)
        inputs = {
            "in_phase": np.column_stack((self.signal, self.signal)),
            "left_only": np.column_stack((self.signal, zero)),
            "right_only": np.column_stack((zero, self.signal)),
            "silence": np.column_stack((zero, zero)),
            "native_mono": self.signal,
        }
        for name, samples in inputs.items():
            with self.subTest(source=name):
                path = self.fixture(samples)
                transcriber, waveforms = collecting_transcriber()
                transcriber.transcribe_video(str(path))
                np.testing.assert_allclose(np.concatenate(waveforms), self.old_mono(path), atol=1e-7)
                self.assertAlmostEqual(transcriber._last_duration, len(self.signal) / SAMPLE_RATE)

    def test_url_and_downloaded_tail_preserve_opposite_channels(self):
        path = self.fixture(np.column_stack((self.signal, -self.signal)))
        handler = functools.partial(QuietHandler, directory=self.temp)
        with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/{path.name}"
                with self.subTest(path="url"):
                    transcriber, waveforms = collecting_transcriber()
                    transcriber.transcribe_url(url, timeout=10)
                    self.assert_preserved(transcriber, waveforms)

                downloader = AudioDownloader(str(Path(self.temp) / "decoded"), max_concurrent=1)
                client = Mock()
                client.get_video_url.return_value = url
                client.get_stream_params.return_value = (url, "")
                try:
                    downloader.schedule(client, "course", "lecture")
                    handle = downloader.get("lecture", timeout=10)
                    self.assertIsNotNone(handle)
                    with self.subTest(path="downloaded_tail"):
                        transcriber, waveforms = collecting_transcriber()
                        transcriber.transcribe_tail(handle.path, handle.process, handle.stderr_chunks, timeout=10)
                        self.assert_preserved(transcriber, waveforms)
                finally:
                    downloader.shutdown()
            finally:
                server.shutdown()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
