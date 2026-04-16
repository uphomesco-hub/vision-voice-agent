import { useState, useEffect, useRef, useCallback } from 'react';
import './RepairAssistant.css';

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const WS_BASE = process.env.REACT_APP_BACKEND_URL.replace('https://', 'wss://').replace('http://', 'ws://');

function log(cat, ...args) {
  const ts = new Date().toISOString().slice(11, 23);
  console.log(`%c[${ts}] [${cat}]`, 'color: #ff6b5a; font-weight: bold', ...args);
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
  const [showTranscript, setShowTranscript] = useState(true);
  const [visionStatus, setVisionStatus] = useState(null);
  const [searchQueries, setSearchQueries] = useState([]);
  const [mobileCardIndex, setMobileCardIndex] = useState(0);

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
  const sessionIdRef = useRef(null);
  const transcriptEndRef = useRef(null);
  const frameCountRef = useRef(0);
  const reconnectCountRef = useRef(0);
  const touchStartRef = useRef(null);

  useEffect(() => { log('INIT', 'Loading...'); loadData(); return () => { sessionActiveRef.current = false; cleanup(); }; }, []);
  useEffect(() => { transcriptEndRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [transcript]);

  const loadData = async () => {
    try {
      const [p, v] = await Promise.all([fetch(`${API}/personas`).then(r => r.json()), fetch(`${API}/voices`).then(r => r.json())]);
      setPersonas(p); setVoices(v); log('DATA', `${p.length} personas, ${v.length} voices`);
    } catch (e) { setStatus('Error loading'); }
  };

  // ─── Audio Playback ──────────────
  const playAudioChunk = useCallback((b64) => {
    try {
      const bin = atob(b64); const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      audioQueueRef.current.push(bytes); if (!isPlayingRef.current) drainQueue();
    } catch (e) { log('AUDIO', 'Decode err:', e); }
  }, []);
  const drainQueue = async () => {
    if (isPlayingRef.current) return; isPlayingRef.current = true;
    if (!playbackCtxRef.current) playbackCtxRef.current = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
    const ctx = playbackCtxRef.current;
    while (audioQueueRef.current.length > 0) {
      const bytes = audioQueueRef.current.shift();
      const samples = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2);
      const floats = new Float32Array(samples.length); for (let i = 0; i < samples.length; i++) floats[i] = samples[i] / 32768;
      const buf = ctx.createBuffer(1, floats.length, 24000); buf.getChannelData(0).set(floats);
      const src = ctx.createBufferSource(); src.buffer = buf; src.connect(ctx.destination);
      await new Promise(r => { src.onended = r; src.start(); });
    }
    isPlayingRef.current = false;
  };

  // ─── WS Message Handler ──────────
  const handleMsg = useCallback((event) => {
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
          if (/[^\u0000-\u024F\u1E00-\u1EFF\u2000-\u206F\u2070-\u209F\u20A0-\u20CF\u2100-\u214F.,!?;:'"()\-\s\d]/.test(text)) { log('SPEECH', `User (non-English filtered): "${text}"`); return; }
          log('SPEECH', `User: "${text}"`);
        } else { log('SPEECH', `AI: "${text}"`); }
        setTranscript(prev => {
          const last = prev[prev.length - 1];
          if (last && last.role === msg.role && !last.final) return [...prev.slice(0, -1), { ...last, text: last.text + text }];
          return [...prev, { role: msg.role, text, final: false }];
        });
      } else if (msg.type === 'turn_complete') {
        log('TURN', 'Complete'); setVoiceState('listening'); setStatus('Listening...');
        setTranscript(p => { if (!p.length) return p; return [...p.slice(0, -1), { ...p[p.length - 1], final: true }]; });
      } else if (msg.type === 'interrupted') { audioQueueRef.current = []; setVoiceState('listening'); setStatus('Listening...');
      } else if (msg.type === 'tool.status') {
        if (msg.status === 'running') { log('TOOL', `${msg.tool} running`, msg.args); setToolActivity({ tool: msg.tool, status: 'running', detail: `Looking up: ${msg.args?.brand || ''} ${msg.args?.model || ''}`.trim() || 'Searching...' }); setVoiceState('thinking'); setStatus(`Running ${msg.tool}...`);
        } else if (msg.status === 'done') { log('TOOL', `${msg.tool} done:`, msg.result_summary);
          if (msg.tool === 'google_search') { setSearchQueries(msg.queries || []); setToolActivity({ tool: msg.tool, status: 'done', detail: `Searched: ${(msg.queries || []).join(', ')}` }); log('SEARCH', msg.queries);
          } else { if (msg.manual_id) { setToolActivity({ tool: msg.tool, status: 'done', detail: msg.result_summary }); setActiveManual({ id: msg.manual_id, summary: msg.result_summary }); log('MANUAL', msg.result_summary);
          } else { setToolActivity({ tool: msg.tool, status: 'done', detail: 'No manual in database — using AI knowledge' }); setActiveManual(null); log('MANUAL', 'No manual found'); }
          if (msg.warnings?.length) { setWarnings(msg.warnings); log('WARN', `${msg.warnings.length} warnings`); }
          if (msg.steps?.length) { setSteps(msg.steps); log('STEPS', `${msg.steps.length} steps`); } }
          setTimeout(() => setToolActivity(null), 4000);
        }
      } else if (msg.type === 'error') { log('ERROR', msg.message); if (msg.message.includes('disconnected')) setStatus('Reconnecting...'); else setStatus('Error: ' + msg.message);
      } else if (msg.type === 'vision.status') { log('VISION', msg.status); setVisionStatus(msg.status === 'analyzing' ? 'Analyzing...' : null); if (msg.status === 'analyzing') setTimeout(() => setVisionStatus(null), 3000);
      } else if (msg.type === 'step.update') { log('STEP', `→ step ${msg.step}`); }
    } catch (e) { log('ERROR', 'Parse:', e); }
  }, [playAudioChunk]);

  // ─── WebSocket ───────────────────
  const connectWS = useCallback(() => {
    return new Promise((resolve, reject) => {
      log('WS', 'Connecting...'); const ws = new WebSocket(`${WS_BASE}/api/ws/session`); wsRef.current = ws;
      const t = setTimeout(() => reject(new Error('Timeout')), 10000);
      ws.onopen = () => { clearTimeout(t); log('WS', 'Open'); const cfg = configRef.current;
        const m = { type: 'config', persona_id: cfg.persona_id, voice_id: cfg.voice_id };
        if (sessionIdRef.current) { m.resume_session_id = sessionIdRef.current; log('WS', `Resume ${sessionIdRef.current}`); }
        ws.send(JSON.stringify(m));
        if (heartbeatRef.current) clearInterval(heartbeatRef.current);
        heartbeatRef.current = setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'heartbeat' })); }, 15000);
        resolve(ws);
      };
      ws.onmessage = handleMsg;
      ws.onclose = () => { if (heartbeatRef.current) { clearInterval(heartbeatRef.current); heartbeatRef.current = null; }
        if (sessionActiveRef.current) { reconnectCountRef.current++; log('WS', `Reconnecting #${reconnectCountRef.current}`); setStatus('Reconnecting...'); setVoiceState('thinking');
          reconnectRef.current = setTimeout(() => { if (!sessionActiveRef.current) return;
            connectWS().then(() => { setVoiceState('listening'); setStatus('Listening...'); setTranscript(p => [...p, { role: 'system', text: `(Reconnected #${reconnectCountRef.current})`, final: true }]); }).catch(() => { setStatus('Reconnect failed'); setIsConnected(false); sessionActiveRef.current = false; });
          }, 1000);
        } else { setIsConnected(false); setVoiceState('idle'); setStatus('Ready'); }
      };
      ws.onerror = () => { clearTimeout(t); reject(new Error('WS error')); };
    });
  }, [handleMsg]);

  // ─── Media ───────────────────────
  const startMic = async () => {
    log('MIC', 'Starting...'); const stream = await navigator.mediaDevices.getUserMedia({ audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    micStreamRef.current = stream; const ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 }); audioCtxRef.current = ctx;
    const src = ctx.createMediaStreamSource(stream); const proc = ctx.createScriptProcessor(8192, 1, 1); let c = 0;
    proc.onaudioprocess = (e) => { if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
      const input = e.inputBuffer.getChannelData(0); const pcm = new Int16Array(input.length);
      for (let i = 0; i < input.length; i++) pcm[i] = Math.max(-32768, Math.min(32767, Math.round(input[i] * 32767)));
      try { wsRef.current.send(JSON.stringify({ type: 'audio', data: u8ToB64(new Uint8Array(pcm.buffer)) })); c++; } catch {}
      if (c % 20 === 0) log('MIC', `${c} chunks`);
    };
    src.connect(proc); proc.connect(ctx.destination); processorRef.current = proc; setIsMicEnabled(true); log('MIC', 'Active');
  };
  const stopMic = () => { processorRef.current?.disconnect(); processorRef.current = null; audioCtxRef.current?.close(); audioCtxRef.current = null; micStreamRef.current?.getTracks().forEach(t => t.stop()); micStreamRef.current = null; setIsMicEnabled(false); };
  const startCamera = async () => {
    try { const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment', width: 640, height: 480 } });
      camStreamRef.current = stream; if (videoRef.current) { videoRef.current.srcObject = stream; videoRef.current.play(); } setCameraEnabled(true);
      frameCountRef.current = 0; frameIntervalRef.current = setInterval(() => captureFrame(), 2000); log('CAM', 'Active');
    } catch (e) { log('CAM', 'Denied:', e.message); }
  };
  const captureFrame = () => { if (!videoRef.current || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    const c = document.createElement('canvas'); c.width = 640; c.height = 480; c.getContext('2d').drawImage(videoRef.current, 0, 0, 640, 480);
    try { wsRef.current.send(JSON.stringify({ type: 'video', data: c.toDataURL('image/jpeg', 0.6).split(',')[1] })); frameCountRef.current++; if (frameCountRef.current % 5 === 0) log('CAM', `${frameCountRef.current} frames`); } catch {}
  };
  const stopCamera = () => { if (frameIntervalRef.current) clearInterval(frameIntervalRef.current); frameIntervalRef.current = null; camStreamRef.current?.getTracks().forEach(t => t.stop()); camStreamRef.current = null; setCameraEnabled(false); };
  const cleanup = () => { if (heartbeatRef.current) clearInterval(heartbeatRef.current); if (reconnectRef.current) clearTimeout(reconnectRef.current);
    if (wsRef.current?.readyState === WebSocket.OPEN) { wsRef.current.send(JSON.stringify({ type: 'end' })); wsRef.current.close(); } wsRef.current = null; stopMic(); stopCamera(); audioQueueRef.current = []; playbackCtxRef.current?.close(); playbackCtxRef.current = null; };

  const toggleCamera = async () => {
    if (cameraEnabled) { stopCamera(); log('CAM', 'Toggled OFF'); }
    else { await startCamera(); log('CAM', 'Toggled ON'); }
  };

  const startSession = async () => {
    try { log('SESSION', 'Starting...'); setStatus('Connecting...'); configRef.current = { persona_id: selectedPersona, voice_id: selectedVoice }; sessionActiveRef.current = true; reconnectCountRef.current = 0;
      await connectWS(); setIsConnected(true); await new Promise(r => setTimeout(r, 1500)); await startMic(); await startCamera(); setVoiceState('listening'); setStatus('Listening...'); setTranscript([{ role: 'system', text: 'Session started — speak and show your device!', final: true }]); log('SESSION', 'Active');
    } catch (e) { sessionActiveRef.current = false; setStatus('Error: ' + e.message); stopMic(); stopCamera(); wsRef.current?.close(); wsRef.current = null; }
  };
  const endSession = () => { log('SESSION', 'Ending'); sessionActiveRef.current = false; cleanup();
    setIsConnected(false); setIsMicEnabled(false); setVoiceState('idle'); setStatus('Ready'); setTranscript([]); setSessionId(null); sessionIdRef.current = null; reconnectCountRef.current = 0;
    setActiveManual(null); setToolActivity(null); setWarnings([]); setSteps([]); setSearchQueries([]);
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
          <div className="ra-session-layout">
            <div className="ra-camera-section">
              <div className="ra-camera-container" data-testid="camera-container">
                <video ref={videoRef} className={`ra-camera-feed ${!cameraEnabled ? 'hidden' : ''}`} autoPlay playsInline muted data-testid="camera-feed" />
                {!cameraEnabled && <div className="ra-camera-placeholder">Camera is off</div>}
                <div className={`ra-voice-aura ${voiceState}`} data-testid="voice-aura">
                  <div className="ra-aura-ring"></div><div className="ra-aura-ring delay-1"></div><div className="ra-aura-ring delay-2"></div>
                </div>
                <div className="ra-voice-badge" data-testid="voice-state-badge">
                  {voiceState === 'listening' && 'Listening...'}{voiceState === 'speaking' && 'Speaking...'}{voiceState === 'thinking' && 'Thinking...'}{voiceState === 'idle' && 'Idle'}
                </div>
                {visionStatus && <div className="ra-vision-status">{visionStatus}</div>}
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
                </div>
              </div>
            </div>
            {showTranscript && <div className="ra-info-section">
              {toolActivity && <div className={`ra-panel ra-tool-panel ${toolActivity.status}`} data-testid="tool-panel"><div className="ra-panel-title">{toolActivity.tool === 'google_search' ? 'Google Search' : 'Tool Activity'}</div><div className="ra-tool-detail">{toolActivity.detail}</div>{toolActivity.status === 'running' && <div className="ra-tool-spinner"></div>}</div>}
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
function u8ToB64(bytes) { let b = ''; for (let i = 0; i < bytes.length; i++) b += String.fromCharCode(bytes[i]); return btoa(b); }
