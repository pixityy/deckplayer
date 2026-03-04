import os
import asyncio
import base64
import json
import random

import decky
from typing import Optional, List, Dict, Any

_MPV_SOCKET = "/tmp/deckplayer_mpv.sock"


class Plugin:
    _mpv_proc: Optional[asyncio.subprocess.Process] = None
    _reader: Optional[asyncio.StreamReader] = None
    _writer: Optional[asyncio.StreamWriter] = None
    _reading_task: Optional[asyncio.Task] = None
    _poll_task: Optional[asyncio.Task] = None
    _status: Dict[str, Any] = {}
    _playlist: List[str] = []
    _current_index: int = -1
    _auto_advance: bool = True
    _req_id: int = 0

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
        await self._start_mpv()
        decky.logger.info("DeckPlayer: started")

    async def _unload(self):
        decky.logger.info("DeckPlayer: unloading…")
        if self._reading_task:
            self._reading_task.cancel()
        if self._poll_task:
            self._poll_task.cancel()
        if self._writer:
            try:
                await self._ipc(["quit"])
                await asyncio.sleep(0.1)
            except Exception:
                pass
        if self._mpv_proc:
            try:
                self._mpv_proc.terminate()
            except Exception:
                pass
        # clean up socket file
        try:
            os.remove(_MPV_SOCKET)
        except OSError:
            pass
        decky.logger.info("DeckPlayer: unloaded")

    async def _uninstall(self):
        decky.logger.info("DeckPlayer: uninstalled")

    async def _migration(self):
        pass

    # ── mpv process ────────────────────────────────────────────────────────

    async def _start_mpv(self):
        # Remove stale socket if present
        try:
            os.remove(_MPV_SOCKET)
        except OSError:
            pass

        try:
            self._mpv_proc = await asyncio.create_subprocess_exec(
                "mpv",
                f"--input-ipc-server={_MPV_SOCKET}",
                "--no-video",
                "--idle=yes",
                "--quiet",
                "--really-quiet",
                "--audio-display=no",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            decky.logger.error("DeckPlayer: mpv not found – install with: sudo pacman -S mpv")
            return
        except Exception as exc:
            decky.logger.error(f"DeckPlayer: failed to start mpv: {exc}")
            return

        # Wait up to 3 s for the socket to appear
        for _ in range(30):
            if os.path.exists(_MPV_SOCKET):
                break
            await asyncio.sleep(0.1)
        else:
            decky.logger.error("DeckPlayer: mpv IPC socket never appeared")
            return

        try:
            self._reader, self._writer = await asyncio.open_unix_connection(_MPV_SOCKET)
        except Exception as exc:
            decky.logger.error(f"DeckPlayer: could not connect to mpv socket: {exc}")
            return

        self._reading_task = self.loop.create_task(self._read_events())

        # Observe properties we care about
        await self._ipc(["observe_property", 1, "time-pos"])
        await self._ipc(["observe_property", 2, "pause"])
        await self._ipc(["observe_property", 3, "duration"])

        # Apply initial volume
        await self._ipc(["set_property", "volume", self._status["volume"]])

        decky.logger.info("DeckPlayer: mpv IPC connected")

    # ── IPC helpers ────────────────────────────────────────────────────────

    async def _ipc(self, command: list) -> Optional[Any]:
        """Send a command and return the response data (fire-and-forget if writer is gone)."""
        if not self._writer or self._writer.is_closing():
            return None
        self._req_id += 1
        msg = json.dumps({"command": command, "request_id": self._req_id}) + "\n"
        try:
            self._writer.write(msg.encode())
            await self._writer.drain()
        except Exception as exc:
            decky.logger.error(f"DeckPlayer: IPC write error: {exc}")
        return None

    async def _get_property(self, name: str) -> Optional[Any]:
        """Request a property value synchronously (best-effort)."""
        if not self._writer or self._writer.is_closing():
            return None
        self._req_id += 1
        rid = self._req_id
        msg = json.dumps({"command": ["get_property", name], "request_id": rid}) + "\n"
        try:
            self._writer.write(msg.encode())
            await self._writer.drain()
        except Exception:
            return None
        # We don't wait for the response; property-change events keep state updated.
        return None

    # ── Event reader ───────────────────────────────────────────────────────

    async def _read_events(self):
        decky.logger.info("DeckPlayer: event reader started")
        _last_emit = 0.0

        while True:
            try:
                raw = await asyncio.wait_for(self._reader.readline(), timeout=1.0)
                if not raw:
                    break
                try:
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue

                event = data.get("event")

                # --- property-change ---
                if event == "property-change":
                    name = data.get("name")
                    val = data.get("data")

                    if name == "time-pos" and val is not None:
                        pos = float(val)
                        self._status["current_position"] = pos
                        if pos - _last_emit >= 0.25:
                            _last_emit = pos
                            await decky.emit(
                                "player_position",
                                pos,
                                self._status["duration"],
                            )

                    elif name == "duration" and val is not None:
                        self._status["duration"] = float(val)

                    elif name == "pause" and val is not None:
                        self._status["paused"] = bool(val)
                        await decky.emit("player_status", self._status)

                # --- playback started ---
                elif event == "start-file":
                    self._status["playing"] = True
                    self._status["paused"] = False
                    self._status["current_position"] = 0.0
                    await decky.emit("player_status", self._status)

                # --- track ended ---
                elif event == "end-file":
                    reason = data.get("reason", "")
                    if reason in ("eof", "stop"):
                        was_playing = self._status.get("playing", False)
                        self._status["playing"] = False
                        self._status["paused"] = False
                        self._status["current_position"] = 0.0
                        await decky.emit("player_status", self._status)
                        if reason == "eof" and was_playing and self._auto_advance:
                            self.loop.create_task(self._on_track_end())

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                decky.logger.error(f"DeckPlayer: event reader error: {exc}")
                break

        decky.logger.info("DeckPlayer: event reader stopped")

    # ── Playlist helpers ───────────────────────────────────────────────────

    async def _on_track_end(self):
        repeat = self._status.get("repeat", "none")
        if repeat == "one":
            await self._play_at(self._current_index)
        elif repeat == "all" or self._current_index < len(self._playlist) - 1:
            await self._advance(+1)

    async def _play_at(self, index: int) -> bool:
        if not (0 <= index < len(self._playlist)):
            return False
        path = self._playlist[index]
        self._current_index = index
        self._status["current_index"] = index
        self._status["current_file"] = path
        await self._ipc(["loadfile", path])
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

        audio_exts = {".mp3", ".ogg", ".flac", ".wav", ".m4a", ".aac", ".opus", ".wma"}
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

            # Album art – MP3 / ID3
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

            # Album art – M4A / AAC
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
        await self._ipc(["loadfile", path])
        return True

    async def pause_resume(self) -> bool:
        paused = self._status.get("paused", False)
        await self._ipc(["set_property", "pause", not paused])
        return True

    async def stop(self) -> bool:
        self._auto_advance = False
        await self._ipc(["stop"])
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
            await self._ipc(["seek", 0, "absolute"])
        else:
            await self._advance(-1)
        self._auto_advance = True
        return self._current_index

    async def play_index(self, index: int) -> bool:
        return await self._play_at(index)

    async def set_volume(self, volume: int) -> int:
        vol = max(0, min(100, int(volume)))
        self._status["volume"] = vol
        await self._ipc(["set_property", "volume", vol])
        return vol

    async def seek(self, seconds: float) -> bool:
        await self._ipc(["seek", float(seconds), "absolute"])
        self._status["current_position"] = float(seconds)
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
