/**
 * Zeno AI - Frontend Application
 * Step 1: Voice-only with LiveKit and Gemini Live
 */

const API_BASE = window.location.origin + '/api';

// Application state
const state = {
    session: null,
    room: null,
    personas: [],
    voices: [],
    isConnected: false,
    isMicEnabled: false,
    currentStatus: 'idle',
    transcriptCollapsed: false
};

// DOM Elements
const elements = {
    setupPanel: document.getElementById('setupPanel'),
    voicePanel: document.getElementById('voicePanel'),
    personaSelect: document.getElementById('personaSelect'),
    voiceSelect: document.getElementById('voiceSelect'),
    startBtn: document.getElementById('startBtn'),
    micBtn: document.getElementById('micBtn'),
    endBtn: document.getElementById('endBtn'),
    statusText: document.getElementById('statusText'),
    voiceStatus: document.getElementById('voiceStatus'),
    transcriptMessages: document.getElementById('transcriptMessages'),
    transcriptToggle: document.getElementById('transcriptToggle'),
    transcriptContainer: document.getElementById('transcriptContainer')
};

// Initialize app
async function init() {
    console.log('Initializing Zeno AI...');
    
    try {
        // Load personas and voices
        await Promise.all([
            loadPersonas(),
            loadVoices()
        ]);
        
        // Setup event listeners
        setupEventListeners();
        
        console.log('App initialized successfully');
    } catch (error) {
        console.error('Initialization error:', error);
        updateStatus('ERROR');
    }
}

// Load personas from API
async function loadPersonas() {
    try {
        const response = await fetch(`${API_BASE}/personas`);
        if (!response.ok) throw new Error('Failed to load personas');
        
        state.personas = await response.json();
        
        // Populate select
        elements.personaSelect.innerHTML = state.personas.map(p => 
            `<option value=\"${p.id}\">${p.name} - ${p.description}</option>`
        ).join('');
        
        console.log('Personas loaded:', state.personas.length);
    } catch (error) {
        console.error('Error loading personas:', error);
        elements.personaSelect.innerHTML = '<option value=\"\">Error loading personas</option>';
    }
}

// Load voices from API
async function loadVoices() {
    try {
        const response = await fetch(`${API_BASE}/voices`);
        if (!response.ok) throw new Error('Failed to load voices');
        
        state.voices = await response.json();
        
        // Populate select
        elements.voiceSelect.innerHTML = state.voices.map(v => 
            `<option value=\"${v.id}\">${v.name} - ${v.description}</option>`
        ).join('');
        
        console.log('Voices loaded:', state.voices.length);
    } catch (error) {
        console.error('Error loading voices:', error);
        elements.voiceSelect.innerHTML = '<option value=\"\">Error loading voices</option>';
    }
}

// Setup event listeners
function setupEventListeners() {
    elements.startBtn.addEventListener('click', startSession);
    elements.endBtn.addEventListener('click', endSession);
    elements.micBtn.addEventListener('click', toggleMicrophone);
    elements.transcriptToggle.addEventListener('click', toggleTranscript);
}

// Start session
async function startSession() {
    try {
        updateStatus('CONNECTING');
        elements.startBtn.disabled = true;
        elements.startBtn.textContent = 'Starting...';
        
        const personaId = elements.personaSelect.value;
        const voiceId = elements.voiceSelect.value;
        
        console.log('Creating session with persona:', personaId, 'voice:', voiceId);
        
        // Create session
        const sessionResponse = await fetch(`${API_BASE}/sessions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ persona_id: personaId, voice_id: voiceId })
        });
        
        if (!sessionResponse.ok) {
            throw new Error('Failed to create session');
        }
        
        state.session = await sessionResponse.json();
        console.log('Session created:', state.session);
        
        // Get token
        const tokenResponse = await fetch(`${API_BASE}/sessions/${state.session.id}/token`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        
        if (!tokenResponse.ok) {
            throw new Error('Failed to get token');
        }
        
        const tokenData = await tokenResponse.json();
        console.log('Token received for room:', tokenData.room_name);
        
        // Request microphone permission
        try {
            await navigator.mediaDevices.getUserMedia({ audio: true });
            console.log('Microphone permission granted');
        } catch (error) {\n            console.error('Microphone permission denied:', error);
            alert('Microphone permission is required for voice interaction');
            throw error;
        }
        
        // Connect to LiveKit
        await connectToLiveKit(tokenData);
        
        // Switch UI
        elements.setupPanel.classList.add('hidden');
        elements.voicePanel.classList.remove('hidden');
        elements.endBtn.classList.remove('hidden');
        
        updateStatus('LIVE');
        setVoiceStatus('Listening');
        
        // Start polling for transcript
        startTranscriptPolling();
        
    } catch (error) {
        console.error('Error starting session:', error);
        updateStatus('ERROR');
        elements.startBtn.disabled = false;
        elements.startBtn.textContent = 'Start Session';
        alert('Failed to start session. Please try again.');
    }
}

// Connect to LiveKit room
async function connectToLiveKit(tokenData) {
    const LiveKit = window.LivekitClient;
    
    try {
        state.room = new LiveKit.Room({
            adaptiveStream: true,
            dynacast: true,
            audioCaptureDefaults: {
                autoGainControl: true,
                echoCancellation: true,
                noiseSuppression: true,
            }
        });
        
        // Setup event handlers
        state.room.on(LiveKit.RoomEvent.Connected, () => {
            console.log('Connected to room');
            state.isConnected = true;
            elements.micBtn.disabled = false;
        });
        
        state.room.on(LiveKit.RoomEvent.Disconnected, () => {
            console.log('Disconnected from room');
            state.isConnected = false;
            handleDisconnect();
        });
        
        state.room.on(LiveKit.RoomEvent.TrackSubscribed, (track, publication, participant) => {
            console.log('Track subscribed:', track.kind);
            if (track.kind === 'audio') {
                const audioElement = track.attach();
                document.body.appendChild(audioElement);
                setVoiceStatus('Agent speaking...');
                elements.voicePanel.classList.remove('listening');
                elements.voicePanel.classList.add('thinking');
            }
        });
        
        state.room.on(LiveKit.RoomEvent.TrackUnsubscribed, (track) => {
            console.log('Track unsubscribed:', track.kind);
            track.detach();
            setVoiceStatus('Listening');
            elements.voicePanel.classList.remove('thinking');
            elements.voicePanel.classList.add('listening');
        });
        
        // Connect to room
        console.log('Connecting to LiveKit server:', tokenData.livekit_url);
        await state.room.connect(tokenData.livekit_url, tokenData.token);
        
        // Publish local audio track
        await state.room.localParticipant.setMicrophoneEnabled(true);
        state.isMicEnabled = true;
        elements.micBtn.classList.add('active');
        elements.voicePanel.classList.add('listening');
        
        console.log('Audio track published');
        
    } catch (error) {
        console.error('LiveKit connection error:', error);
        throw error;
    }
}

// Toggle microphone
async function toggleMicrophone() {
    if (!state.room) return;
    
    try {
        state.isMicEnabled = !state.isMicEnabled;
        await state.room.localParticipant.setMicrophoneEnabled(state.isMicEnabled);
        
        if (state.isMicEnabled) {
            elements.micBtn.classList.add('active');
            elements.voicePanel.classList.add('listening');
            setVoiceStatus('Listening');
        } else {
            elements.micBtn.classList.remove('active');
            elements.voicePanel.classList.remove('listening');
            setVoiceStatus('Microphone muted');
        }
        
        console.log('Microphone', state.isMicEnabled ? 'enabled' : 'disabled');
    } catch (error) {
        console.error('Error toggling microphone:', error);
    }
}

// End session
async function endSession() {
    try {
        console.log('Ending session...');
        
        // Disconnect from LiveKit
        if (state.room) {
            await state.room.disconnect();
            state.room = null;
        }
        
        // End session on backend
        if (state.session) {
            await fetch(`${API_BASE}/sessions/${state.session.id}/end`, {
                method: 'POST'
            });
        }
        
        // Reset UI
        elements.voicePanel.classList.add('hidden');
        elements.setupPanel.classList.remove('hidden');
        elements.endBtn.classList.add('hidden');
        elements.micBtn.disabled = true;
        elements.micBtn.classList.remove('active');
        elements.startBtn.disabled = false;
        elements.startBtn.innerHTML = '<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z\"/><path d=\"M19 10v2a7 7 0 0 1-14 0v-2\"/><line x1=\"12\" x2=\"12\" y1=\"19\" y2=\"22\"/></svg>Start Session';\n        \n        // Clear transcript\n        elements.transcriptMessages.innerHTML = '';\n        \n        updateStatus('READY');\n        state.session = null;\n        state.isConnected = false;\n        state.isMicEnabled = false;\n        \n        console.log('Session ended');
    } catch (error) {
        console.error('Error ending session:', error);
    }
}

// Handle disconnect
function handleDisconnect() {
    console.log('Handling disconnect...');
    if (state.isConnected) {
        updateStatus('DISCONNECTED');
        setVoiceStatus('Connection lost');
    }
}

// Start polling for transcript
function startTranscriptPolling() {
    const pollInterval = setInterval(async () => {
        if (!state.session || state.session.status === 'ended') {
            clearInterval(pollInterval);
            return;
        }
        
        try {
            const response = await fetch(`${API_BASE}/sessions/${state.session.id}/state`);
            if (!response.ok) throw new Error('Failed to fetch state');
            
            const stateData = await response.json();
            updateTranscript(stateData.turns);
        } catch (error) {
            console.error('Error polling transcript:', error);
        }
    }, 2000);
}

// Update transcript display
function updateTranscript(turns) {
    const currentLength = elements.transcriptMessages.children.length;
    
    // Only add new turns
    if (turns.length > currentLength) {
        const newTurns = turns.slice(currentLength);
        
        newTurns.forEach(turn => {
            const messageDiv = document.createElement('div');
            messageDiv.className = `message ${turn.role}`;
            messageDiv.innerHTML = `
                <div class=\"message-role\">${turn.role === 'user' ? 'YOU' : 'ASSISTANT'}</div>
                <div class=\"message-content\">${turn.content}</div>
            `;
            elements.transcriptMessages.appendChild(messageDiv);
        });
        
        // Scroll to bottom
        elements.transcriptMessages.scrollTop = elements.transcriptMessages.scrollHeight;
    }
}

// Toggle transcript
function toggleTranscript() {
    state.transcriptCollapsed = !state.transcriptCollapsed;
    elements.transcriptContainer.classList.toggle('collapsed');
}

// Update status text
function updateStatus(status) {
    state.currentStatus = status;
    elements.statusText.textContent = status;
}

// Update voice status
function setVoiceStatus(status) {
    elements.voiceStatus.textContent = status;
}

// Initialize when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}
