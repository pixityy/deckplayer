import {
  callable,
  definePlugin,
  addEventListener,
  removeEventListener,
  toaster,
} from "@decky/api";
import { staticClasses } from "@decky/ui";
import { useState, useEffect, useRef, FC } from "react";
import {
  FaPlay,
  FaPause,
  FaBackwardStep,
  FaForwardStep,
  FaShuffle,
  FaRepeat,
  FaVolumeHigh,
  FaVolumeXmark,
  FaMusic,
  FaBars,
  FaCompactDisc,
} from "react-icons/fa6";

// ── Types ──────────────────────────────────────────────────────────────────

interface TrackMeta {
  title: string;
  artist: string;
  album: string;
  duration: number;
  cover: string | null;
  cover_mime?: string;
}

interface TrackInfo {
  path: string;
  title: string;
  artist: string;
  duration: number;
}

interface PlayerStatus {
  playing: boolean;
  paused: boolean;
  current_file: string | null;
  current_position: number;
  duration: number;
  volume: number;
  shuffle: boolean;
  repeat: string;
  current_index: number;
}

// ── Backend callables ──────────────────────────────────────────────────────

const beScanMusic = callable<[], string[]>("scan_music");
const beGetMeta = callable<[path: string], TrackMeta>("get_track_metadata");
const beGetBatch = callable<[paths: string[]], TrackInfo[]>("get_track_metadata_batch");
const bePlay = callable<[path: string], boolean>("play");
const bePauseResume = callable<[], boolean>("pause_resume");
const beNext = callable<[], number>("next_track");
const bePrev = callable<[], number>("prev_track");
const beSetVolume = callable<[volume: number], number>("set_volume");
const beSeek = callable<[seconds: number], boolean>("seek");
const beSetPlaylist = callable<[paths: string[]], boolean>("set_playlist");
const bePlayIndex = callable<[index: number], boolean>("play_index");
const beGetStatus = callable<[], PlayerStatus>("get_status");
const beSetShuffle = callable<[enabled: boolean], boolean>("set_shuffle");
const beSetRepeat = callable<[mode: string], string>("set_repeat");

// ── Helpers ────────────────────────────────────────────────────────────────

function fmt(secs: number): string {
  if (!secs || isNaN(secs) || secs < 0) return "0:00";
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

const ACCENT = "#64b4ff";
const ACCENT2 = "#0073ff";
const BG_CARD = "rgba(255,255,255,0.05)";
const TXT = "rgba(255,255,255,0.92)";
const TXT_DIM = "rgba(255,255,255,0.55)";
const TXT_MUTED = "rgba(255,255,255,0.30)";

// ── CSS injection ──────────────────────────────────────────────────────────

const CSS_ID = "deckplayer-styles";
const CSS = `
@keyframes dp-spin {
  from { transform: rotate(0deg); }
  to   { transform: rotate(360deg); }
}
@keyframes dp-pulse {
  0%,100% { opacity:1; }
  50%      { opacity:0.55; }
}
.dp-disc-wrap {
  animation: dp-spin 5s linear infinite;
  animation-play-state: paused;
}
.dp-disc-wrap.playing {
  animation-play-state: running;
}
.dp-btn {
  display:flex; align-items:center; justify-content:center;
  border:none; background:none; cursor:pointer; padding:0;
  transition: opacity .15s, transform .12s;
}
.dp-btn:hover  { opacity:.8; transform:scale(1.12); }
.dp-btn:active { transform:scale(.93); }
.dp-track {
  display:flex; align-items:center; gap:8px;
  padding:5px 8px; border-radius:7px; cursor:pointer;
  transition: background .13s;
}
.dp-track:hover { background:rgba(255,255,255,0.07); }
.dp-track.active { background:rgba(100,180,255,0.13); }
.dp-progress {
  height:4px; border-radius:2px; cursor:pointer;
  transition: height .12s;
  background: rgba(255,255,255,0.15);
  position:relative;
}
.dp-progress:hover { height:7px; }
.dp-vol-range {
  -webkit-appearance:none; appearance:none;
  width:100%; height:4px; background:transparent;
  outline:none; cursor:pointer;
}
.dp-vol-range::-webkit-slider-thumb {
  -webkit-appearance:none;
  width:13px; height:13px; border-radius:50%;
  background:${ACCENT}; cursor:pointer;
  box-shadow:0 0 6px rgba(100,180,255,.5);
}
.dp-vol-range::-moz-range-thumb {
  width:13px; height:13px; border-radius:50%;
  background:${ACCENT}; border:none; cursor:pointer;
}
.dp-glow { animation: dp-pulse 2s ease-in-out infinite; }
`;

function injectStyles() {
  if (document.getElementById(CSS_ID)) return;
  const el = document.createElement("style");
  el.id = CSS_ID;
  el.textContent = CSS;
  document.head.appendChild(el);
}
function removeStyles() {
  document.getElementById(CSS_ID)?.remove();
}

// ── CD Disc ────────────────────────────────────────────────────────────────

const CDDisc: FC<{
  isPlaying: boolean;
  cover: string | null;
  mime?: string;
  size?: number;
}> = ({ isPlaying, cover, mime = "image/jpeg", size = 152 }) => {
  const coverUrl = cover ? `data:${mime};base64,${cover}` : null;

  return (
    <div style={{ position: "relative", width: size, height: size, flexShrink: 0 }}>
      {/* Spinning disc */}
      <div
        className={`dp-disc-wrap${isPlaying ? " playing" : ""}`}
        style={{
          width: size,
          height: size,
          borderRadius: "50%",
          overflow: "hidden",
          position: "relative",
          boxShadow: `
            0 0 0 2px rgba(255,255,255,0.08),
            0 0 0 4px rgba(0,0,0,0.6),
            0 6px 28px rgba(0,0,0,0.85),
            0 0 40px rgba(100,180,255,${isPlaying ? "0.18" : "0"})
          `,
          transition: "box-shadow 0.6s ease",
        }}
      >
        {/* Base – album art or dark gradient */}
        <div
          style={{
            position: "absolute",
            inset: 0,
            borderRadius: "50%",
            background: coverUrl
              ? `url(${coverUrl}) center/cover no-repeat`
              : `radial-gradient(circle at 35% 30%, #2a2a4a 0%, #111128 60%, #0a0a18 100%)`,
          }}
        />

        {/* Groove rings (semi-transparent) */}
        <div
          style={{
            position: "absolute",
            inset: 0,
            borderRadius: "50%",
            background: `
              radial-gradient(circle,
                transparent 38%,
                rgba(0,0,0,0.18) 39%,
                transparent 40%,
                transparent 46%,
                rgba(0,0,0,0.14) 47%,
                transparent 48%,
                transparent 54%,
                rgba(0,0,0,0.10) 55%,
                transparent 56%,
                transparent 62%,
                rgba(0,0,0,0.08) 63%,
                transparent 64%
              )`,
            pointerEvents: "none",
          }}
        />

        {/* Sheen highlight */}
        <div
          style={{
            position: "absolute",
            inset: 0,
            borderRadius: "50%",
            background:
              "linear-gradient(135deg, rgba(255,255,255,0.14) 0%, transparent 45%, rgba(0,0,0,0.22) 100%)",
            pointerEvents: "none",
          }}
        />

        {/* Default icon when no cover art */}
        {!coverUrl && (
          <div
            style={{
              position: "absolute",
              inset: 0,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "rgba(255,255,255,0.22)",
              fontSize: size * 0.22,
            }}
          >
            <FaMusic />
          </div>
        )}
      </div>

      {/* Center hole – not spinning */}
      <div
        style={{
          position: "absolute",
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          width: size * 0.125,
          height: size * 0.125,
          borderRadius: "50%",
          background: "radial-gradient(circle, #0b0b14 55%, #1c1c30)",
          boxShadow:
            "inset 0 1px 4px rgba(255,255,255,0.12), 0 0 0 1.5px rgba(0,0,0,0.9)",
          zIndex: 5,
        }}
      />
    </div>
  );
};

// ── Progress Bar ───────────────────────────────────────────────────────────

const ProgressBar: FC<{
  position: number;
  duration: number;
  onSeek: (s: number) => void;
}> = ({ position, duration, onSeek }) => {
  const ref = useRef<HTMLDivElement>(null);
  const pct = duration > 0 ? Math.min(100, (position / duration) * 100) : 0;

  const seek = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!ref.current || !duration) return;
    const rect = ref.current.getBoundingClientRect();
    onSeek(Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)) * duration);
  };

  return (
    <div style={{ padding: "0 2px" }}>
      <div ref={ref} className="dp-progress" onClick={seek}>
        {/* Fill */}
        <div
          style={{
            width: `${pct}%`,
            height: "100%",
            background: `linear-gradient(90deg, ${ACCENT}, ${ACCENT2})`,
            borderRadius: "2px",
            position: "relative",
            transition: "width 0.35s linear",
          }}
        >
          {/* Knob */}
          <div
            style={{
              position: "absolute",
              right: -5,
              top: "50%",
              transform: "translateY(-50%)",
              width: 11,
              height: 11,
              borderRadius: "50%",
              background: "#fff",
              boxShadow: `0 0 6px rgba(100,180,255,0.9)`,
            }}
          />
        </div>
      </div>
      {/* Time */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          marginTop: 5,
          fontSize: 11,
          color: TXT_MUTED,
          fontVariantNumeric: "tabular-nums",
        }}
      >
        <span>{fmt(position)}</span>
        <span>{fmt(duration)}</span>
      </div>
    </div>
  );
};

// ── Volume Slider ──────────────────────────────────────────────────────────

const VolumeControl: FC<{
  volume: number;
  onChange: (v: number) => void;
}> = ({ volume, onChange }) => (
  <div
    style={{
      display: "flex",
      alignItems: "center",
      gap: 8,
      padding: "2px 4px",
    }}
  >
    <button
      className="dp-btn"
      style={{ color: TXT_MUTED, fontSize: 14 }}
      onClick={() => onChange(volume === 0 ? 70 : 0)}
    >
      {volume === 0 ? <FaVolumeXmark /> : <FaVolumeHigh />}
    </button>

    {/* Track + fill */}
    <div style={{ flex: 1, position: "relative", height: 20 }}>
      {/* Background track */}
      <div
        style={{
          position: "absolute",
          top: "50%",
          left: 0,
          right: 0,
          transform: "translateY(-50%)",
          height: 4,
          background: "rgba(255,255,255,0.13)",
          borderRadius: 2,
          pointerEvents: "none",
        }}
      >
        <div
          style={{
            width: `${volume}%`,
            height: "100%",
            background: `linear-gradient(90deg, ${ACCENT}, ${ACCENT2})`,
            borderRadius: 2,
          }}
        />
      </div>
      <input
        type="range"
        min={0}
        max={100}
        value={volume}
        className="dp-vol-range"
        style={{ position: "relative", zIndex: 1 }}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </div>

    <span
      style={{
        fontSize: 11,
        color: TXT_MUTED,
        minWidth: 24,
        textAlign: "right",
        fontVariantNumeric: "tabular-nums",
      }}
    >
      {volume}
    </span>
  </div>
);

// ── Main content ───────────────────────────────────────────────────────────

function Content() {
  const [status, setStatus] = useState<PlayerStatus>({
    playing: false,
    paused: false,
    current_file: null,
    current_position: 0,
    duration: 0,
    volume: 70,
    shuffle: false,
    repeat: "none",
    current_index: -1,
  });
  const [meta, setMeta] = useState<TrackMeta | null>(null);
  const [tracks, setTracks] = useState<TrackInfo[]>([]);
  const [showList, setShowList] = useState(false);
  const [scanning, setScanning] = useState(false);
  const loadedFile = useRef<string | null>(null);

  // Styles
  useEffect(() => {
    injectStyles();
    return removeStyles;
  }, []);

  // Fetch initial status
  useEffect(() => {
    beGetStatus()
      .then((s) => { if (s) setStatus(s); })
      .catch(console.error);
  }, []);

  // Backend events
  useEffect(() => {
    const onStatus = addEventListener<[PlayerStatus]>(
      "player_status",
      (s) => setStatus((p) => ({ ...p, ...s }))
    );
    const onPos = addEventListener<[number, number]>(
      "player_position",
      (pos, dur) =>
        setStatus((p) => ({ ...p, current_position: pos, duration: dur }))
    );
    return () => {
      removeEventListener("player_status", onStatus);
      removeEventListener("player_position", onPos);
    };
  }, []);

  // Load full metadata (+ cover) when current track changes
  useEffect(() => {
    const file = status.current_file;
    if (!file || file === loadedFile.current) return;
    loadedFile.current = file;
    setMeta(null);
    beGetMeta(file)
      .then(setMeta)
      .catch(console.error);
  }, [status.current_file]);

  // Initial scan
  useEffect(() => { doScan(); }, []);

  async function doScan() {
    setScanning(true);
    try {
      const files = await beScanMusic();
      const batch = await beGetBatch(files);
      setTracks(batch);
      await beSetPlaylist(files);
    } catch (e) {
      console.error(e);
      toaster.toast({ title: "DeckPlayer", body: "Error scanning music files" });
    } finally {
      setScanning(false);
    }
  }

  // ── Handlers ──

  const handlePlayPause = async () => {
    if (!status.current_file && tracks.length > 0) {
      await bePlay(tracks[0].path);
    } else {
      await bePauseResume();
    }
  };

  const handleSeek = async (s: number) => {
    await beSeek(s);
    setStatus((p) => ({ ...p, current_position: s }));
  };

  const handleVolume = async (v: number) => {
    setStatus((p) => ({ ...p, volume: v }));
    await beSetVolume(v);
  };

  const handleShuffle = async () => {
    const next = !status.shuffle;
    setStatus((p) => ({ ...p, shuffle: next }));
    await beSetShuffle(next);
  };

  const handleRepeat = async () => {
    const order = ["none", "all", "one"] as const;
    const next = order[(order.indexOf(status.repeat as any) + 1) % 3];
    setStatus((p) => ({ ...p, repeat: next }));
    await beSetRepeat(next);
  };

  // ── Derived ──

  const isPlaying = status.playing && !status.paused;
  const title =
    meta?.title ??
    (status.current_file
      ? status.current_file.split("/").pop()?.replace(/\.[^.]+$/, "") ?? "Unknown"
      : "No track loaded");
  const artist = meta?.artist ?? "";
  const album = meta?.album ?? "";

  return (
    <div style={{ padding: "6px 2px 2px", userSelect: "none" }}>

      {/* ── Top card: CD + info + progress ── */}
      <div
        style={{
          background: "linear-gradient(160deg, rgba(20,22,42,0.95) 0%, rgba(10,12,28,0.95) 100%)",
          border: "1px solid rgba(255,255,255,0.07)",
          borderRadius: 14,
          padding: "16px 14px 12px",
          marginBottom: 10,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 12,
        }}
      >
        {/* CD */}
        <CDDisc
          isPlaying={isPlaying}
          cover={meta?.cover ?? null}
          mime={meta?.cover_mime}
          size={152}
        />

        {/* Track info */}
        <div style={{ width: "100%", textAlign: "center" }}>
          <div
            style={{
              fontSize: 14,
              fontWeight: 700,
              color: TXT,
              overflow: "hidden",
              whiteSpace: "nowrap",
              textOverflow: "ellipsis",
              marginBottom: 3,
              letterSpacing: "0.01em",
            }}
          >
            {title}
          </div>
          {artist && (
            <div
              style={{
                fontSize: 12,
                color: TXT_DIM,
                overflow: "hidden",
                whiteSpace: "nowrap",
                textOverflow: "ellipsis",
                marginBottom: 1,
              }}
            >
              {artist}
            </div>
          )}
          {album && (
            <div
              style={{
                fontSize: 11,
                color: TXT_MUTED,
                overflow: "hidden",
                whiteSpace: "nowrap",
                textOverflow: "ellipsis",
              }}
            >
              {album}
            </div>
          )}
        </div>

        {/* Progress bar */}
        <div style={{ width: "100%" }}>
          <ProgressBar
            position={status.current_position}
            duration={status.duration}
            onSeek={handleSeek}
          />
        </div>
      </div>

      {/* ── Controls ── */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "4px 12px 10px",
        }}
      >
        {/* Shuffle */}
        <button
          className="dp-btn"
          onClick={handleShuffle}
          title="Shuffle"
          style={{
            fontSize: 16,
            color: status.shuffle ? ACCENT : TXT_MUTED,
            filter: status.shuffle
              ? `drop-shadow(0 0 4px ${ACCENT})`
              : "none",
          }}
        >
          <FaShuffle />
        </button>

        {/* Prev */}
        <button
          className="dp-btn"
          onClick={() => bePrev()}
          title="Previous"
          style={{ fontSize: 24, color: TXT_DIM }}
        >
          <FaBackwardStep />
        </button>

        {/* Play / Pause */}
        <button
          className={`dp-btn${isPlaying ? " dp-glow" : ""}`}
          onClick={handlePlayPause}
          title={isPlaying ? "Pause" : "Play"}
          style={{
            width: 54,
            height: 54,
            borderRadius: "50%",
            background: `linear-gradient(135deg, ${ACCENT} 0%, ${ACCENT2} 100%)`,
            fontSize: 20,
            color: "#fff",
            boxShadow: isPlaying
              ? `0 4px 18px rgba(100,180,255,0.55)`
              : `0 3px 12px rgba(0,0,0,0.5)`,
            transition: "box-shadow 0.4s ease",
          }}
        >
          {isPlaying ? (
            <FaPause />
          ) : (
            <FaPlay style={{ marginLeft: 2 }} />
          )}
        </button>

        {/* Next */}
        <button
          className="dp-btn"
          onClick={() => beNext()}
          title="Next"
          style={{ fontSize: 24, color: TXT_DIM }}
        >
          <FaForwardStep />
        </button>

        {/* Repeat */}
        <button
          className="dp-btn"
          onClick={handleRepeat}
          title={`Repeat: ${status.repeat}`}
          style={{
            fontSize: 16,
            color: status.repeat !== "none" ? ACCENT : TXT_MUTED,
            filter: status.repeat !== "none"
              ? `drop-shadow(0 0 4px ${ACCENT})`
              : "none",
            position: "relative",
          }}
        >
          <FaRepeat />
          {status.repeat === "one" && (
            <span
              style={{
                position: "absolute",
                top: -3,
                right: -3,
                fontSize: 8,
                fontWeight: 900,
                color: ACCENT,
                lineHeight: 1,
              }}
            >
              1
            </span>
          )}
        </button>
      </div>

      {/* ── Volume ── */}
      <div
        style={{
          background: BG_CARD,
          borderRadius: 10,
          padding: "8px 10px",
          marginBottom: 8,
        }}
      >
        <VolumeControl volume={status.volume} onChange={handleVolume} />
      </div>

      {/* ── Track list toggle ── */}
      <div
        style={{
          background: BG_CARD,
          borderRadius: showList ? "10px 10px 0 0" : 10,
          padding: "8px 12px",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          cursor: "pointer",
          userSelect: "none",
          borderBottom: showList ? "1px solid rgba(255,255,255,0.06)" : "none",
        }}
        onClick={() => setShowList((v) => !v)}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8, color: TXT_DIM, fontSize: 13 }}>
          <FaBars style={{ fontSize: 11 }} />
          <span>
            {scanning
              ? "Scanning…"
              : `${tracks.length} track${tracks.length !== 1 ? "s" : ""}`}
          </span>
        </div>
        <span style={{ color: TXT_MUTED, fontSize: 11 }}>
          {showList ? "▲" : "▼"}
        </span>
      </div>

      {/* ── Track list ── */}
      {showList && (
        <div
          style={{
            background: "rgba(10,10,20,0.7)",
            borderRadius: "0 0 10px 10px",
            maxHeight: 220,
            overflowY: "auto",
            padding: "4px 4px 6px",
          }}
        >
          {scanning ? (
            <div style={{ color: TXT_MUTED, fontSize: 12, textAlign: "center", padding: 18 }}>
              Scanning music files…
            </div>
          ) : tracks.length === 0 ? (
            <div style={{ color: TXT_MUTED, fontSize: 12, textAlign: "center", padding: 18, lineHeight: 1.6 }}>
              No audio files found.{"\n"}Add MP3/FLAC files to ~/Music
            </div>
          ) : (
            tracks.map((t, i) => {
              const active = i === status.current_index;
              return (
                <div
                  key={t.path}
                  className={`dp-track${active ? " active" : ""}`}
                  onClick={() => bePlayIndex(i)}
                >
                  <span style={{ fontSize: 10, color: TXT_MUTED, minWidth: 18, textAlign: "right" }}>
                    {active && isPlaying ? "▶" : i + 1}
                  </span>
                  <div style={{ flex: 1, overflow: "hidden" }}>
                    <div
                      style={{
                        fontSize: 12,
                        fontWeight: active ? 700 : 400,
                        color: active ? ACCENT : TXT,
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                    >
                      {t.title}
                    </div>
                    <div
                      style={{
                        fontSize: 10,
                        color: TXT_MUTED,
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                    >
                      {t.artist}
                    </div>
                  </div>
                  <span style={{ fontSize: 10, color: TXT_MUTED, flexShrink: 0 }}>
                    {fmt(t.duration)}
                  </span>
                </div>
              );
            })
          )}
        </div>
      )}
    </div>
  );
}

// ── Plugin export ──────────────────────────────────────────────────────────

export default definePlugin(() => {
  injectStyles();

  return {
    name: "DeckPlayer",
    titleView: (
      <div
        className={staticClasses.Title}
        style={{ display: "flex", alignItems: "center", gap: 8 }}
      >
        <FaCompactDisc style={{ color: ACCENT, fontSize: 16 }} />
        <span>DeckPlayer</span>
      </div>
    ),
    content: <Content />,
    icon: <FaCompactDisc />,
    onDismount() {
      removeStyles();
    },
  };
});
