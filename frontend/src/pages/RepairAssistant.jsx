import { useState, useEffect, useRef, useCallback } from 'react';
import { usePrecisionHudTracking } from '../hooks/usePrecisionHudTracking';
import './RepairAssistant.css';

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || '';
const API = BACKEND_URL ? `${BACKEND_URL}/api` : '';
const WS_BASE = BACKEND_URL ? BACKEND_URL.replace('https://', 'wss://').replace('http://', 'ws://') : '';
const INPUT_SAMPLE_RATE = 16000;
const OUTPUT_SAMPLE_RATE = 24000;
const AUDIO_FRAME_MS = 20;
const BARGE_IN_RMS_THRESHOLD = 0.075;
const BARGE_IN_REQUIRED_FRAMES = 10;
const BARGE_IN_ARM_MS = 900;
const BARGE_IN_COOLDOWN_MS = 900;
const VIDEO_FRAME_INTERVAL_MS = 300;
const VIDEO_FRAME_WIDTH = 640;

function log(cat, ...args) {
  const ts = new Date().toISOString().slice(11, 23);
  console.log(`%c[${ts}] [${cat}]`, 'color: #ff6b5a; font-weight: bold', ...args);
}

function HudMarker({ marker, selected = false, editMode = 'select', onPointerDown }) {
  const g = marker.display_geometry || marker.geometry || {};
  const modelG = marker.model_display_geometry;
  const vector = marker.action_vector || { dx: 0, dy: 0 };
  const angle = Math.atan2(vector.dy || 0, vector.dx || 0) * 180 / Math.PI;
  const length = Math.min(92, Math.max(28, Math.hypot(vector.dx || 0, vector.dy || 0) * 360));
  const tone = marker.style?.tone || 'inspect';
  const shape = marker.style?.shape || 'ring';
  const markerType = marker.marker_type || 'contour';
  const confidence = Math.round((marker.confidence || 0) * 100);
  const minSize = markerType === 'point' ? 2.2 : 3.5;
  const style = {
    '--hud-x': `${(g.x || 0) * 100}%`,
    '--hud-y': `${(g.y || 0) * 100}%`,
    '--hud-w': `${Math.max(minSize, (g.width || 0.08) * 100)}%`,
    '--hud-h': `${Math.max(minSize, (g.height || 0.08) * 100)}%`,
    '--hud-angle': `${Number.isFinite(angle) ? angle : 0}deg`,
    '--hud-arrow': `${length}px`,
  };
  const modelStyle = modelG ? {
    left: `${(modelG.x || 0) * 100}%`,
    top: `${(modelG.y || 0) * 100}%`,
    width: `${Math.max(3.5, (modelG.width || 0.08) * 100)}%`,
    height: `${Math.max(3.5, (modelG.height || 0.08) * 100)}%`,
  } : {};
  const debug = marker.debug || {};
  const contourPoints = (marker.display_contour || []).map((p) => `${(p.x || 0) * 100},${(p.y || 0) * 100}`).join(' ');
  const modelContourPoints = (marker.model_display_contour || []).map((p) => `${(p.x || 0) * 100},${(p.y || 0) * 100}`).join(' ');
  return (
    <>
      {modelContourPoints && (
        <svg className="ra-hud-contour-svg model" viewBox="0 0 100 100" preserveAspectRatio="none" data-testid="hud-model-contour">
          <polygon points={modelContourPoints} />
        </svg>
      )}
      {contourPoints && (
        <svg className={`ra-hud-contour-svg live ${marker.tracking_status || 'seeded'}`} viewBox="0 0 100 100" preserveAspectRatio="none" data-testid="hud-live-contour">
          <polygon points={contourPoints} />
        </svg>
      )}
      {modelG && <div className="ra-hud-model-box" style={modelStyle} data-testid="hud-model-box"></div>}
      <div
        className={`ra-hud-marker ${tone} ${shape} ${markerType} ${marker.tracking_status || 'seeded'} ${selected ? 'selected' : ''} ${marker.style?.pulse ? 'pulse' : ''}`}
        style={style}
        data-testid="hud-marker"
        onPointerDown={(event) => onPointerDown?.(event, marker, editMode === 'move' ? 'move' : 'select')}
      >
        <div className="ra-hud-depth"></div>
        <div className="ra-hud-box">
          <span className="ra-hud-corner c1"></span><span className="ra-hud-corner c2"></span><span className="ra-hud-corner c3"></span><span className="ra-hud-corner c4"></span>
        </div>
        {selected && editMode === 'resize' && ['nw', 'ne', 'se', 'sw'].map(handle => (
          <button
            key={handle}
            className={`ra-hud-resize-handle ${handle}`}
            type="button"
            aria-label={`Resize ${handle}`}
            onPointerDown={(event) => onPointerDown?.(event, marker, 'resize', handle)}
          />
        ))}
        {shape === 'arrow' && <div className="ra-hud-arrow"><span></span></div>}
        {shape === 'arc' && <div className="ra-hud-arc"><span></span></div>}
        <div className="ra-hud-label">
          <span>{marker.label || marker.target_hint || 'Target'}</span>
          <small>{marker.tracking_status || 'tracking'} · {confidence}%</small>
        </div>
        <div className="ra-hud-debug-card">
          <b>{marker.id}</b>
          <span>score {debug.score ?? '-'}</span>
          <span>raw {debug.raw_confidence ?? '-'}</span>
          <span>lost {debug.lost_frames ?? 0}</span>
          <span>move {debug.movement_px ?? '-'}px</span>
          <span>seg {debug.segmentation_provider ?? '-'} {debug.segmentation_device ?? ''}</span>
          <span>frames {debug.video_frames ?? '-'}</span>
          <span>pts {debug.contour_points ?? marker.mask_contour?.length ?? '-'}</span>
          <span>v {debug.velocity_px ? `${debug.velocity_px.x},${debug.velocity_px.y}` : '-'}</span>
          <span>{debug.update_ms ?? '-'}ms</span>
        </div>
      </div>
    </>
  );
}

export default function RepairAssistant() {
  const [personas, setPersonas] = useState([]);
  const [voices, setVoices] = useState([]);
  const [selectedPersona, setSelectedPersona] = useState('calm-expert');
  const [selectedVoice, setSelectedVoice] = useState('Puck');
  const [isConnected, setIsConnected] = useState(false);
  const [isMicEnabled, setIsMicEnabled] = useState(false);
  const [voiceState, setVoiceState] = useState('idle');
  const [status, setStatus] = useState('Ready');
  const [transcript, setTranscript] = useState([]);
  const [sessionId, setSessionId] = useState(null);
  const [activeManual, setActiveManual] = useState(null);
  const [toolActivity, setToolActivity] = useState(null);
  const [warnings, setWarnings] = useState([]);
  const [steps, setSteps] = useState([]);
  const [cameraEnabled, setCameraEnabled] = useState(false);
  const [cameraFacing, setCameraFacing] = useState('environment');
  const [cameraMirrored, setCameraMirrored] = useState(false);
  const [showTranscript, setShowTranscript] = useState(true);
  const [visionStatus, setVisionStatus] = useState(null);
  const [searchQueries, setSearchQueries] = useState([]);
  const [hudMarkers, setHudMarkers] = useState([]);
  const [hudVersion, setHudVersion] = useState(0);
  const [hudEditMode, setHudEditMode] = useState('select');
  const [selectedHudMarkerId, setSelectedHudMarkerId] = useState(null);
  const [mobileCardIndex, setMobileCardIndex] = useState(0);
  const [endedSessionId, setEndedSessionId] = useState(null);
  const [showDatasetPrompt, setShowDatasetPrompt] = useState(false);
  const [datasetReview, setDatasetReview] = useState(null);
  const [datasetReviewBusy, setDatasetReviewBusy] = useState(false);

  const wsRef = useRef(null);
  const audioWsRef = useRef(null);
  const audioCtxRef = useRef(null);
  const processorRef = useRef(null);
  const micSourceRef = useRef(null);
  const micStreamRef = useRef(null);
  const camStreamRef = useRef(null);
  const videoRef = useRef(null);
  const frameIntervalRef = useRef(null);
  const audioQueueRef = useRef([]);
  const isPlayingRef = useRef(false);
  const playbackCtxRef = useRef(null);
  const playbackSourcesRef = useRef(new Set());
  const nextPlaybackTimeRef = useRef(0);
  const heartbeatRef = useRef(null);
  const reconnectRef = useRef(null);
  const sessionActiveRef = useRef(false);
  const configRef = useRef(null);
  const sessionIdRef = useRef(null);
  const transcriptEndRef = useRef(null);
  const frameCountRef = useRef(0);
  const reconnectCountRef = useRef(0);
  const touchStartRef = useRef(null);
  const voiceStateRef = useRef('idle');
  const bargeInFramesRef = useRef(0);
  const lastBargeInRef = useRef(0);
  const assistantSpeechStartedAtRef = useRef(0);
  const sessionStartAtRef = useRef(0);
  const firstAudioAtRef = useRef(0);
  const hudDragRef = useRef(null);
  const reviewPollRef = useRef(null);
  const lastHudCorrectionRef = useRef({ at: 0, x: -1, y: -1, label: null });
  const hasBackendConfig = Boolean(BACKEND_URL);

  useEffect(() => { transcriptEndRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [transcript]);
  useEffect(() => { voiceStateRef.current = voiceState; }, [voiceState]);

  const loadData = useCallback(async () => {
    try {
      const [p, v] = await Promise.all([fetch(`${API}/personas`).then(r => r.json()), fetch(`${API}/voices`).then(r => r.json())]);
      setPersonas(p); setVoices(v); log('DATA', `${p.length} personas, ${v.length} voices`);
    } catch (e) { setStatus('Error loading'); }
  }, []);

  // ─── Audio Playback ──────────────
  const stopPlayback = useCallback((bargeIn = false) => {
    audioQueueRef.current = [];
    playbackSourcesRef.current.forEach(source => {
      try { source.stop(); } catch {}
    });
    playbackSourcesRef.current.clear();
    if (playbackCtxRef.current) nextPlaybackTimeRef.current = playbackCtxRef.current.currentTime;
    isPlayingRef.current = false;
    assistantSpeechStartedAtRef.current = 0;
    if (bargeIn) log('AUDIO', 'Playback stopped for barge-in');
  }, []);

  const scheduleQueuedAudio = useCallback(() => {
    if (!playbackCtxRef.current) playbackCtxRef.current = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: OUTPUT_SAMPLE_RATE });
    const ctx = playbackCtxRef.current;
    if (!nextPlaybackTimeRef.current || nextPlaybackTimeRef.current < ctx.currentTime) {
      nextPlaybackTimeRef.current = ctx.currentTime + 0.015;
    }

    while (audioQueueRef.current.length > 0) {
      const floats = audioQueueRef.current.shift();
      const buf = ctx.createBuffer(1, floats.length, OUTPUT_SAMPLE_RATE); buf.getChannelData(0).set(floats);
      const src = ctx.createBufferSource(); src.buffer = buf; src.connect(ctx.destination);
      playbackSourcesRef.current.add(src);
      src.onended = () => {
        playbackSourcesRef.current.delete(src);
        if (audioQueueRef.current.length > 0) scheduleQueuedAudio();
        if (playbackSourcesRef.current.size === 0 && audioQueueRef.current.length === 0) {
          isPlayingRef.current = false;
          nextPlaybackTimeRef.current = playbackCtxRef.current?.currentTime || 0;
        }
      };
      const startAt = Math.max(ctx.currentTime + 0.015, nextPlaybackTimeRef.current);
      src.start(startAt);
      nextPlaybackTimeRef.current = startAt + (floats.length / OUTPUT_SAMPLE_RATE);
    }
    isPlayingRef.current = playbackSourcesRef.current.size > 0;
  }, []);

  const playAudioBytes = useCallback((bytes) => {
    try {
      const samples = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2);
      const floats = new Float32Array(samples.length);
      for (let i = 0; i < samples.length; i++) floats[i] = samples[i] / 32768;
      audioQueueRef.current.push(floats);
      scheduleQueuedAudio();
    } catch (e) { log('AUDIO', 'Decode err:', e); }
  }, [scheduleQueuedAudio]);

  const playAudioChunk = useCallback((b64) => {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    playAudioBytes(bytes);
  }, [playAudioBytes]);

  // ─── WS Message Handler ──────────
  const handleMsg = useCallback((event) => {
    if (event.data instanceof Blob) {
      event.data.arrayBuffer().then(buffer => {
        if (!assistantSpeechStartedAtRef.current) assistantSpeechStartedAtRef.current = performance.now();
        setVoiceState('speaking');
        setStatus('Speaking...');
        if (!firstAudioAtRef.current) {
          firstAudioAtRef.current = performance.now();
          log('LATENCY', `first audio heard after ${Math.round(firstAudioAtRef.current - sessionStartAtRef.current)}ms`);
        }
        playAudioBytes(new Uint8Array(buffer));
      }).catch(e => log('AUDIO', 'Blob decode err:', e));
      return;
    }
    if (event.data instanceof ArrayBuffer) {
      if (!assistantSpeechStartedAtRef.current) assistantSpeechStartedAtRef.current = performance.now();
      setVoiceState('speaking');
      setStatus('Speaking...');
      playAudioBytes(new Uint8Array(event.data));
      return;
    }
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === 'heartbeat') return;
      if (msg.type === 'session.ready') {
        if (!sessionIdRef.current) { sessionIdRef.current = msg.session_id; setSessionId(msg.session_id); }
        log('SESSION', `=== SESSION ID: ${sessionIdRef.current} ===`);
        console.log(`%c SESSION: ${sessionIdRef.current} `, 'background:#ff6b5a;color:#111;font-size:14px;font-weight:bold;padding:4px 8px;');
      } else if (msg.type === 'session.state') {
        log('SESSION', 'Resumed state'); const d = msg.data;
        if (d.active_manual_id) setActiveManual({ id: d.active_manual_id, summary: `${d.active_device_model || ''} manual` });
      } else if (msg.type === 'status') { log('STATUS', msg.message); setStatus(msg.message);
      } else if (msg.type === 'assistant.state') {
        log('STATE', msg.state);
        if (msg.state === 'listening') setVoiceState('listening');
        else if (msg.state === 'connecting') setVoiceState('thinking');
        else if (msg.state === 'speaking') setVoiceState('speaking');
      } else if (msg.type === 'audio') { setVoiceState('speaking'); setStatus('Speaking...'); playAudioChunk(msg.data);
      } else if (msg.type === 'transcription') {
        let text = msg.text;
        if (msg.role === 'user') {
          const hasNonLatin = /[^\u0000-\u024F\u1E00-\u1EFF\u2000-\u206F\u2070-\u209F\u20A0-\u20CF\u2100-\u214F.,!?;:'"()\-\s\d]/.test(text);
          log('SPEECH', hasNonLatin ? `User: "${text}" (non-Latin STT)` : `User: "${text}"`);
        } else { log('SPEECH', `AI: "${text}"`); }
        setTranscript(prev => {
          const last = prev[prev.length - 1];
          if (last && last.role === msg.role && !last.final) return [...prev.slice(0, -1), { ...last, text: last.text + text }];
          return [...prev, { role: msg.role, text, final: false }];
        });
      } else if (msg.type === 'turn_complete') {
        log('TURN', 'Complete'); setVoiceState('listening'); setStatus('Listening...');
        assistantSpeechStartedAtRef.current = 0;
        bargeInFramesRef.current = 0;
        setTranscript(p => { if (!p.length) return p; return [...p.slice(0, -1), { ...p[p.length - 1], final: true }]; });
      } else if (msg.type === 'interrupted') { stopPlayback(); bargeInFramesRef.current = 0; setVoiceState('listening'); setStatus('Listening...');
      } else if (msg.type === 'tool.status') {
        if (msg.status === 'running') {
          const detail = msg.tool === 'highlight'
            ? `Marking: ${msg.args?.label || msg.args?.target_hint || 'target'}`
            : `Looking up: ${msg.args?.brand || ''} ${msg.args?.model || ''}`.trim() || 'Searching...';
          log('TOOL', `${msg.tool} running`, msg.args); setToolActivity({ tool: msg.tool, status: 'running', detail }); setVoiceState('thinking'); setStatus(`Running ${msg.tool}...`);
        } else if (msg.status === 'done') { log('TOOL', `${msg.tool} done:`, msg.result_summary);
          if (msg.tool === 'google_search') { setSearchQueries(msg.queries || []); setToolActivity({ tool: msg.tool, status: 'done', detail: `Searched: ${(msg.queries || []).join(', ')}` }); log('SEARCH', msg.queries);
          } else if (msg.tool === 'highlight') { setToolActivity({ tool: msg.tool, status: 'done', detail: msg.result_summary || 'HUD marker active' }); log('HUD', msg.result_summary);
          } else { if (msg.manual_id) { setToolActivity({ tool: msg.tool, status: 'done', detail: msg.result_summary }); setActiveManual({ id: msg.manual_id, summary: msg.result_summary }); log('MANUAL', msg.result_summary);
          } else { setToolActivity({ tool: msg.tool, status: 'done', detail: 'No manual in database — using AI knowledge' }); setActiveManual(null); log('MANUAL', 'No manual found'); }
          if (msg.warnings?.length) { setWarnings(msg.warnings); log('WARN', `${msg.warnings.length} warnings`); }
          if (msg.steps?.length) { setSteps(msg.steps); log('STEPS', `${msg.steps.length} steps`); } }
          setTimeout(() => setToolActivity(null), 4000);
        }
      } else if (msg.type === 'error') { log('ERROR', msg.message); if (msg.message.includes('disconnected')) setStatus('Reconnecting...'); else setStatus('Error: ' + msg.message);
      } else if (msg.type === 'hud.state') {
        const markers = msg.data?.markers || [];
        setHudMarkers(markers);
        setSelectedHudMarkerId(current => current || markers[0]?.id || null);
        setHudVersion(msg.data?.version || 0);
        log('HUD', `${markers.length} marker(s), reason=${msg.reason || 'state'}`);
      } else if (msg.type === 'vision.status') { log('VISION', msg.status); setVisionStatus(msg.status === 'analyzing' ? 'Analyzing...' : null); if (msg.status === 'analyzing') setTimeout(() => setVisionStatus(null), 3000);
      } else if (msg.type === 'step.update') { log('STEP', `→ step ${msg.step}`);
      } else if (msg.type === 'vision.perception') { log('PERCEPTION', `${msg.status} | conf=${msg.confidence ?? '-'} | diff=${msg.diff ?? '-'} | state="${msg.device_state || ''}" | changes=${JSON.stringify(msg.changes || [])}`); }
    } catch (e) { log('ERROR', 'Parse:', e); }
  }, [playAudioBytes, playAudioChunk, stopPlayback]);

  // ─── WebSocket ───────────────────
  const connectAudioWS = useCallback((sid) => {
    return new Promise((resolve, reject) => {
      if (audioWsRef.current?.readyState === WebSocket.OPEN) {
        resolve(audioWsRef.current);
        return;
      }
      const audioWs = new WebSocket(`${WS_BASE}/api/ws/audio/${sid}`);
      audioWs.binaryType = 'arraybuffer';
      audioWsRef.current = audioWs;
      const t = setTimeout(() => reject(new Error('Audio WS timeout')), 5000);
      audioWs.onopen = () => { clearTimeout(t); log('AUDIO_WS', 'Open'); resolve(audioWs); };
      audioWs.onclose = () => { if (audioWsRef.current === audioWs) audioWsRef.current = null; log('AUDIO_WS', 'Closed'); };
      audioWs.onerror = () => { clearTimeout(t); reject(new Error('Audio WS error')); };
    });
  }, []);

  const connectWS = useCallback(() => {
    return new Promise((resolve, reject) => {
      log('WS', 'Connecting...'); const ws = new WebSocket(`${WS_BASE}/api/ws/session`); wsRef.current = ws;
      const t = setTimeout(() => {
        settled = true;
        reject(new Error('Session ready timeout'));
      }, 20000);
      let settled = false;
      const settleReady = (sid) => {
        if (settled) return;
        settled = true;
        clearTimeout(t);
        resolve(sid);
      };
      ws.onopen = () => { log('WS', 'Open'); const cfg = configRef.current;
        const m = { type: 'config', persona_id: cfg.persona_id, voice_id: cfg.voice_id };
        if (sessionIdRef.current) { m.resume_session_id = sessionIdRef.current; log('WS', `Resume ${sessionIdRef.current}`); }
        ws.send(JSON.stringify(m));
        if (heartbeatRef.current) clearInterval(heartbeatRef.current);
        heartbeatRef.current = setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'heartbeat' })); }, 15000);
      };
      ws.onmessage = (event) => {
        if (typeof event.data === 'string') {
          try {
            const msg = JSON.parse(event.data);
            if (msg.type === 'session.ready') settleReady(msg.session_id);
          } catch {}
        }
        handleMsg(event);
      };
      ws.onclose = () => { if (heartbeatRef.current) { clearInterval(heartbeatRef.current); heartbeatRef.current = null; }
        if (sessionActiveRef.current) { reconnectCountRef.current++; log('WS', `Reconnecting #${reconnectCountRef.current}`);
          reconnectRef.current = setTimeout(() => { if (!sessionActiveRef.current) return;
            connectWS().then((sid) => connectAudioWS(sid)).then(() => { setVoiceState('listening'); setStatus('Listening...'); log('WS', 'Reconnected seamlessly'); }).catch(() => { setStatus('Reconnect failed'); setIsConnected(false); sessionActiveRef.current = false; });
          }, 300);
        } else { setIsConnected(false); setVoiceState('idle'); setStatus('Ready'); }
      };
      ws.onerror = () => { clearTimeout(t); reject(new Error('WS error')); };
    });
  }, [connectAudioWS, handleMsg]);

  // ─── Media ───────────────────────
  const sendPcmFrame = useCallback((pcm, rms) => {
    const audioWs = audioWsRef.current;
    if (!audioWs || audioWs.readyState !== WebSocket.OPEN) return;

    const now = performance.now();
    const assistantAudioActive = voiceStateRef.current === 'speaking' || playbackSourcesRef.current.size > 0;
    const armedForBargeIn = assistantAudioActive
      && assistantSpeechStartedAtRef.current
      && now - assistantSpeechStartedAtRef.current >= BARGE_IN_ARM_MS
      && now - lastBargeInRef.current > BARGE_IN_COOLDOWN_MS;

    if (armedForBargeIn && rms >= BARGE_IN_RMS_THRESHOLD) {
      bargeInFramesRef.current += 1;
    } else if (!assistantAudioActive || rms < BARGE_IN_RMS_THRESHOLD * 0.65) {
      bargeInFramesRef.current = 0;
    }

    if (armedForBargeIn && bargeInFramesRef.current >= BARGE_IN_REQUIRED_FRAMES) {
      lastBargeInRef.current = now;
      bargeInFramesRef.current = 0;
      stopPlayback(true);
      setVoiceState('listening');
      setStatus('Listening...');
      if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(JSON.stringify({ type: 'barge_in' }));
    }

    audioWs.send(pcm.buffer.slice(pcm.byteOffset, pcm.byteOffset + pcm.byteLength));
  }, [stopPlayback]);

  const startMic = async () => {
    log('MIC', 'Starting...'); const stream = await navigator.mediaDevices.getUserMedia({ audio: { sampleRate: INPUT_SAMPLE_RATE, channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    micStreamRef.current = stream; const ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: INPUT_SAMPLE_RATE }); audioCtxRef.current = ctx;
    if (ctx.state === 'suspended') await ctx.resume();
    const src = ctx.createMediaStreamSource(stream); micSourceRef.current = src;

    try {
      await ctx.audioWorklet.addModule('/audio-capture-worklet.js');
      const node = new AudioWorkletNode(ctx, 'pcm-capture-processor', {
        processorOptions: { targetSampleRate: INPUT_SAMPLE_RATE, frameMs: AUDIO_FRAME_MS },
      });
      let c = 0;
      node.port.onmessage = (event) => {
        const { pcm, rms } = event.data;
        sendPcmFrame(pcm, rms || 0);
        c++;
        if (c % 100 === 0) log('MIC', `${c} x ${AUDIO_FRAME_MS}ms frames`);
      };
      src.connect(node);
      processorRef.current = node;
      log('MIC', `Active worklet (${AUDIO_FRAME_MS}ms frames)`);
    } catch (e) {
      log('MIC', 'AudioWorklet unavailable, falling back:', e.message);
      const proc = ctx.createScriptProcessor(1024, 1, 1); let c = 0;
      proc.onaudioprocess = (event) => {
        const input = event.inputBuffer.getChannelData(0);
        const pcm = new Int16Array(input.length);
        let sumSquares = 0;
        for (let i = 0; i < input.length; i++) {
          const sample = Math.max(-1, Math.min(1, input[i]));
          sumSquares += sample * sample;
          pcm[i] = sample < 0 ? sample * 32768 : sample * 32767;
        }
        sendPcmFrame(pcm, Math.sqrt(sumSquares / input.length));
        c++;
        if (c % 100 === 0) log('MIC', `${c} fallback frames`);
      };
      src.connect(proc); proc.connect(ctx.destination); processorRef.current = proc;
    }
    setIsMicEnabled(true); setVoiceState('listening'); setStatus('Listening...');
  };
  const stopMic = useCallback(() => { processorRef.current?.disconnect(); processorRef.current = null; micSourceRef.current?.disconnect(); micSourceRef.current = null; audioCtxRef.current?.close(); audioCtxRef.current = null; micStreamRef.current?.getTracks().forEach(t => t.stop()); micStreamRef.current = null; setIsMicEnabled(false); }, []);
  const facingRef = useRef('environment');
  const startCamera = async (facing) => {
    const mode = facing || facingRef.current;
    try { const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: mode, width: 640, height: 480 } });
      const actualFacing = stream.getVideoTracks()[0]?.getSettings?.().facingMode || mode;
      setCameraMirrored(actualFacing === 'user');
      camStreamRef.current = stream; if (videoRef.current) { videoRef.current.srcObject = stream; await videoRef.current.play(); setTimeout(() => captureFrame(), 120); } setCameraEnabled(true);
      frameCountRef.current = 0; frameIntervalRef.current = setInterval(() => captureFrame(), VIDEO_FRAME_INTERVAL_MS); log('CAM', `Active (${mode})`);
    } catch (e) { log('CAM', 'Denied:', e.message); }
  };
  const captureFrame = () => { if (!videoRef.current || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    if (!videoRef.current.videoWidth || !videoRef.current.videoHeight) return;
    const frameHeight = Math.max(1, Math.round((videoRef.current.videoHeight / videoRef.current.videoWidth) * VIDEO_FRAME_WIDTH));
    const c = document.createElement('canvas'); c.width = VIDEO_FRAME_WIDTH; c.height = frameHeight; c.getContext('2d').drawImage(videoRef.current, 0, 0, VIDEO_FRAME_WIDTH, frameHeight);
    try { wsRef.current.send(JSON.stringify({ type: 'video', data: c.toDataURL('image/jpeg', 0.6).split(',')[1] })); frameCountRef.current++; if (frameCountRef.current % 5 === 0) log('CAM', `${frameCountRef.current} frames`); } catch {}
  };
  const stopCamera = useCallback(() => { if (frameIntervalRef.current) clearInterval(frameIntervalRef.current); frameIntervalRef.current = null; camStreamRef.current?.getTracks().forEach(t => t.stop()); camStreamRef.current = null; setCameraEnabled(false); setCameraMirrored(false); }, []);
  const flipCamera = async () => {
    const newFacing = facingRef.current === 'environment' ? 'user' : 'environment';
    facingRef.current = newFacing; setCameraFacing(newFacing);
    if (cameraEnabled) { stopCamera(); await startCamera(newFacing); }
    log('CAM', `Flipped to ${newFacing}`);
  };
  const normalizeHudGeometry = useCallback((geometry) => {
    const width = Math.max(0.015, Math.min(0.8, Number(geometry?.width || 0.08)));
    const height = Math.max(0.015, Math.min(0.8, Number(geometry?.height || 0.08)));
    return {
      type: 'box',
      x: Math.max(0, Math.min(1 - width, Number(geometry?.x || 0))),
      y: Math.max(0, Math.min(1 - height, Number(geometry?.y || 0))),
      width,
      height,
    };
  }, []);
  const getVideoPointFromClient = useCallback((clientX, clientY) => {
    if (!videoRef.current?.videoWidth || !videoRef.current?.videoHeight) return null;
    const container = videoRef.current.closest('.ra-camera-container');
    if (!container) return null;
    const rect = container.getBoundingClientRect();
    const localX = clientX - rect.left;
    const localY = clientY - rect.top;
    const scale = Math.max(rect.width / videoRef.current.videoWidth, rect.height / videoRef.current.videoHeight);
    const renderedW = videoRef.current.videoWidth * scale;
    const renderedH = videoRef.current.videoHeight * scale;
    const offsetX = (rect.width - renderedW) / 2;
    const offsetY = (rect.height - renderedH) / 2;
    let x = (localX - offsetX) / renderedW;
    const y = (localY - offsetY) / renderedH;
    if (cameraMirrored) x = 1 - x;
    if (x < 0 || x > 1 || y < 0 || y > 1) return null;
    return { x, y };
  }, [cameraMirrored]);
  const getVideoPointFromEvent = useCallback((event) => getVideoPointFromClient(event.clientX, event.clientY), [getVideoPointFromClient]);
  const findNearestHudMarker = useCallback((point) => {
    if (!hudMarkers.length) return null;
    return hudMarkers.reduce((best, marker) => {
      const g = marker.geometry || {};
      const cx = (g.x || 0) + (g.width || 0) / 2;
      const cy = (g.y || 0) + (g.height || 0) / 2;
      const dist = (cx - point.x) ** 2 + (cy - point.y) ** 2;
      return !best || dist < best.dist ? { marker, dist } : best;
    }, null)?.marker || null;
  }, [hudMarkers]);
  const sendHudUpdate = useCallback((markerId, geometry, options = {}) => {
    const normalized = normalizeHudGeometry(geometry);
    setHudMarkers(prev => prev.map(marker => marker.id === markerId ? {
      ...marker,
      geometry: normalized,
      model_geometry: normalized,
      tracking_status: options.locked === false ? 'corrected' : 'manual_locked',
      manual_locked: options.locked !== false,
      updated_at_ms: Date.now(),
    } : marker));
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'hud.update',
        marker_id: markerId,
        geometry: normalized,
        locked: options.locked !== false,
        refine: Boolean(options.refine),
      }));
    }
    log('HUD', `manual ${options.refine ? 'refine' : 'update'}`, markerId, normalized);
  }, [normalizeHudGeometry]);
  const sendHudCorrection = useCallback((point, marker, label) => {
    if (wsRef.current?.readyState !== WebSocket.OPEN || !point || !marker) return;
    const now = performance.now();
    const last = lastHudCorrectionRef.current;
    const duplicate = last.label === label && Math.hypot(point.x - last.x, point.y - last.y) < 0.006 && now - last.at < 450;
    if (duplicate) return;
    lastHudCorrectionRef.current = { at: now, x: point.x, y: point.y, label };
    wsRef.current.send(JSON.stringify({
      type: 'hud.correct',
      marker_id: marker.id,
      point,
      label,
    }));
    log('HUD', `${label === 0 ? 'negative' : 'positive'} correction`, point, marker.id);
  }, []);
  const handleHudPointerDown = useCallback((event) => {
    if (!cameraEnabled || !hudMarkers.length) return;
    const point = getVideoPointFromEvent(event);
    if (!point) return;
    const marker = findNearestHudMarker(point);
    if (!marker) return;
    setSelectedHudMarkerId(marker.id);
    if (hudEditMode === 'positive' || event.altKey) {
      sendHudCorrection(point, marker, event.altKey ? 0 : 1);
    } else if (hudEditMode === 'negative') {
      sendHudCorrection(point, marker, 0);
    } else if (hudEditMode === 'select') {
      sendHudUpdate(marker.id, marker.geometry, { locked: true, refine: false });
    }
  }, [cameraEnabled, findNearestHudMarker, getVideoPointFromEvent, hudEditMode, hudMarkers.length, sendHudCorrection, sendHudUpdate]);
  const handleHudMarkerPointerDown = useCallback((event, marker, action, handle = null) => {
    if (!marker?.geometry) return;
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    setSelectedHudMarkerId(marker.id);
    const point = getVideoPointFromEvent(event);
    if (!point) return;
    if (action === 'resize' || action === 'move') {
      hudDragRef.current = {
        pointerId: event.pointerId,
        markerId: marker.id,
        action,
        handle,
        startPoint: point,
        startGeometry: normalizeHudGeometry(marker.geometry),
        lastGeometry: normalizeHudGeometry(marker.geometry),
      };
    } else {
      sendHudUpdate(marker.id, marker.geometry, { locked: true, refine: false });
    }
  }, [getVideoPointFromEvent, normalizeHudGeometry, sendHudUpdate]);
  const handleHudPointerMove = useCallback((event) => {
    const drag = hudDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const point = getVideoPointFromEvent(event);
    if (!point) return;
    const dx = point.x - drag.startPoint.x;
    const dy = point.y - drag.startPoint.y;
    const g = drag.startGeometry;
    let next = { ...g };
    if (drag.action === 'move') {
      next.x = g.x + dx;
      next.y = g.y + dy;
    } else if (drag.action === 'resize') {
      const left0 = g.x;
      const top0 = g.y;
      const right0 = g.x + g.width;
      const bottom0 = g.y + g.height;
      let left = drag.handle.includes('w') ? left0 + dx : left0;
      let right = drag.handle.includes('e') ? right0 + dx : right0;
      let top = drag.handle.includes('n') ? top0 + dy : top0;
      let bottom = drag.handle.includes('s') ? bottom0 + dy : bottom0;
      if (right - left < 0.015) drag.handle.includes('w') ? (left = right - 0.015) : (right = left + 0.015);
      if (bottom - top < 0.015) drag.handle.includes('n') ? (top = bottom - 0.015) : (bottom = top + 0.015);
      next = { type: 'box', x: left, y: top, width: right - left, height: bottom - top };
    }
    const normalized = normalizeHudGeometry(next);
    hudDragRef.current = { ...drag, lastGeometry: normalized };
    setHudMarkers(prev => prev.map(marker => marker.id === drag.markerId ? {
      ...marker,
      geometry: normalized,
      tracking_status: 'manual_locked',
      manual_locked: true,
      debug: { ...(marker.debug || {}), manual_dragging: drag.action },
    } : marker));
  }, [getVideoPointFromEvent, normalizeHudGeometry]);
  const handleHudPointerUp = useCallback((event) => {
    const drag = hudDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    hudDragRef.current = null;
    if (drag.lastGeometry) sendHudUpdate(drag.markerId, drag.lastGeometry, { locked: true, refine: true });
  }, [sendHudUpdate]);
  const cleanup = useCallback(() => { if (heartbeatRef.current) clearInterval(heartbeatRef.current); if (reconnectRef.current) clearTimeout(reconnectRef.current);
    if (audioWsRef.current?.readyState === WebSocket.OPEN) audioWsRef.current.close(); audioWsRef.current = null;
    if (wsRef.current?.readyState === WebSocket.OPEN) { wsRef.current.send(JSON.stringify({ type: 'end' })); wsRef.current.close(); } wsRef.current = null; stopMic(); stopCamera(); stopPlayback(); playbackCtxRef.current?.close(); playbackCtxRef.current = null; }, [stopCamera, stopMic, stopPlayback]);

  useEffect(() => {
    log('INIT', 'Loading...');
    if (!hasBackendConfig) {
      setStatus('Backend URL missing');
      return;
    }
    loadData();
    return () => {
      sessionActiveRef.current = false;
      cleanup();
    };
  }, [cleanup, hasBackendConfig, loadData]);

  useEffect(() => {
    return () => {
      if (reviewPollRef.current) clearInterval(reviewPollRef.current);
    };
  }, []);

  const toggleCamera = async () => {
    if (cameraEnabled) { stopCamera(); log('CAM', 'Toggled OFF'); }
    else { await startCamera(); log('CAM', 'Toggled ON'); }
  };

  const startSession = async () => {
    if (!hasBackendConfig) {
      setStatus('Backend URL missing');
      return;
    }
    try { log('SESSION', 'Starting...'); setStatus('Connecting...'); setShowDatasetPrompt(false); setDatasetReview(null); setEndedSessionId(null); sessionStartAtRef.current = performance.now(); firstAudioAtRef.current = 0; assistantSpeechStartedAtRef.current = 0; bargeInFramesRef.current = 0; configRef.current = { persona_id: selectedPersona, voice_id: selectedVoice }; sessionActiveRef.current = true; reconnectCountRef.current = 0;
      const readySessionId = await connectWS(); setIsConnected(true); setTranscript([{ role: 'system', text: 'Session started — speak and show your device!', final: true }]);
      await connectAudioWS(readySessionId);
      await Promise.all([startMic(), startCamera()]);
      if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(JSON.stringify({ type: 'client_ready' }));
      setVoiceState('listening'); setStatus('Listening...'); log('SESSION', 'Active');
    } catch (e) { sessionActiveRef.current = false; setStatus('Error: ' + e.message); stopMic(); stopCamera(); wsRef.current?.close(); wsRef.current = null; }
  };

  const pollDatasetReview = useCallback((sid) => {
    if (reviewPollRef.current) clearInterval(reviewPollRef.current);
    reviewPollRef.current = setInterval(async () => {
      try {
        const res = await fetch(`${API}/hud/review/${sid}`);
        const data = await res.json();
        setDatasetReview(data);
        if (!['queued', 'running'].includes(data.status)) {
          clearInterval(reviewPollRef.current);
          reviewPollRef.current = null;
          setDatasetReviewBusy(false);
        }
      } catch (error) {
        setDatasetReview(prev => ({ ...(prev || {}), status: 'status_error', error: error.message }));
        clearInterval(reviewPollRef.current);
        reviewPollRef.current = null;
        setDatasetReviewBusy(false);
      }
    }, 2000);
  }, []);

  const startDatasetReview = async (mode) => {
    if (!endedSessionId || datasetReviewBusy) return;
    try {
      setDatasetReviewBusy(true);
      setDatasetReview({ status: 'starting', mode });
      const res = await fetch(`${API}/hud/review/${endedSessionId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
      });
      const data = await res.json();
      setDatasetReview(data);
      if (mode === 'gemini' && ['queued', 'running'].includes(data.status)) {
        pollDatasetReview(endedSessionId);
      } else {
        setDatasetReviewBusy(false);
      }
    } catch (error) {
      setDatasetReview({ status: 'failed', mode, error: error.message });
      setDatasetReviewBusy(false);
    }
  };

  const skipDatasetReview = () => {
    if (reviewPollRef.current) clearInterval(reviewPollRef.current);
    reviewPollRef.current = null;
    setShowDatasetPrompt(false);
    setDatasetReview(null);
    setDatasetReviewBusy(false);
  };

  const endSession = () => { const finishedSessionId = sessionIdRef.current || sessionId; log('SESSION', 'Ending'); sessionActiveRef.current = false; cleanup();
    setIsConnected(false); setIsMicEnabled(false); setVoiceState('idle'); setStatus('Ready'); setTranscript([]); setSessionId(null); sessionIdRef.current = null; reconnectCountRef.current = 0;
    setActiveManual(null); setToolActivity(null); setWarnings([]); setSteps([]); setSearchQueries([]); setHudMarkers([]); setHudVersion(0); setSelectedHudMarkerId(null); setHudEditMode('select');
    if (finishedSessionId) {
      setEndedSessionId(finishedSessionId);
      setShowDatasetPrompt(true);
      setDatasetReview({ status: 'waiting_for_choice', session_id: finishedSessionId });
    }
  };

  // ─── Mobile Swipe ────────────────
  const handleTouchStart = (e) => { touchStartRef.current = e.touches[0].clientX; };
  const handleTouchEnd = (e) => {
    if (!touchStartRef.current) return;
    const diff = touchStartRef.current - e.changedTouches[0].clientX;
    if (Math.abs(diff) > 50) {
      if (diff > 0) { setMobileCardIndex(i => (i + 1) % personas.length); }
      else { setMobileCardIndex(i => (i - 1 + personas.length) % personas.length); }
    }
    touchStartRef.current = null;
  };
  useEffect(() => { if (personas.length > 0) setSelectedPersona(personas[mobileCardIndex]?.id); }, [mobileCardIndex, personas]);

  // ─── RENDER ──────────────────────
  const selectedPersonaData = personas.find(p => p.id === selectedPersona);
  const liveHudMarkers = usePrecisionHudTracking({
    markers: hudMarkers,
    videoRef,
    wsRef,
    enabled: isConnected && cameraEnabled && hudMarkers.length > 0,
    mirrored: cameraMirrored,
  });

  if (!hasBackendConfig) {
    return (
      <div className="repair-assistant" data-testid="repair-assistant">
        <main className="ra-main-content" style={{ display: 'grid', placeItems: 'center', minHeight: '100vh', padding: '24px' }}>
          <section style={{ maxWidth: '560px', padding: '24px', border: '1px solid rgba(255,255,255,0.12)', borderRadius: '20px', background: 'rgba(10,10,10,0.9)', color: '#f5f5f5' }}>
            <h1 style={{ margin: '0 0 12px', fontSize: '1.5rem' }}>Frontend configuration missing</h1>
            <p style={{ margin: '0 0 8px' }}>Set <code>REACT_APP_BACKEND_URL</code> in Netlify and redeploy.</p>
            <p style={{ margin: 0 }}>Expected value: <code>https://16-16-11-190.sslip.io</code></p>
          </section>
        </main>
      </div>
    );
  }

  return (
    <div className="repair-assistant" data-testid="repair-assistant">
      <header className="ra-header">
        <div className="ra-header-left">
          <div className="ra-live-indicator">
            <span className={`ra-live-dot ${isConnected ? 'connected' : ''}`}></span>
            <span className="ra-live-text" data-testid="status-text">{status}</span>
          </div>
        </div>
        <div className="ra-header-center"><h1 className="ra-app-title">Repair Assistant</h1></div>
        <div className="ra-header-right">
          {sessionId && <span className="ra-session-id" data-testid="session-id">{sessionId.slice(0, 8)}</span>}
        </div>
      </header>

      <main className="ra-main-content">
        {!isConnected ? (
          /* ═══ SETUP PAGE ═══ */
          <div className="ra-setup-page">
            {showDatasetPrompt && endedSessionId && (
              <section className="ra-post-session-panel" data-testid="post-session-review-panel">
                <div>
                  <h2 className="ra-section-heading">Use Last Session Data?</h2>
                  <p className="ra-post-session-copy">
                    Session {endedSessionId.slice(0, 8)} saved HUD frames, marker events, and corrections. Review runs after the session, so live voice speed is unaffected.
                  </p>
                  {datasetReview && (
                    <div className="ra-review-status" data-testid="dataset-review-status">
                      <span>{datasetReview.status}</span>
                      {Number.isFinite(datasetReview.reviewed_examples) && Number.isFinite(datasetReview.total_examples) && (
                        <small>{datasetReview.reviewed_examples}/{datasetReview.total_examples}</small>
                      )}
                      {datasetReview.export?.root && <code>{datasetReview.export.root}</code>}
                      {datasetReview.error && <small>{datasetReview.error}</small>}
                    </div>
                  )}
                </div>
                <div className="ra-post-session-actions">
                  <button type="button" className="ra-review-btn primary" onClick={() => startDatasetReview('gemini')} disabled={datasetReviewBusy}>
                    Gemini Review
                  </button>
                  <button type="button" className="ra-review-btn" onClick={() => startDatasetReview('manual')} disabled={datasetReviewBusy}>
                    Manual Review
                  </button>
                  <button type="button" className="ra-review-btn subtle" onClick={skipDatasetReview}>
                    Skip
                  </button>
                </div>
              </section>
            )}
            <section className="ra-personality-section">
              <h2 className="ra-section-heading">Select Personality</h2>
              <div className="ra-persona-grid">
                {personas.map(p => (
                  <div key={p.id} className={`ra-persona-card ${selectedPersona === p.id ? 'selected' : ''}`}
                    onClick={() => setSelectedPersona(p.id)} data-testid={`persona-card-${p.id}`}>
                    {selectedPersona === p.id && <div className="ra-persona-check">
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3"><polyline points="20 6 9 17 4 12"/></svg>
                    </div>}
                    <div className="ra-persona-avatar"><img src={p.avatar} alt={p.name} /></div>
                    <h3 className="ra-persona-name">{p.name}</h3>
                    <span className="ra-persona-trait">{p.trait}</span>
                    <p className="ra-persona-desc">{p.description}</p>
                  </div>
                ))}
              </div>
              <div className="ra-persona-carousel" onTouchStart={handleTouchStart} onTouchEnd={handleTouchEnd}>
                <div className="ra-carousel-track" style={{ transform: `translateX(${-mobileCardIndex * 85}%)` }}>
                  {personas.map((p, i) => (
                    <div key={p.id} className={`ra-carousel-card ${i === mobileCardIndex ? 'active' : ''}`} onClick={() => { setMobileCardIndex(i); setSelectedPersona(p.id); }}>
                      <div className="ra-carousel-img"><img src={p.avatar} alt={p.name} /></div>
                      <span className="ra-carousel-trait">{p.trait}</span>
                      <h3 className="ra-carousel-name">{p.name}</h3>
                      <p className="ra-carousel-desc">{p.description}</p>
                    </div>
                  ))}
                </div>
                <div className="ra-carousel-dots">{personas.map((_, i) => <span key={i} className={`ra-dot ${i === mobileCardIndex ? 'active' : ''}`} />)}</div>
              </div>
            </section>

            <section className="ra-voice-section">
              <h2 className="ra-section-heading">Choose Voice</h2>
              <div className="ra-voice-list">
                {voices.map(v => (
                  <div key={v.id} className={`ra-voice-row ${selectedVoice === v.id ? 'selected' : ''}`}
                    onClick={() => setSelectedVoice(v.id)} data-testid={`voice-row-${v.id}`}>
                    <div className="ra-voice-play">
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21"/></svg>
                    </div>
                    <div className="ra-voice-info">
                      <div className="ra-voice-name">{v.name}</div>
                      <div className="ra-voice-meta">{v.gender} &middot; {v.trait}</div>
                    </div>
                    <div className="ra-voice-wave">{[1,2,3,4,5].map(i => <div key={i} className="ra-wave-line" />)}</div>
                    <div className={`ra-voice-radio ${selectedVoice === v.id ? 'checked' : ''}`}>
                      {selectedVoice === v.id && <div className="ra-radio-dot" />}
                    </div>
                  </div>
                ))}
              </div>
            </section>

            <div className="ra-init-section">
              <button className="ra-btn-init" onClick={startSession} data-testid="start-session-button">
                Initialize Session
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
              </button>
            </div>
          </div>
        ) : (
          /* ═══ ACTIVE SESSION ═══ */
          <div className={`ra-session-layout ${!showTranscript ? 'no-transcript' : ''}`}>
            <div className="ra-camera-section">
              <div
                className="ra-camera-container"
                data-testid="camera-container"
                onPointerDown={handleHudPointerDown}
                onPointerMove={handleHudPointerMove}
                onPointerUp={handleHudPointerUp}
                onPointerCancel={handleHudPointerUp}
              >
                <video ref={videoRef} className={`ra-camera-feed ${cameraMirrored ? 'mirrored' : ''} ${!cameraEnabled ? 'hidden' : ''}`} autoPlay playsInline muted data-testid="camera-feed" />
                {!cameraEnabled && <div className="ra-camera-placeholder">Camera is off</div>}
                {cameraEnabled && liveHudMarkers.length > 0 && (
                  <div className="ra-hud-overlay" data-testid="hud-overlay">
                    {liveHudMarkers.map(marker => (
                      <HudMarker
                        key={marker.id}
                        marker={marker}
                        selected={marker.id === selectedHudMarkerId}
                        editMode={hudEditMode}
                        onPointerDown={handleHudMarkerPointerDown}
                      />
                    ))}
                  </div>
                )}
                <div className={`ra-voice-aura ${voiceState}`} data-testid="voice-aura">
                  <div className="ra-aura-ring"></div><div className="ra-aura-ring delay-1"></div><div className="ra-aura-ring delay-2"></div>
                </div>
                <div className="ra-voice-badge" data-testid="voice-state-badge">
                  {voiceState === 'listening' && 'Listening...'}{voiceState === 'speaking' && 'Speaking...'}{voiceState === 'thinking' && 'Thinking...'}{voiceState === 'idle' && 'Idle'}
                </div>
                {visionStatus && <div className="ra-vision-status">{visionStatus}</div>}
                {cameraEnabled && liveHudMarkers.length > 0 && (
                  <div className="ra-hud-toolbar" data-testid="hud-toolbar" onPointerDown={(event) => event.stopPropagation()}>
                    {[
                      ['select', 'Lock'],
                      ['positive', 'This'],
                      ['negative', 'Not'],
                      ['move', 'Move'],
                      ['resize', 'Size'],
                    ].map(([mode, label]) => (
                      <button
                        key={mode}
                        type="button"
                        className={`ra-hud-tool ${hudEditMode === mode ? 'active' : ''}`}
                        onClick={() => setHudEditMode(mode)}
                      >
                        {label}
                      </button>
                    ))}
                    <span className="ra-hud-mirror-state">{cameraMirrored ? 'mirrored' : cameraFacing}</span>
                  </div>
                )}
              </div>
              {/* Controls — inside camera section so they center relative to the camera like the aura */}
              <div className="ra-controls-bar" data-testid="controls-bar">
                <button className={`ra-control-btn ra-control-btn-text ${showTranscript ? 'active' : ''}`} onClick={() => setShowTranscript(!showTranscript)} data-testid="transcript-toggle-button">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
                  <span className="ra-control-label">{showTranscript ? 'Hide' : 'Show'}</span>
                </button>
                <div className="ra-controls-center">
                  <button className={`ra-control-btn ${isMicEnabled ? 'active' : ''}`} onClick={() => { if (isMicEnabled) { stopMic(); setVoiceState('idle'); } else { startMic().then(() => setVoiceState('listening')); } }} disabled={!isConnected} data-testid="mic-button">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>
                  </button>
                  <button className="ra-control-btn ra-control-btn-end" onClick={endSession} data-testid="end-session-button">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                  </button>
                  <button className={`ra-control-btn ${cameraEnabled ? 'active' : ''}`} onClick={toggleCamera} data-testid="camera-toggle-button">
                    {cameraEnabled ? (
                      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
                    ) : (
                      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M16 16v1a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h2m5.66 0H14a2 2 0 0 1 2 2v3.34l1 1L23 7v10"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
                    )}
                  </button>
                  <button className="ra-control-btn ra-control-btn-flip" onClick={flipCamera} disabled={!cameraEnabled} data-testid="flip-camera-button">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M11 19H4a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h5"/><path d="M13 5h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-5"/><polyline points="16 3 19 6 16 9"/><polyline points="8 15 5 18 8 21"/></svg>
                  </button>
                </div>
              </div>
            </div>
            {showTranscript && <div className="ra-info-section">
              {toolActivity && <div className={`ra-panel ra-tool-panel ${toolActivity.status}`} data-testid="tool-panel"><div className="ra-panel-title">{toolActivity.tool === 'google_search' ? 'Google Search' : 'Tool Activity'}</div><div className="ra-tool-detail">{toolActivity.detail}</div>{toolActivity.status === 'running' && <div className="ra-tool-spinner"></div>}</div>}
              {liveHudMarkers.length > 0 && <div className="ra-panel ra-hud-panel" data-testid="hud-panel"><div className="ra-panel-title">HUD Tracking Debug</div><div className="ra-hud-panel-row"><span>{liveHudMarkers.length} active marker{liveHudMarkers.length === 1 ? '' : 's'}</span><span>v{hudVersion}</span></div>{liveHudMarkers.slice(0, 3).map(m => <div key={m.id} className="ra-hud-panel-marker"><span>{m.label || m.target_hint}</span><small>{m.tracking_status} · {Math.round((m.confidence || 0) * 100)}%</small><code>score {m.debug?.score ?? '-'} · raw {m.debug?.raw_confidence ?? '-'} · lost {m.debug?.lost_frames ?? 0}</code></div>)}</div>}
              {activeManual && <div className="ra-panel ra-manual-panel" data-testid="manual-panel"><div className="ra-panel-title">Active Manual</div><div className="ra-manual-summary">{activeManual.summary}</div></div>}
              {warnings.length > 0 && <div className="ra-panel ra-warnings-panel" data-testid="warnings-panel"><div className="ra-panel-title">Warnings</div>{warnings.slice(0, 3).map((w, i) => <div key={i} className="ra-warning-item">{w}</div>)}</div>}
              {steps.length > 0 && <div className="ra-panel ra-steps-panel" data-testid="steps-panel"><div className="ra-panel-title">Steps</div>{steps.map((s, i) => <div key={i} className="ra-step-item"><span className="ra-step-num">{s.step || i + 1}</span><div className="ra-step-content"><div className="ra-step-title">{s.title}</div>{s.action && <div className="ra-step-action">{s.action}</div>}</div></div>)}</div>}
              <div className="ra-panel ra-transcript-panel" data-testid="transcript-panel">
                <div className="ra-panel-title">Conversation</div>
                <div className="ra-transcript-messages">{transcript.map((t, i) => <div key={i} className={`ra-message ${t.role}`}><span className="ra-message-role">{t.role === 'user' ? 'YOU' : t.role === 'system' ? 'SYS' : 'AI'}</span><span className="ra-message-content">{t.text}</span></div>)}<div ref={transcriptEndRef} /></div>
              </div>
            </div>}
          </div>
        )}
      </main>
    </div>
  );
}
