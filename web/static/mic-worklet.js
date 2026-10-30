// Runs on the browser's audio thread (AudioWorklet). Turns the microphone
// signal (usually 48 kHz float) into 16 kHz int16 PCM in 20 ms blocks, the
// format Deepgram streaming STT expects.
//
// The low-pass filter before downsampling is not optional: without it,
// anything between 8 kHz and the mic's Nyquist frequency folds back into
// the speech band as aliasing. Same design as agent/audio_io.py
// (_lowpass_kernel + Downsampler): a 63-tap Hamming-windowed sinc with its
// cutoff at the output Nyquist, and filter state carried across blocks so
// there is no click at block seams. Generalised to non-integer ratios
// (44.1 kHz mics) by linearly interpolating between filtered samples.

const OUT_RATE = 16000;
const BLOCK_SAMPLES = 320; // 20 ms at 16 kHz
const TAPS = 63;

function lowpassKernel(ratio, taps) {
  const h = new Float64Array(taps);
  let sum = 0;
  for (let i = 0; i < taps; i++) {
    const n = i - (taps - 1) / 2;
    const x = n / ratio;
    const sinc = x === 0 ? 1 : Math.sin(Math.PI * x) / (Math.PI * x);
    const hamming = 0.54 - 0.46 * Math.cos((2 * Math.PI * i) / (taps - 1));
    h[i] = sinc * hamming;
    sum += h[i];
  }
  for (let i = 0; i < taps; i++) h[i] /= sum;
  return h;
}

class Resampler {
  constructor(inRate, outRate = OUT_RATE, taps = TAPS) {
    this.step = inRate / outRate;
    this.taps = taps;
    this.h = lowpassKernel(this.step, taps);
    // History starts as taps-1 zeros, like Downsampler.tail.
    this.x = new Float64Array(taps - 1);
    this.pos = taps - 1; // index in x of the next output sample
    this.block = new Int16Array(BLOCK_SAMPLES);
    this.fill = 0;
  }

  // Filtered value centred on the filter window that ends at x[i].
  filtered(i) {
    const h = this.h, x = this.x, start = i - (this.taps - 1);
    let acc = 0;
    for (let j = 0; j < this.taps; j++) acc += h[j] * x[start + j];
    return acc;
  }

  // Feed float samples in [-1, 1]; calls onBlock(Int16Array) per 20 ms.
  push(samples, onBlock) {
    const merged = new Float64Array(this.x.length + samples.length);
    merged.set(this.x);
    merged.set(samples, this.x.length);
    this.x = merged;

    while (Math.floor(this.pos) + 1 < this.x.length) {
      const i = Math.floor(this.pos);
      const frac = this.pos - i;
      let y = this.filtered(i);
      if (frac > 1e-9) y += frac * (this.filtered(i + 1) - y);
      const s = Math.max(-1, Math.min(1, y));
      this.block[this.fill++] = Math.round(s * 32767);
      if (this.fill === BLOCK_SAMPLES) {
        onBlock(this.block);
        this.block = new Int16Array(BLOCK_SAMPLES);
        this.fill = 0;
      }
      this.pos += this.step;
    }

    // Keep only the history the next output sample still needs.
    const keepFrom = Math.floor(this.pos) - (this.taps - 1);
    if (keepFrom > 0) {
      this.x = this.x.slice(keepFrom);
      this.pos -= keepFrom;
    }
  }
}

function rms(block) {
  let acc = 0;
  for (let i = 0; i < block.length; i++) acc += block[i] * block[i];
  return Math.sqrt(acc / block.length);
}

if (typeof registerProcessor === "function") {
  registerProcessor(
    "mic-downsampler",
    class extends AudioWorkletProcessor {
      constructor() {
        super();
        // `sampleRate` is a global inside the worklet: the context's rate.
        this.rs = new Resampler(sampleRate);
      }

      process(inputs) {
        const chans = inputs[0];
        if (!chans || chans.length === 0) return true;
        let mono = chans[0];
        if (chans.length > 1) {
          mono = new Float32Array(chans[0].length);
          for (const c of chans) for (let i = 0; i < c.length; i++) mono[i] += c[i] / chans.length;
        }
        this.rs.push(mono, (block) => {
          // Energy is computed here so the main thread's VAD sees exactly
          // what the server's VAD will see.
          this.port.postMessage({ pcm: block.buffer, rms: rms(block) }, [block.buffer]);
        });
        return true;
      }
    }
  );
}

if (typeof module !== "undefined") module.exports = { Resampler, lowpassKernel, rms, OUT_RATE, BLOCK_SAMPLES };
