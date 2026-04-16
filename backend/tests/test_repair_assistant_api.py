"""
Backend API Tests for Voice-First Repair Assistant
Tests: health, personas, voices, sessions CRUD, token generation
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

class TestHealthEndpoint:
    """Health check endpoint tests"""
    
    def test_health_returns_healthy_status(self):
        """GET /api/health returns healthy status"""
        response = requests.get(f"{BASE_URL}/api/health")
        assert response.status_code == 200
        
        data = response.json()
        assert data["status"] == "healthy"
        assert "agent_name" in data
        assert "livekit_url" in data


class TestPersonasEndpoint:
    """Personas endpoint tests"""
    
    def test_personas_returns_list(self):
        """GET /api/personas returns list of personas"""
        response = requests.get(f"{BASE_URL}/api/personas")
        assert response.status_code == 200
        
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 4  # Should have 4 personas
    
    def test_personas_have_required_fields(self):
        """Each persona has id, name, description"""
        response = requests.get(f"{BASE_URL}/api/personas")
        data = response.json()
        
        for persona in data:
            assert "id" in persona
            assert "name" in persona
            assert "description" in persona
            assert isinstance(persona["id"], str)
            assert isinstance(persona["name"], str)
            assert isinstance(persona["description"], str)
    
    def test_calm_expert_persona_exists(self):
        """Calm Expert persona is available"""
        response = requests.get(f"{BASE_URL}/api/personas")
        data = response.json()
        
        calm_expert = next((p for p in data if p["id"] == "calm-expert"), None)
        assert calm_expert is not None
        assert calm_expert["name"] == "Calm Expert"


class TestVoicesEndpoint:
    """Voices endpoint tests"""
    
    def test_voices_returns_list(self):
        """GET /api/voices returns list of voices"""
        response = requests.get(f"{BASE_URL}/api/voices")
        assert response.status_code == 200
        
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 5  # Should have 5 voices
    
    def test_voices_have_required_fields(self):
        """Each voice has id, name, description"""
        response = requests.get(f"{BASE_URL}/api/voices")
        data = response.json()
        
        for voice in data:
            assert "id" in voice
            assert "name" in voice
            assert "description" in voice
            assert isinstance(voice["id"], str)
            assert isinstance(voice["name"], str)
            assert isinstance(voice["description"], str)
    
    def test_puck_voice_exists(self):
        """Puck voice is available"""
        response = requests.get(f"{BASE_URL}/api/voices")
        data = response.json()
        
        puck = next((v for v in data if v["id"] == "puck"), None)
        assert puck is not None
        assert puck["name"] == "Puck"


class TestSessionsEndpoint:
    """Sessions CRUD endpoint tests"""
    
    def test_create_session_success(self):
        """POST /api/sessions creates a new session"""
        response = requests.post(
            f"{BASE_URL}/api/sessions",
            json={"persona_id": "calm-expert", "voice_id": "puck"}
        )
        assert response.status_code == 200
        
        data = response.json()
        assert "id" in data
        assert data["persona_id"] == "calm-expert"
        assert data["voice_id"] == "puck"
        assert data["status"] == "active"
        assert "room_name" in data
        assert "created_at" in data
        
        # Cleanup
        requests.post(f"{BASE_URL}/api/sessions/{data['id']}/end")
    
    def test_create_session_with_different_persona(self):
        """Session can be created with different persona"""
        response = requests.post(
            f"{BASE_URL}/api/sessions",
            json={"persona_id": "friendly-helper", "voice_id": "charon"}
        )
        assert response.status_code == 200
        
        data = response.json()
        assert data["persona_id"] == "friendly-helper"
        assert data["voice_id"] == "charon"
        
        # Cleanup
        requests.post(f"{BASE_URL}/api/sessions/{data['id']}/end")
    
    def test_get_session_token(self):
        """POST /api/sessions/{id}/token returns token and livekit_url"""
        # Create session first
        create_res = requests.post(
            f"{BASE_URL}/api/sessions",
            json={"persona_id": "calm-expert", "voice_id": "puck"}
        )
        session_id = create_res.json()["id"]
        
        # Get token
        response = requests.post(f"{BASE_URL}/api/sessions/{session_id}/token")
        assert response.status_code == 200
        
        data = response.json()
        assert "token" in data
        assert "livekit_url" in data
        assert "room_name" in data
        assert data["livekit_url"].startswith("wss://")
        assert len(data["token"]) > 0
        
        # Cleanup
        requests.post(f"{BASE_URL}/api/sessions/{session_id}/end")
    
    def test_get_session_state(self):
        """GET /api/sessions/{id}/state returns session state with turns"""
        # Create session first
        create_res = requests.post(
            f"{BASE_URL}/api/sessions",
            json={"persona_id": "calm-expert", "voice_id": "puck"}
        )
        session_id = create_res.json()["id"]
        
        # Get state
        response = requests.get(f"{BASE_URL}/api/sessions/{session_id}/state")
        assert response.status_code == 200
        
        data = response.json()
        assert data["session_id"] == session_id
        assert data["status"] == "active"
        assert "turns" in data
        assert isinstance(data["turns"], list)
        assert "agent_state" in data
        
        # Cleanup
        requests.post(f"{BASE_URL}/api/sessions/{session_id}/end")
    
    def test_end_session(self):
        """POST /api/sessions/{id}/end ends a session"""
        # Create session first
        create_res = requests.post(
            f"{BASE_URL}/api/sessions",
            json={"persona_id": "calm-expert", "voice_id": "puck"}
        )
        session_id = create_res.json()["id"]
        
        # End session
        response = requests.post(f"{BASE_URL}/api/sessions/{session_id}/end")
        assert response.status_code == 200
        
        data = response.json()
        assert data["status"] == "ended"
        assert data["session_id"] == session_id
    
    def test_get_token_for_nonexistent_session(self):
        """Token request for nonexistent session returns 404"""
        response = requests.post(f"{BASE_URL}/api/sessions/nonexistent-id/token")
        assert response.status_code == 404
    
    def test_get_state_for_nonexistent_session(self):
        """State request for nonexistent session returns 404"""
        response = requests.get(f"{BASE_URL}/api/sessions/nonexistent-id/state")
        assert response.status_code == 404


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
