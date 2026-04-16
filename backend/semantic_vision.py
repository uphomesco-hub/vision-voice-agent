"""Semantic Vision — structured scene analysis from camera frames."""
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class SceneState:
    """Structured representation of what the camera sees."""
    def __init__(self):
        self.device_visible = False
        self.close_up_enough = False
        self.focus_area = None
        self.tool_in_hand = False
        self.housing_open = False
        self.internals_visible = False
        self.wires_visible = False
        self.loose_wire_visible = False
        self.label_visible = False
        self.label_text_hint = None
        self.led_visible = False
        self.led_color = None
        self.screw_visible = False
        self.safety_risk = False
        self.confidence = 0.0
        self.timestamp = datetime.now(timezone.utc)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}

    def diff(self, other: 'SceneState') -> list:
        """Return list of meaningful changes between two scene states."""
        changes = []
        if other is None:
            if self.device_visible:
                changes.append(("device_visible", False, True))
            return changes
        checks = [
            ("device_visible", other.device_visible, self.device_visible),
            ("housing_open", other.housing_open, self.housing_open),
            ("internals_visible", other.internals_visible, self.internals_visible),
            ("wires_visible", other.wires_visible, self.wires_visible),
            ("loose_wire_visible", other.loose_wire_visible, self.loose_wire_visible),
            ("label_visible", other.label_visible, self.label_visible),
            ("led_visible", other.led_visible, self.led_visible),
            ("screw_visible", other.screw_visible, self.screw_visible),
            ("safety_risk", other.safety_risk, self.safety_risk),
            ("tool_in_hand", other.tool_in_hand, self.tool_in_hand),
        ]
        for name, old_val, new_val in checks:
            if old_val != new_val:
                changes.append((name, old_val, new_val))
        if self.led_color != other.led_color and self.led_color:
            changes.append(("led_color_change", other.led_color, self.led_color))
        if self.label_text_hint and self.label_text_hint != other.label_text_hint:
            changes.append(("label_text_change", other.label_text_hint, self.label_text_hint))
        return changes


class VisionTracker:
    """Tracks scene state across frames. Lightweight — actual ML analysis delegated to Gemini."""
    def __init__(self):
        self.baseline: Optional[SceneState] = None
        self.current: Optional[SceneState] = None
        self.frame_count = 0
        self.last_analysis_time = None

    def should_analyze(self) -> bool:
        """Decide if we should run expensive analysis on this frame."""
        self.frame_count += 1
        # Analyze every 5th frame (~10s at 2s intervals) or if no baseline
        if self.baseline is None:
            return True
        return self.frame_count % 5 == 0

    def update(self, new_state: SceneState) -> list:
        """Update tracker with new state, return changes."""
        changes = new_state.diff(self.baseline)
        self.current = new_state
        if self.baseline is None or len(changes) > 0:
            self.baseline = new_state
        self.last_analysis_time = datetime.now(timezone.utc)
        return changes
