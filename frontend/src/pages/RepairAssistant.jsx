import { useCallback, useEffect, useRef, useState } from 'react';
import './RepairAssistant.css';

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || '';
const API = BACKEND_URL ? `${BACKEND_URL}/api` : '';
const WS_BASE = BACKEND_URL ? BACKEND_URL.replace('https://', 'wss://').replace('http://', 'ws://') : '';
const AUDIO_SAMPLE_RATE = 24000;

function log(cat, ...args) {
  const ts = new Date().toISOString().slice(11, 23);
  console.log(`%c[${ts}] [${cat}]`, 'color: #ff6b5a; font-weight: bold', ...args);
}

export default function RepairAssistant() {
  const [isConnected, setIsConnected] = useState(false);
  const [isMicEnabled, setIsMicEnabled] = useState(false);
  const [voiceState, setVoiceState] = useState('idle');
  const [status, setStatus] = useState('Ready');
  const [transcript, setTranscript] = useState([]);
  const [sessionId, setSessionId] = useState(null);
  const [isStarting, setIsStarting] = useState(false);

  const wsRef = useRef(null);
  const audioCtxRef = useRef(null);
  const processorRef = useRef(null);
  const micStreamRef = useRef(null);
  const isStartingRef = useRef(false);
  const micStartingRef = useRef(false);
  const heartbeatRef = useRef(null);
  const reconnectRef = useRef(null);
  const sessionActiveRef = useRef(false);
  const sessionIdRef = useRef(null);
  const reconnectCountRef = useRef(0);
  const hasBackendConfig = Boolean(BACKEND_URL);

  const appendTranscript = useCallback((role, text, final = false, replace = false) => {
    setTranscript(prev => {
      const last = prev[prev.length - 1];
      if (last && last.role === role && !last.final) {
        const nextText = replace ? (text || last.text) : `${last.text}${text || ''}`;
        return [...prev.slice(0, -1), { ...last, text: nextText, final }];
      }
      if (!text) return prev;
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
        appendTranscript(msg.role, msg.text, Boolean(msg.final), Boolean(msg.replace));
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
    ws.binaryType = 'arraybuffer';
    wsRef.current = ws;
    const timeout = setTimeout(() => {
      ws.onopen = null;
      ws.onerror = null;
      ws.onclose = null;
      ws.close();
      reject(new Error('Timeout'));
    }, 10000);

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
    if (micStartingRef.current) return;
    if (micStreamRef.current || processorRef.current) {
      setIsMicEnabled(true);
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error('Microphone access is not available in this browser.');
    }
    micStartingRef.current = true;
    let stream = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          sampleRate: AUDIO_SAMPLE_RATE,
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      micStreamRef.current = stream;

      const ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: AUDIO_SAMPLE_RATE });
      audioCtxRef.current = ctx;
      await ctx.audioWorklet.addModule(new URL('pcm-worklet.js', window.location.href).toString());
      const src = ctx.createMediaStreamSource(stream);
      const proc = new AudioWorkletNode(ctx, 'pcm-capture-processor', {
        processorOptions: {
          chunkSize: 480,
          vadThreshold: 0.012,
          silenceMs: 360,
          minSpeechMs: 160,
          sampleRate: ctx.sampleRate || AUDIO_SAMPLE_RATE,
        },
      });
      const silentGain = ctx.createGain();
      silentGain.gain.value = 0;

      proc.port.onmessage = (event) => {
        const { type, pcm } = event.data || {};
        const ws = wsRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        try {
          if (type === 'audio' && pcm) {
            ws.send(pcm);
          } else if (type === 'speech_end') {
            ws.send(JSON.stringify({ type: 'audio.commit' }));
          }
        } catch {}
      };

      src.connect(proc);
      proc.connect(silentGain);
      silentGain.connect(ctx.destination);
      processorRef.current = proc;
      setIsMicEnabled(true);
      log('MIC', 'Active');
    } catch (error) {
      stream?.getTracks().forEach(track => track.stop());
      micStreamRef.current = null;
      throw error;
    } finally {
      micStartingRef.current = false;
    }
  }, []);

  const stopMic = useCallback(() => {
    micStartingRef.current = false;
    try {
      processorRef.current?.port?.postMessage({ type: 'stop' });
      processorRef.current?.port?.close?.();
    } catch {}
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
    if (isStartingRef.current || isConnected) return;
    isStartingRef.current = true;
    setIsStarting(true);
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
    } finally {
      isStartingRef.current = false;
      setIsStarting(false);
    }
  };

  const endSession = () => {
    isStartingRef.current = false;
    setIsStarting(false);
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
    if (micStartingRef.current) return;
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
              <button className="ra-btn-init" onClick={startSession} disabled={isStarting} data-testid="start-session-button">
                {isStarting ? 'Connecting...' : 'Start Session'}
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
              </button>
            </section>
          </div>
        ) : (
          <div className="ra-session-layout" data-testid="session-layout">
            <section className="ra-chat-section" data-testid="transcript-panel">
              <div className="ra-transcript-messages">
                {transcript.map((turn, index) => (
                  <div key={`${turn.role}-${index}`} className={`ra-message ${turn.role}`}>
                    <span className="ra-message-role">{turn.role === 'user' ? 'YOU' : turn.role === 'system' ? 'SYS' : 'AI'}</span>
                    <span className="ra-message-content">{turn.text}</span>
                  </div>
                ))}
              </div>
            </section>
            <div className="ra-controls-bar" data-testid="controls-bar">
              <button
                className={`ra-control-btn ${isMicEnabled ? 'active' : ''}`}
                onClick={toggleMic}
                disabled={!isConnected}
                data-testid="mic-button"
                aria-label={isMicEnabled ? 'Mute microphone' : 'Unmute microphone'}
              >
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
                  <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
                  <line x1="12" x2="12" y1="19" y2="22"/>
                </svg>
              </button>
              <button
                className="ra-control-btn ra-control-btn-cancel"
                onClick={endSession}
                data-testid="cancel-button"
                aria-label="Cancel session"
              >
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <line x1="18" y1="6" x2="6" y2="18"/>
                  <line x1="6" y1="6" x2="18" y2="18"/>
                </svg>
              </button>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
