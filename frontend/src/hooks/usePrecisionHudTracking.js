import { useEffect, useMemo, useRef, useState } from 'react';
import { PrecisionHudTrackerPool, mapContourToDisplay, mapGeometryToDisplay, transformContour } from '../lib/precisionHudTracker';

const SEND_INTERVAL_MS = 120;

export function usePrecisionHudTracking({ markers, videoRef, wsRef, enabled, mirrored = false }) {
  const [liveMarkers, setLiveMarkers] = useState([]);
  const poolRef = useRef(new PrecisionHudTrackerPool());
  const markerSig = useMemo(
    () => (markers || []).map((m) => `${m.id}:${m.updated_at_ms || 0}:${m.expires_at_ms || 0}`).join('|'),
    [markers],
  );

  useEffect(() => {
    if (!enabled || !videoRef.current) {
      setLiveMarkers(markers || []);
      return undefined;
    }

    let raf = 0;
    let lastSent = 0;
    let lastDebugLog = 0;
    let stopped = false;
    const video = videoRef.current;
    const container = video.closest('.ra-camera-container');

    poolRef.current.sync(markers || [], video);

    const tick = () => {
      if (stopped) return;
      const now = performance.now();
      const tracked = poolRef.current.update(video);
      const source = tracked.length ? tracked : (markers || []);
      const displayMarkers = source.map((marker) => ({
        ...marker,
        display_geometry: mapGeometryToDisplay(marker, video, container, mirrored),
        model_display_geometry: marker.model_geometry
          ? mapGeometryToDisplay({ geometry: marker.model_geometry }, video, container, mirrored)
          : null,
        display_contour: mapContourToDisplay(transformContour(marker), video, container, mirrored),
        model_display_contour: mapContourToDisplay(marker.model_contour || marker.mask_contour, video, container, mirrored),
      }));
      setLiveMarkers(displayMarkers);

      if (now - lastDebugLog >= 500 && tracked.length) {
        lastDebugLog = now;
        tracked.forEach((marker) => {
          const debug = marker.debug || {};
          console.log(
            `[HUD_TRACK] ${marker.id} ${marker.tracking_status} conf=${(marker.confidence || 0).toFixed(2)} score=${debug.score ?? '-'} raw=${debug.raw_confidence ?? '-'} lost=${debug.lost_frames ?? 0} move=${debug.movement_px ?? '-'}px v=${debug.velocity_px ? `${debug.velocity_px.x},${debug.velocity_px.y}` : '-'}`,
          );
        });
      }

      if (now - lastSent >= SEND_INTERVAL_MS && wsRef.current?.readyState === WebSocket.OPEN && tracked.length) {
        lastSent = now;
        wsRef.current.send(JSON.stringify({
          type: 'hud.local_track',
          tracks: tracked.map((marker) => ({
            id: marker.id,
            geometry: marker.geometry,
            confidence: marker.confidence,
            tracking_status: marker.tracking_status,
            debug: marker.debug,
          })),
        }));
      }
      raf = requestAnimationFrame(tick);
    };

    raf = requestAnimationFrame(tick);
    return () => {
      stopped = true;
      if (raf) cancelAnimationFrame(raf);
    };
  }, [enabled, markerSig, markers, mirrored, videoRef, wsRef]);

  return liveMarkers;
}
