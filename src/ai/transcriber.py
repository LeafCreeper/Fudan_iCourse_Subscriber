"""Speech-to-text using ffmpeg + sherpa-onnx (SenseVoice / FireRed / Zipformer)
with silero VAD.

Two consumption modes share the same VAD/ASR core:

  transcribe_url   stream from URL through a ffmpeg pipe (legacy fallback).
                   Network and ASR are coupled: download is throttled to ASR
                   consumption rate (~8 MB/s of video).
  transcribe_tail  read a disk file (``ffmpeg ... -f f32le path``) with tail-f
                   semantics while ffmpeg writes it at full network speed.
                   Network and ASR are decoupled: ffmpeg pulls 20 MB/s of video
                   into audio.raw, ASR processes from disk at its own pace.
                   The audio file is created and the ffmpeg process is owned by
                   ``scheduler.AudioDownloader`` — Transcriber only reads.

Both modes return ``(transcript, segments)`` where segments is a list of
``{start_ms, end_ms, text}`` dicts kept in memory for bucketer prompt assembly
and NEVER persisted to DB (the joined transcript string is what the DB holds).
"""

import os
import re
import subprocess
import threading
import time
from typing import Callable, Optional

import numpy as np
import sherpa_onnx

from src.runtime import config


SAMPLE_RATE= 16000
WINDOW_SIZE = 512  # VAD window in samples (~32 ms at 16 kHz)
BYTES_PER_SAMPLE = 4  # float32
BYTES_PER_SECOND = SAMPLE_RATE * BYTES_PER_SAMPLE
SILENCE_GAP_THRESHOLD_SEC = 30 * 60  # 30 min of no speech → suspected cutoff


def _safe_ffmpeg_stderr(data: bytes) -> str:
    """Keep useful errors without exposing signed URLs or HTTP credentials."""
    text = data.decode(errors="replace")
    text = re.sub(r"https?://[^\s\"'<>]+", "[redacted URL]", text)
    text = re.sub(r"(?im)^.*(?:cookie|authorization):[^\r\n]*",
                  "[redacted header]", text)
    return text[-2000:]


# ── Shared resource meter state (network delta tracking) ──────────────────
_rm_net_last: tuple[float, int, int] | None = None


def _resource_meter(bound: str = "") -> str:
    """CPU + memory + network throughput suffix for progress lines.
    ``bound`` indicates the limiting resource (e.g. "cpu", "io").
    """
    global _rm_net_last
    try:
        import psutil
        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory().percent
        now = time.time()
        net = psutil.net_io_counters()
        if _rm_net_last is not None:
            dt = now - _rm_net_last[0]
            up = (net.bytes_sent - _rm_net_last[1]) / max(dt, 0.1) / 1024
            down = (net.bytes_recv - _rm_net_last[2]) / max(dt, 0.1) / 1024
            ns = f" down={down:.0f}KB/s"
        else:
            ns = ""
        _rm_net_last = (now, net.bytes_sent, net.bytes_recv)
        tag = f"[{bound.upper()}] " if bound else ""
        return f"  {tag}(cpu={cpu:.0f}% mem={mem:.0f}%{ns})"
    except Exception:
        return ""


# ── Generic per-segment text post-processing ─────────────────────────────
# Applied to every recognized segment regardless of backend.  The cleanups
# below address noise that the LLM otherwise has to spend tokens ignoring:
#
#   • SenseVoice's zh-en-ja-ko-yue model hallucinates kana / hangul tokens
#     in pure-Chinese audio ("うん", "あの", "그래") because the model was
#     trained to handle code-switching and biases toward emitting non-empty
#     output even for filler sounds.
#   • FireRed inserts ``<sil>`` between recognized chunks.
#   • Both backends transcribe occasional bilingual filler English ("Yeah",
#     "OK", "well") from bilingual classroom speech that adds nothing.
#
# What we do NOT touch: real technical English (CNN, FCN, YOLO, RGB, VGG…).
# The English filter is a fixed whitelist of fillers, so tech terms survive
# verbatim.  This is by design — anything subject-specific lives at the
# prompt / LLM layer, not here.
#
# Module-level compiled patterns so we don't recompile per call.

# Japanese hiragana + katakana + half-width katakana
_JP_NOISE_RE = re.compile(r"[぀-ゟ゠-ヿｦ-ﾟ]+")
# Korean: precomposed hangul syllables + jamo blocks
_KR_NOISE_RE = re.compile(r"[가-힯ᄀ-ᇿ㄰-㆏]+")
# English + Chinese filler / interjection words.  Word-bounded where
# possible so tech terms ("CNN", "RNN") are never touched.
#
#   [\s.。,，;；!！?？]*  — leading punctuation / whitespace (greedy)
#   \b(…)\b                 — filler word (word-bounded for ASCII)
#   [\s.。,，;；!！?？、。]+ — trailing punctuation sequence (at least 1)
#
# All three are removed together so we don't leave orphan punctuation
# behind.  For Chinese fillers that can't be word-bounded, we require
# at least ONE punctuation neighbour.
#
# CJK interjections — ONLY pure onomatopoeia / filler sounds, NOT sentence
# particles ("吗""呢""吧""嘛""呀" are meaningful and must stay).
_CN_FILLER_WORDS = r"嗯|呃|啊+|哦+|哈+|呵+|唉|哎|哟|嗨|喔+|噢+|啧|嘶|啧|唔+"
_EN_FILLER_WORDS = (
    r"yeah|yep|yup|ok|okay|uh+|um+|hmm+|ohh*|huh+|hey+|"
    r"you know|i mean|"
    r"the|it|its|that|this|these|those|"
    r"of|to|for|a|an|be|was|were|do|does|did|"
    r"just|only|very|really|actually"
)
# Pattern: filler word surrounded by punctuation on either side.
# Requires AT LEAST ONE punctuation neighbour (leading or trailing) so
# we don't touch fillers embedded in real content.  The entire match
# (optional leading punct + filler + mandatory trailing punct) is
# removed, so no orphan punctuation remains.
_FILLER_PUNCT_RE = re.compile(
    r"(?:"
    r"[\s.。,，;；!！?？、。]+"
    r"\b(?:" + _EN_FILLER_WORDS + r")\b"
    r"[\s.。,，;；!！?？、。]*"
    r"|"
    r"[\s.。,，;；!！?？、。]*"
    r"\b(?:" + _EN_FILLER_WORDS + r")\b"
    r"[\s.。,，;；!！?？、。]+"
    r")",
    re.IGNORECASE,
)
_CN_FILLER_PUNCT_RE = re.compile(
    r"(?:"
    r"[\s.。,，;；!！?？、。]+"
    r"(?:" + _CN_FILLER_WORDS + r")"
    r"[\s.。,，;；!！?？、。]*"
    r"|"
    r"[\s.。,，;；!！?？、。]*"
    r"(?:" + _CN_FILLER_WORDS + r")"
    r"[\s.。,，;；!！?？、。]+"
    r")",
)
# Angle-bracket tokens emitted as literal text by some backends:
#   <sil>          FireRed silence
#   <|zh|>, <|EMO|>, <|HAPPY|>, <|Speech|> …  SenseVoice format tags
#                  (sherpa-onnx usually strips these, but defense in depth)
_BRACKET_TOK_RE = re.compile(r"<\|?[^<>]*\|?>")
# Collapse the leftover whitespace (incl. ideographic full-width space U+3000)
_WS_COLLAPSE_RE = re.compile(r"[ \t　]+")
# After deletions, runs of dangling punctuation + whitespace pile up (e.g.
# "P. Yeah. Yeah。" → "P. . 。" — the periods are orphans left by the
# removed fillers).  Collapse 2+ adjacent punctuation/space chars to a
# single full-width period so the LLM still sees ONE sentence break.
_ORPHAN_PUNCT_RE = re.compile(r"[\s.。,，;；!！?？]{2,}")
# Trim leading/trailing punctuation+whitespace on each segment — the inter-
# segment join in ``_consume_pcm_stream`` already inserts a space, so any
# punctuation at the edges is noise from a deletion at the boundary.
_EDGE_PUNCT = " \t　.。,，;；!！?？"


def _postprocess_segment(text: str) -> str:
    """Strip cross-backend ASR noise from one recognized segment."""
    text = _BRACKET_TOK_RE.sub("", text)
    # Japanese / Korean kana — always noise in Chinese lectures
    text = _JP_NOISE_RE.sub("", text)
    text = _KR_NOISE_RE.sub("", text)
    # Filler words with punctuation neighbours — remove word + punct together
    text = _FILLER_PUNCT_RE.sub("", text)
    text = _CN_FILLER_PUNCT_RE.sub("", text)
    # Clean up what's left
    text = _ORPHAN_PUNCT_RE.sub("。", text)
    text = _WS_COLLAPSE_RE.sub(" ", text)
    text = text.strip(_EDGE_PUNCT).strip()
    if text in (".", "。", "？", "。", "!", "；", "，"):
        return ""
    return text


class Transcriber:
    """Sherpa-onnx ASR transcriber with VAD segmentation.

    Backend is chosen at construction by ``config.ASR_BACKEND``; pass
    ``backend=`` explicitly to override.  Supports:
      - ``firered``    sherpa-onnx-fire-red-asr2-ctc-* (single .onnx)
      - ``sensevoice`` sherpa-onnx-sense-voice-*       (single .onnx)
      - ``zipformer``  sherpa-onnx-zipformer-*         (encoder/decoder/joiner)
    Each backend looks at ``config.ASR_MODEL_DIR`` (default determined by
    the env var) for its files; the same VAD is used across backends.

    Use Cases:
      - Production: pick one backend via env, leave it for the run.
      - A/B testing (scripts/test_prod_lecture.py): build N Transcribers
        with different backend= kwargs, transcribe same audio, diff outputs.
    """

    def __init__(self, backend: Optional[str] = None,
                 model_dir: Optional[str] = None,
                 num_threads: Optional[int] = None):
        self._backend = (backend or config.ASR_BACKEND).lower()
        self._model_dir = model_dir or config.ASR_MODEL_DIR
        self._num_threads = num_threads or config.ASR_NUM_THREADS
        self._recognizer = None
        self._vad = None
        self._vad_config = None
        self._last_duration = 0.0           # audio seconds from last transcription
        self._last_transcript = ""           # text from last transcription
        self._last_segments: list[dict] = []
        self._media_duration: Optional[float] = None

    # ── Model lifecycle ─────────────────────────────────────────────────

    def _init(self):
        if self._recognizer is not None:
            return

        backend = self._backend
        if backend == "firered":
            self._recognizer = self._init_firered()
        elif backend == "sensevoice":
            self._recognizer = self._init_sensevoice()
        elif backend == "zipformer":
            self._recognizer = self._init_zipformer()
        else:
            raise ValueError(
                f"Unknown ASR_BACKEND={backend!r}; expected "
                f"firered / sensevoice / zipformer"
            )

        vad_path = config.SILERO_VAD_PATH
        if not os.path.isfile(vad_path):
            raise FileNotFoundError(
                f"silero_vad.onnx not found at '{vad_path}'. Download from "
                f"https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models"
            )
        self._vad_config = sherpa_onnx.VadModelConfig()
        self._vad_config.silero_vad.model = vad_path
        # VAD tuned to feed the recognizer longer chunks.  Past defaults
        # (min_silence_duration=0.25) fragmented continuous lecturer speech
        # at every breath, which both increased the per-segment ASR fixed
        # cost AND gave SenseVoice less context to anchor language detection
        # on (a leading "うん" hallucination is much more likely on a 2-second
        # clip than on a 20-second one).  Values now:
        #   min_silence_duration = 0.8   sec  — only end on a real pause
        #   max_speech_duration  = 30.0  sec  — SenseVoice was trained on
        #                                       up-to-30s windows; matches
        #                                       its receptive field
        self._vad_config.silero_vad.min_silence_duration = 0.8
        self._vad_config.silero_vad.max_speech_duration = 30.0
        self._vad_config.sample_rate = SAMPLE_RATE
        self._reset_vad()
        print(f"[Transcriber] Model loaded (backend={backend}, "
              f"threads={self._num_threads}).")

    def _resolve_first(self, dir_: str, candidates: list[str]) -> str:
        """Return the first existing path in dir_ matching any candidate name."""
        for name in candidates:
            p = os.path.join(dir_, name)
            if os.path.isfile(p):
                return p
        raise FileNotFoundError(
            f"None of {candidates} found in {dir_}"
        )

    def _init_firered(self):
        model_path = self._resolve_first(
            self._model_dir, ["model.int8.onnx", "model.onnx"],
        )
        tokens_path = os.path.join(self._model_dir, "tokens.txt")
        if not os.path.isfile(tokens_path):
            raise FileNotFoundError(f"tokens.txt missing in {self._model_dir}")
        print(f"[Transcriber] Loading FireRed ASR2 CTC from {model_path}...")
        return sherpa_onnx.OfflineRecognizer.from_fire_red_asr_ctc(
            model=model_path,
            tokens=tokens_path,
            num_threads=self._num_threads,
            debug=False,
        )

    def _init_sensevoice(self):
        model_path = self._resolve_first(
            self._model_dir, ["model.int8.onnx", "model.onnx"],
        )
        tokens_path = os.path.join(self._model_dir, "tokens.txt")
        if not os.path.isfile(tokens_path):
            raise FileNotFoundError(f"tokens.txt missing in {self._model_dir}")
        print(f"[Transcriber] Loading SenseVoice from {model_path}...")
        return sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=model_path,
            tokens=tokens_path,
            num_threads=self._num_threads,
            use_itn=True,
            debug=False,
        )

    def _init_zipformer(self):
        # Zipformer ships three sibling files in a release: encoder, decoder,
        # joiner.  Filenames vary slightly across releases (epoch-99 vs
        # bilingual etc.), so glob the dir for the first match.
        import glob
        def find_one(pattern_list):
            for pat in pattern_list:
                hits = sorted(glob.glob(os.path.join(self._model_dir, pat)))
                if hits:
                    return hits[0]
            raise FileNotFoundError(
                f"No file matching {pattern_list} in {self._model_dir}"
            )
        enc = find_one(["encoder*.int8.onnx", "encoder*.onnx"])
        dec = find_one(["decoder*.int8.onnx", "decoder*.onnx"])
        joi = find_one(["joiner*.int8.onnx", "joiner*.onnx"])
        tokens_path = os.path.join(self._model_dir, "tokens.txt")
        if not os.path.isfile(tokens_path):
            raise FileNotFoundError(f"tokens.txt missing in {self._model_dir}")
        print(f"[Transcriber] Loading Zipformer from {self._model_dir}...")
        return sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=enc, decoder=dec, joiner=joi,
            tokens=tokens_path,
            num_threads=self._num_threads,
            debug=False,
        )

    def _reset_vad(self):
        """Re-create VAD to reset internal counters (prevents INT32 overflow)."""
        self._vad = sherpa_onnx.VoiceActivityDetector(
            self._vad_config, buffer_size_in_seconds=120
        )

    def _drain_segments(self, segments: list[dict]):
        """Recognize and append every complete VAD segment as {start_ms, end_ms, text}."""
        while not self._vad.empty():
            speech = self._vad.front
            samples = speech.samples
            seg_start_samples = int(getattr(speech, "start", 0))
            self._vad.pop()
            stream = self._recognizer.create_stream()
            stream.accept_waveform(SAMPLE_RATE, samples)
            self._recognizer.decode_stream(stream)
            text = _postprocess_segment(stream.result.text)
            if text:
                start_ms = int(seg_start_samples / SAMPLE_RATE * 1000)
                end_ms = int(
                    (seg_start_samples + len(samples)) / SAMPLE_RATE * 1000
                )
                segments.append({
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "text": text,
                })

    # ── Shared consumer core ────────────────────────────────────────────

    def _consume_pcm_stream(
        self,
        read_fn: Callable[[int], bytes],
        is_eof_fn: Callable[[], bool],
        stderr_provider: Callable[[], bytes],
        return_code_fn: Callable[[], Optional[int]],
        timeout: int,
        wait_on_empty_sec: float = 0.0,
        label: str = "stream",
    ) -> tuple[str, list[dict]]:
        """Pull PCM f32le mono 16 kHz bytes through a callable, feed VAD/ASR,
        return ``(transcript, segments)``.

        Args:
            read_fn(n): return up to n bytes; ``b""`` means "no data right now".
            is_eof_fn(): ``True`` iff there will be no more data ever.  When
                read_fn returns empty and is_eof_fn() is False, we sleep
                ``wait_on_empty_sec`` then retry (tail-f pattern).
            stderr_provider(): returns the accumulated ffmpeg stderr bytes; used
                to extract media duration and report errors.
            return_code_fn(): ffmpeg's process returncode (``None`` if running).
            timeout: total seconds before we give up and raise TimeoutError.
            wait_on_empty_sec: poll interval for the tail-f wait branch; set
                0 for pipe sources (where empty read truly means EOF).
        """
        self._init()
        self._reset_vad()
        t0 = time.time()
        print(f"[Transcriber] Starting {label} at {time.strftime('%H:%M:%S')}",
              flush=True)

        segments: list[dict] = []
        total_read = 0      # samples
        total_bytes = 0
        last_report = t0
        last_segment_at = 0.0
        silence_marked = False

        while True:
            now = time.time()
            if now - t0 > timeout:
                raise TimeoutError(
                    f"Transcription timed out after {timeout}s"
                )

            raw = read_fn(BYTES_PER_SECOND)
            if not raw:
                if is_eof_fn():
                    break
                if wait_on_empty_sec > 0:
                    time.sleep(wait_on_empty_sec)
                continue

            total_bytes += len(raw)
            samples = np.frombuffer(raw, dtype=np.float32)
            total_read += len(samples)
            audio_pos = total_read / SAMPLE_RATE

            # Progress report every 60 s
            if now - last_report >= 60:
                elapsed = now - t0
                speed_kbps = (total_bytes / 1024) / elapsed
                print(
                    f"[Transcriber] Progress: {audio_pos:.0f}s audio,"
                    f" {total_bytes / 1024 / 1024:.1f} MB consumed,"
                    f" {speed_kbps:.1f} KB/s,"
                    f" {len(segments)} segments so far{_resource_meter("cpu")}",
                    flush=True,
                )
                last_report = now

            # Feed VAD in WINDOW_SIZE chunks
            prev_count = len(segments)
            idx = 0
            while idx + WINDOW_SIZE <= len(samples):
                self._vad.accept_waveform(samples[idx:idx + WINDOW_SIZE])
                idx += WINDOW_SIZE
                self._drain_segments(segments)

            if idx < len(samples):
                self._vad.accept_waveform(samples[idx:])

            if len(segments) > prev_count:
                last_segment_at = audio_pos
                silence_marked = False

            # Detect 30-min silence gap mid-stream
            if (not silence_marked
                    and segments
                    and audio_pos - last_segment_at >= SILENCE_GAP_THRESHOLD_SEC):
                gap_min = (audio_pos - last_segment_at) / 60
                marker_text = (
                    f"\n\n[注意：从 {last_segment_at / 60:.0f} 分钟处起"
                    f"已超过 {gap_min:.0f} 分钟未检测到语音，"
                    f"音频可能已中断或录音设备出现故障。"
                    f"以下内容可能不完整。]\n\n"
                )
                segments.append({
                    "start_ms": int(last_segment_at * 1000),
                    "end_ms": int(audio_pos * 1000),
                    "text": marker_text,
                })
                silence_marked = True
                print(
                    f"[Transcriber] WARNING: {gap_min:.0f} min silence "
                    f"after {last_segment_at / 60:.0f} min of audio",
                    flush=True,
                )

        # Flush VAD
        self._vad.flush()
        self._drain_segments(segments)

        stderr_output = stderr_provider()

        # Parse total media duration from ffmpeg stderr
        self._media_duration = None
        dur_match = re.search(
            rb"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr_output,
        )
        if dur_match:
            h, m, s = dur_match.groups()
            self._media_duration = int(h) * 3600 + int(m) * 60 + float(s)

        elapsed = time.time() - t0
        duration = total_read / SAMPLE_RATE
        self._last_duration = duration

        if self._media_duration:
            print(
                f"[Transcriber] Media duration: {self._media_duration:.0f}s "
                f"({self._media_duration / 60:.1f} min), "
                f"received: {duration:.0f}s ({duration / 60:.1f} min)",
                flush=True,
            )

        rc = return_code_fn()
        # -9/-15: SIGKILL/SIGTERM — the downloader's release() terminates
        # ffmpeg once we're done reading; neither is an ffmpeg failure.
        if rc not in (0, -9, -15, None):
            stderr_text = _safe_ffmpeg_stderr(stderr_output)
            if "does not contain any stream" in stderr_text:
                raise NoAudioStreamError(
                    f"ffmpeg found no audio stream (video-only file).\n"
                    f"stderr (last 500 chars):\n{stderr_text[-500:]}"
                )
            raise AudioDownloadError(
                f"ffmpeg exited with code {rc}.\n"
                f"stderr (last 500 chars):\n{stderr_text[-500:]}"
            )

        if total_bytes == 0:
            stderr_text = _safe_ffmpeg_stderr(stderr_output)[-500:]
            raise AudioDownloadError(
                f"ffmpeg produced no audio output (0 bytes received).\n"
                f"stderr (last 500 chars):\n{stderr_text}"
            )

        speed_kbps = (total_bytes / 1024) / elapsed if elapsed > 0 else 0
        transcript = " ".join(s["text"] for s in segments)

        # Final silence check
        if (not silence_marked
                and segments
                and duration - last_segment_at >= SILENCE_GAP_THRESHOLD_SEC):
            gap_min = (duration - last_segment_at) / 60
            tail_marker = (
                f"\n\n[注意：从 {last_segment_at / 60:.0f} 分钟处起"
                f"至音频结束（{duration / 60:.0f} 分钟），"
                f"共 {gap_min:.0f} 分钟未检测到语音，"
                f"音频可能已中断或录音设备出现故障。以上内容可能不完整。]"
            )
            transcript += tail_marker
            segments.append({
                "start_ms": int(last_segment_at * 1000),
                "end_ms": int(duration * 1000),
                "text": tail_marker,
            })
            print(
                f"[Transcriber] WARNING: audio ended with {gap_min:.0f} min "
                f"of silence after {last_segment_at / 60:.0f} min",
                flush=True,
            )

        print(
            f"[Transcriber] Done at {time.strftime('%H:%M:%S')}: "
            f"{duration:.0f}s audio, {total_bytes / 1024 / 1024:.1f} MB, "
            f"avg {speed_kbps:.1f} KB/s, "
            f"{len(transcript)} chars, {len(segments)} segments "
            f"in {elapsed:.0f}s",
            flush=True,
        )
        self._last_transcript = transcript
        self._last_segments = segments
        return transcript, segments

    def _check_completeness(self, transcript: str,
                            segments: list[dict], diagnostics: str = "") -> None:
        """Raise IncompleteAudioError when we received <90 % of the media.

        ``_media_duration`` is parsed from ffmpeg's stderr by
        ``_consume_pcm_stream``; when it's unknown the check is skipped.
        The partial result rides on the exception so the caller can still
        inspect it."""
        if self._media_duration and self._media_duration > 0:
            ratio = self._last_duration / self._media_duration
            if ratio < 0.9:
                raise IncompleteAudioError(
                    f"Only received {self._last_duration:.0f}s of "
                    f"{self._media_duration:.0f}s audio ({ratio:.0%}). "
                    f"Connection may have dropped.{diagnostics}",
                    actual_duration=self._last_duration,
                    expected_duration=self._media_duration,
                    transcript=transcript,
                    segments=segments,
                )

    # ── Public mode 1 — disk tail-f (preferred) ─────────────────────────

    def transcribe_tail(self, audio_path: str,
                        ffmpeg_proc: subprocess.Popen,
                        stderr_chunks: list[bytes],
                        timeout: int = 7200,
                        ) -> tuple[str, list[dict]]:
        """Read PCM samples from a disk file that ffmpeg is *concurrently* writing.

        Args:
            audio_path: Path of the f32le mono 16 kHz file ffmpeg writes to.
            ffmpeg_proc: The Popen handle for the ffmpeg process so we can
                know when "no more data ever".  Owned by the caller (Scheduler).
            stderr_chunks: List that the caller's stderr-drain thread appends to.
            timeout: Total seconds before giving up.

        Returns:
            ``(transcript, segments)``.

        Raises:
            RuntimeError if ffmpeg never wrote any audio.
            NoAudioStreamError if ffmpeg reported "does not contain any stream".
            IncompleteAudioError if <90 % of the media duration arrived
                (e.g. ffmpeg exited cleanly on a truncated stream).
            TimeoutError on overall ``timeout``.
        """
        # Wait for file to exist (ffmpeg may not have flushed the first byte yet)
        t_wait = time.time()
        while not os.path.exists(audio_path):
            if ffmpeg_proc.poll() is not None:
                stderr_text = _safe_ffmpeg_stderr(b"".join(stderr_chunks))
                raise AudioDownloadError(
                    f"ffmpeg exited (rc={ffmpeg_proc.returncode}) before "
                    f"writing audio file {audio_path}.\nstderr:\n{stderr_text}"
                )
            if time.time() - t_wait > 60:
                raise TimeoutError(
                    f"ffmpeg did not create {audio_path} within 60 s"
                )
            time.sleep(0.1)

        f = open(audio_path, "rb")
        try:
            def read_fn(n: int) -> bytes:
                return f.read(n)

            def is_eof_fn() -> bool:
                # Truly EOF iff ffmpeg has exited.  ``_consume_pcm_stream``
                # will do one more read after we say EOF, but our open file
                # handle already reflects all bytes ffmpeg wrote.
                return ffmpeg_proc.poll() is not None

            transcript, segments = self._consume_pcm_stream(
                read_fn=read_fn,
                is_eof_fn=is_eof_fn,
                stderr_provider=lambda: b"".join(stderr_chunks),
                return_code_fn=lambda: ffmpeg_proc.returncode,
                timeout=timeout,
                wait_on_empty_sec=0.1,
                label="tail",
            )
            # ffmpeg can exit 0 on a server-side truncated stream; without
            # this check the partial transcript would silently pass as a
            # complete lecture.
            self._check_completeness(
                transcript, segments,
                f"\nffmpeg rc={ffmpeg_proc.returncode}; stderr:\n"
                + _safe_ffmpeg_stderr(b"".join(stderr_chunks)),
            )
            return transcript, segments
        finally:
            f.close()

    # ── Public mode 2 — legacy URL streaming (fallback) ─────────────────

    def transcribe_url(self, url: str, timeout: int = 7200,
                       http_headers: Optional[str] = None
                       ) -> tuple[str, list[dict]]:
        """Stream audio directly from a URL through an ffmpeg pipe.

        Kept as a fallback for the case where disk-buffer prefetch can't run
        (e.g. local dev without VideoDownloadCache).  Network speed gets
        coupled to ASR speed via the pipe — preferred path is
        ``transcribe_tail`` instead.
        """
        cmd = ["ffmpeg"]
        if http_headers:
            cmd += ["-headers", http_headers]
        cmd += [
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", url,
            "-vn",
            "-ar", str(SAMPLE_RATE), "-ac", "1",
            "-f", "f32le", "-",
        ]
        return self._transcribe_with_inline_ffmpeg(cmd, timeout=timeout)

    def transcribe_video(self, video_path: str) -> tuple[str, list[dict]]:
        """Transcribe a local mp4 file via ffmpeg-to-pipe."""
        cmd = [
            "ffmpeg", "-i", video_path,
            "-ar", str(SAMPLE_RATE), "-ac", "1",
            "-f", "f32le", "-",
        ]
        return self._transcribe_with_inline_ffmpeg(cmd)

    def _transcribe_with_inline_ffmpeg(self, cmd: list[str],
                                       timeout: int = 7200
                                       ) -> tuple[str, list[dict]]:
        """Spawn ffmpeg ourselves, pipe stdout into the VAD/ASR consumer."""
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        stderr_chunks: list[bytes] = []

        def _drain_stderr():
            try:
                for line in proc.stderr:
                    stderr_chunks.append(line)
            except Exception:
                pass

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        try:
            transcript, segments = self._consume_pcm_stream(
                read_fn=lambda n: proc.stdout.read(n),
                is_eof_fn=lambda: True,  # pipe: empty read == EOF
                stderr_provider=lambda: b"".join(stderr_chunks),
                return_code_fn=lambda: proc.returncode,
                timeout=timeout,
                wait_on_empty_sec=0,
                label="pipe",
            )
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            stderr_thread.join(timeout=5)

        # Both URL/pipe modes enforce the 90 % completeness check
        self._check_completeness(
            transcript, segments,
            f"\nffmpeg rc={proc.returncode}; stderr:\n"
            + _safe_ffmpeg_stderr(b"".join(stderr_chunks)),
        )

        return transcript, segments

    @staticmethod
    def probe_duration(url: str, http_headers: Optional[str] = None,
                       timeout: int = 30) -> Optional[float]:
        """Use ffprobe to get media duration in seconds. Returns None on failure."""
        cmd = ["ffprobe", "-v", "error"]
        if http_headers:
            cmd += ["-headers", http_headers]
        cmd += [
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            url,
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        except (subprocess.TimeoutExpired, ValueError):
            pass
        return None


class AudioDownloadError(RuntimeError):
    """Media extraction failed; a fresh signed URL may recover it."""


class IncompleteAudioError(AudioDownloadError):
    """Raised when downloaded audio is significantly shorter than expected.

    Carries the partial result so the caller can decide what to do with it
    without reaching into Transcriber internals."""

    def __init__(self, message: str, actual_duration: float,
                 expected_duration: float,
                 transcript: str = "",
                 segments: Optional[list[dict]] = None):
        super().__init__(message)
        self.actual_duration = actual_duration
        self.expected_duration = expected_duration
        self.transcript = transcript
        self.segments = segments or []


class NoAudioStreamError(RuntimeError):
    """Raised when the media contains no audio stream (video-only file)."""
