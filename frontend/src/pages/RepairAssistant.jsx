import { useCallback, useEffect, useRef, useState } from 'react';
import './RepairAssistant.css';

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || '';
const API = BACKEND_URL ? `${BACKEND_URL}/api` : '';
const WS_BASE = BACKEND_URL ? BACKEND_URL.replace('https://', 'wss://').replace('http://', 'ws://') : '';

function log(cat, ...args) {
  const ts = new Date().toISOString().slice(11, 23);
  console.log(`%c[${ts}] [${cat}]`, 'color: #ff6b5a; font-weight: bold', ...args);
}

function u8ToB64(bytes) {
  let binary = '';
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

export default function RepairAssistant() {
  const [isConnected, setIsConnected] = useState(false);
  const [isMicEnabled, setIsMicEnabled] = useState(false);
  const [voiceState, setVoiceState] = useState('idle');
  const [status, setStatus] = useState('Ready');
  const [transcript, setTranscript] = useState([]);
  const [sessionId, setSessionId] = useState(null);
  const [showTranscript, setShowTranscript] = useState(true);

  const wsRef = useRef(null);
  const audioCtxRef = useRef(null);
  const processorRef = useRef(null);
  const micStreamRef = useRef(null);
  const heartbeatRef = useRef(null);
  const reconnectRef = useRef(null);
  const sessionActiveRef = useRef(false);
  const sessionIdRef = useRef(null);
  const reconnectCountRef = useRef(0);
  const transcriptEndRef = useRef(null);
  const hasBackendConfig = Boolean(BACKEND_URL);

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [transcript]);

  const appendTranscript = useCallback((role, text, final = false) => {
    if (!text) return;
    setTranscript(prev => {
      const last = prev[prev.length - 1];
      if (last && last.role === role && !last.final && !final) {
        return [...prev.slice(0, -1), { ...last, text: `${last.text}${text}` }];
      }
      return [...prev, { role, text, final }];
    });
  }, []);

  const handleMsg = useCallback((event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === 'heartbeat') return;

      if (msg.type === 'session.ready') {
        if (!sessionIdRef.current) {
          sessionIdRef.current = msg.session_id;
          setSessionId(msg.session_id);
        }
        log('SESSION', `=== SESSION ID: ${sessionIdRef.current} ===`);
      } else if (msg.type === 'status') {
        setStatus(msg.message);
        log('STATUS', msg.message);
      } else if (msg.type === 'assistant.state') {
        setVoiceState(msg.state === 'connecting' ? 'thinking' : msg.state);
      } else if (msg.type === 'transcription') {
        appendTranscript(msg.role, msg.text, Boolean(msg.final));
      } else if (msg.type === 'turn_complete') {
        setVoiceState('listening');
        setStatus('Listening for a coding question...');
      } else if (msg.type === 'interrupted') {
        setVoiceState('listening');
        setStatus('Listening for the new question...');
      } else if (msg.type === 'recitation.ignored') {
        setVoiceState('listening');
        setStatus('Listening...');
        log('VOICE', 'Ignored likely answer recitation');
      } else if (msg.type === 'non_question.ignored') {
        setVoiceState('listening');
        setStatus('Listening for a coding question...');
        log('VOICE', 'Ignored non-question turn');
      } else if (msg.type === 'error') {
        setStatus(msg.message.includes('disconnected') ? 'Reconnecting...' : `Error: ${msg.message}`);
        log('ERROR', msg.message);
      }
    } catch (e) {
      log('ERROR', 'Message parse failed:', e);
    }
  }, [appendTranscript]);

  const connectWS = useCallback(() => new Promise((resolve, reject) => {
    const ws = new WebSocket(`${WS_BASE}/api/ws/session`);
    wsRef.current = ws;
    const timeout = setTimeout(() => reject(new Error('Timeout')), 10000);

    ws.onopen = () => {
      clearTimeout(timeout);
      ws.send(JSON.stringify({
        type: 'config',
        mode: 'coding_helper',
        output_mode: 'text',
        persona_id: 'coding-helper',
        voice_id: 'none',
        language: 'en-US',
        resume_session_id: sessionIdRef.current || undefined,
      }));
      if (heartbeatRef.current) clearInterval(heartbeatRef.current);
      heartbeatRef.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'heartbeat' }));
      }, 15000);
      resolve(ws);
    };

    ws.onmessage = handleMsg;
    ws.onclose = () => {
      if (heartbeatRef.current) {
        clearInterval(heartbeatRef.current);
        heartbeatRef.current = null;
      }
      if (sessionActiveRef.current) {
        reconnectCountRef.current += 1;
        setStatus('Reconnecting...');
        reconnectRef.current = setTimeout(() => {
          if (!sessionActiveRef.current) return;
          connectWS()
            .then(() => {
              setVoiceState('listening');
              setStatus('Listening for a coding question...');
            })
            .catch(() => {
              setStatus('Reconnect failed');
              setIsConnected(false);
              sessionActiveRef.current = false;
            });
        }, 300);
      } else {
        setIsConnected(false);
        setVoiceState('idle');
        setStatus('Ready');
      }
    };
    ws.onerror = () => {
      clearTimeout(timeout);
      reject(new Error('WebSocket error'));
    };
  }), [handleMsg]);

  const startMic = useCallback(async () => {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error('Microphone access is not available in this browser.');
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        sampleRate: 16000,
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    micStreamRef.current = stream;

    const ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
    audioCtxRef.current = ctx;
    const src = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(8192, 1, 1);
    const silentGain = ctx.createGain();
    silentGain.gain.value = 0;

    proc.onaudioprocess = (event) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
      const input = event.inputBuffer.getChannelData(0);
      const pcm = new Int16Array(input.length);
      for (let i = 0; i < input.length; i += 1) {
        pcm[i] = Math.max(-32768, Math.min(32767, Math.round(input[i] * 32767)));
      }
      try {
        wsRef.current.send(JSON.stringify({ type: 'audio', data: u8ToB64(new Uint8Array(pcm.buffer)) }));
      } catch {}
    };

    src.connect(proc);
    proc.connect(silentGain);
    silentGain.connect(ctx.destination);
    processorRef.current = proc;
    setIsMicEnabled(true);
    log('MIC', 'Active');
  }, []);

  const stopMic = useCallback(() => {
    processorRef.current?.disconnect();
    processorRef.current = null;
    audioCtxRef.current?.close();
    audioCtxRef.current = null;
    micStreamRef.current?.getTracks().forEach(track => track.stop());
    micStreamRef.current = null;
    setIsMicEnabled(false);
  }, []);

  const cleanup = useCallback(() => {
    if (heartbeatRef.current) clearInterval(heartbeatRef.current);
    if (reconnectRef.current) clearTimeout(reconnectRef.current);
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'end' }));
      wsRef.current.close();
    }
    wsRef.current = null;
    stopMic();
  }, [stopMic]);

  useEffect(() => () => {
    sessionActiveRef.current = false;
    cleanup();
  }, [cleanup]);

  const startSession = async () => {
    if (!hasBackendConfig) {
      setStatus('Backend URL missing');
      return;
    }
    try {
      setStatus('Connecting...');
      sessionActiveRef.current = true;
      reconnectCountRef.current = 0;
      await connectWS();
      setIsConnected(true);
      await startMic();
      setVoiceState('listening');
      setStatus('Listening for a coding question...');
      setTranscript([{ role: 'system', text: 'Session started. Ask a coding question out loud.', final: true }]);
    } catch (e) {
      sessionActiveRef.current = false;
      stopMic();
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
      }
      wsRef.current = null;
      setIsConnected(false);
      setSessionId(null);
      sessionIdRef.current = null;
      setStatus(`Error: ${e.message}`);
    }
  };

  const endSession = () => {
    sessionActiveRef.current = false;
    cleanup();
    setIsConnected(false);
    setIsMicEnabled(false);
    setVoiceState('idle');
    setStatus('Ready');
    setTranscript([]);
    setSessionId(null);
    sessionIdRef.current = null;
    reconnectCountRef.current = 0;
  };

  const toggleMic = async () => {
    if (isMicEnabled) {
      stopMic();
      setVoiceState('idle');
      setStatus('Mic off');
      return;
    }
    await startMic();
    setVoiceState('listening');
    setStatus('Listening for a coding question...');
  };

  if (!hasBackendConfig) {
    return (
      <div className="repair-assistant" data-testid="repair-assistant">
        <main className="ra-main-content ra-centered">
          <section className="ra-config-card">
            <h1>Frontend configuration missing</h1>
            <p>Set <code>REACT_APP_BACKEND_URL</code> and redeploy.</p>
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
        <div className="ra-header-center"><h1 className="ra-app-title">Zeno AI</h1></div>
        <div className="ra-header-right">
          <span className="ra-mode-badge">Coding Helper</span>
          {sessionId && <span className="ra-session-id" data-testid="session-id">{sessionId.slice(0, 8)}</span>}
        </div>
      </header>

      <main className="ra-main-content">
        {!isConnected ? (
          <div className="ra-setup-page ra-helper-setup">
            <section className="ra-helper-card">
              <div className="ra-helper-orb">
                <div className="ra-aura-ring"></div>
                <div className="ra-aura-ring delay-1"></div>
                <div className="ra-aura-ring delay-2"></div>
              </div>
              <h2>Zeno AI Coding Helper</h2>
              <p>Ask coding questions by voice. Answers appear in chat only.</p>
              <button className="ra-btn-init" onClick={startSession} data-testid="start-session-button">
                Start Session
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
              </button>
            </section>
          </div>
        ) : (
          <div className={`ra-session-layout ${!showTranscript ? 'no-transcript' : ''}`}>
            <section className="ra-helper-section">
              <div className="ra-helper-stage">
                <div className={`ra-voice-aura ${voiceState}`} data-testid="voice-aura">
                  <div className="ra-aura-ring"></div>
                  <div className="ra-aura-ring delay-1"></div>
                  <div className="ra-aura-ring delay-2"></div>
                </div>
                <div className="ra-voice-badge" data-testid="voice-state-badge">
                  {voiceState === 'listening' && 'Listening'}
                  {voiceState === 'thinking' && 'Thinking'}
                  {voiceState === 'idle' && 'Idle'}
                </div>
              </div>
              <div className="ra-controls-bar" data-testid="controls-bar">
                <button className={`ra-control-btn ra-control-btn-text ${showTranscript ? 'active' : ''}`} onClick={() => setShowTranscript(!showTranscript)} data-testid="transcript-toggle-button">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
                  <span className="ra-control-label">{showTranscript ? 'Hide' : 'Show'}</span>
                </button>
                <div className="ra-controls-center">
                  <button className={`ra-control-btn ${isMicEnabled ? 'active' : ''}`} onClick={toggleMic} disabled={!isConnected} data-testid="mic-button">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>
                  </button>
                  <button className="ra-control-btn ra-control-btn-end" onClick={endSession} data-testid="end-session-button">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                  </button>
                </div>
              </div>
            </section>

            {showTranscript && (
              <section className="ra-info-section">
                <div className="ra-panel ra-transcript-panel" data-testid="transcript-panel">
                  <div className="ra-panel-title">Conversation</div>
                  <div className="ra-transcript-messages">
                    {transcript.map((turn, index) => (
                      <div key={`${turn.role}-${index}`} className={`ra-message ${turn.role}`}>
                        <span className="ra-message-role">{turn.role === 'user' ? 'YOU' : turn.role === 'system' ? 'SYS' : 'AI'}</span>
                        <span className="ra-message-content">{turn.text}</span>
                      </div>
                    ))}
                    <div ref={transcriptEndRef} />
                  </div>
                </div>
              </section>
            )}
          </div>
        )}
      </main>
    </div>
  );
}
