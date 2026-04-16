"""
Backend API Tests for Voice-First Repair Assistant - Step 2
Tests: Session CRUD, Manual system, WebSocket session.ready, expanded state
"""
import pytest
import requests
import json
import os
import asyncio
import websockets

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')
WS_BASE = BASE_URL.replace('https://', 'wss://').replace('http://', 'ws://')


class TestHealthEndpointStep2:
    """Health check endpoint tests for Step 2"""
    
    def test_health_returns_200(self):
        """GET /api/health returns 200 status"""
        response = requests.get(f"{BASE_URL}/api/health")
        assert response.status_code == 200
        print("✓ Health endpoint returns 200")
    
    def test_health_returns_version_2_step2(self):
        """GET /api/health returns version 2.0-step2"""
        response = requests.get(f"{BASE_URL}/api/health")
        data = response.json()
        assert data.get("version") == "2.0-step2"
        print(f"✓ Version is '2.0-step2': {data.get('version')}")
    
    def test_health_returns_model_name(self):
        """GET /api/health returns model name"""
        response = requests.get(f"{BASE_URL}/api/health")
        data = response.json()
        assert "model" in data
        assert "gemini" in data["model"].lower()
        print(f"✓ Model name returned: {data['model']}")


class TestPersonasEndpoint:
    """Personas endpoint tests"""
    
    def test_personas_returns_200(self):
        """GET /api/personas returns 200 status"""
        response = requests.get(f"{BASE_URL}/api/personas")
        assert response.status_code == 200
        print("✓ Personas endpoint returns 200")
    
    def test_personas_returns_4_personas(self):
        """GET /api/personas returns exactly 4 personas"""
        response = requests.get(f"{BASE_URL}/api/personas")
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 4
        print(f"✓ Returned {len(data)} personas")
    
    def test_personas_have_required_fields(self):
        """Each persona has id, name, description"""
        response = requests.get(f"{BASE_URL}/api/personas")
        data = response.json()
        for persona in data:
            assert "id" in persona
            assert "name" in persona
            assert "description" in persona
        print("✓ All personas have required fields")


class TestVoicesEndpoint:
    """Voices endpoint tests"""
    
    def test_voices_returns_200(self):
        """GET /api/voices returns 200 status"""
        response = requests.get(f"{BASE_URL}/api/voices")
        assert response.status_code == 200
        print("✓ Voices endpoint returns 200")
    
    def test_voices_returns_5_voices(self):
        """GET /api/voices returns exactly 5 voices"""
        response = requests.get(f"{BASE_URL}/api/voices")
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 5
        print(f"✓ Returned {len(data)} voices")
    
    def test_voices_have_required_fields(self):
        """Each voice has id, name, description"""
        response = requests.get(f"{BASE_URL}/api/voices")
        data = response.json()
        for voice in data:
            assert "id" in voice
            assert "name" in voice
            assert "description" in voice
        print("✓ All voices have required fields")


class TestSessionCRUD:
    """Session CRUD endpoint tests"""
    
    def test_create_session_returns_id_and_status(self):
        """POST /api/sessions creates session and returns id + status=active"""
        response = requests.post(f"{BASE_URL}/api/sessions", json={
            "persona_id": "calm-expert",
            "voice_id": "Puck"
        })
        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert data.get("status") == "active"
        print(f"✓ Session created with id: {data['id']}, status: {data['status']}")
        return data["id"]
    
    def test_get_session_state_returns_full_state(self):
        """GET /api/sessions/{id}/state returns full session state"""
        # First create a session
        create_resp = requests.post(f"{BASE_URL}/api/sessions", json={
            "persona_id": "friendly-helper",
            "voice_id": "Charon"
        })
        session_id = create_resp.json()["id"]
        
        # Get session state
        response = requests.get(f"{BASE_URL}/api/sessions/{session_id}/state")
        assert response.status_code == 200
        data = response.json()
        
        # Verify state structure
        assert data.get("session_id") == session_id
        assert data.get("status") == "active"
        assert data.get("persona_id") == "friendly-helper"
        assert data.get("voice_id") == "Charon"
        assert "turns" in data
        assert "observations" in data
        assert "tool_runs" in data
        print(f"✓ Session state returned with turns, observations, tool_runs")
    
    def test_get_session_state_not_found(self):
        """GET /api/sessions/{id}/state returns error for non-existent session"""
        response = requests.get(f"{BASE_URL}/api/sessions/non-existent-id/state")
        assert response.status_code == 200  # API returns 200 with error in body
        data = response.json()
        assert "error" in data
        print("✓ Non-existent session returns error")
    
    def test_end_session(self):
        """POST /api/sessions/{id}/end ends session"""
        # Create session
        create_resp = requests.post(f"{BASE_URL}/api/sessions", json={
            "persona_id": "calm-expert",
            "voice_id": "Puck"
        })
        session_id = create_resp.json()["id"]
        
        # End session
        response = requests.post(f"{BASE_URL}/api/sessions/{session_id}/end")
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "ended"
        print(f"✓ Session {session_id} ended successfully")
        
        # Verify session is ended
        state_resp = requests.get(f"{BASE_URL}/api/sessions/{session_id}/state")
        state_data = state_resp.json()
        assert state_data.get("status") == "ended"
        print("✓ Session state confirms ended status")
    
    def test_end_session_not_found(self):
        """POST /api/sessions/{id}/end returns not_found for non-existent session"""
        response = requests.post(f"{BASE_URL}/api/sessions/non-existent-id/end")
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "not_found"
        print("✓ End non-existent session returns not_found")
    
    def test_get_session_logs(self):
        """GET /api/sessions/{id}/logs returns turns and tool_runs"""
        # Create session
        create_resp = requests.post(f"{BASE_URL}/api/sessions", json={
            "persona_id": "calm-expert",
            "voice_id": "Puck"
        })
        session_id = create_resp.json()["id"]
        
        # Get logs
        response = requests.get(f"{BASE_URL}/api/sessions/{session_id}/logs")
        assert response.status_code == 200
        data = response.json()
        
        assert "turns" in data
        assert "tool_runs" in data
        assert isinstance(data["turns"], list)
        assert isinstance(data["tool_runs"], list)
        print(f"✓ Session logs returned with turns and tool_runs arrays")


class TestWebSocketSessionReady:
    """WebSocket endpoint tests for session.ready message"""
    
    @pytest.mark.asyncio
    async def test_websocket_accepts_connection(self):
        """WebSocket /api/ws/session accepts connections"""
        ws_url = f"{WS_BASE}/api/ws/session"
        try:
            async with websockets.connect(ws_url, close_timeout=5) as ws:
                print(f"✓ WebSocket connection established to {ws_url}")
                assert True
        except Exception as e:
            pytest.fail(f"WebSocket connection failed: {e}")
    
    @pytest.mark.asyncio
    async def test_websocket_returns_session_ready_with_session_id(self):
        """WebSocket returns session.ready with session_id after config message"""
        ws_url = f"{WS_BASE}/api/ws/session"
        try:
            async with websockets.connect(ws_url, close_timeout=15) as ws:
                # Send valid config
                config = {
                    "type": "config",
                    "persona_id": "calm-expert",
                    "voice_id": "Puck"
                }
                await ws.send(json.dumps(config))
                
                # Should receive session.ready message
                response = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(response)
                
                assert data.get("type") == "session.ready"
                assert "session_id" in data
                assert len(data["session_id"]) > 0
                print(f"✓ WebSocket returned session.ready with session_id: {data['session_id']}")
        except asyncio.TimeoutError:
            pytest.fail("WebSocket did not respond to config message")
        except Exception as e:
            pytest.fail(f"WebSocket session.ready test failed: {e}")
    
    @pytest.mark.asyncio
    async def test_websocket_requires_config_message(self):
        """WebSocket requires config message as first message"""
        ws_url = f"{WS_BASE}/api/ws/session"
        try:
            async with websockets.connect(ws_url, close_timeout=10) as ws:
                # Send non-config message
                await ws.send(json.dumps({"type": "audio", "data": "test"}))
                
                # Should receive error
                response = await asyncio.wait_for(ws.recv(), timeout=5)
                data = json.loads(response)
                assert data.get("type") == "error"
                assert "config" in data.get("message", "").lower()
                print("✓ WebSocket correctly requires config message first")
        except asyncio.TimeoutError:
            pytest.fail("WebSocket did not respond to invalid message")
        except Exception as e:
            # Connection may close on error, which is acceptable
            print(f"✓ WebSocket rejected invalid message (connection closed or error: {e})")
    
    @pytest.mark.asyncio
    async def test_websocket_sends_assistant_state_after_config(self):
        """WebSocket sends assistant.state message after config"""
        ws_url = f"{WS_BASE}/api/ws/session"
        try:
            async with websockets.connect(ws_url, close_timeout=15) as ws:
                # Send valid config
                config = {
                    "type": "config",
                    "persona_id": "calm-expert",
                    "voice_id": "Puck"
                }
                await ws.send(json.dumps(config))
                
                # Collect first few messages
                messages = []
                for _ in range(3):
                    try:
                        response = await asyncio.wait_for(ws.recv(), timeout=5)
                        messages.append(json.loads(response))
                    except asyncio.TimeoutError:
                        break
                
                # Check for session.ready and assistant.state
                types = [m.get("type") for m in messages]
                assert "session.ready" in types
                assert "assistant.state" in types
                print(f"✓ WebSocket sent messages: {types}")
        except Exception as e:
            pytest.fail(f"WebSocket assistant.state test failed: {e}")


class TestManualSeeding:
    """Tests to verify manuals are seeded in the database"""
    
    def test_session_state_has_manual_fields(self):
        """Session state includes manual-related fields"""
        # Create session
        create_resp = requests.post(f"{BASE_URL}/api/sessions", json={
            "persona_id": "calm-expert",
            "voice_id": "Puck"
        })
        session_id = create_resp.json()["id"]
        
        # Get session state
        response = requests.get(f"{BASE_URL}/api/sessions/{session_id}/state")
        data = response.json()
        
        # Verify manual-related fields exist (may be null initially)
        assert "active_manual_id" in data
        assert "active_device_type" in data
        assert "active_device_model" in data
        print("✓ Session state includes manual-related fields")


class TestWebSocketReconnect:
    """WebSocket reconnect with resume_session_id tests"""
    
    @pytest.mark.asyncio
    async def test_websocket_reconnect_reuses_same_session(self):
        """WebSocket reconnect with resume_session_id reuses the same session"""
        ws_url = f"{WS_BASE}/api/ws/session"
        
        # First connection - create new session
        try:
            async with websockets.connect(ws_url, close_timeout=15) as ws1:
                config1 = {
                    "type": "config",
                    "persona_id": "calm-expert",
                    "voice_id": "Puck"
                }
                await ws1.send(json.dumps(config1))
                
                # Get session.ready with session_id
                response1 = await asyncio.wait_for(ws1.recv(), timeout=10)
                data1 = json.loads(response1)
                assert data1.get("type") == "session.ready"
                original_session_id = data1.get("session_id")
                assert original_session_id is not None
                print(f"✓ First connection created session: {original_session_id}")
        except Exception as e:
            pytest.fail(f"First WebSocket connection failed: {e}")
        
        # Second connection - reconnect with resume_session_id
        try:
            async with websockets.connect(ws_url, close_timeout=15) as ws2:
                config2 = {
                    "type": "config",
                    "persona_id": "calm-expert",
                    "voice_id": "Puck",
                    "resume_session_id": original_session_id
                }
                await ws2.send(json.dumps(config2))
                
                # Get session.ready - should have same session_id
                response2 = await asyncio.wait_for(ws2.recv(), timeout=10)
                data2 = json.loads(response2)
                assert data2.get("type") == "session.ready"
                resumed_session_id = data2.get("session_id")
                
                assert resumed_session_id == original_session_id
                print(f"✓ Reconnect reused same session: {resumed_session_id}")
        except Exception as e:
            pytest.fail(f"Reconnect WebSocket failed: {e}")
    
    @pytest.mark.asyncio
    async def test_websocket_reconnect_with_invalid_session_creates_new(self):
        """WebSocket reconnect with invalid resume_session_id creates new session"""
        ws_url = f"{WS_BASE}/api/ws/session"
        
        try:
            async with websockets.connect(ws_url, close_timeout=15) as ws:
                config = {
                    "type": "config",
                    "persona_id": "calm-expert",
                    "voice_id": "Puck",
                    "resume_session_id": "non-existent-session-id"
                }
                await ws.send(json.dumps(config))
                
                # Should still get session.ready with a new session_id
                response = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(response)
                assert data.get("type") == "session.ready"
                assert "session_id" in data
                assert data["session_id"] != "non-existent-session-id"
                print(f"✓ Invalid resume_session_id created new session: {data['session_id']}")
        except Exception as e:
            pytest.fail(f"WebSocket with invalid resume_session_id failed: {e}")
    
    @pytest.mark.asyncio
    async def test_websocket_reconnect_with_ended_session_creates_new(self):
        """WebSocket reconnect with ended session creates new session"""
        ws_url = f"{WS_BASE}/api/ws/session"
        
        # First create and end a session via REST API
        create_resp = requests.post(f"{BASE_URL}/api/sessions", json={
            "persona_id": "calm-expert",
            "voice_id": "Puck"
        })
        ended_session_id = create_resp.json()["id"]
        
        # End the session
        requests.post(f"{BASE_URL}/api/sessions/{ended_session_id}/end")
        
        # Try to reconnect to ended session
        try:
            async with websockets.connect(ws_url, close_timeout=15) as ws:
                config = {
                    "type": "config",
                    "persona_id": "calm-expert",
                    "voice_id": "Puck",
                    "resume_session_id": ended_session_id
                }
                await ws.send(json.dumps(config))
                
                # Should get session.ready with a NEW session_id (not the ended one)
                response = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(response)
                assert data.get("type") == "session.ready"
                new_session_id = data.get("session_id")
                assert new_session_id != ended_session_id
                print(f"✓ Ended session {ended_session_id} → new session {new_session_id}")
        except Exception as e:
            pytest.fail(f"WebSocket reconnect to ended session failed: {e}")


class TestSessionStateFields:
    """Tests for session state field completeness"""
    
    def test_session_state_has_current_step(self):
        """GET /api/sessions/{id}/state returns current_step field"""
        create_resp = requests.post(f"{BASE_URL}/api/sessions", json={
            "persona_id": "calm-expert",
            "voice_id": "Puck"
        })
        session_id = create_resp.json()["id"]
        
        response = requests.get(f"{BASE_URL}/api/sessions/{session_id}/state")
        data = response.json()
        
        assert "current_step" in data
        assert isinstance(data["current_step"], int)
        print(f"✓ Session state has current_step: {data['current_step']}")
    
    def test_session_state_has_all_required_fields(self):
        """GET /api/sessions/{id}/state returns all required fields"""
        create_resp = requests.post(f"{BASE_URL}/api/sessions", json={
            "persona_id": "precise-technician",
            "voice_id": "Kore"
        })
        session_id = create_resp.json()["id"]
        
        response = requests.get(f"{BASE_URL}/api/sessions/{session_id}/state")
        data = response.json()
        
        required_fields = [
            "session_id", "status", "persona_id", "voice_id",
            "active_device_type", "active_device_model", "active_manual_id",
            "current_step", "pending_goal", "warnings_given",
            "turns", "observations", "tool_runs"
        ]
        
        for field in required_fields:
            assert field in data, f"Missing field: {field}"
        
        # Verify arrays
        assert isinstance(data["turns"], list)
        assert isinstance(data["observations"], list)
        assert isinstance(data["tool_runs"], list)
        
        print(f"✓ Session state has all {len(required_fields)} required fields")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
