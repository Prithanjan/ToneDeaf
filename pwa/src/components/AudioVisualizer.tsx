import { useEffect, useRef, useState } from 'react';
import type { ReactElement } from 'react';
import styles from './AudioVisualizer.module.css';

export interface AudioVisualizerProps {
  /** Live streaming active indicator */
  isLive: boolean;
  /** Callback to trigger simulated telephony stream test when mic is blocked */
  onStartSimulatedTest?: () => void;
}

export function AudioVisualizer({ isLive, onStartSimulatedTest }: AudioVisualizerProps): ReactElement {
  const [permissionState, setPermissionState] = useState<'granted' | 'prompt' | 'denied' | 'unknown'>('unknown');
  const [micVolume, setMicVolume] = useState<number>(0);
  const [isAudioDetected, setIsAudioDetected] = useState<boolean>(false);
  const [peakFreqHz, setPeakFreqHz] = useState<number>(0);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const animFrameRef = useRef<number | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);

  // Check microphone permissions
  useEffect(() => {
    if (typeof navigator !== 'undefined' && 'permissions' in navigator) {
      navigator.permissions
        .query({ name: 'microphone' })
        .then((permission) => {
          setPermissionState(permission.state);
          permission.onchange = () => {
            setPermissionState(permission.state);
          };
        })
        .catch(() => {
          setPermissionState('unknown');
        });
    }
  }, []);

  // Set up live AudioContext analyzer when live stream opens
  useEffect(() => {
    if (!isLive) {
      if (animFrameRef.current !== null) cancelAnimationFrame(animFrameRef.current);
      if (audioCtxRef.current !== null) {
        audioCtxRef.current.close().catch(() => {
          // Ignore close error
        });
        audioCtxRef.current = null;
      }
      setMicVolume(0);
      setIsAudioDetected(false);
      setPeakFreqHz(0);
      return;
    }

    let stream: MediaStream | null = null;
    let cancelled = false;

    navigator.mediaDevices
      .getUserMedia({ audio: true })
      .then((micStream) => {
        if (cancelled) {
          micStream.getTracks().forEach((t) => {
            t.stop();
          });
          return;
        }
        stream = micStream;
        setPermissionState('granted');

        const ctx = new AudioContext();
        audioCtxRef.current = ctx;
        const source = ctx.createMediaStreamSource(micStream);
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 512;
        analyser.smoothingTimeConstant = 0.8;
        analyserRef.current = analyser;
        source.connect(analyser);

        const dataArray = new Uint8Array(analyser.frequencyBinCount);
        const canvas = canvasRef.current;
        const canvasCtx = canvas?.getContext('2d');

        const render = (): void => {
          if (cancelled) return;
          analyser.getByteFrequencyData(dataArray);

          let sum = 0;
          let maxVal = 0;
          let maxIndex = 0;

          for (let i = 0; i < dataArray.length; i += 1) {
            const val = dataArray[i] ?? 0;
            sum += val;
            if (val > maxVal) {
              maxVal = val;
              maxIndex = i;
            }
          }

          const avg = sum / dataArray.length;
          const normalizedVolume = Math.min(100, Math.round((avg / 128) * 100));
          setMicVolume(normalizedVolume);
          setIsAudioDetected(normalizedVolume > 6);

          // Calculate peak frequency Hz
          const nyquist = ctx.sampleRate / 2;
          const peakHz = Math.round((maxIndex / dataArray.length) * nyquist);
          setPeakFreqHz(normalizedVolume > 6 ? peakHz : 0);

          // Draw High-Res FFT Spectrogram / Frequency Spectrum Canvas
          if (canvas !== null && canvasCtx !== undefined && canvasCtx !== null) {
            const width = canvas.width;
            const height = canvas.height;

            canvasCtx.fillStyle = '#09090b';
            canvasCtx.fillRect(0, 0, width, height);

            // Draw radar grid lines
            canvasCtx.strokeStyle = '#18181b';
            canvasCtx.lineWidth = 1;

            // Grid horizontals
            for (let y = 0; y < height; y += 15) {
              canvasCtx.beginPath();
              canvasCtx.moveTo(0, y);
              canvasCtx.lineTo(width, y);
              canvasCtx.stroke();
            }

            // Grid verticals (Frequency markers: 0Hz, 2kHz, 4kHz, 8kHz)
            for (let x = 0; x < width; x += width / 4) {
              canvasCtx.beginPath();
              canvasCtx.moveTo(x, 0);
              canvasCtx.lineTo(x, height);
              canvasCtx.stroke();
            }

            // Draw FFT Frequency Bars
            const barWidth = (width / dataArray.length) * 1.2;
            let x = 0;

            for (const val of dataArray) {
              const barHeight = (val / 255) * height;
              // High-tech emerald to cyan gradient
              const intensity = val / 255;
              const r = Math.round(16 + intensity * 40);
              const g = Math.round(160 + intensity * 95);
              const b = Math.round(120 + intensity * 135);

              canvasCtx.fillStyle = `rgb(${r.toString()}, ${g.toString()}, ${b.toString()})`;
              canvasCtx.fillRect(x, height - barHeight, barWidth - 1, barHeight);
              x += barWidth;
            }
          }

          animFrameRef.current = requestAnimationFrame(render);
        };

        render();
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && (err.name === 'NotAllowedError' || err.name === 'SecurityError')) {
          setPermissionState('denied');
        }
      });

    return () => {
      cancelled = true;
      if (animFrameRef.current !== null) cancelAnimationFrame(animFrameRef.current);
      if (stream !== null) {
        stream.getTracks().forEach((t) => {
          t.stop();
        });
      }
      if (audioCtxRef.current !== null) {
        audioCtxRef.current.close().catch(() => {
          // Ignore close error
        });
        audioCtxRef.current = null;
      }
    };
  }, [isLive]);

  return (
    <div className={styles.container}>
      {/* Microphone Status Banner */}
      <div className={styles.statusBanner} data-permission={permissionState}>
        <div className={styles.statusHeader}>
          <span className={styles.statusDot} />
          <span className={styles.statusTitle}>
            Microphone Hardware Status:{' '}
            <strong>
              {permissionState === 'granted'
                ? 'Granted & Active ✅'
                : permissionState === 'denied'
                ? 'Blocked by Browser / OS Settings 🛑'
                : permissionState === 'prompt'
                ? 'Awaiting Permission Approval ⚠️'
                : 'Detecting Input...'}
            </strong>
          </span>
        </div>

        {permissionState === 'denied' ? (
          <div className={styles.blockedNotice}>
            <p className={styles.warningText}>
              <strong>Microphone access is blocked by your browser settings.</strong>
            </p>
            <ol className={styles.instructions}>
              <li>Click the 🔒 icon next to the URL in your browser address bar.</li>
              <li>Change <strong>Microphone</strong> from <em>Block</em> to <em>Allow</em>.</li>
              <li>Refresh this page and tap <em>Start Session</em>.</li>
            </ol>
            {onStartSimulatedTest !== undefined ? (
              <button type="button" className={styles.fallbackButton} onClick={onStartSimulatedTest}>
                ⚡ Run Telephony Stream Test (Direct AI Scoring Test - No Mic Required)
              </button>
            ) : null}
          </div>
        ) : null}
      </div>

      {/* Live FFT Spectrogram Visualizer & Level Meter */}
      {isLive ? (
        <div className={styles.meterPanel}>
          <div className={styles.meterHeader}>
            <div className={styles.titleGroup}>
              <span className={styles.meterTitle}>LIVE AUDIO SPECTROGRAM & FREQUENCY SPECTRUM</span>
              {peakFreqHz > 0 ? (
                <span className={styles.freqBadge}>PEAK: {peakFreqHz.toString()} Hz</span>
              ) : null}
            </div>
            <span className={isAudioDetected ? styles.audioActiveBadge : styles.audioIdleBadge}>
              {isAudioDetected ? 'VOICE DETECTED 🎙️' : 'SILENT / QUIET'}
            </span>
          </div>

          <div className={styles.visualizerRow}>
            <div className={styles.canvasContainer}>
              <canvas ref={canvasRef} width={420} height={60} className={styles.canvas} />
              <div className={styles.freqAxis}>
                <span>0Hz</span>
                <span>2kHz</span>
                <span>4kHz</span>
                <span>8kHz</span>
              </div>
            </div>

            <div className={styles.vuMeterColumn}>
              <div className={styles.vuHeader}>
                <span className={styles.vuTitle}>RMS VOLUME</span>
                <span className={styles.vuLabel}>{micVolume.toString()}%</span>
              </div>
              <div className={styles.vuTrack}>
                <div className={styles.vuFill} style={{ width: `${micVolume.toString()}%` }} />
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
