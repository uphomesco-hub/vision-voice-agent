class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = options.processorOptions || {};
    this.chunkSize = opts.chunkSize || 480;
    this.sampleRateHz = opts.sampleRate || sampleRate || 24000;
    this.vadThreshold = opts.vadThreshold || 0.012;
    this.silenceLimitMs = opts.silenceMs || 360;
    this.minSpeechMs = opts.minSpeechMs || 160;
    this.chunk = new Float32Array(this.chunkSize);
    this.offset = 0;
    this.isSpeaking = false;
    this.silenceMs = 0;
    this.speechMs = 0;
    this.stopped = false;

    this.port.onmessage = (event) => {
      if (event.data?.type === 'stop') {
        this.stopped = true;
      }
    };
  }

  process(inputs) {
    if (this.stopped) return false;
    const input = inputs[0]?.[0];
    if (!input) return true;

    for (let i = 0; i < input.length; i += 1) {
      this.chunk[this.offset] = input[i];
      this.offset += 1;
      if (this.offset >= this.chunkSize) {
        this.flushChunk();
        this.offset = 0;
      }
    }
    return true;
  }

  flushChunk() {
    let sumSquares = 0;
    const pcm = new Int16Array(this.chunkSize);
    for (let i = 0; i < this.chunkSize; i += 1) {
      const sample = Math.max(-1, Math.min(1, this.chunk[i] || 0));
      sumSquares += sample * sample;
      pcm[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      this.chunk[i] = 0;
    }

    const rms = Math.sqrt(sumSquares / this.chunkSize);
    const chunkMs = (this.chunkSize / this.sampleRateHz) * 1000;

    if (rms >= this.vadThreshold) {
      if (!this.isSpeaking) {
        this.isSpeaking = true;
        this.speechMs = 0;
        this.port.postMessage({ type: 'speech_start' });
      }
      this.silenceMs = 0;
      this.speechMs += chunkMs;
    } else if (this.isSpeaking) {
      this.silenceMs += chunkMs;
      if (this.silenceMs >= this.silenceLimitMs && this.speechMs >= this.minSpeechMs) {
        this.isSpeaking = false;
        this.silenceMs = 0;
        this.speechMs = 0;
        this.port.postMessage({ type: 'speech_end' });
      }
    }

    this.port.postMessage({ type: 'audio', pcm: pcm.buffer, rms }, [pcm.buffer]);
  }
}

registerProcessor('pcm-capture-processor', PcmCaptureProcessor);
