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
        analyser.fftSize = 256;
        analyserRef.current = analyser;
        source.connect(analyser);

        const dataArray = new Uint8Array(analyser.frequencyBinCount);
        const canvas = canvasRef.current;
        const canvasCtx = canvas?.getContext('2d');

        const render = (): void => {
          if (cancelled) return;
          analyser.getByteFrequencyData(dataArray);

          let sum = 0;
          for (const val of dataArray) {
            sum += val;
          }
          const avg = sum / dataArray.length;
          const normalizedVolume = Math.min(100, Math.round((avg / 128) * 100));
          setMicVolume(normalizedVolume);
          setIsAudioDetected(normalizedVolume > 8);

          if (canvas !== null && canvasCtx !== undefined && canvasCtx !== null) {
            canvasCtx.clearRect(0, 0, canvas.width, canvas.height);
            canvasCtx.fillStyle = '#09090b';
            canvasCtx.fillRect(0, 0, canvas.width, canvas.height);

            const barWidth = (canvas.width / dataArray.length) * 1.5;
            let x = 0;

            for (const val of dataArray) {
              const barHeight = (val / 255) * canvas.height;
              const hue = 140 - (val / 255) * 100;
              canvasCtx.fillStyle = `hsl(${hue.toString()}, 80%, 50%)`;
              canvasCtx.fillRect(x, canvas.height - barHeight, barWidth - 1, barHeight);
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

      {/* Live Audio Visualizer & Level Meter */}
      {isLive ? (
        <div className={styles.meterPanel}>
          <div className={styles.meterHeader}>
            <span className={styles.meterTitle}>LIVE AUDIO OSCILLOSCOPE</span>
            <span className={isAudioDetected ? styles.audioActiveBadge : styles.audioIdleBadge}>
              {isAudioDetected ? 'VOICE DETECTED 🎙️' : 'SILENT / QUIET'}
            </span>
          </div>

          <div className={styles.visualizerRow}>
            <canvas ref={canvasRef} width={280} height={40} className={styles.canvas} />
            <div className={styles.vuMeterColumn}>
              <div className={styles.vuTrack}>
                <div className={styles.vuFill} style={{ width: `${micVolume.toString()}%` }} />
              </div>
              <span className={styles.vuLabel}>{micVolume.toString()}% Level</span>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
