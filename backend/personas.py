from typing import List, Dict

PERSONAS: List[Dict] = [
    {
        "id": "calm-expert",
        "name": "Calm Expert",
        "description": "Professional and calm troubleshooting assistant with years of technical experience"
    },
    {
        "id": "friendly-helper",
        "name": "Friendly Helper",
        "description": "Warm and approachable assistant who makes technical help feel easy"
    },
    {
        "id": "precise-technician",
        "name": "Precise Technician",
        "description": "Detail-oriented expert who provides exact, step-by-step guidance"
    },
    {
        "id": "patient-guide",
        "name": "Patient Guide",
        "description": "Understanding assistant who takes time to explain things clearly"
    }
]

VOICES: List[Dict] = [
    {
        "id": "puck",
        "name": "Puck",
        "description": "Default English voice - clear and professional"
    },
    {
        "id": "charon",
        "name": "Charon",
        "description": "Deep male voice - authoritative and confident"
    },
    {
        "id": "kore",
        "name": "Kore",
        "description": "Female voice - warm and reassuring"
    },
    {
        "id": "breeze",
        "name": "Breeze",
        "description": "Soft female voice - gentle and calming"
    },
    {
        "id": "ember",
        "name": "Ember",
        "description": "Energetic male voice - enthusiastic and engaging"
    }
]

def get_personas() -> List[Dict]:
    return PERSONAS

def get_voices() -> List[Dict]:
    return VOICES

def get_persona(persona_id: str) -> Dict:
    return next((p for p in PERSONAS if p["id"] == persona_id), PERSONAS[0])

def get_voice(voice_id: str) -> Dict:
    return next((v for v in VOICES if v["id"] == voice_id), VOICES[0])