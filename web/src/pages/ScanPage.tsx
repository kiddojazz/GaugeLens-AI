// Scan: record the displays with the phone's camera right here (or take photos), then go straight to the results
// and the chat.
// Camera access needs HTTPS (the Azure URL) or localhost. Browsers without in-page recording get the phone's own
// camera app through a file input with capture="environment".
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Card, ErrorBox } from "../components";
import { Icon } from "../icons";

const MAX_SECONDS = 120;
const MAX_PHOTOS = 10;
const GOOD_SECONDS = 30;
const MIME_TYPES = ["video/mp4;codecs=avc1", "video/mp4", "video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm"];

type Stage = "idle" | "starting" | "ready" | "recording" | "review" | "uploading";

function pickMime(): string | undefined {
  if (typeof MediaRecorder === "undefined") return undefined;
  return MIME_TYPES.find((t) => MediaRecorder.isTypeSupported(t));
}

/** A recorder that has actually started. Browsers may claim a format (e.g. H.264 MP4) and then fail to record it,
 *  so each supported format is tried in turn, then the browser's own default. */
function startRecorder(media: MediaStream): MediaRecorder {
  const candidates: (string | undefined)[] = [...MIME_TYPES.filter((t) => MediaRecorder.isTypeSupported(t)), undefined];
  let last: unknown;
  for (const mimeType of candidates) {
    try {
      const rec = new MediaRecorder(media, mimeType ? { mimeType, videoBitsPerSecond: 4_000_000 } : undefined);
      rec.start(1000);
      return rec;
    } catch (e) {
      last = e;
    }
  }
  throw last instanceof Error ? last : new Error("this browser could not start recording");
}

function clock(s: number) {
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

export default function ScanPage() {
  const navigate = useNavigate();
  const { me, refresh } = useAuth();
  const live = useRef<HTMLVideoElement>(null);
  const stream = useRef<MediaStream | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const timer = useRef<number | null>(null);
  const [stage, setStage] = useState<Stage>("idle");
  const [seconds, setSeconds] = useState(0);
  const [clip, setClip] = useState<{ blob: Blob; url: string; name: string } | null>(null);
  const [torch, setTorch] = useState<boolean | null>(null);       // null = not available on this camera
  const [simulated, setSimulated] = useState(false);
  const [shots, setShots] = useState<{ blob: Blob; url: string }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const canRecord = typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia && !!pickMime();

  useEffect(() => () => stopCamera(), []);                     // release the camera when leaving the page

  function stopCamera() {
    if (timer.current) window.clearInterval(timer.current);
    stream.current?.getTracks().forEach((t) => t.stop());
    stream.current = null;
  }

  async function startCamera() {
    setError(null);
    setStage("starting");
    try {
      const media = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } },
      });
      stream.current = media;
      if (live.current) {
        live.current.srcObject = media;
        await live.current.play().catch(() => undefined);
      }
      const track = media.getVideoTracks()[0];
      const caps = (track.getCapabilities?.() ?? {}) as MediaTrackCapabilities & { torch?: boolean };
      setTorch(caps.torch ? false : null);
      setStage("ready");
    } catch (e) {
      setStage("idle");
      setError(e instanceof DOMException && e.name === "NotAllowedError"
        ? "Camera permission was refused. Allow camera access for this site in the browser settings, then try again."
        : `The camera could not start: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  async function toggleTorch() {
    const track = stream.current?.getVideoTracks()[0];
    if (!track || torch === null) return;
    try {
      await track.applyConstraints({ advanced: [{ torch: !torch } as MediaTrackConstraintSet] });
      setTorch(!torch);
    } catch {
      setTorch(null);
    }
  }

  /** A still from the live camera, at the camera's full resolution. */
  function takePhoto() {
    const video = live.current;
    if (!video || !video.videoWidth || shots.length >= MAX_PHOTOS) return;
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d")!.drawImage(video, 0, 0);
    canvas.toBlob((blob) => { if (blob) setShots((s) => [...s, { blob, url: URL.createObjectURL(blob) }]); }, "image/jpeg", 0.92);
  }

  function clearShots() {
    shots.forEach((s) => URL.revokeObjectURL(s.url));
    setShots([]);
  }

  async function sendShots() {
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    const files = shots.map((s, n) => new File([s.blob], `photo-${stamp}-${n + 1}.jpg`, { type: "image/jpeg" }));
    stopCamera();
    await send(files);
  }

  function startRecording() {
    if (!stream.current) return;
    chunks.current = [];
    let rec: MediaRecorder;
    try {
      rec = startRecorder(stream.current);
    } catch (e) {
      setError(`Recording could not start: ${e instanceof Error ? e.message : String(e)}. Use the camera app button instead.`);
      return;
    }
    rec.ondataavailable = (e) => { if (e.data.size) chunks.current.push(e.data); };
    rec.onstop = () => {
      const type = rec.mimeType || "video/webm";
      const blob = new Blob(chunks.current, { type });
      const ext = type.includes("mp4") ? "mp4" : "webm";
      setClip({ blob, url: URL.createObjectURL(blob), name: `scan-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.${ext}` });
      stopCamera();
      setStage("review");
    };
    recorder.current = rec;
    setSeconds(0);
    setStage("recording");
    const started = Date.now();
    timer.current = window.setInterval(() => {
      const s = (Date.now() - started) / 1000;
      setSeconds(s);
      if (s >= MAX_SECONDS) stopRecording();
    }, 250);
  }

  function stopRecording() {
    if (timer.current) window.clearInterval(timer.current);
    if (recorder.current?.state === "recording") recorder.current.stop();
  }

  function retake() {
    if (clip) URL.revokeObjectURL(clip.url);
    setClip(null);
    void startCamera();
  }

  async function send(file: File | File[]) {
    setStage("uploading");
    setError(null);
    try {
      const { clip_id } = await api.upload(file, "auto", simulated);
      if (me?.kind === "guest") void refresh();             // scans left this visit
      navigate(`/clips/${clip_id}`);
    } catch (e) {
      setStage(clip ? "review" : "idle");
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Scan</h1>
          <p>Point your phone at the displays and record, or take photos. GaugeLens identifies the equipment and reads the values it can see, then you can ask it anything about what you filmed.</p>
        </div>
      </div>
      <ErrorBox error={error} />
      <div className="grid-2">
        <Card pad={false}>
          <div className="scanner">
            {stage === "review" && clip ? (
              <video className="scan-video" src={clip.url} controls playsInline aria-label="Your recording" />
            ) : (
              <video ref={live} className="scan-video" muted playsInline autoPlay aria-label="Camera" />
            )}
            {(stage === "ready" || stage === "recording") && <div className="scan-guide" aria-hidden><span>Fit every display inside the frame</span></div>}
            {stage === "ready" && shots.length > 0 && (
              <div className="scan-shots" aria-label="Photos taken">
                {shots.map((s, n) => <img key={s.url} src={s.url} alt={`Photo ${n + 1}`} />)}
              </div>
            )}
            {stage === "recording" && (
              <div className={`scan-timer${seconds >= GOOD_SECONDS ? " good" : ""}`}>
                <span className="rec-dot" /> {clock(seconds)} {seconds < GOOD_SECONDS ? `· keep going (${GOOD_SECONDS - Math.floor(seconds)} s)` : "· good, stop when ready"}
              </div>
            )}
            {stage === "idle" && (
              <div className="scan-empty">
                <Icon name="scan" size={40} />
                <p>{canRecord ? "The camera starts when you're ready." : "This browser can't record in the page. Use your camera app instead."}</p>
              </div>
            )}
          </div>
          <div className="scan-controls">
            {stage === "idle" && canRecord && <button className="btn primary" onClick={startCamera}><Icon name="scan" size={14} /> Start camera</button>}
            {stage === "starting" && <span className="muted small">Starting the camera…</span>}
            {stage === "ready" && !shots.length && (
              <>
                <button className="btn rec" onClick={startRecording} aria-label="Start recording"><span className="rec-dot" /> Record</button>
                <button className="btn" onClick={takePhoto} aria-label="Take a photo"><Icon name="frame" size={14} /> Photo</button>
                {torch !== null && <button className="btn" onClick={toggleTorch} aria-pressed={torch}>{torch ? "Torch off" : "Torch on"}</button>}
              </>
            )}
            {stage === "ready" && shots.length > 0 && (
              <>
                <button className="btn primary" onClick={() => void sendShots()}><Icon name="send" size={14} /> Use {shots.length === 1 ? "this photo" : `these ${shots.length} photos`}</button>
                <button className="btn" onClick={takePhoto} disabled={shots.length >= MAX_PHOTOS} aria-label="Take a photo"><Icon name="frame" size={14} /> Another</button>
                <button className="btn" onClick={clearShots}>Clear</button>
              </>
            )}
            {stage === "recording" && (
              <>
                <button className="btn rec" onClick={stopRecording} aria-label="Stop recording"><span className="rec-square" /> Stop</button>
                {torch !== null && <button className="btn" onClick={toggleTorch} aria-pressed={torch}>{torch ? "Torch off" : "Torch on"}</button>}
              </>
            )}
            {stage === "review" && clip && (
              <>
                <button className="btn primary" onClick={() => send(new File([clip.blob], clip.name, { type: clip.blob.type }))}>
                  <Icon name="send" size={14} /> Use this clip
                </button>
                <button className="btn" onClick={retake}>Retake</button>
                <span className="muted small">{clock(seconds)} · {(clip.blob.size / 1e6).toFixed(1)} MB</span>
              </>
            )}
            {stage === "uploading" && <span className="row muted small"><span className="spinner" aria-hidden /> Sending to GaugeLens…</span>}
            {(stage === "idle" || stage === "review") && (
              <>
                <label className="btn file-btn">
                  <Icon name="clips" size={14} /> {canRecord ? "Or use the camera app" : "Open the camera app"}
                  <input type="file" accept="video/*" capture="environment" aria-label="Record with the camera app"
                         onChange={(e) => { const f = e.target.files?.[0]; if (f) void send(f); }} />
                </label>
                <label className="btn file-btn">
                  <Icon name="frame" size={14} /> Photos
                  <input type="file" accept="image/jpeg,image/png,image/webp" multiple aria-label="Choose or take photos"
                         onChange={(e) => { const f = Array.from(e.target.files ?? []); if (f.length) void send(f.slice(0, MAX_PHOTOS)); }} />
                </label>
              </>
            )}
          </div>
          <label className="scan-sim small muted"><input type="checkbox" checked={simulated} onChange={(e) => setSimulated(e.target.checked)} /> This is a simulated test (e.g. the mock panel)</label>
          {me?.kind === "guest" && me.clips_left !== undefined && (
            <p className="scan-sim small muted" style={{ marginTop: 0 }}>Guest: {me.clips_left} {me.clips_left === 1 ? "scan" : "scans"} left this visit.</p>
          )}
        </Card>
        <Card title="For a good scan">
          <ol className="tips">
            <li><strong>Every display in the frame,</strong> and the nameplate sticker if you can: GaugeLens reads it to identify the equipment.</li>
            <li><strong>Steady for 30-60 seconds.</strong> Brace your elbow or lean on something.</li>
            <li><strong>Let the screen cycle</strong> through its pages, and change the load once if you can.</li>
            <li><strong>No glare:</strong> tilt slightly if a light reflects. In a dark cabinet, use the torch.</li>
            <li><strong>A photo works too</strong> for a quick reading. Take one per screen page; video also checks how values change.</li>
            <li><strong>Then ask.</strong> The results page opens with the chat: "is anything wrong?", "what was the battery voltage?"</li>
          </ol>
          <p className="muted small" style={{ marginBottom: 0 }}>Stay outside enclosures. Film only what's visible from the front panel.</p>
        </Card>
      </div>
    </>
  );
}
