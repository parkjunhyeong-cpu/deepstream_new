from .detections import DetectionProbe, add_detection_probe
from .fps import FpsProbe, add_fps_probe
from .zone import ZoneProbe, add_zone_probe

__all__ = [
    "FpsProbe",
    "add_fps_probe",
    "DetectionProbe",
    "add_detection_probe",
    "ZoneProbe",
    "add_zone_probe",
]
