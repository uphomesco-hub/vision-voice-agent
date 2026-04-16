from typing import List, Dict

PERSONAS = [
    {
        "id": "calm-expert",
        "name": "Marcus",
        "trait": "ANALYTICAL",
        "description": "Precise and methodical. Ideal for complex repairs.",
        "avatar": "https://static.prod-images.emergentagent.com/jobs/e73cbd0e-0d8c-4e33-907d-162db56c91e7/images/955d5921715a458e8abe9af5d8628810e08c9951f0f1c2f672b67afdfc22bdba.png",
    },
    {
        "id": "tech-savvy",
        "name": "Nova",
        "trait": "TECHNICAL",
        "description": "Deep technical knowledge. Perfect for electronics.",
        "avatar": "https://static.prod-images.emergentagent.com/jobs/e73cbd0e-0d8c-4e33-907d-162db56c91e7/images/dc92cdde3c4212378c71b6b8b5a47d315e8b781c3fd08257ecdf0c7b5a469eac.png",
    },
    {
        "id": "patient-mentor",
        "name": "Walter",
        "trait": "MENTOR",
        "description": "Patient and wise. Great for beginners.",
        "avatar": "https://static.prod-images.emergentagent.com/jobs/e73cbd0e-0d8c-4e33-907d-162db56c91e7/images/d87a539c6dc352bb68c168fc764f603f010e5ade14fdd08c8ed181c920236ec8.png",
    },
    {
        "id": "energetic-coach",
        "name": "Rex",
        "trait": "ENERGETIC",
        "description": "High-energy motivator. Fast and encouraging.",
        "avatar": "https://static.prod-images.emergentagent.com/jobs/e73cbd0e-0d8c-4e33-907d-162db56c91e7/images/35aa824d046016ae51ebb1dad5540f2133664bfffe57ed63fd38c742b85c95d2.png",
    },
    {
        "id": "funky-jester",
        "name": "Ziggy",
        "trait": "FUNKY",
        "description": "Witty and playful. Makes repairs fun with humor.",
        "avatar": "https://images.unsplash.com/photo-1770658033413-803892b38df6?w=400&h=400&fit=crop&crop=face",
    },
]

VOICES = [
    {"id": "Puck", "name": "Puck", "gender": "Male", "trait": "CLEAR", "description": "Clear and professional"},
    {"id": "Charon", "name": "Charon", "gender": "Male", "trait": "DEEP", "description": "Deep and authoritative"},
    {"id": "Kore", "name": "Kore", "gender": "Female", "trait": "WARM", "description": "Warm and reassuring"},
    {"id": "Aoede", "name": "Aoede", "gender": "Female", "trait": "SOFT", "description": "Calm and soothing"},
    {"id": "Fenrir", "name": "Fenrir", "gender": "Male", "trait": "BOLD", "description": "Enthusiastic and engaging"},
]

def get_personas() -> List[Dict]:
    return PERSONAS

def get_voices() -> List[Dict]:
    return VOICES

def get_persona(persona_id: str) -> Dict:
    return next((p for p in PERSONAS if p["id"] == persona_id), PERSONAS[0])

def get_voice(voice_id: str) -> Dict:
    return next((v for v in VOICES if v["id"] == voice_id), VOICES[0])
