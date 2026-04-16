import { useState, useEffect, useRef } from 'react';
import { Room } from 'livekit-client';
import axios from 'axios';
import './RepairAssistant.css';

// Use full backend URL for production
const API = process.env.REACT_APP_BACKEND_URL ? `${process.env.REACT_APP_BACKEND_URL}/api` : '/api';

export default function RepairAssistant() {
  const [personas, setPersonas] = useState([]);
  const [voices, setVoices] = useState([]);
  const [selectedPersona, setSelectedPersona] = useState('calm-expert');
  const [selectedVoice, setSelectedVoice] = useState('puck');
  const [sessionId, setSessionId] = useState(null);
  const [status, setStatus] = useState('Ready');
  const [isConnected, setIsConnected] = useState(false);
  const [isMicEnabled, setIsMicEnabled] = useState(false);
  const [transcript, setTranscript] = useState([]);
  const [showTranscript, setShowTranscript] = useState(true);
  const [voiceState, setVoiceState] = useState('idle');
  
  const roomRef = useRef(null);
  const pollIntervalRef = useRef(null);

  useEffect(() => {
    loadData();
    return () => {
      if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
    };
  }, []);

  const loadData = async () => {
    try {
      const [personasRes, voicesRes] = await Promise.all([
        axios.get(`${API}/personas`),
        axios.get(`${API}/voices`)
      ]);
      setPersonas(personasRes.data);
      setVoices(voicesRes.data);
    } catch (error) {
      console.error('Error loading data:', error);
      setStatus('Error loading options');
    }
  };

  const startSession = async () => {
    try {
      setStatus('Creating session...');
      
      // Create session
      const sessionRes = await axios.post(`${API}/sessions`, {
        persona_id: selectedPersona,
        voice_id: selectedVoice
      });
      const session = sessionRes.data;
      setSessionId(session.id);
      
      // Get token
      setStatus('Getting token...');
      const tokenRes = await axios.post(`${API}/sessions/${session.id}/token`);
      const { token, livekit_url } = tokenRes.data;
      
      // Request microphone
      setStatus('Requesting microphone...');
      await navigator.mediaDevices.getUserMedia({ audio: true });
      
      // Connect to LiveKit
      setStatus('Connecting...');
      const room = new Room();
      roomRef.current = room;
      
      // Setup event handlers
      room.on('trackSubscribed', (track) => {
        if (track.kind === 'audio') {
          const audioElement = track.attach();
          document.body.appendChild(audioElement);
          setVoiceState('thinking');
          setStatus('Agent speaking...');
        }
      });
      
      room.on('trackUnsubscribed', (track) => {
        track.detach();
        setVoiceState('listening');
        setStatus('Listening...');
      });
      
      room.on('disconnected', () => {
        setIsConnected(false);
        setStatus('Disconnected');
      });
      
      // Connect
      await room.connect(livekit_url, token);
      await room.localParticipant.setMicrophoneEnabled(true);
      
      setIsConnected(true);
      setIsMicEnabled(true);
      setVoiceState('listening');
      setStatus('Listening...');
      
      // Start polling transcript
      startTranscriptPolling(session.id);
      
      addTranscript('system', 'Session started - speak now!');
      
    } catch (error) {
      console.error('Error starting session:', error);
      setStatus('Error: ' + error.message);
    }
  };

  const endSession = async () => {
    try {
      if (roomRef.current) {
        await roomRef.current.disconnect();
        roomRef.current = null;
      }
      
      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current);
      }
      
      if (sessionId) {
        await axios.post(`${API}/sessions/${sessionId}/end`);
      }
      
      setIsConnected(false);
      setIsMicEnabled(false);
      setVoiceState('idle');
      setStatus('Ready');
      setSessionId(null);
      
    } catch (error) {
      console.error('Error ending session:', error);
    }
  };

  const toggleMicrophone = async () => {
    if (!roomRef.current) return;
    
    try {
      const newState = !isMicEnabled;
      await roomRef.current.localParticipant.setMicrophoneEnabled(newState);
      setIsMicEnabled(newState);
      setVoiceState(newState ? 'listening' : 'idle');
      setStatus(newState ? 'Listening...' : 'Microphone muted');
    } catch (error) {
      console.error('Error toggling mic:', error);
    }
  };

  const startTranscriptPolling = (sid) => {
    pollIntervalRef.current = setInterval(async () => {
      try {
        const res = await axios.get(`${API}/sessions/${sid}/state`);
        if (res.data.turns && res.data.turns.length > transcript.length) {
          setTranscript(res.data.turns);
        }
      } catch (error) {
        console.error('Error polling transcript:', error);
      }
    }, 2000);
  };

  const addTranscript = (role, content) => {
    setTranscript(prev => [...prev, { role, content, timestamp: new Date().toISOString() }]);
  };

  return (
    <div className="repair-assistant">
      {/* Header */}
      <header className="ra-header">
        <div className="ra-header-left">
          <div className="ra-live-indicator">
            <span className="ra-live-dot"></span>
            <span className="ra-live-text" data-testid="status-text">{status}</span>
          </div>
        </div>
        <div className="ra-header-center">
          <h1 className="ra-app-title">Repair Assistant</h1>
        </div>
        <div className="ra-header-right">
          <button className="ra-icon-btn" data-testid="settings-button">
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="3"/><path d="M12 1v6m0 6v6m5.5-13 4.5 4.5M5.5 18.5 1 23m17.5-5.5 4.5 4.5M1 1l4.5 4.5M23 1l-4.5 4.5M5.5 5.5 1 1"/></svg>
          </button>
        </div>
      </header>

      {/* Main Content */}
      <main className="ra-main-content">
        {!isConnected ? (
          // Setup Panel
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
                  <select 
                    className="ra-option-select" 
                    value={selectedPersona}
                    onChange={(e) => setSelectedPersona(e.target.value)}
                    data-testid="persona-select"
                  >
                    {personas.map(p => (
                      <option key={p.id} value={p.id}>{p.name} - {p.description}</option>
                    ))}
                  </select>
                </div>

                <div className="ra-option-group">
                  <label className="ra-option-label">Voice</label>
                  <select 
                    className="ra-option-select"
                    value={selectedVoice}
                    onChange={(e) => setSelectedVoice(e.target.value)}
                    data-testid="voice-select"
                  >
                    {voices.map(v => (
                      <option key={v.id} value={v.id}>{v.name} - {v.description}</option>
                    ))}
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
          // Voice Session Panel
          <div className={`ra-voice-panel ${voiceState}`}>
            <div className="ra-voice-status-area">
              <div className="ra-voice-visualizer" data-testid="voice-visualizer">
                <div className="ra-visualizer-wave">
                  <div className="ra-wave-bar"></div>
                  <div className="ra-wave-bar"></div>
                  <div className="ra-wave-bar"></div>
                  <div className="ra-wave-bar"></div>
                  <div className="ra-wave-bar"></div>
                </div>
              </div>
              <div className="ra-voice-status-text" data-testid="voice-status-text">{status}</div>
            </div>

            {/* Transcript */}
            <div className={`ra-transcript-container ${!showTranscript ? 'collapsed' : ''}`}>
              <div className="ra-transcript-header">
                <h3 className="ra-transcript-title">Conversation</h3>
                <button 
                  className="ra-transcript-toggle" 
                  onClick={() => setShowTranscript(!showTranscript)}
                  data-testid="transcript-toggle"
                >
                  <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="6 9 12 15 18 9"/></svg>
                </button>
              </div>
              {showTranscript && (
                <div className="ra-transcript-messages" data-testid="transcript-messages">
                  {transcript.map((turn, i) => (
                    <div key={i} className={`ra-message ${turn.role}`}>
                      <div className="ra-message-role">{turn.role === 'user' ? 'YOU' : 'ASSISTANT'}</div>
                      <div className="ra-message-content">{turn.content}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </main>

      {/* Controls Bar */}
      <div className="ra-controls-bar">
        <div className="ra-controls-container">
          <button 
            className={`ra-control-btn ${isMicEnabled ? 'active' : ''}`}
            onClick={toggleMicrophone}
            disabled={!isConnected}
            data-testid="mic-button"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>
          </button>
          
          {isConnected && (
            <button 
              className="ra-control-btn ra-control-btn-end"
              onClick={endSession}
              data-testid="end-session-button"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/><line x1="18" x2="18" y1="2" y2="8"/></svg>
            </button>
          )}
        </div>
      </div>
    </div>
  );
}