import { useState, useEffect, useRef } from 'react';
import { Room, RoomEvent } from 'livekit-client';
import axios from 'axios';
import './RepairAssistantClientSide.css';

// Client-side only - no backend needed!
// Using LiveKit Cloud playground (free tier)
const LIVEKIT_CLOUD_URL = 'wss://voice-repair-hub-xqh5m51d.livekit.cloud';

export default function RepairAssistantClientSide() {
  const [status, setStatus] = useState('Ready to start');
  const [isConnected, setIsConnected] = useState(false);
  const [transcript, setTranscript] = useState([]);
  const [voiceState, setVoiceState] = useState('idle');
  const [showSetup, setShowSetup] = useState(true);
  
  const roomRef = useRef(null);
  const recognitionRef = useRef(null);

  const startSession = async () => {
    try {
      setStatus('Requesting microphone...');
      
      // Request microphone permission
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      
      setStatus('Initializing voice recognition...');
      
      // Use Web Speech API for voice interaction
      if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        const recognition = new SpeechRecognition();
        
        recognition.continuous = true;
        recognition.interimResults = true;
        recognition.lang = 'en-US';
        
        recognition.onstart = () => {
          setVoiceState('listening');
          setStatus('Listening... speak now!');
          setIsConnected(true);
          setShowSetup(false);
          
          // Initial greeting
          addToTranscript('assistant', 'Hello! I\'m your repair assistant. What device or problem can I help you troubleshoot today?');
          speakText('Hello! I\'m your repair assistant. What device or problem can I help you troubleshoot today?');
        };
        
        recognition.onresult = async (event) => {
          const transcript = Array.from(event.results)
            .map(result => result[0].transcript)
            .join('');
          
          if (event.results[event.results.length - 1].isFinal) {
            console.log('User said:', transcript);
            addToTranscript('user', transcript);
            
            // Get AI response
            setVoiceState('thinking');
            setStatus('Thinking...');
            
            const response = await getAIResponse(transcript);
            addToTranscript('assistant', response);
            
            // Speak response
            speakText(response);
          }
        };
        
        recognition.onerror = (event) => {
          console.error('Speech recognition error:', event.error);
          setStatus('Error: ' + event.error);
        };
        
        recognition.onend = () => {
          if (isConnected) {
            recognition.start(); // Restart if still connected
          }
        };
        
        recognitionRef.current = recognition;
        recognition.start();
        
      } else {
        alert('Speech recognition not supported in this browser. Please use Chrome or Edge.');
      }
      
    } catch (error) {
      console.error('Error starting session:', error);
      setStatus('Error: ' + error.message);
      alert('Please allow microphone access to use voice features.');
    }
  };

  const endSession = () => {
    if (recognitionRef.current) {
      recognitionRef.current.stop();
      recognitionRef.current = null;
    }
    
    window.speechSynthesis.cancel();
    
    setIsConnected(false);
    setVoiceState('idle');
    setStatus('Session ended');
    setShowSetup(true);
    setTranscript([]);
  };

  const addToTranscript = (role, content) => {
    setTranscript(prev => [...prev, { role, content, timestamp: new Date().toISOString() }]);
  };

  const speakText = (text) => {
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1.0;
    utterance.pitch = 1.0;
    utterance.volume = 1.0;
    
    utterance.onstart = () => {
      setVoiceState('speaking');
      setStatus('Speaking...');
    };
    
    utterance.onend = () => {
      setVoiceState('listening');
      setStatus('Listening...');
    };
    
    window.speechSynthesis.speak(utterance);
  };

  const getAIResponse = async (userMessage) => {
    // Simple rule-based responses for demo
    // In production, you could call Gemini API directly from browser
    const lowerMessage = userMessage.toLowerCase();
    
    if (lowerMessage.includes('screen') && (lowerMessage.includes('crack') || lowerMessage.includes('broken'))) {
      return "A cracked screen can often be repaired at a phone repair shop. Costs typically range from $100-300 depending on your device model. Would you like tips on temporary protection while you arrange repair?";
    }
    
    if (lowerMessage.includes('battery') || lowerMessage.includes('charge')) {
      return "Battery issues are common. First, try restarting your device. If the problem persists, check if any apps are draining battery in settings. Battery replacement might be needed if it's old. What device are you using?";
    }
    
    if (lowerMessage.includes('slow') || lowerMessage.includes('lag')) {
      return "Device slowness can have several causes. Try clearing cache, closing background apps, and freeing up storage space. A restart often helps too. Which device type is running slow?";
    }
    
    if (lowerMessage.includes('wifi') || lowerMessage.includes('internet') || lowerMessage.includes('connection')) {
      return "Let's troubleshoot your connection. First, try turning WiFi off and on. Then restart your router. If that doesn't work, forget the network and reconnect. Are other devices connecting successfully?";
    }
    
    if (lowerMessage.includes('sound') || lowerMessage.includes('audio') || lowerMessage.includes('speaker')) {
      return "For audio issues, check if the volume is up and not muted. Try plugging in headphones to see if speakers are the problem. Also check for debris in the speaker grille. What exactly is happening with the sound?";
    }
    
    if (lowerMessage.includes('update') || lowerMessage.includes('upgrade')) {
      return "Software updates can fix many issues. Go to Settings, look for Software Update or System Update. Make sure you're connected to WiFi and have sufficient battery. Should I guide you through the update process?";
    }
    
    if (lowerMessage.includes('camera') || lowerMessage.includes('photo')) {
      return "Camera problems can often be fixed by cleaning the lens, restarting the app, or clearing the camera app cache. What specific issue are you experiencing with the camera?";
    }
    
    // Default response
    return `I understand you're having an issue with: "${userMessage}". Could you tell me more about what's happening? What device are you using, and when did this problem start?`;
  };

  return (
    <div className="repair-assistant">
      {/* Header */}
      <header className=\"ra-header\">
        <div className=\"ra-header-left\">
          <div className=\"ra-live-indicator\">
            <span className=\"ra-live-dot\"></span>
            <span className=\"ra-live-text\" data-testid=\"status-text\">{status}</span>
          </div>
        </div>
        <div className=\"ra-header-center\">
          <h1 className=\"ra-app-title\">AI Repair Assistant</h1>
        </div>
        <div className=\"ra-header-right\">
          <span className=\"ra-badge\">Voice AI</span>
        </div>
      </header>

      {/* Main Content */}
      <main className=\"ra-main-content\">
        {showSetup ? (
          // Setup Panel
          <div className=\"ra-setup-panel\">
            <div className=\"ra-setup-content\">
              <div className=\"ra-setup-hero\">
                <div className=\"ra-voice-icon-large\" data-testid=\"voice-icon\">
                  <svg xmlns=\"http://www.w3.org/2000/svg\" width=\"64\" height=\"64\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" strokeWidth=\"2\"><path d=\"M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z\"/><path d=\"M19 10v2a7 7 0 0 1-14 0v-2\"/><line x1=\"12\" x2=\"12\" y1=\"19\" y2=\"22\"/></svg>
                </div>
                <h2 className=\"ra-setup-title\">Voice Troubleshooting</h2>
                <p className=\"ra-setup-subtitle\">Talk naturally about your device problem</p>
                <div className=\"ra-features\">
                  <div className=\"ra-feature\">✓ Real-time voice interaction</div>
                  <div className=\"ra-feature\">✓ Smart troubleshooting tips</div>
                  <div className=\"ra-feature\">✓ No backend required</div>
                </div>
              </div>

              <button className=\"ra-btn-primary\" onClick={startSession} data-testid=\"start-session-button\">
                <svg xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" strokeWidth=\"2\"><path d=\"M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z\"/><path d=\"M19 10v2a7 7 0 0 1-14 0v-2\"/><line x1=\"12\" x2=\"12\" y1=\"19\" y2=\"22\"/></svg>
                Start Voice Session
              </button>
              
              <p className=\"ra-note\">Works best in Chrome or Edge browser</p>
            </div>
          </div>
        ) : (
          // Voice Session Panel
          <div className={`ra-voice-panel ${voiceState}`}>
            <div className=\"ra-voice-status-area\">
              <div className=\"ra-voice-visualizer\" data-testid=\"voice-visualizer\">
                <div className=\"ra-visualizer-wave\">
                  <div className=\"ra-wave-bar\"></div>
                  <div className=\"ra-wave-bar\"></div>
                  <div className=\"ra-wave-bar\"></div>
                  <div className=\"ra-wave-bar\"></div>
                  <div className=\"ra-wave-bar\"></div>
                </div>
              </div>
              <div className=\"ra-voice-status-text\" data-testid=\"voice-status-text\">{status}</div>
            </div>

            {/* Transcript */}
            <div className=\"ra-transcript-container\">
              <div className=\"ra-transcript-header\">
                <h3 className=\"ra-transcript-title\">Conversation</h3>
              </div>
              <div className=\"ra-transcript-messages\" data-testid=\"transcript-messages\">
                {transcript.map((turn, i) => (
                  <div key={i} className={`ra-message ${turn.role}`}>
                    <div className=\"ra-message-role\">{turn.role === 'user' ? 'YOU' : 'AI ASSISTANT'}</div>
                    <div className=\"ra-message-content\">{turn.content}</div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </main>

      {/* Controls Bar */}
      <div className=\"ra-controls-bar\">
        <div className=\"ra-controls-container\">
          {isConnected && (
            <button 
              className=\"ra-control-btn ra-control-btn-end\"
              onClick={endSession}
              data-testid=\"end-session-button\"
            >
              <svg xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" strokeWidth=\"2\"><path d=\"M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z\"/><line x1=\"18\" x2=\"18\" y1=\"2\" y2=\"8\"/></svg>
              End Session
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
