import { useState, useEffect, useRef, useCallback } from 'react';
import './RepairAssistant.css';

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const WS_BASE = process.env.REACT_APP_BACKEND_URL
  .replace('https://', 'wss://')
  .replace('http://', 'ws://');

export default function RepairAssistant() {
  const [personas, setPersonas] = useState([]);
  const [voices, setVoices] = useState([]);
  const [selectedPersona, setSelectedPersona] = useState('calm-expert');
  const [selectedVoice, setSelectedVoice] = useState('Puck');
  const [isConnected, setIsConnected] = useState(false);
  const [isMicEnabled, setIsMicEnabled] = useState(false);
  const [voiceState, setVoiceState] = useState('idle'); // idle, listening, thinking, speaking
  const [status, setStatus] = useState('Ready');
  const [transcript, setTranscript] = useState([]);

  const wsRef = useRef(null);
  const audioContextRef = useRef(null);
  const workletNodeRef = useRef(null);
  const streamRef = useRef(null);
  const audioQueueRef = useRef([]);
  const isPlayingRef = useRef(false);
  const playbackCtxRef = useRef(null);

  useEffect(() => {
    loadData();
    return () => { endSession(); };
  }, []);

  const loadData = async () => {
    try {
      const [pRes, vRes] = await Promise.all([
        fetch(`${API}/personas`).then(r => r.json()),
        fetch(`${API}/voices`).then(r => r.json()),
      ]);
      setPersonas(pRes);
      setVoices(vRes);
    } catch (e) {
      console.error('Error loading data:', e);
      setStatus('Error loading options');
    }
  };

  // --- Audio Playback ---
  const playAudioChunk = useCallback(async (base64Data) => {
    try {
      const binaryStr = atob(base64Data);
      const bytes = new Uint8Array(binaryStr.length);
      for (let i = 0; i < binaryStr.length; i++) bytes[i] = binaryStr.charCodeAt(i);
      audioQueueRef.current.push(bytes);
      if (!isPlayingRef.current) drainAudioQueue();
    } catch (e) {
      console.error('Audio decode error:', e);
    }
  }, []);

  const drainAudioQueue = async () => {
    if (isPlayingRef.current) return;
    isPlayingRef.current = true;

    if (!playbackCtxRef.current) {
      playbackCtxRef.current = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
    }
    const ctx = playbackCtxRef.current;

    while (audioQueueRef.current.length > 0) {
      const bytes = audioQueueRef.current.shift();
      const samples = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2);
      const floats = new Float32Array(samples.length);
      for (let i = 0; i < samples.length; i++) floats[i] = samples[i] / 32768;

      const buf = ctx.createBuffer(1, floats.length, 24000);
      buf.getChannelData(0).set(floats);
      const source = ctx.createBufferSource();
      source.buffer = buf;
      source.connect(ctx.destination);

      await new Promise(resolve => {
        source.onended = resolve;
        source.start();
      });
    }
    isPlayingRef.current = false;
  };

  // --- WebSocket Handlers ---
  const handleWsMessage = useCallback((event) => {
    try {
      const msg = JSON.parse(event.data);

      if (msg.type === 'status') {
        setStatus(msg.message);
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
        // Mark last transcript as final
        setTranscript(prev => {
          if (prev.length === 0) return prev;
          const last = prev[prev.length - 1];
          return [...prev.slice(0, -1), { ...last, final: true }];
        });
      } else if (msg.type === 'interrupted') {
        audioQueueRef.current = [];
        setVoiceState('listening');
        setStatus('Listening...');
      } else if (msg.type === 'error') {
        console.error('Server error:', msg.message);
        setStatus('Error: ' + msg.message);
      }
    } catch (e) {
      console.error('WS message parse error:', e);
    }
  }, [playAudioChunk]);

  // --- Mic Capture via AudioWorklet ---
  const startMicCapture = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true } });
      streamRef.current = stream;

      const ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      audioContextRef.current = ctx;

      // Use ScriptProcessor as fallback (widely supported)
      const source = ctx.createMediaStreamSource(stream);
      const processor = ctx.createScriptProcessor(4096, 1, 1);
      
      processor.onaudioprocess = (e) => {
        if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
        const input = e.inputBuffer.getChannelData(0);
        const pcm16 = new Int16Array(input.length);
        for (let i = 0; i < input.length; i++) {
          pcm16[i] = Math.max(-32768, Math.min(32767, Math.round(input[i] * 32767)));
        }
        const b64 = uint8ArrayToBase64(new Uint8Array(pcm16.buffer));
        wsRef.current.send(JSON.stringify({ type: 'audio', data: b64 }));
      };

      source.connect(processor);
      processor.connect(ctx.destination);
      workletNodeRef.current = processor;

      setIsMicEnabled(true);
    } catch (e) {
      console.error('Mic capture error:', e);
      throw e;
    }
  };

  const stopMicCapture = () => {
    if (workletNodeRef.current) { workletNodeRef.current.disconnect(); workletNodeRef.current = null; }
    if (audioContextRef.current) { audioContextRef.current.close(); audioContextRef.current = null; }
    if (streamRef.current) { streamRef.current.getTracks().forEach(t => t.stop()); streamRef.current = null; }
    setIsMicEnabled(false);
  };

  // --- Session Control ---
  const startSession = async () => {
    try {
      setStatus('Requesting microphone...');
      
      // Open WebSocket
      setStatus('Connecting...');
      const ws = new WebSocket(`${WS_BASE}/api/ws/session`);
      wsRef.current = ws;

      await new Promise((resolve, reject) => {
        ws.onopen = resolve;
        ws.onerror = () => reject(new Error('WebSocket connection failed'));
        setTimeout(() => reject(new Error('Connection timeout')), 10000);
      });

      ws.onmessage = handleWsMessage;
      ws.onclose = () => {
        setIsConnected(false);
        setVoiceState('idle');
        setStatus('Disconnected');
        stopMicCapture();
      };

      // Send config
      ws.send(JSON.stringify({
        type: 'config',
        persona_id: selectedPersona,
        voice_id: selectedVoice,
      }));

      setIsConnected(true);
      setStatus('Connecting to Gemini Live...');

      // Wait briefly for Gemini connection, then start mic
      await new Promise(r => setTimeout(r, 1500));
      await startMicCapture();

      setVoiceState('listening');
      setStatus('Listening...');
      setTranscript([{ role: 'system', text: 'Session started — speak now!', final: true }]);

    } catch (e) {
      console.error('Start session error:', e);
      let msg = e.message;
      if (msg.includes('Permission denied') || msg.includes('NotAllowedError')) {
        msg = 'Microphone access denied. Please allow microphone permissions.';
      }
      setStatus('Error: ' + msg);
      stopMicCapture();
      if (wsRef.current) { wsRef.current.close(); wsRef.current = null; }
    }
  };

  const endSession = () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'end' }));
      wsRef.current.close();
    }
    wsRef.current = null;
    stopMicCapture();
    audioQueueRef.current = [];
    if (playbackCtxRef.current) { playbackCtxRef.current.close(); playbackCtxRef.current = null; }
    setIsConnected(false);
    setIsMicEnabled(false);
    setVoiceState('idle');
    setStatus('Ready');
  };

  const toggleMicrophone = () => {
    if (isMicEnabled) {
      stopMicCapture();
      setVoiceState('idle');
      setStatus('Microphone muted');
    } else {
      startMicCapture().then(() => {
        setVoiceState('listening');
        setStatus('Listening...');
      });
    }
  };

  return (
    <div className="repair-assistant">
      <header className="ra-header">
        <div className="ra-header-left">
          <div className="ra-live-indicator">
            <span className={`ra-live-dot ${isConnected ? 'connected' : ''}`}></span>
            <span className="ra-live-text" data-testid="status-text">{status}</span>
          </div>
        </div>
        <div className="ra-header-center">
          <h1 className="ra-app-title">Repair Assistant</h1>
        </div>
        <div className="ra-header-right">
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
                <h2 className="ra-setup-title">Voice Troubleshooting</h2>
                <p className="ra-setup-subtitle">Talk naturally with your AI repair assistant powered by Gemini Live</p>
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
          <div className={`ra-voice-panel ${voiceState}`}>
            <div className="ra-voice-status-area">
              <div className="ra-voice-visualizer" data-testid="voice-visualizer">
                <div className="ra-visualizer-wave">
                  {[0, 1, 2, 3, 4].map(i => <div key={i} className="ra-wave-bar"></div>)}
                </div>
              </div>
              <div className="ra-voice-status-text" data-testid="voice-status-text">{status}</div>
            </div>

            <div className="ra-transcript-container">
              <div className="ra-transcript-header">
                <h3 className="ra-transcript-title">Conversation</h3>
              </div>
              <div className="ra-transcript-messages" data-testid="transcript-messages">
                {transcript.map((turn, i) => (
                  <div key={i} className={`ra-message ${turn.role}`}>
                    <div className="ra-message-role">{turn.role === 'user' ? 'YOU' : turn.role === 'system' ? 'SYSTEM' : 'ASSISTANT'}</div>
                    <div className="ra-message-content">{turn.text}</div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </main>

      <div className="ra-controls-bar">
        <div className="ra-controls-container">
          <button className={`ra-control-btn ${isMicEnabled ? 'active' : ''}`} onClick={toggleMicrophone} disabled={!isConnected} data-testid="mic-button">
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

function uint8ArrayToBase64(bytes) {
  let binary = '';
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}
