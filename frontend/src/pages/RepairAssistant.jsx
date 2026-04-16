import { useState, useEffect, useRef, useCallback } from 'react';
import './RepairAssistant.css';

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const WS_BASE = process.env.REACT_APP_BACKEND_URL.replace('https://', 'wss://').replace('http://', 'ws://');

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

  const wsRef = useRef(null);
  const audioCtxRef = useRef(null);
  const processorRef = useRef(null);
  const micStreamRef = useRef(null);
  const camStreamRef = useRef(null);
  const videoRef = useRef(null);
  const frameIntervalRef = useRef(null);
  const audioQueueRef = useRef([]);
  const isPlayingRef = useRef(false);
  const playbackCtxRef = useRef(null);
  const heartbeatRef = useRef(null);
  const reconnectRef = useRef(null);
  const sessionActiveRef = useRef(false);
  const configRef = useRef(null);
  const transcriptEndRef = useRef(null);

  useEffect(() => {
    loadData();
    return () => { sessionActiveRef.current = false; cleanup(); };
  }, []);

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [transcript]);

  const loadData = async () => {
    try {
      const [p, v] = await Promise.all([fetch(`${API}/personas`).then(r => r.json()), fetch(`${API}/voices`).then(r => r.json())]);
      setPersonas(p); setVoices(v);
    } catch (e) { setStatus('Error loading options'); }
  };

  // ─── Audio Playback ────────────────────
  const playAudioChunk = useCallback((b64) => {
    try {
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      audioQueueRef.current.push(bytes);
      if (!isPlayingRef.current) drainQueue();
    } catch (e) { console.error('Audio decode err:', e); }
  }, []);

  const drainQueue = async () => {
    if (isPlayingRef.current) return;
    isPlayingRef.current = true;
    if (!playbackCtxRef.current) playbackCtxRef.current = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
    const ctx = playbackCtxRef.current;
    while (audioQueueRef.current.length > 0) {
      const bytes = audioQueueRef.current.shift();
      const samples = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2);
      const floats = new Float32Array(samples.length);
      for (let i = 0; i < samples.length; i++) floats[i] = samples[i] / 32768;
      const buf = ctx.createBuffer(1, floats.length, 24000);
      buf.getChannelData(0).set(floats);
      const src = ctx.createBufferSource();
      src.buffer = buf; src.connect(ctx.destination);
      await new Promise(r => { src.onended = r; src.start(); });
    }
    isPlayingRef.current = false;
  };

  // ─── WS Message Handler ────────────────
  const handleMsg = useCallback((event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === 'heartbeat') return;

      if (msg.type === 'session.ready') {
        setSessionId(msg.session_id);
      } else if (msg.type === 'status') {
        setStatus(msg.message);
      } else if (msg.type === 'assistant.state') {
        if (msg.state === 'listening') setVoiceState('listening');
        else if (msg.state === 'connecting') setVoiceState('thinking');
      } else if (msg.type === 'audio') {
        setVoiceState('speaking');
        setStatus('Assistant speaking...');
        playAudioChunk(msg.data);
      } else if (msg.type === 'transcription') {
        setTranscript(prev => {
          const last = prev[prev.length - 1];
          if (last && last.role === msg.role && !last.final) {
            return [...prev.slice(0, -1), { ...last, text: last.text + msg.text }];
          }
          return [...prev, { role: msg.role, text: msg.text, final: false }];
        });
      } else if (msg.type === 'turn_complete') {
        setVoiceState('listening');
        setStatus('Listening...');
        setTranscript(prev => {
          if (!prev.length) return prev;
          return [...prev.slice(0, -1), { ...prev[prev.length - 1], final: true }];
        });
      } else if (msg.type === 'interrupted') {
        audioQueueRef.current = [];
        setVoiceState('listening');
        setStatus('Listening...');
      } else if (msg.type === 'tool.status') {
        if (msg.status === 'running') {
          setToolActivity({ tool: msg.tool, status: 'Looking up manual...', args: msg.args });
          setVoiceState('thinking');
          setStatus('Looking up repair manual...');
        } else if (msg.status === 'done') {
          setToolActivity({ tool: msg.tool, status: 'Manual found', summary: msg.result_summary });
          if (msg.manual_id) setActiveManual({ id: msg.manual_id, summary: msg.result_summary });
          if (msg.warnings?.length) setWarnings(msg.warnings);
          if (msg.steps?.length) setSteps(msg.steps);
          setTimeout(() => setToolActivity(null), 3000);
        }
      } else if (msg.type === 'error') {
        console.error('[Error]', msg.message);
        if (msg.message.includes('disconnected')) {
          setStatus('Reconnecting...');
        } else {
          setStatus('Error: ' + msg.message);
        }
      }
    } catch (e) { console.error('WS parse err:', e); }
  }, [playAudioChunk]);

  // ─── WebSocket Connect ─────────────────
  const connectWS = useCallback(() => {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(`${WS_BASE}/api/ws/session`);
      wsRef.current = ws;
      const tmout = setTimeout(() => reject(new Error('Connection timeout')), 10000);

      ws.onopen = () => {
        clearTimeout(tmout);
        const cfg = configRef.current;
        ws.send(JSON.stringify({ type: 'config', persona_id: cfg.persona_id, voice_id: cfg.voice_id }));
        if (heartbeatRef.current) clearInterval(heartbeatRef.current);
        heartbeatRef.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'heartbeat' }));
        }, 20000);
        resolve(ws);
      };
      ws.onmessage = handleMsg;
      ws.onclose = () => {
        if (heartbeatRef.current) { clearInterval(heartbeatRef.current); heartbeatRef.current = null; }
        if (sessionActiveRef.current) {
          setStatus('Reconnecting...');
          reconnectRef.current = setTimeout(() => {
            if (!sessionActiveRef.current) return;
            connectWS().then(() => {
              setStatus('Listening...');
              setVoiceState('listening');
              setTranscript(p => [...p, { role: 'system', text: '(Reconnected)', final: true }]);
            }).catch(() => {
              setStatus('Reconnection failed');
              setIsConnected(false);
              sessionActiveRef.current = false;
            });
          }, 1000);
        } else {
          setIsConnected(false);
          setVoiceState('idle');
          setStatus('Ready');
        }
      };
      ws.onerror = () => { clearTimeout(tmout); reject(new Error('WS error')); };
    });
  }, [handleMsg]);

  // ─── Mic Capture ───────────────────────
  const startMic = async () => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    micStreamRef.current = stream;
    const ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
    audioCtxRef.current = ctx;
    const src = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(8192, 1, 1);
    proc.onaudioprocess = (e) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
      const input = e.inputBuffer.getChannelData(0);
      const pcm = new Int16Array(input.length);
      for (let i = 0; i < input.length; i++) pcm[i] = Math.max(-32768, Math.min(32767, Math.round(input[i] * 32767)));
      try { wsRef.current.send(JSON.stringify({ type: 'audio', data: u8ToB64(new Uint8Array(pcm.buffer)) })); } catch {}
    };
    src.connect(proc); proc.connect(ctx.destination);
    processorRef.current = proc;
    setIsMicEnabled(true);
  };

  const stopMic = () => {
    processorRef.current?.disconnect(); processorRef.current = null;
    audioCtxRef.current?.close(); audioCtxRef.current = null;
    micStreamRef.current?.getTracks().forEach(t => t.stop()); micStreamRef.current = null;
    setIsMicEnabled(false);
  };

  // ─── Camera Capture ────────────────────
  const startCamera = async () => {
    const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment', width: 640, height: 480 } });
    camStreamRef.current = stream;
    if (videoRef.current) { videoRef.current.srcObject = stream; videoRef.current.play(); }
    setCameraEnabled(true);
    // Send frames every 2 seconds
    frameIntervalRef.current = setInterval(() => captureAndSendFrame(), 2000);
  };

  const captureAndSendFrame = () => {
    if (!videoRef.current || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    const canvas = document.createElement('canvas');
    canvas.width = 640; canvas.height = 480;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(videoRef.current, 0, 0, 640, 480);
    const dataUrl = canvas.toDataURL('image/jpeg', 0.6);
    const b64 = dataUrl.split(',')[1];
    try { wsRef.current.send(JSON.stringify({ type: 'video', data: b64 })); } catch {}
  };

  const stopCamera = () => {
    if (frameIntervalRef.current) { clearInterval(frameIntervalRef.current); frameIntervalRef.current = null; }
    camStreamRef.current?.getTracks().forEach(t => t.stop()); camStreamRef.current = null;
    setCameraEnabled(false);
  };

  const cleanup = () => {
    if (heartbeatRef.current) clearInterval(heartbeatRef.current);
    if (reconnectRef.current) clearTimeout(reconnectRef.current);
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'end' }));
      wsRef.current.close();
    }
    wsRef.current = null;
    stopMic(); stopCamera();
    audioQueueRef.current = [];
    playbackCtxRef.current?.close(); playbackCtxRef.current = null;
  };

  // ─── Session Control ───────────────────
  const startSession = async () => {
    try {
      setStatus('Connecting...');
      configRef.current = { persona_id: selectedPersona, voice_id: selectedVoice };
      sessionActiveRef.current = true;
      await connectWS();
      setIsConnected(true);
      await new Promise(r => setTimeout(r, 1500));
      await startMic();
      await startCamera();
      setVoiceState('listening');
      setStatus('Listening...');
      setTranscript([{ role: 'system', text: 'Session started — speak and show your device!', final: true }]);
    } catch (e) {
      sessionActiveRef.current = false;
      let msg = e.message;
      if (msg.includes('Permission denied') || msg.includes('NotAllowed')) msg = 'Microphone/camera access denied.';
      setStatus('Error: ' + msg);
      stopMic(); stopCamera();
      wsRef.current?.close(); wsRef.current = null;
    }
  };

  const endSession = () => {
    sessionActiveRef.current = false;
    cleanup();
    setIsConnected(false); setIsMicEnabled(false); setVoiceState('idle');
    setStatus('Ready'); setTranscript([]); setSessionId(null);
    setActiveManual(null); setToolActivity(null); setWarnings([]); setSteps([]);
  };

  // ─── Render ────────────────────────────
  return (
    <div className="repair-assistant" data-testid="repair-assistant">
      {/* Header */}
      <header className="ra-header">
        <div className="ra-header-left">
          <div className="ra-live-indicator">
            <span className={`ra-live-dot ${isConnected ? 'connected' : ''}`}></span>
            <span className="ra-live-text" data-testid="status-text">{status}</span>
          </div>
        </div>
        <div className="ra-header-center"><h1 className="ra-app-title">Repair Assistant</h1></div>
        <div className="ra-header-right"><span className="ra-mode-badge" data-testid="mode-badge">Gemini Live</span></div>
      </header>

      <main className="ra-main-content">
        {!isConnected ? (
          /* ─── Setup Screen ─── */
          <div className="ra-setup-panel">
            <div className="ra-setup-content">
              <div className="ra-setup-hero">
                <div className="ra-voice-icon-large" data-testid="voice-icon">
                  <svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>
                </div>
                <h2 className="ra-setup-title">Voice + Vision Repair</h2>
                <p className="ra-setup-subtitle">Talk naturally and show your device — powered by Gemini Live</p>
              </div>
              <div className="ra-setup-options">
                <div className="ra-option-group">
                  <label className="ra-option-label">Assistant Personality</label>
                  <select className="ra-option-select" value={selectedPersona} onChange={e => setSelectedPersona(e.target.value)} data-testid="persona-select">
                    {personas.map(p => <option key={p.id} value={p.id}>{p.name} — {p.description}</option>)}
                  </select>
                </div>
                <div className="ra-option-group">
                  <label className="ra-option-label">Voice</label>
                  <select className="ra-option-select" value={selectedVoice} onChange={e => setSelectedVoice(e.target.value)} data-testid="voice-select">
                    {voices.map(v => <option key={v.id} value={v.id}>{v.name} — {v.description}</option>)}
                  </select>
                </div>
              </div>
              <button className="ra-btn-primary" onClick={startSession} data-testid="start-session-button">
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>
                Start Session
              </button>
            </div>
          </div>
        ) : (
          /* ─── Active Session ─── */
          <div className="ra-session-layout">
            {/* Left: Camera Feed */}
            <div className="ra-camera-section">
              <div className="ra-camera-container" data-testid="camera-container">
                <video ref={videoRef} className="ra-camera-feed" autoPlay playsInline muted data-testid="camera-feed" />
                {!cameraEnabled && <div className="ra-camera-placeholder">Camera loading...</div>}
                <div className={`ra-voice-aura ${voiceState}`} data-testid="voice-aura">
                  <div className="ra-aura-ring"></div>
                  <div className="ra-aura-ring delay-1"></div>
                  <div className="ra-aura-ring delay-2"></div>
                </div>
                <div className="ra-voice-badge" data-testid="voice-state-badge">
                  {voiceState === 'listening' && 'Listening...'}
                  {voiceState === 'speaking' && 'Speaking...'}
                  {voiceState === 'thinking' && 'Thinking...'}
                  {voiceState === 'idle' && 'Idle'}
                </div>
              </div>
            </div>

            {/* Right: Info Panels */}
            <div className="ra-info-section">
              {/* Tool Activity */}
              {toolActivity && (
                <div className="ra-panel ra-tool-panel" data-testid="tool-panel">
                  <div className="ra-panel-title">Tool Activity</div>
                  <div className="ra-tool-status">{toolActivity.status}</div>
                  {toolActivity.summary && <div className="ra-tool-summary">{toolActivity.summary}</div>}
                </div>
              )}

              {/* Active Manual */}
              {activeManual && (
                <div className="ra-panel ra-manual-panel" data-testid="manual-panel">
                  <div className="ra-panel-title">Active Manual</div>
                  <div className="ra-manual-summary">{activeManual.summary}</div>
                </div>
              )}

              {/* Warnings */}
              {warnings.length > 0 && (
                <div className="ra-panel ra-warnings-panel" data-testid="warnings-panel">
                  <div className="ra-panel-title">Warnings</div>
                  {warnings.map((w, i) => <div key={i} className="ra-warning-item">{w}</div>)}
                </div>
              )}

              {/* Steps */}
              {steps.length > 0 && (
                <div className="ra-panel ra-steps-panel" data-testid="steps-panel">
                  <div className="ra-panel-title">Troubleshooting Steps</div>
                  {steps.map((s, i) => (
                    <div key={i} className="ra-step-item">
                      <span className="ra-step-num">{s.step || i + 1}</span>
                      <span className="ra-step-text">{s.title || s.action}</span>
                    </div>
                  ))}
                </div>
              )}

              {/* Transcript */}
              <div className="ra-panel ra-transcript-panel" data-testid="transcript-panel">
                <div className="ra-panel-title">Conversation</div>
                <div className="ra-transcript-messages">
                  {transcript.map((t, i) => (
                    <div key={i} className={`ra-message ${t.role}`}>
                      <span className="ra-message-role">{t.role === 'user' ? 'YOU' : t.role === 'system' ? 'SYS' : 'AI'}</span>
                      <span className="ra-message-content">{t.text}</span>
                    </div>
                  ))}
                  <div ref={transcriptEndRef} />
                </div>
              </div>
            </div>
          </div>
        )}
      </main>

      {/* Controls */}
      <div className="ra-controls-bar">
        <div className="ra-controls-container">
          <button className={`ra-control-btn ${isMicEnabled ? 'active' : ''}`} onClick={() => isMicEnabled ? stopMic() : startMic()} disabled={!isConnected} data-testid="mic-button">
            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>
          </button>
          {isConnected && (
            <button className="ra-control-btn ra-control-btn-end" onClick={endSession} data-testid="end-session-button">
              <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function u8ToB64(bytes) {
  let bin = '';
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}
