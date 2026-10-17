"""Render a voice script: each line cached as a small CBR MP3 (keyed by voice+text), joined
byte-for-byte into one game.mp3 (with ID3 chapters), plus game.vtt captions and manifest.json
with line timings. No intermediate PCM is stored, so the cache is about the size of the MP3."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from nomic.voice.script import Line, VoiceOptions
from nomic.voice.tts import INSTALL_HINT, Synth

log = logging.getLogger(__name__)

GAP_LINE = 0.35  # seconds of silence between lines
GAP_CHAPTER = 1.2  # ...and between turns / sections
MP3_KBPS = 48  # plenty for 24 kHz mono speech
FILES = ("game.mp3", "game.vtt", "manifest.json")  # the only names the API will serve

Progress = Callable[[dict[str, Any]], None]


def _ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


class VoiceRenderer:
    def __init__(self, root: Path, synth_factory: Callable[[], Synth] | None) -> None:
        self.root = root
        self.synth_factory = synth_factory
        self._synth: Synth | None = None
        self._lock = asyncio.Lock()  # one render at a time across the server
        self._status: dict[str, dict[str, Any]] = {}
        self._cancel: set[str] = set()  # games whose render should stop at the next line
        # one model call at a time, so voice previews can slot in between a render's lines
        self._speak_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self.synth_factory is not None

    def game_dir(self, game_id: str) -> Path:
        return self.root / game_id

    def manifest(self, game_id: str) -> dict[str, Any] | None:
        path = self.game_dir(game_id) / "manifest.json"
        if not path.exists():
            return None
        data: dict[str, Any] = json.loads(path.read_text())
        return data

    def status(self, game_id: str) -> dict[str, Any]:
        if game_id in self._status:
            return dict(self._status[game_id])
        return {"state": "done" if self.manifest(game_id) else "idle"}

    def busy(self, game_id: str) -> bool:
        return self._status.get(game_id, {}).get("state") in ("queued", "running")

    def cancel(self, game_id: str) -> bool:
        """Ask a queued or running render to stop after the line in progress. Lines already
        rendered stay cached, so a later render resumes where this one stopped."""
        if not self.busy(game_id):
            return False
        self._cancel.add(game_id)
        return True

    async def render(
        self,
        game_id: str,
        lines: list[Line],
        voices: dict[str, str],
        opts: VoiceOptions,
        on_progress: Progress = lambda s: None,
    ) -> dict[str, Any] | None:
        if self.synth_factory is None:
            raise RuntimeError(INSTALL_HINT)
        st: dict[str, Any] = {
            "state": "queued",
            "done_lines": 0,
            "total_lines": len(lines),
            "new_lines": 0,
            "eta_seconds": None,
            "error": None,
        }
        self._status[game_id] = st
        self._cancel.discard(game_id)
        on_progress(dict(st))

        def cancelled() -> bool:
            if game_id not in self._cancel:
                return False
            self._cancel.discard(game_id)
            st.update(state="cancelled", eta_seconds=None)
            on_progress(dict(st))
            return True

        async with self._lock:
            try:
                if cancelled():  # cancelled while waiting for another render
                    return None
                st["state"] = "running"
                on_progress(dict(st))
                if self._synth is None:
                    self._synth = await asyncio.to_thread(self.synth_factory)
                d = self.game_dir(game_id)
                (d / "lines").mkdir(parents=True, exist_ok=True)
                # one synthesis per distinct (voice, speed, gain, text): repeats share a file
                unique = list({ln.cache_key: ln for ln in lines}.values())
                todo = [ln for ln in unique if not self._line_path(d, ln).exists()]
                st["total_lines"] = len(unique)
                st["new_lines"] = len(todo)
                st["done_lines"] = len(unique) - len(todo)
                t0 = time.monotonic()
                for i, line in enumerate(todo):
                    if cancelled():
                        return None
                    await asyncio.to_thread(self._synth_line, line, self._line_path(d, line))
                    st["done_lines"] += 1
                    per_line = (time.monotonic() - t0) / (i + 1)
                    st["eta_seconds"] = round(per_line * (len(todo) - i - 1))
                    on_progress(dict(st))
                if cancelled():
                    return None
                manifest = await asyncio.to_thread(self._assemble, game_id, d, lines, voices, opts)
                st.update(state="done", eta_seconds=0, duration=manifest["duration"])
                on_progress(dict(st))
                return manifest
            except Exception as e:
                log.exception("voiceover for %s failed", game_id)
                st.update(state="error", error=str(e)[:300])
                on_progress(dict(st))
                return None

    async def preview(self, voice: str, text: str) -> Path:
        """A short sample of one voice, synthesised once and cached in _previews/."""
        if self.synth_factory is None:
            raise RuntimeError(INSTALL_HINT)
        path = self.root / "_previews" / f"{voice}.mp3"
        if path.exists():
            return path
        if self._synth is None:
            self._synth = await asyncio.to_thread(self.synth_factory)
        path.parent.mkdir(parents=True, exist_ok=True)

        def make() -> None:
            assert self._synth is not None
            with self._speak_lock:
                audio = self._synth.speak(text, voice, 1.0)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(self._encode(audio))
            tmp.replace(path)

        await asyncio.to_thread(make)
        return path

    # ------------------------------------------------------------------ steps
    @staticmethod
    def _line_path(d: Path, line: Line) -> Path:
        return d / "lines" / f"{line.cache_key}.mp3"

    def _encode(self, audio: np.ndarray) -> bytes:
        """Mono float audio -> raw CBR MP3 frames. lameenc writes no ID3/Xing header and
        fixed-size frames, so segments can be joined byte-for-byte into one valid stream."""
        import lameenc

        assert self._synth is not None
        enc = lameenc.Encoder()
        enc.set_bit_rate(MP3_KBPS)
        enc.set_in_sample_rate(self._synth.sample_rate)
        enc.set_channels(1)
        enc.set_quality(2)
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        return bytes(enc.encode(pcm.tobytes()) + enc.flush())

    def _seconds(self, mp3: bytes) -> float:
        """Exact duration of a CBR segment from its frame count."""
        assert self._synth is not None
        sr = self._synth.sample_rate
        samples_per_frame = 1152 if sr >= 32000 else 576  # MPEG-1 vs MPEG-2 layer III
        frame_bytes = (144 if sr >= 32000 else 72) * MP3_KBPS * 1000 // sr
        return (len(mp3) // frame_bytes) * samples_per_frame / sr

    def _synth_line(self, line: Line, path: Path) -> None:
        assert self._synth is not None
        with self._speak_lock:
            audio = self._synth.speak(line.text, line.voice, line.speed)
        if line.gain_db:
            audio = audio * (10 ** (line.gain_db / 20))
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(self._encode(audio))
        tmp.replace(path)  # atomic: a half-written file is never mistaken for a cached line

    def _assemble(
        self, game_id: str, d: Path, lines: list[Line], voices: dict[str, str], opts: VoiceOptions
    ) -> dict[str, Any]:
        assert self._synth is not None
        sr = self._synth.sample_rate
        gaps = {
            s: self._encode(np.zeros(int(sr * s), dtype=np.float32))
            for s in (GAP_LINE, GAP_CHAPTER)
        }

        pos = 0.0  # seconds written so far
        entries: list[dict[str, Any]] = []
        chapters: list[dict[str, Any]] = []
        tmp = d / "game.tmp.mp3"
        with tmp.open("wb") as out:

            def write(segment: bytes) -> float:
                nonlocal pos
                out.write(segment)
                start, pos = pos, pos + self._seconds(segment)
                return start

            for line in lines:
                if not chapters or chapters[-1]["title"] != line.chapter:
                    if chapters:
                        write(gaps[GAP_CHAPTER])
                    chapters.append({"title": line.chapter, "start": pos})
                elif entries:
                    write(gaps[GAP_LINE])
                seg = self._line_path(d, line).read_bytes()
                start = write(seg)
                entries.append({**line.to_dict(), "start": start, "duration": pos - start})
        duration = pos
        for i, ch in enumerate(chapters):
            ch["end"] = chapters[i + 1]["start"] if i + 1 < len(chapters) else duration
        self._tag(tmp, game_id, chapters)
        tmp.replace(d / "game.mp3")

        vtt = ["WEBVTT", ""]
        for e in entries:
            speaker = e["speaker"] + (" (thinking)" if e["kind"] == "aside" else "")
            vtt += [
                f"{_ts(e['start'])} --> {_ts(e['start'] + e['duration'])}",
                f"<v {speaker}>{e['text']}",
                "",
            ]
        (d / "game.vtt").write_text("\n".join(vtt))

        manifest = {
            "game_id": game_id,
            "version": int(time.time()),
            "options": opts.model_dump(),
            "voices": voices,
            "duration": duration,
            "chapters": chapters,
            "lines": entries,
        }
        (d / "manifest.json").write_text(json.dumps(manifest))
        # Keep the cache to what this voiceover uses (old wordings, dropped options), so
        # storage stays about the size of game.mp3. Cancelled renders never reach this point.
        keep = {self._line_path(d, ln).name for ln in lines}
        for f in (d / "lines").iterdir():
            if f.name not in keep:
                f.unlink(missing_ok=True)
        return manifest

    @staticmethod
    def _tag(path: Path, game_id: str, chapters: list[dict[str, Any]]) -> None:
        from mutagen.id3 import CHAP, CTOC, ID3, TIT2, CTOCFlags

        tags = ID3()
        tags.add(TIT2(text=[f"Nomic game {game_id}"]))
        ids = [f"ch{i}" for i in range(len(chapters))]
        tags.add(
            CTOC(
                element_id="toc",
                flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
                child_element_ids=ids,
                sub_frames=[TIT2(text=["Chapters"])],
            )
        )
        for cid, ch in zip(ids, chapters, strict=True):
            tags.add(
                CHAP(
                    element_id=cid,
                    start_time=int(ch["start"] * 1000),
                    end_time=int(ch["end"] * 1000),
                    sub_frames=[TIT2(text=[ch["title"]])],
                )
            )
        tags.save(path)
