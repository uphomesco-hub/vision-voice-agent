from livekit import api
from config import settings
import logging

logger = logging.getLogger(__name__)

def generate_token(room_name: str, participant_identity: str, participant_name: str) -> str:
    """
    Generate a LiveKit access token for a participant.
    """
    try:
        token = api.AccessToken(
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret
        )
        
        token.identity = participant_identity
        token.name = participant_name
        
        # Grant permissions
        token.add_grant(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True
            )
        )
        
        # Token valid for 1 hour
        jwt_token = token.to_jwt()
        
        logger.info(f"Generated token for {participant_identity} in room {room_name}")
        return jwt_token
        
    except Exception as e:
        logger.error(f"Error generating token: {e}")
        raise