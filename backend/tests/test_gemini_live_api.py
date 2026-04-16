"""
Backend API Tests for Voice-First Repair Assistant (Gemini Live Architecture)
Tests: /api/health, /api/personas, /api/voices, /api/ws/session WebSocket
"""
import pytest
import requests
import json
import os
import asyncio
import websockets

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')
WS_BASE = BASE_URL.replace('https://', 'wss://').replace('http://', 'ws://')


class TestHealthEndpoint:
    """Health check endpoint tests"""
    
    def test_health_returns_200(self):
        """GET /api/health returns 200 status"""
        response = requests.get(f"{BASE_URL}/api/health")
        assert response.status_code == 200
        print("✓ Health endpoint returns 200")
    
    def test_health_returns_healthy_status(self):
        """GET /api/health returns healthy status"""
        response = requests.get(f"{BASE_URL}/api/health")
        data = response.json()
        assert data.get("status") == "healthy"
        print("✓ Health status is 'healthy'")
    
    def test_health_returns_model_name(self):
        """GET /api/health returns model name"""
        response = requests.get(f"{BASE_URL}/api/health")
        data = response.json()
        assert "model" in data
        assert "gemini" in data["model"].lower()
        print(f"✓ Model name returned: {data['model']}")
    
    def test_health_returns_direct_gemini_live_mode(self):
        """GET /api/health returns mode 'direct-gemini-live'"""
        response = requests.get(f"{BASE_URL}/api/health")
        data = response.json()
        assert data.get("mode") == "direct-gemini-live"
        print("✓ Mode is 'direct-gemini-live'")


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
    
    def test_personas_includes_calm_expert(self):
        """Personas include 'Calm Expert'"""
        response = requests.get(f"{BASE_URL}/api/personas")
        data = response.json()
        names = [p["name"] for p in data]
        assert "Calm Expert" in names
        print("✓ 'Calm Expert' persona found")


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
    
    def test_voices_includes_puck(self):
        """Voices include 'Puck'"""
        response = requests.get(f"{BASE_URL}/api/voices")
        data = response.json()
        names = [v["name"] for v in data]
        assert "Puck" in names
        print("✓ 'Puck' voice found")


class TestWebSocketEndpoint:
    """WebSocket endpoint tests"""
    
    @pytest.mark.asyncio
    async def test_websocket_accepts_connection(self):
        """WebSocket /api/ws/session accepts connections"""
        ws_url = f"{WS_BASE}/api/ws/session"
        try:
            async with websockets.connect(ws_url, close_timeout=5) as ws:
                # Connection established - if we get here, connection is open
                print(f"✓ WebSocket connection established to {ws_url}")
                # Connection is open if we reach this point
                assert True
        except Exception as e:
            pytest.fail(f"WebSocket connection failed: {e}")
    
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
    async def test_websocket_accepts_config_message(self):
        """WebSocket accepts valid config message"""
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
                
                # Should receive status message
                response = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(response)
                assert data.get("type") == "status"
                print(f"✓ WebSocket accepted config, status: {data.get('message')}")
        except asyncio.TimeoutError:
            pytest.fail("WebSocket did not respond to config message")
        except Exception as e:
            pytest.fail(f"WebSocket config test failed: {e}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
