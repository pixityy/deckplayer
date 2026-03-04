import os
import asyncio
import base64
import random

import decky
from typing import Optional, List, Dict, Any


class Plugin:
    _mpg123_proc: Optional[asyncio.subprocess.Process] = None
    _reading_task: Optional[asyncio.Task] = None
    _status: Dict[str, Any] = {}
    _playlist: List[str] = []
    _current_index: int = -1
    _current_fps: float = 38.28   # updated from @S output
    _auto_advance: bool = True
    _seeking: bool = False

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def _main(self):
        self.loop = asyncio.get_event_loop()
        self._status = {
            "playing": False,
            "paused": False,
            "current_file": None,
            "current_position": 0.0,
            "duration": 0.0,
            "volume": 70,
            "shuffle": False,
            "repeat": "none",   # none | one | all
            "current_index": -1,
        }
        self._playlist = []
        self._current_index = -1
        await self._start_mpg123()
        decky.logger.info("DeckPlayer: started")

    async def _unload(self):
        decky.logger.info("DeckPlayer: unloading…")
        if self._reading_task:
            self._reading_task.cancel()
        if self._mpg123_proc:
            try:
                await self._send_command("QUIT")
                await asyncio.sleep(0.15)
                self._mpg123_proc.terminate()
            except Exception:
                pass
        decky.logger.info("DeckPlayer: unloaded")

    async def _uninstall(self):
        decky.logger.info("DeckPlayer: uninstalled")

    async def _migration(self):
        pass

    # ── mpg123 process management ──────────────────────────────────────────

    async def _start_mpg123(self):
        try:
            self._mpg123_proc = await asyncio.create_subprocess_exec(
                "mpg123", "-R", "--no-gapless",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._reading_task = self.loop.create_task(self._read_output())
            await self._send_command(f"VOLUME {self._status['volume']}")
            decky.logger.info("DeckPlayer: mpg123 process started")
        except FileNotFoundError:
            decky.logger.error("DeckPlayer: mpg123 not found – please run: sudo pacman -S mpg123")
        except Exception as exc:
            decky.logger.error(f"DeckPlayer: could not start mpg123: {exc}")

    async def _send_command(self, cmd: str):
        proc = self._mpg123_proc
        if proc and proc.stdin and not proc.stdin.is_closing():
            try:
                proc.stdin.write(f"{cmd}\n".encode())
                await proc.stdin.drain()
            except Exception as exc:
                decky.logger.error(f"DeckPlayer: send_command '{cmd}' failed: {exc}")

    # ── Output reader ──────────────────────────────────────────────────────

    async def _read_output(self):
        decky.logger.info("DeckPlayer: output reader started")
        _last_pos_emit = 0.0
        while True:
            try:
                if not self._mpg123_proc or self._mpg123_proc.stdout.at_eof():
                    break
                raw = await asyncio.wait_for(
                    self._mpg123_proc.stdout.readline(), timeout=1.0
                )
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                # --- @P  state update ---
                if line.startswith("@P "):
                    try:
                        state = int(line[3:].strip())
                        was_playing = self._status.get("playing", False)
                        if state == 0:
                            self._status["playing"] = False
                            self._status["paused"] = False
                            self._status["current_position"] = 0.0
                            if was_playing and self._auto_advance:
                                self.loop.create_task(self._on_track_end())
                        elif state == 1:
                            self._status["playing"] = True
                            self._status["paused"] = True
                        elif state == 2:
                            self._status["playing"] = True
                            self._status["paused"] = False
                        await decky.emit("player_status", self._status)
                    except ValueError:
                        pass

                # --- @F  frame / position ---
                elif line.startswith("@F "):
                    try:
                        parts = line[3:].strip().split()
                        if len(parts) >= 4:
                            cur_sec = float(parts[2])
                            rem_sec = float(parts[3])
                            total = cur_sec + rem_sec
                            self._status["current_position"] = cur_sec
                            if total > 0:
                                self._status["duration"] = total
                            # throttle to ~4 Hz to avoid flooding UI
                            if cur_sec - _last_pos_emit >= 0.25:
                                _last_pos_emit = cur_sec
                                await decky.emit(
                                    "player_position",
                                    cur_sec,
                                    self._status["duration"],
                                )
                    except (ValueError, IndexError):
                        pass

                # --- @S  stream info (get fps) ---
                elif line.startswith("@S "):
                    try:
                        parts = line[3:].strip().split()
                        if len(parts) > 2:
                            sample_rate = int(parts[2])
                            layer = int(float(parts[1]))
                            spf = 576 if layer == 2 else 1152
                            self._current_fps = sample_rate / spf
                    except (ValueError, IndexError):
                        pass

                # --- @E  error ---
                elif line.startswith("@E "):
                    decky.logger.error(f"DeckPlayer: mpg123 error: {line[3:]}")

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                decky.logger.error(f"DeckPlayer: reader error: {exc}")
                break
        decky.logger.info("DeckPlayer: output reader stopped")

    # ── Playlist helpers ───────────────────────────────────────────────────

    async def _on_track_end(self):
        repeat = self._status.get("repeat", "none")
        if repeat == "one":
            await self._play_at(self._current_index)
        elif repeat == "all" or self._current_index < len(self._playlist) - 1:
            await self._advance(+1)
        # else: end of playlist, do nothing

    async def _play_at(self, index: int) -> bool:
        if not (0 <= index < len(self._playlist)):
            return False
        path = self._playlist[index]
        self._current_index = index
        self._status["current_index"] = index
        self._status["current_file"] = path
        self._status["current_position"] = 0.0
        await self._send_command(f"LOAD {path}")
        await decky.emit("player_status", self._status)
        return True

    async def _advance(self, direction: int = 1):
        if not self._playlist:
            return
        if self._status.get("shuffle"):
            idx = random.randint(0, len(self._playlist) - 1)
        else:
            idx = (self._current_index + direction) % len(self._playlist)
        await self._play_at(idx)

    # ── Public API ─────────────────────────────────────────────────────────

    async def scan_music(self) -> List[str]:
        """Scan standard music directories for audio files."""
        search_dirs = [
            os.path.join(decky.DECKY_USER_HOME, "Music"),
            os.path.join(decky.DECKY_USER_HOME, "Downloads"),
        ]
        media_base = "/run/media"
        if os.path.exists(media_base):
            try:
                for entry in os.listdir(media_base):
                    search_dirs.append(os.path.join(media_base, entry))
            except PermissionError:
                pass

        audio_exts = {".mp3", ".ogg", ".flac", ".wav", ".m4a", ".aac", ".opus"}
        found: List[str] = []
        for base in search_dirs:
            if not os.path.isdir(base):
                continue
            try:
                for root, dirs, files in os.walk(base):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    for f in files:
                        if os.path.splitext(f)[1].lower() in audio_exts:
                            found.append(os.path.join(root, f))
            except PermissionError:
                continue

        found.sort(key=lambda p: os.path.basename(p).lower())
        decky.logger.info(f"DeckPlayer: found {len(found)} audio files")
        return found

    async def get_track_metadata(self, path: str) -> Dict[str, Any]:
        """Return full metadata (including base64 album art) for one track."""
        meta: Dict[str, Any] = {
            "title": os.path.splitext(os.path.basename(path))[0],
            "artist": "Unknown Artist",
            "album": "Unknown Album",
            "duration": 0.0,
            "cover": None,
            "cover_mime": "image/jpeg",
        }
        try:
            from mutagen import File as MutagenFile  # type: ignore

            audio = MutagenFile(path, easy=True)
            if audio is None:
                return meta
            if audio.info:
                meta["duration"] = audio.info.length
            if audio.tags:
                t = audio.tags
                if "title" in t:
                    meta["title"] = str(t["title"][0])
                if "artist" in t:
                    meta["artist"] = str(t["artist"][0])
                if "album" in t:
                    meta["album"] = str(t["album"][0])

            # Album art – MP3
            try:
                from mutagen.id3 import ID3  # type: ignore
                id3 = ID3(path)
                for key in id3.keys():
                    if key.startswith("APIC"):
                        apic = id3[key]
                        meta["cover"] = base64.b64encode(apic.data).decode()
                        meta["cover_mime"] = apic.mime
                        break
            except Exception:
                pass

            # Album art – FLAC
            if meta["cover"] is None:
                try:
                    from mutagen.flac import FLAC  # type: ignore
                    if path.lower().endswith(".flac"):
                        flac = FLAC(path)
                        if flac.pictures:
                            pic = flac.pictures[0]
                            meta["cover"] = base64.b64encode(pic.data).decode()
                            meta["cover_mime"] = pic.mime
                except Exception:
                    pass

            # Album art – M4A/AAC
            if meta["cover"] is None:
                try:
                    if path.lower().endswith((".m4a", ".aac", ".mp4")):
                        from mutagen.mp4 import MP4  # type: ignore
                        mp4 = MP4(path)
                        if "covr" in mp4:
                            cov = mp4["covr"][0]
                            meta["cover"] = base64.b64encode(bytes(cov)).decode()
                            meta["cover_mime"] = (
                                "image/jpeg" if cov.imageformat == 13 else "image/png"
                            )
                except Exception:
                    pass

        except Exception as exc:
            decky.logger.error(f"DeckPlayer: metadata error for {path}: {exc}")
        return meta

    async def get_track_metadata_batch(self, paths: List[str]) -> List[Dict[str, Any]]:
        """Return lightweight metadata (no cover art) for a list of paths."""
        results = []
        for path in paths:
            meta: Dict[str, Any] = {
                "path": path,
                "title": os.path.splitext(os.path.basename(path))[0],
                "artist": "Unknown Artist",
                "album": "Unknown Album",
                "duration": 0.0,
            }
            try:
                from mutagen import File as MutagenFile  # type: ignore
                audio = MutagenFile(path, easy=True)
                if audio:
                    if audio.info:
                        meta["duration"] = audio.info.length
                    if audio.tags:
                        t = audio.tags
                        if "title" in t:
                            meta["title"] = str(t["title"][0])
                        if "artist" in t:
                            meta["artist"] = str(t["artist"][0])
                        if "album" in t:
                            meta["album"] = str(t["album"][0])
            except Exception:
                pass
            results.append(meta)
        return results

    async def play(self, path: str) -> bool:
        self._status["current_file"] = path
        await self._send_command(f"LOAD {path}")
        return True

    async def pause_resume(self) -> bool:
        await self._send_command("PAUSE")
        return True

    async def stop(self) -> bool:
        self._auto_advance = False
        await self._send_command("STOP")
        self._auto_advance = True
        return True

    async def next_track(self) -> int:
        self._auto_advance = False
        await self._advance(+1)
        self._auto_advance = True
        return self._current_index

    async def prev_track(self) -> int:
        self._auto_advance = False
        if self._status.get("current_position", 0) > 3.0:
            # restart current track if more than 3 s in
            await self._send_command("JUMP 0")
        else:
            await self._advance(-1)
        self._auto_advance = True
        return self._current_index

    async def play_index(self, index: int) -> bool:
        return await self._play_at(index)

    async def set_volume(self, volume: int) -> int:
        vol = max(0, min(100, int(volume)))
        self._status["volume"] = vol
        await self._send_command(f"VOLUME {vol}")
        return vol

    async def seek(self, seconds: float) -> bool:
        frame = int(float(seconds) * self._current_fps)
        await self._send_command(f"JUMP {frame}")
        return True

    async def set_playlist(self, paths: List[str]) -> bool:
        self._playlist = paths
        self._current_index = -1
        return True

    async def get_status(self) -> Dict[str, Any]:
        return self._status

    async def set_shuffle(self, enabled: bool) -> bool:
        self._status["shuffle"] = bool(enabled)
        return self._status["shuffle"]

    async def set_repeat(self, mode: str) -> str:
        if mode not in ("none", "one", "all"):
            mode = "none"
        self._status["repeat"] = mode
        return mode
