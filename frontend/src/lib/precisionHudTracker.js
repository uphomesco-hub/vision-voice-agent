const TRACK_WIDTH = 384;
const MAX_TEMPLATE_SIDE = 112;
const MIN_TEMPLATE_SIDE = 18;
const SEARCH_RADIUS = 54;
const WIDE_SEARCH_RADIUS = 128;
const SCALES = [0.78, 0.9, 1, 1.08, 1.18];
const SCORE_CONFIDENCE_DENOMINATOR = 64;

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function normalizeGeometry(geometry) {
  if (!geometry) return null;
  const width = clamp(Number(geometry.width || 0.04), 0.015, 0.8);
  const height = clamp(Number(geometry.height || 0.04), 0.015, 0.8);
  const x = clamp(Number(geometry.x || 0), 0, 1 - width);
  const y = clamp(Number(geometry.y || 0), 0, 1 - height);
  return { type: geometry.type || 'box', x, y, width, height };
}

function makeCanvas(width, height) {
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  return canvas;
}

function grayscale(imageData) {
  const data = imageData.data;
  const gray = new Uint8ClampedArray(imageData.width * imageData.height);
  for (let i = 0, j = 0; i < data.length; i += 4, j += 1) {
    gray[j] = (data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114) | 0;
  }
  return gray;
}

function captureVideoGray(video) {
  if (!video?.videoWidth || !video?.videoHeight) return null;
  const width = TRACK_WIDTH;
  const height = Math.max(1, Math.round((video.videoHeight / video.videoWidth) * width));
  const canvas = captureVideoGray.canvas || (captureVideoGray.canvas = makeCanvas(width, height));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(video, 0, 0, width, height);
  return {
    width,
    height,
    gray: grayscale(ctx.getImageData(0, 0, width, height)),
  };
}

function cropTemplate(frame, box) {
  const x = clamp(Math.round(box.x), 0, frame.width - 1);
  const y = clamp(Math.round(box.y), 0, frame.height - 1);
  const width = clamp(Math.round(box.width), MIN_TEMPLATE_SIDE, Math.min(MAX_TEMPLATE_SIDE, frame.width - x));
  const height = clamp(Math.round(box.height), MIN_TEMPLATE_SIDE, Math.min(MAX_TEMPLATE_SIDE, frame.height - y));
  const data = new Uint8ClampedArray(width * height);
  for (let row = 0; row < height; row += 1) {
    const src = (y + row) * frame.width + x;
    const dst = row * width;
    data.set(frame.gray.subarray(src, src + width), dst);
  }
  return { width, height, data };
}

function scaledTemplate(template, scale) {
  if (scale === 1) return template;
  const width = clamp(Math.round(template.width * scale), MIN_TEMPLATE_SIDE, MAX_TEMPLATE_SIDE);
  const height = clamp(Math.round(template.height * scale), MIN_TEMPLATE_SIDE, MAX_TEMPLATE_SIDE);
  const data = new Uint8ClampedArray(width * height);
  for (let y = 0; y < height; y += 1) {
    const sy = clamp(Math.round((y / height) * template.height), 0, template.height - 1);
    for (let x = 0; x < width; x += 1) {
      const sx = clamp(Math.round((x / width) * template.width), 0, template.width - 1);
      data[y * width + x] = template.data[sy * template.width + sx];
    }
  }
  return { width, height, data };
}

function averageAbsDiff(frame, template, x, y) {
  let total = 0;
  let count = 0;
  for (let row = 0; row < template.height; row += 2) {
    const src = (y + row) * frame.width + x;
    const dst = row * template.width;
    for (let col = 0; col < template.width; col += 2) {
      total += Math.abs(frame.gray[src + col] - template.data[dst + col]);
      count += 1;
    }
  }
  return total / Math.max(1, count);
}

function refineBestMatch(frame, template, seed) {
  let best = seed;
  const baseX = Math.round(seed.x);
  const baseY = Math.round(seed.y);
  for (let y = baseY - 3; y <= baseY + 3; y += 1) {
    if (y < 0 || y > frame.height - template.height) continue;
    for (let x = baseX - 3; x <= baseX + 3; x += 1) {
      if (x < 0 || x > frame.width - template.width) continue;
      const score = averageAbsDiff(frame, template, x, y);
      if (score < best.score) best = { x, y, width: template.width, height: template.height, score };
    }
  }
  return best;
}

function findBestMatch(frame, templates, predicted, lostFrames) {
  const radius = lostFrames > 2 ? WIDE_SEARCH_RADIUS : SEARCH_RADIUS;
  const step = lostFrames > 2 ? 6 : 3;
  let best = null;

  for (const template of templates) {
    const minX = clamp(Math.round(predicted.x - radius), 0, frame.width - template.width);
    const maxX = clamp(Math.round(predicted.x + radius), 0, frame.width - template.width);
    const minY = clamp(Math.round(predicted.y - radius), 0, frame.height - template.height);
    const maxY = clamp(Math.round(predicted.y + radius), 0, frame.height - template.height);
    for (let y = minY; y <= maxY; y += step) {
      for (let x = minX; x <= maxX; x += step) {
        const score = averageAbsDiff(frame, template, x, y);
        if (!best || score < best.score) {
          best = { x, y, width: template.width, height: template.height, score, template };
        }
      }
    }
  }
  if (!best) return null;
  return refineBestMatch(frame, best.template, best);
}

function imageBoxFromGeometry(frame, geometry) {
  const g = normalizeGeometry(geometry);
  if (!g) return null;
  const cx = (g.x + g.width / 2) * frame.width;
  const cy = (g.y + g.height / 2) * frame.height;
  let width = Math.max(MIN_TEMPLATE_SIDE, g.width * frame.width * 1.05);
  let height = Math.max(MIN_TEMPLATE_SIDE, g.height * frame.height * 1.05);
  const maxSide = Math.max(width, height);
  if (maxSide > MAX_TEMPLATE_SIDE) {
    const scale = MAX_TEMPLATE_SIDE / maxSide;
    width *= scale;
    height *= scale;
  }
  return {
    x: clamp(cx - width / 2, 0, frame.width - width),
    y: clamp(cy - height / 2, 0, frame.height - height),
    width,
    height,
  };
}

export function mapGeometryToDisplay(marker, video, container, mirrored = false) {
  const g = normalizeGeometry(marker.geometry);
  if (!g || !video?.videoWidth || !video?.videoHeight || !container) return g;
  const rect = container.getBoundingClientRect();
  const scale = Math.max(rect.width / video.videoWidth, rect.height / video.videoHeight);
  const renderedW = video.videoWidth * scale;
  const renderedH = video.videoHeight * scale;
  const offsetX = (rect.width - renderedW) / 2;
  const offsetY = (rect.height - renderedH) / 2;
  const mapped = {
    type: g.type,
    x: (offsetX + g.x * video.videoWidth * scale) / rect.width,
    y: (offsetY + g.y * video.videoHeight * scale) / rect.height,
    width: (g.width * video.videoWidth * scale) / rect.width,
    height: (g.height * video.videoHeight * scale) / rect.height,
  };
  if (mirrored) mapped.x = 1 - mapped.x - mapped.width;
  mapped.x = clamp(mapped.x, -0.2, 1.2);
  mapped.y = clamp(mapped.y, -0.2, 1.2);
  return mapped;
}

export function transformContour(marker) {
  const contour = marker.mask_contour || marker.model_contour;
  const model = normalizeGeometry(marker.model_geometry || marker.geometry);
  const current = normalizeGeometry(marker.geometry);
  if (!Array.isArray(contour) || contour.length < 3 || !model || !current) return [];
  return contour.map((point) => {
    const rx = model.width ? (Number(point.x) - model.x) / model.width : 0.5;
    const ry = model.height ? (Number(point.y) - model.y) / model.height : 0.5;
    return {
      x: clamp(current.x + rx * current.width, 0, 1),
      y: clamp(current.y + ry * current.height, 0, 1),
    };
  });
}

export function mapContourToDisplay(contour, video, container, mirrored = false) {
  if (!Array.isArray(contour) || !video?.videoWidth || !video?.videoHeight || !container) return [];
  const rect = container.getBoundingClientRect();
  const scale = Math.max(rect.width / video.videoWidth, rect.height / video.videoHeight);
  const renderedW = video.videoWidth * scale;
  const renderedH = video.videoHeight * scale;
  const offsetX = (rect.width - renderedW) / 2;
  const offsetY = (rect.height - renderedH) / 2;
  return contour.map((point) => {
    let x = (offsetX + Number(point.x) * video.videoWidth * scale) / rect.width;
    const y = (offsetY + Number(point.y) * video.videoHeight * scale) / rect.height;
    if (mirrored) x = 1 - x;
    return { x: clamp(x, -0.2, 1.2), y: clamp(y, -0.2, 1.2) };
  });
}

class PrecisionTracker {
  constructor(marker, frame) {
    this.id = marker.id;
    this.marker = marker;
    this.lostFrames = 0;
    this.vx = 0;
    this.vy = 0;
    this.geometry = normalizeGeometry(marker.geometry);
    this.confidence = Number(marker.confidence || 0.4);
    this.status = marker.tracking_status || 'seeded';
    this.templates = [];
    this.lockedFrames = 0;
    this.lastDebug = {};
    this.seedGeometry = this.geometry ? { ...this.geometry } : null;
    this.seed(frame, marker);
  }

  seed(frame, marker) {
    this.marker = marker;
    this.geometry = normalizeGeometry(marker.geometry) || this.geometry;
    this.seedGeometry = normalizeGeometry(marker.model_geometry || marker.geometry) || this.geometry;
    if (!frame || !this.geometry) return;
    const box = imageBoxFromGeometry(frame, this.geometry);
    if (!box) return;
    const base = cropTemplate(frame, box);
    this.templates = SCALES.map((scale) => scaledTemplate(base, scale));
    this.lostFrames = 0;
    this.lockedFrames = 0;
    this.status = 'locked';
    this.lastDebug = {
      event: 'seed',
      template_count: this.templates.length,
      template_px: `${Math.round(base.width)}x${Math.round(base.height)}`,
      frame_px: `${frame.width}x${frame.height}`,
    };
  }

  updateMarker(marker, frame) {
    const oldGeometry = JSON.stringify(this.marker.geometry);
    this.marker = marker;
    if (JSON.stringify(marker.geometry) !== oldGeometry && frame) this.seed(frame, marker);
  }

  update(frame) {
    if (!frame || !this.geometry || !this.templates.length) {
      return { ...this.marker, geometry: this.geometry, confidence: this.confidence, tracking_status: this.status, debug: this.lastDebug };
    }

    const startedAt = performance.now();
    const px = {
      x: (this.geometry.x * frame.width) + this.vx,
      y: (this.geometry.y * frame.height) + this.vy,
    };
    const best = findBestMatch(frame, this.templates, px, this.lostFrames);
    if (!best) {
      this.lastDebug = {
        ...this.lastDebug,
        score: null,
        lost_frames: this.lostFrames,
        search_radius_px: this.lostFrames > 2 ? WIDE_SEARCH_RADIUS : SEARCH_RADIUS,
        frame_px: `${frame.width}x${frame.height}`,
      };
      return { ...this.marker, geometry: this.geometry, confidence: this.confidence, tracking_status: 'lost', debug: this.lastDebug };
    }

    const rawConfidence = clamp(1 - (best.score / SCORE_CONFIDENCE_DENOMINATOR), 0.05, 0.99);
    if (rawConfidence < 0.2) {
      this.lostFrames += 1;
      this.confidence = Math.max(0.05, this.confidence * 0.76);
      this.status = this.lostFrames > 14 ? 'lost' : this.lostFrames > 3 ? 'reacquiring' : 'low_confidence';
      this.lastDebug = {
        ...this.lastDebug,
        score: Number(best.score.toFixed(2)),
        raw_confidence: Number(rawConfidence.toFixed(3)),
        lost_frames: this.lostFrames,
        search_radius_px: this.lostFrames > 2 ? WIDE_SEARCH_RADIUS : SEARCH_RADIUS,
        update_ms: Number((performance.now() - startedAt).toFixed(2)),
      };
      return { ...this.marker, geometry: this.geometry, confidence: this.confidence, tracking_status: this.status, debug: this.lastDebug };
    }

    const newGeometry = {
      type: 'box',
      x: clamp(best.x / frame.width, 0, 0.99),
      y: clamp(best.y / frame.height, 0, 0.99),
      width: clamp(best.width / frame.width, 0.015, 0.8),
      height: clamp(best.height / frame.height, 0.015, 0.8),
    };
    const prev = this.geometry;
    const prevCx = prev.x + prev.width / 2;
    const prevCy = prev.y + prev.height / 2;
    const rawCx = newGeometry.x + newGeometry.width / 2;
    const rawCy = newGeometry.y + newGeometry.height / 2;
    const dxPx = (rawCx - prevCx) * frame.width;
    const dyPx = (rawCy - prevCy) * frame.height;
    const movePx = Math.hypot(dxPx, dyPx);
    const deadbandPx = rawConfidence > 0.62 ? 1.2 : 2.2;
    const positionAlpha = movePx < deadbandPx ? 0 : clamp((movePx / 42) + 0.22, 0.28, 0.78);
    const sizeAlpha = rawConfidence > 0.62 ? 0.08 : 0.04;
    const nextCx = positionAlpha === 0 ? prevCx : (prevCx * (1 - positionAlpha)) + (rawCx * positionAlpha);
    const nextCy = positionAlpha === 0 ? prevCy : (prevCy * (1 - positionAlpha)) + (rawCy * positionAlpha);
    const boundedW = clamp(newGeometry.width, prev.width * 0.82, prev.width * 1.16);
    const boundedH = clamp(newGeometry.height, prev.height * 0.82, prev.height * 1.16);
    const nextW = clamp((prev.width * (1 - sizeAlpha)) + (boundedW * sizeAlpha), 0.015, 0.8);
    const nextH = clamp((prev.height * (1 - sizeAlpha)) + (boundedH * sizeAlpha), 0.015, 0.8);
    this.geometry = {
      type: 'box',
      x: clamp(nextCx - nextW / 2, 0, 1 - nextW),
      y: clamp(nextCy - nextH / 2, 0, 1 - nextH),
      width: nextW,
      height: nextH,
    };
    this.vx = clamp(((this.geometry.x - prev.x) * frame.width * 0.72) + (this.vx * 0.28), -58, 58);
    this.vy = clamp(((this.geometry.y - prev.y) * frame.height * 0.72) + (this.vy * 0.28), -58, 58);
    this.confidence = (this.confidence * 0.55) + (rawConfidence * 0.45);
    this.lostFrames = 0;
    this.lockedFrames += 1;
    this.status = this.confidence > 0.5 ? 'locked' : 'tracking';
    if (this.confidence > 0.66 && this.lockedFrames % 18 === 0) {
      const refreshBox = {
        x: clamp(this.geometry.x * frame.width, 0, frame.width - 1),
        y: clamp(this.geometry.y * frame.height, 0, frame.height - 1),
        width: clamp(this.geometry.width * frame.width, MIN_TEMPLATE_SIDE, MAX_TEMPLATE_SIDE),
        height: clamp(this.geometry.height * frame.height, MIN_TEMPLATE_SIDE, MAX_TEMPLATE_SIDE),
      };
      const base = cropTemplate(frame, refreshBox);
      this.templates = SCALES.map((scale) => scaledTemplate(base, scale));
    }
    this.lastDebug = {
      score: Number(best.score.toFixed(2)),
      raw_confidence: Number(rawConfidence.toFixed(3)),
      confidence: Number(this.confidence.toFixed(3)),
      lost_frames: this.lostFrames,
      locked_frames: this.lockedFrames,
      movement_px: Number(movePx.toFixed(2)),
      position_alpha: Number(positionAlpha.toFixed(2)),
      size_alpha: Number(sizeAlpha.toFixed(2)),
      velocity_px: { x: Number(this.vx.toFixed(2)), y: Number(this.vy.toFixed(2)) },
      search_radius_px: this.lostFrames > 2 ? WIDE_SEARCH_RADIUS : SEARCH_RADIUS,
      match_px: { x: best.x, y: best.y, width: best.width, height: best.height },
      frame_px: `${frame.width}x${frame.height}`,
      update_ms: Number((performance.now() - startedAt).toFixed(2)),
    };
    return {
      ...this.marker,
      geometry: this.geometry,
      model_geometry: this.marker.model_geometry || this.seedGeometry,
      confidence: this.confidence,
      tracking_status: this.status,
      debug: this.lastDebug,
    };
  }
}

export class PrecisionHudTrackerPool {
  constructor() {
    this.trackers = new Map();
  }

  sync(markers, video) {
    const frame = captureVideoGray(video);
    const ids = new Set((markers || []).map((marker) => marker.id));
    for (const id of Array.from(this.trackers.keys())) {
      if (!ids.has(id)) this.trackers.delete(id);
    }
    for (const marker of markers || []) {
      const existing = this.trackers.get(marker.id);
      if (existing) existing.updateMarker(marker, frame);
      else if (marker.geometry) this.trackers.set(marker.id, new PrecisionTracker(marker, frame));
    }
  }

  update(video) {
    const frame = captureVideoGray(video);
    if (!frame) return [];
    return Array.from(this.trackers.values()).map((tracker) => tracker.update(frame));
  }
}
