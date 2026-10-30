// Unit test for the browser downsampler in static/mic-worklet.js.
//
//     node web/test_resampler.js
//
// Checks that speech-band tones pass, that tones above 8 kHz are removed
// instead of aliasing into the speech band, and that feeding audio in the
// worklet's 128-sample quanta gives the same output as one big buffer.
const { Resampler, OUT_RATE } = require("./static/mic-worklet.js");

function tone(freq, rate, seconds, amp = 0.5) {
  const n = Math.round(rate * seconds);
  const x = new Float32Array(n);
  for (let i = 0; i < n; i++) x[i] = amp * Math.sin((2 * Math.PI * freq * i) / rate);
  return x;
}

function run(rate, signal, quantum) {
  const rs = new Resampler(rate);
  const out = [];
  for (let i = 0; i < signal.length; i += quantum) {
    rs.push(signal.subarray(i, i + quantum), (b) => out.push(...b));
  }
  return Int16Array.from(out);
}

function rmsOf(a, skip = 200) {
  let acc = 0;
  for (let i = skip; i < a.length; i++) acc += a[i] * a[i];
  return Math.sqrt(acc / (a.length - skip));
}

let failed = 0;
function check(name, ok, detail) {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}  ${detail}`);
  if (!ok) failed++;
}

const inRms = (0.5 / Math.SQRT2) * 32767;
for (const rate of [48000, 44100]) {
  const pass = rmsOf(run(rate, tone(1000, rate, 1), 128));
  check(`${rate} Hz: 1 kHz passes`, Math.abs(pass / inRms - 1) < 0.02,
        `(output/input level ${(pass / inRms).toFixed(3)})`);

  for (const f of [9000, 12000, 18000]) {
    const leak = rmsOf(run(rate, tone(f, rate, 1), 128)) / inRms;
    const db = 20 * Math.log10(Math.max(leak, 1e-9));
    // 9 kHz sits in the filter's transition band; above that, demand a
    // strong stopband.
    const limit = f === 9000 ? -6 : -40;
    check(`${rate} Hz: ${f} Hz removed`, db < limit, `(${db.toFixed(1)} dB, limit ${limit} dB)`);
  }

  const sig = tone(440, rate, 0.5);
  const a = run(rate, sig, 128), b = run(rate, sig, sig.length);
  const same = a.length === b.length && a.every((v, i) => v === b[i]);
  check(`${rate} Hz: 128-sample blocks == one buffer`, same, `(${a.length} vs ${b.length} samples)`);

  const expect = Math.floor((rate * 1 - 62) / (rate / OUT_RATE) / 320) * 320;
  const got = run(rate, tone(1000, rate, 1), 128).length;
  check(`${rate} Hz: output rate`, Math.abs(got - expect) <= 320, `(${got} samples from 1 s, ~${OUT_RATE} expected)`);
}

process.exit(failed ? 1 : 0);
