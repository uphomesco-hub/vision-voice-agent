"""Nudge Engine — controls when the assistant should proactively speak about visual observations."""
import logging
import time
from typing import Optional, Dict

logger = logging.getLogger(__name__)

# Minimum seconds between nudges of the same type
DEFAULT_COOLDOWNS = {
    "device_visible": 60,
    "housing_open": 30,
    "label_visible": 30,
    "led_visible": 30,
    "led_color_change": 20,
    "wires_visible": 30,
    "loose_wire_visible": 15,  # Important — shorter cooldown
    "screw_visible": 45,
    "safety_risk": 10,  # Critical — always allow quickly
    "tool_in_hand": 45,
    "internals_visible": 30,
    "angle_hint": 90,  # Very slow — don't nag about framing
    "label_text_change": 20,
    "vision_check": 5,  # Fast nudges with Flash grounding
}

# Grace period after user speaks before allowing visual nudges (seconds)
USER_SPEECH_GRACE = 2


class NudgeEngine:
    def __init__(self):
        self.last_nudge_time: Dict[str, float] = {}
        self.last_user_speech_time: float = 0
        self.last_nudge_summary: Optional[str] = None
        self.suppressed_count = 0

    def user_spoke(self):
        """Call when user speech is detected — sets grace window."""
        self.last_user_speech_time = time.time()

    def should_nudge(self, nudge_type: str, summary: str = "") -> bool:
        """Decide if a nudge should be surfaced to the user."""
        now = time.time()

        # Grace period after user speech
        if now - self.last_user_speech_time < USER_SPEECH_GRACE:
            logger.debug(f"Nudge suppressed (user speech grace): {nudge_type}")
            self.suppressed_count += 1
            return False

        # Per-type cooldown
        cooldown = DEFAULT_COOLDOWNS.get(nudge_type, 30)
        last = self.last_nudge_time.get(nudge_type, 0)
        if now - last < cooldown:
            logger.debug(f"Nudge suppressed (cooldown {cooldown}s): {nudge_type}")
            self.suppressed_count += 1
            return False

        # Duplicate summary suppression
        if summary and summary == self.last_nudge_summary:
            logger.debug(f"Nudge suppressed (duplicate): {nudge_type}")
            self.suppressed_count += 1
            return False

        # Safety always passes
        if nudge_type == "safety_risk":
            self.last_nudge_time[nudge_type] = now
            self.last_nudge_summary = summary
            return True

        # Allow the nudge
        self.last_nudge_time[nudge_type] = now
        self.last_nudge_summary = summary
        return True

    def get_stats(self) -> dict:
        return {
            "suppressed_count": self.suppressed_count,
            "active_cooldowns": {k: round(time.time() - v, 1) for k, v in self.last_nudge_time.items()},
        }
