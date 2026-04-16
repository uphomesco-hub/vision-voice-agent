import { useState, useEffect, useRef, useCallback } from 'react';
import './RepairAssistant.css';

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const WS_BASE = process.env.REACT_APP_BACKEND_URL.replace('https://', 'wss://').replace('http://', 'ws://');

function log(category, ...args) {
  const ts = new Date().toISOString().slice(11, 23);
  console.log(`%c[${ts}] [${category}]`, 'color: #ffb4a9; font-weight: bold', ...args);
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

  useEffect(() => {
    log('INIT', 'Repair Assistant loading...');
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
      log('DATA', `Loaded ${p.length} personas, ${v.length} voices`);
    } catch (e) { log('ERROR', 'Failed to load data:', e); setStatus('Error loading options'); }
  };

  // ─── Audio Playback ────────────────────
  const playAudioChunk = useCallback((b64) => {
    try {
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      audioQueueRef.current.push(bytes);
      if (!isPlayingRef.current) drainQueue();
    } catch (e) { log('AUDIO', 'Decode error:', e); }
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
        sessionIdRef.current = msg.session_id;
        setSessionId(msg.session_id);
        log('SESSION', `Session ready: ${msg.session_id}`);
      } else if (msg.type === 'session.state') {
        log('SESSION', 'Received session state for resume:', JSON.stringify(msg.data).slice(0, 200));
        // Restore UI state from resumed session
        const d = msg.data;
        if (d.active_manual_id) setActiveManual({ id: d.active_manual_id, summary: `${d.active_device_model || ''} manual active` });
      } else if (msg.type === 'status') {
        log('STATUS', msg.message);
        setStatus(msg.message);
      } else if (msg.type === 'assistant.state') {
        log('STATE', `Assistant → ${msg.state}`);
        if (msg.state === 'listening') setVoiceState('listening');
        else if (msg.state === 'connecting') setVoiceState('thinking');
        else if (msg.state === 'speaking') setVoiceState('speaking');
      } else if (msg.type === 'audio') {
        setVoiceState('speaking');
        setStatus('Speaking...');
        playAudioChunk(msg.data);
      } else if (msg.type === 'transcription') {
        if (msg.role === 'user') log('SPEECH', `User: "${msg.text}"`);
        else log('SPEECH', `Assistant: "${msg.text}"`);
        setTranscript(prev => {
          const last = prev[prev.length - 1];
          if (last && last.role === msg.role && !last.final) {
            return [...prev.slice(0, -1), { ...last, text: last.text + msg.text }];
          }
          return [...prev, { role: msg.role, text: msg.text, final: false }];
        });
      } else if (msg.type === 'turn_complete') {
        log('TURN', 'Turn complete — listening for next input');
        setVoiceState('listening');
        setStatus('Listening...');
        setTranscript(prev => {
          if (!prev.length) return prev;
          return [...prev.slice(0, -1), { ...prev[prev.length - 1], final: true }];
        });
      } else if (msg.type === 'interrupted') {
        log('TURN', 'User interrupted assistant');
        audioQueueRef.current = [];
        setVoiceState('listening');
        setStatus('Listening...');
      } else if (msg.type === 'tool.status') {
        if (msg.status === 'running') {
          log('TOOL', `${msg.tool} running with args:`, msg.args);
          setToolActivity({ tool: msg.tool, status: 'running', detail: `Looking up: ${msg.args?.brand || ''} ${msg.args?.model || ''}`.trim() || 'Searching...' });
          setVoiceState('thinking');
          setStatus(`Running ${msg.tool}...`);
        } else if (msg.status === 'done') {
          log('TOOL', `${msg.tool} done:`, msg.result_summary);
          if (msg.tool === 'google_search') {
            setSearchQueries(msg.queries || []);
            setToolActivity({ tool: msg.tool, status: 'done', detail: `Searched: ${(msg.queries || []).join(', ')}` });
            log('SEARCH', 'Google Search queries:', msg.queries);
          } else {
            setToolActivity({ tool: msg.tool, status: 'done', detail: msg.result_summary || 'Complete' });
            if (msg.manual_id) {
              setActiveManual({ id: msg.manual_id, summary: msg.result_summary });
              log('MANUAL', `Active manual: ${msg.result_summary}`);
            }
            if (msg.warnings?.length) { setWarnings(msg.warnings); log('WARN', `${msg.warnings.length} warnings loaded`); }
            if (msg.steps?.length) { setSteps(msg.steps); log('STEPS', `${msg.steps.length} troubleshooting steps loaded`); }
          }
          setTimeout(() => setToolActivity(null), 4000);
        }
      } else if (msg.type === 'error') {
        log('ERROR', msg.message);
        if (msg.message.includes('disconnected')) {
          setStatus('Reconnecting...');
        } else {
          setStatus('Error: ' + msg.message);
        }
      } else if (msg.type === 'vision.status') {
        log('VISION', `Vision check: ${msg.status}`);
        setVisionStatus(msg.status === 'analyzing' ? 'Analyzing frame...' : null);
        if (msg.status === 'analyzing') setTimeout(() => setVisionStatus(null), 3000);
      } else if (msg.type === 'step.update') {
        log('STEP', `Advanced to step ${msg.step}`);
      }
    } catch (e) { log('ERROR', 'WS parse error:', e); }
  }, [playAudioChunk]);

  // ─── WebSocket Connect (with history resume) ─────────
  const connectWS = useCallback(() => {
    return new Promise((resolve, reject) => {
      log('WS', 'Connecting to WebSocket...');
      const ws = new WebSocket(`${WS_BASE}/api/ws/session`);
      wsRef.current = ws;
      const tmout = setTimeout(() => { log('WS', 'Connection timeout!'); reject(new Error('Connection timeout')); }, 10000);

      ws.onopen = () => {
        clearTimeout(tmout);
        log('WS', 'Connected');
        const cfg = configRef.current;
        const configMsg = {
          type: 'config',
          persona_id: cfg.persona_id,
          voice_id: cfg.voice_id,
        };
        // If we have a session ID from a previous connection, send it for history resume
        if (sessionIdRef.current && reconnectCountRef.current > 0) {
          configMsg.resume_session_id = sessionIdRef.current;
          log('WS', `Resuming session ${sessionIdRef.current} (reconnect #${reconnectCountRef.current})`);
        }
        ws.send(JSON.stringify(configMsg));
        if (heartbeatRef.current) clearInterval(heartbeatRef.current);
        heartbeatRef.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'heartbeat' }));
        }, 20000);
        resolve(ws);
      };
      ws.onmessage = handleMsg;
      ws.onclose = (ev) => {
        log('WS', `Closed: code=${ev.code} clean=${ev.wasClean}`);
        if (heartbeatRef.current) { clearInterval(heartbeatRef.current); heartbeatRef.current = null; }
        if (sessionActiveRef.current) {
          reconnectCountRef.current++;
          log('WS', `Auto-reconnecting in 1s (attempt #${reconnectCountRef.current})...`);
          setStatus('Reconnecting...');
          setVoiceState('thinking');
          reconnectRef.current = setTimeout(() => {
            if (!sessionActiveRef.current) return;
            connectWS().then(() => {
              log('WS', 'Reconnected successfully');
              setVoiceState('listening');
              setStatus('Listening...');
              setTranscript(p => [...p, { role: 'system', text: `(Reconnected #${reconnectCountRef.current})`, final: true }]);
            }).catch((err) => {
              log('ERROR', 'Reconnect failed:', err);
              setStatus('Reconnection failed — click End then Start');
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
      ws.onerror = (e) => { log('ERROR', 'WS error:', e); clearTimeout(tmout); reject(new Error('WS error')); };
    });
  }, [handleMsg]);

  // ─── Mic ───────────────────────────────
  const startMic = async () => {
    log('MIC', 'Requesting microphone...');
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    micStreamRef.current = stream;
    const ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
    audioCtxRef.current = ctx;
    const src = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(8192, 1, 1);
    let chunkCount = 0;
    proc.onaudioprocess = (e) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
      const input = e.inputBuffer.getChannelData(0);
      const pcm = new Int16Array(input.length);
      for (let i = 0; i < input.length; i++) pcm[i] = Math.max(-32768, Math.min(32767, Math.round(input[i] * 32767)));
      try { wsRef.current.send(JSON.stringify({ type: 'audio', data: u8ToB64(new Uint8Array(pcm.buffer)) })); chunkCount++; } catch {}
      if (chunkCount % 20 === 0) log('MIC', `Sent ${chunkCount} audio chunks`);
    };
    src.connect(proc); proc.connect(ctx.destination);
    processorRef.current = proc;
    setIsMicEnabled(true);
    log('MIC', 'Microphone started (PCM16 16kHz, 8192 buffer)');
  };
  const stopMic = () => {
    processorRef.current?.disconnect(); processorRef.current = null;
    audioCtxRef.current?.close(); audioCtxRef.current = null;
    micStreamRef.current?.getTracks().forEach(t => t.stop()); micStreamRef.current = null;
    setIsMicEnabled(false);
    log('MIC', 'Microphone stopped');
  };

  // ─── Camera ────────────────────────────
  const startCamera = async () => {
    log('CAM', 'Requesting camera...');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment', width: 640, height: 480 } });
      camStreamRef.current = stream;
      if (videoRef.current) { videoRef.current.srcObject = stream; videoRef.current.play(); }
      setCameraEnabled(true);
      frameCountRef.current = 0;
      frameIntervalRef.current = setInterval(() => captureFrame(), 2000);
      log('CAM', 'Camera started (640x480, frames every 2s)');
    } catch (e) {
      log('CAM', 'Camera access denied or unavailable:', e.message);
      setCameraEnabled(false);
    }
  };
  const captureFrame = () => {
    if (!videoRef.current || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    const canvas = document.createElement('canvas');
    canvas.width = 640; canvas.height = 480;
    canvas.getContext('2d').drawImage(videoRef.current, 0, 0, 640, 480);
    const b64 = canvas.toDataURL('image/jpeg', 0.6).split(',')[1];
    try {
      wsRef.current.send(JSON.stringify({ type: 'video', data: b64 }));
      frameCountRef.current++;
      if (frameCountRef.current % 5 === 0) log('CAM', `Sent ${frameCountRef.current} frames`);
    } catch {}
  };
  const stopCamera = () => {
    if (frameIntervalRef.current) { clearInterval(frameIntervalRef.current); frameIntervalRef.current = null; }
    camStreamRef.current?.getTracks().forEach(t => t.stop()); camStreamRef.current = null;
    setCameraEnabled(false);
    log('CAM', `Camera stopped (${frameCountRef.current} frames total)`);
  };

  const cleanup = () => {
    log('CLEANUP', 'Cleaning up session...');
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
      log('SESSION', 'Starting session...');
      setStatus('Connecting...');
      configRef.current = { persona_id: selectedPersona, voice_id: selectedVoice };
      sessionActiveRef.current = true;
      reconnectCountRef.current = 0;
      await connectWS();
      setIsConnected(true);
      await new Promise(r => setTimeout(r, 1500));
      await startMic();
      await startCamera();
      setVoiceState('listening');
      setStatus('Listening...');
      setTranscript([{ role: 'system', text: 'Session started — speak and show your device!', final: true }]);
      log('SESSION', 'Session fully started (mic + camera active)');
    } catch (e) {
      log('ERROR', 'Start session failed:', e);
      sessionActiveRef.current = false;
      let msg = e.message;
      if (msg.includes('Permission denied') || msg.includes('NotAllowed')) msg = 'Microphone/camera access denied.';
      setStatus('Error: ' + msg);
      stopMic(); stopCamera();
      wsRef.current?.close(); wsRef.current = null;
    }
  };

  const endSession = () => {
    log('SESSION', 'Ending session');
    sessionActiveRef.current = false;
    cleanup();
    setIsConnected(false); setIsMicEnabled(false); setVoiceState('idle');
    setStatus('Ready'); setTranscript([]); setSessionId(null);
    sessionIdRef.current = null; reconnectCountRef.current = 0;
    setActiveManual(null); setToolActivity(null); setWarnings([]); setSteps([]); setSearchQueries([]);
  };

  // ─── Render ────────────────────────────
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
          {sessionId && <span className="ra-session-id" data-testid="session-id">Session: {sessionId.slice(0, 8)}</span>}
          <span className="ra-mode-badge" data-testid="mode-badge">Gemini Live</span>
        </div>
      </header>

      <main className="ra-main-content">
        {!isConnected ? (
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
          <div className="ra-session-layout">
            {/* Left: Camera */}
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
                {visionStatus && <div className="ra-vision-status">{visionStatus}</div>}
              </div>
            </div>

            {/* Right: Panels */}
            <div className="ra-info-section">
              {/* Tool Activity */}
              {toolActivity && (
                <div className={`ra-panel ra-tool-panel ${toolActivity.status}`} data-testid="tool-panel">
                  <div className="ra-panel-title">
                    {toolActivity.tool === 'google_search' ? 'Google Search' : 'Tool Activity'}
                  </div>
                  <div className="ra-tool-detail">{toolActivity.detail}</div>
                  {toolActivity.status === 'running' && <div className="ra-tool-spinner"></div>}
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
                  {warnings.slice(0, 3).map((w, i) => <div key={i} className="ra-warning-item">{w}</div>)}
                  {warnings.length > 3 && <div className="ra-warning-more">+{warnings.length - 3} more</div>}
                </div>
              )}

              {/* Steps */}
              {steps.length > 0 && (
                <div className="ra-panel ra-steps-panel" data-testid="steps-panel">
                  <div className="ra-panel-title">Troubleshooting Steps</div>
                  {steps.map((s, i) => (
                    <div key={i} className="ra-step-item">
                      <span className="ra-step-num">{s.step || i + 1}</span>
                      <div className="ra-step-content">
                        <div className="ra-step-title">{s.title}</div>
                        {s.action && <div className="ra-step-action">{s.action}</div>}
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {/* Google Search */}
              {searchQueries.length > 0 && (
                <div className="ra-panel ra-search-panel" data-testid="search-panel">
                  <div className="ra-panel-title">Web Search</div>
                  {searchQueries.map((q, i) => <div key={i} className="ra-search-query">{q}</div>)}
                </div>
              )}

              {/* Transcript */}
              <div className="ra-panel ra-transcript-panel" data-testid="transcript-panel">
                <div className="ra-panel-header" onClick={() => setShowTranscript(!showTranscript)}>
                  <div className="ra-panel-title">Conversation</div>
                  <span className="ra-panel-toggle">{showTranscript ? 'Hide' : 'Show'}</span>
                </div>
                {showTranscript && (
                  <div className="ra-transcript-messages">
                    {transcript.map((t, i) => (
                      <div key={i} className={`ra-message ${t.role}`}>
                        <span className="ra-message-role">{t.role === 'user' ? 'YOU' : t.role === 'system' ? 'SYS' : 'AI'}</span>
                        <span className="ra-message-content">{t.text}</span>
                      </div>
                    ))}
                    <div ref={transcriptEndRef} />
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </main>

      {/* Controls */}
      <div className="ra-controls-bar">
        <div className="ra-controls-container">
          <button className={`ra-control-btn ${isMicEnabled ? 'active' : ''}`} onClick={() => { if (isMicEnabled) { stopMic(); setVoiceState('idle'); } else { startMic().then(() => setVoiceState('listening')); } }} disabled={!isConnected} data-testid="mic-button" title={isMicEnabled ? 'Mute' : 'Unmute'}>
            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>
          </button>
          {isConnected && (
            <button className="ra-control-btn ra-control-btn-end" onClick={endSession} data-testid="end-session-button" title="End Session">
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
