class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = options.processorOptions || {};
    this.targetSampleRate = opts.targetSampleRate || 16000;
    this.frameSamples = Math.max(160, Math.floor(this.targetSampleRate * ((opts.frameMs || 20) / 1000)));
    this.inputSampleRate = sampleRate;
    this.ratio = this.inputSampleRate / this.targetSampleRate;
    this.buffer = [];
    this.resampleCursor = 0;
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input || input.length === 0) return true;

    while (this.resampleCursor < input.length) {
      const idx = Math.floor(this.resampleCursor);
      const nextIdx = Math.min(idx + 1, input.length - 1);
      const frac = this.resampleCursor - idx;
      const sample = input[idx] + (input[nextIdx] - input[idx]) * frac;
      this.buffer.push(sample);
      this.resampleCursor += this.ratio;

      if (this.buffer.length >= this.frameSamples) {
        this.flushFrame();
      }
    }

    this.resampleCursor -= input.length;
    return true;
  }

  flushFrame() {
    const pcm = new Int16Array(this.frameSamples);
    let sumSquares = 0;
    for (let i = 0; i < this.frameSamples; i += 1) {
      const sample = Math.max(-1, Math.min(1, this.buffer[i] || 0));
      sumSquares += sample * sample;
      pcm[i] = sample < 0 ? sample * 32768 : sample * 32767;
    }
    this.buffer = this.buffer.slice(this.frameSamples);
    this.port.postMessage({
      pcm,
      rms: Math.sqrt(sumSquares / this.frameSamples),
      capturedAt: currentTime,
    });
  }
}

registerProcessor('pcm-capture-processor', PcmCaptureProcessor);
