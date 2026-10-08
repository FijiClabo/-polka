import { useCallback, useEffect, useRef, useState } from "react";

export type RecState = "idle" | "recording" | "recorded" | "denied" | "unsupported";

function pickMime(): string | undefined {
  const MR = window.MediaRecorder;
  if (!MR || !MR.isTypeSupported) return undefined;
  for (const m of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus", "audio/aac"]) {
    if (MR.isTypeSupported(m)) return m;
  }
  return undefined;
}

export const BARS = 34;

export function useRecorder(maxSec: number) {
  const [state, setState] = useState<RecState>(() =>
    typeof window.MediaRecorder === "undefined" || !navigator.mediaDevices?.getUserMedia ? "unsupported" : "idle",
  );
  const [seconds, setSeconds] = useState(0);
  const [levels, setLevels] = useState<number[]>(() => Array(BARS).fill(0.08));
  const [blob, setBlob] = useState<Blob | null>(null);

  const rec = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const ctx = useRef<AudioContext | null>(null);
  const raf = useRef(0);
  const started = useRef(0);
  const chunks = useRef<BlobPart[]>([]);

  const cleanup = useCallback(() => {
    cancelAnimationFrame(raf.current);
    stream.current?.getTracks().forEach((t) => t.stop());
    stream.current = null;
    ctx.current?.close().catch(() => {});
    ctx.current = null;
  }, []);

  useEffect(() => cleanup, [cleanup]);

  const stop = useCallback(() => {
    if (rec.current && rec.current.state !== "inactive") rec.current.stop();
  }, []);

  const start = useCallback(async () => {
    if (state === "unsupported") return;
    try {
      const s = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      stream.current = s;
      const mime = pickMime();
      const r = new MediaRecorder(s, mime ? { mimeType: mime, audioBitsPerSecond: 32000 } : undefined);
      chunks.current = [];
      r.ondataavailable = (e) => e.data.size && chunks.current.push(e.data);
      r.onstop = () => {
        setBlob(new Blob(chunks.current, { type: r.mimeType || mime || "audio/webm" }));
        setState("recorded");
        cleanup();
      };
      rec.current = r;
      r.start(250);
      started.current = performance.now();
      setSeconds(0);
      setBlob(null);
      setState("recording");

      // уровень звука для «волны»
      const AC = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      if (AC) {
        const ac = new AC();
        ctx.current = ac;
        const src = ac.createMediaStreamSource(s);
        const an = ac.createAnalyser();
        an.fftSize = 512;
        src.connect(an);
        const buf = new Uint8Array(an.fftSize);
        let last = 0;
        const loop = (t: number) => {
          const sec = (t - started.current) / 1000;
          setSeconds(sec);
          if (sec >= maxSec) {
            stop();
            return;
          }
          if (t - last > 90) {
            last = t;
            an.getByteTimeDomainData(buf);
            let sum = 0;
            for (const v of buf) sum += (v - 128) * (v - 128);
            const rms = Math.sqrt(sum / buf.length) / 50;
            setLevels((l) => [...l.slice(1), Math.max(0.08, Math.min(1, rms * 1.8))]);
          }
          raf.current = requestAnimationFrame(loop);
        };
        raf.current = requestAnimationFrame(loop);
      } else {
        const id = window.setInterval(() => {
          const sec = (performance.now() - started.current) / 1000;
          setSeconds(sec);
          if (sec >= maxSec) {
            window.clearInterval(id);
            stop();
          }
        }, 200);
      }
    } catch {
      setState("denied");
      cleanup();
    }
  }, [state, maxSec, stop, cleanup]);

  const reset = useCallback(() => {
    stop();
    cleanup();
    setBlob(null);
    setSeconds(0);
    setLevels(Array(BARS).fill(0.08));
    setState((s) => (s === "denied" || s === "unsupported" ? s : "idle"));
  }, [stop, cleanup]);

  return { state, seconds, levels, blob, start, stop, reset };
}

export function fmtTime(sec: number): string {
  const s = Math.floor(sec);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
