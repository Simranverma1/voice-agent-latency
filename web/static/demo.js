"use strict";
// Browser side of the live demo. Captures the mic through an AudioWorklet
// (mic-worklet.js: 48 kHz -> 16 kHz with an anti-aliasing filter), streams
// it to the server over a WebSocket, plays the reply audio back gaplessly
// with Web Audio, and shows both the server's and the browser's latency.

const PLAYBACK_LEAD_S = 0.1;      // jitter cushion before the first reply frame
const TTS_RATE = 24000;
const WAKE_POLL_MS = 3000;
const WAKE_GIVE_UP_MS = 120000;
const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 15000];
// Mic health check. A muted or wrong input device (e.g. an unplugged headset
// still set as Chrome's default) delivers digital silence, and the page would
// otherwise just sit at "Listening" forever. Below MIC_DEAD_RMS counts as no
// signal at all: real rooms, even after noise suppression, sit well above it.
const MIC_DEAD_RMS = 4;
const MIC_SILENT_WARN_MS = 5000;
// Server error codes after which reconnecting would not help.
const TERMINAL_ERRORS = new Set(["bad_code", "busy", "turn_limit", "idle", "server_misconfigured"]);

const SEGMENTS = [
  ["1_eot_detection", "End-of-speech detection", "--seg1"],
  ["2_stt_finalize", "Transcript finalize", "--seg2"],
  ["3_llm_dispatch", "LLM dispatch", "--seg3"],
  ["4_llm_ttft", "LLM first token", "--seg4"],
  ["5_text_buffering", "Text buffering", "--seg5"],
  ["6_tts_ttfa", "Speech synthesis first audio", "--seg6"],
  ["7_audio_to_speaker", "First audio sent to you", "--seg7"],
];

const $ = (id) => document.getElementById(id);

let ctx = null, micStream = null, micNode = null, micSource = null;
let ws = null;
let state = "idle";
let userStopped = true, terminalError = null, reconnectAttempt = 0, reconnectTimer = null;
let serverListening = false;
let playEnd = 0;           // AudioContext time when queued reply audio finishes
let turn = null;           // browser-side record of the current turn
let turnCount = 0;
let testMode = false;      // set from /health; only true when the server has TEST_MODE=1
let startToken = 0;
let micLabel = "";
let lastMicSound = 0;      // performance.now() of the last block with any signal

// The browser has no speech detector of its own: the server says how many
// bytes into this listen your speech ended (transcript.speech_end_byte), and
// sendLog says when this browser sent that byte. Both sides then time from
// the same decision, so browser − server is network plus playback only. (A
// separate browser VAD used to do this and missed every turn of a session.)
function newTurn() {
  return { sentBytes: 0, sendLog: [], speechEnd: null, firstSound: null, audioEnded: false,
           text: "", pendingText: [], fullReply: null };
}

function sentAt(t, byte) {
  if (byte == null) return null;
  for (const [n, time] of t.sendLog) if (n >= byte) return time;
  return null;
}

// ------------------------------------------------------------------- UI
function setState(s, text) {
  state = s;
  const labels = {
    idle: "Not connected", waking: "Waking up the server…", connecting: "Connecting…",
    ready: "Connected", listening: "Listening — go ahead", thinking: "Thinking…",
    speaking: "Speaking…", reconnecting: "Connection lost — reconnecting…", stopped: "Stopped",
    error: "Stopped",
  };
  $("status").textContent = text || labels[s] || s;
  $("dot").className = "dot " + ({ listening: "listening", thinking: "thinking", speaking: "speaking", error: "error", reconnecting: "error" }[s] || "");
  $("talk").textContent = userStopped ? "Start talking" : "Stop";
  $("test-play").disabled = s !== "listening";
}

function notice(msg, kind = "info") {
  const n = $("notice");
  if (!msg) { n.className = "notice"; n.textContent = ""; return; }
  n.className = "notice " + kind;
  n.textContent = msg;
}

function bubble(who, text) {
  const empty = $("log-empty");
  if (empty) empty.remove();
  const b = document.createElement("div");
  b.className = "bubble " + who;
  b.textContent = text;
  $("log").appendChild(b);
  $("log").scrollTop = $("log").scrollHeight;
  return b;
}

const fmt = (ms) => (ms == null ? "—" : `${Math.round(ms)} ms`);

function renderMetrics(ev) {
  const m = ev.metrics || {}, cp = ev.critical_path || {};
  const server = m.e2e_ms;
  const browser = turn && turn.firstSound != null && turn.speechEnd != null
    ? turn.firstSound - turn.speechEnd : null;
  $("k-server").textContent = fmt(server);
  $("k-browser").textContent = fmt(browser);
  $("k-diff").textContent = browser != null && server != null
    ? `Browser − server = ${Math.round(browser - server)} ms: the internet round trip to the server plus audio buffering in your browser.`
    : (ev.errors && ev.errors.length ? "This turn didn't complete, so there's no end-to-end time." : "");

  const bar = $("bar"), legend = $("legend");
  bar.innerHTML = ""; legend.innerHTML = "";
  const total = SEGMENTS.reduce((a, [k]) => a + Math.max(0, cp[k] || 0), 0);
  for (const [k, label, color] of SEGMENTS) {
    const v = cp[k];
    if (v == null) continue;
    const d = document.createElement("div");
    d.style.background = `var(${color})`;
    d.style.width = total ? `${(Math.max(0, v) / total) * 100}%` : "0";
    d.title = `${label}: ${Math.round(v)} ms`;
    bar.appendChild(d);
    legend.insertAdjacentHTML("beforeend",
      `<span><span class="sw" style="background:var(${color})"></span>${label}</span><span class="num">${Math.round(v)} ms</span>`);
  }

  if (ev.errors && ev.errors.length) return;
  turnCount += 1;
  const row = document.createElement("tr");
  const said = (turn && turn.text) || "";
  row.innerHTML = `<td>${turnCount}</td><td></td><td class="num">${fmt(browser)}</td>` +
    `<td class="num">${fmt(server)}</td><td class="num">${browser != null && server != null ? fmt(browser - server) : "—"}</td>`;
  row.children[1].textContent = said.length > 48 ? said.slice(0, 47) + "…" : said;
  $("history").prepend(row);
}

// ----------------------------------------------------------------- audio
async function startAudio() {
  if (ctx) return;
  ctx = new AudioContext({ latencyHint: "interactive" });
  await ctx.audioWorklet.addModule("/static/mic-worklet.js");
  let src;
  if (testMode) {
    // TEST_MODE: no microphone. A constant zero signal stands in for a
    // quiet room so the worklet produces blocks continuously, exactly as an
    // open mic does; playTestPrompt() adds a recording on top.
    src = ctx.createConstantSource();
    src.offset.value = 0;
    src.start();
  } else {
    micStream = await openMic();
    src = micSource = ctx.createMediaStreamSource(micStream);
    showMics();
  }
  lastMicSound = performance.now();
  micNode = new AudioWorkletNode(ctx, "mic-downsampler");
  // A worklet only runs while its output is pulled; route it to the
  // speakers through a muted gain so it's pulled without being heard.
  const mute = ctx.createGain();
  mute.gain.value = 0;
  src.connect(micNode).connect(mute).connect(ctx.destination);
  micNode.port.onmessage = (e) => onMicBlock(e.data);
  // Don't await: if the browser's autoplay policy holds the context
  // suspended (no user gesture yet), resume() never settles and Start would
  // hang. The click listener below retries on the next interaction.
  if (ctx.state === "suspended") ctx.resume().catch(() => {});
}

document.addEventListener("click", () => { if (ctx && ctx.state === "suspended") ctx.resume().catch(() => {}); });

// ------------------------------------------------------------ microphone
const MIC_CONSTRAINTS = { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true };

function savedMic() {
  try { return localStorage.getItem("va_mic") || ""; } catch (_) { return ""; }
}

function getMic(deviceId) {
  return navigator.mediaDevices.getUserMedia({
    audio: deviceId ? { ...MIC_CONSTRAINTS, deviceId: { exact: deviceId } } : MIC_CONSTRAINTS,
  });
}

// Opens the mic chosen in the list, else the browser's default, else any
// input that works. Windows sometimes refuses to start a device ("Could not
// start audio source": another app holds it, or a Bluetooth headset is
// switching modes); trying the other inputs turns that dead end into a
// working mic, and the list shows which one was used.
async function openMic() {
  const preferred = savedMic();
  let firstErr = null;
  for (const id of preferred ? [preferred, ""] : [""]) {
    try { return await getMic(id); } catch (e) {
      if (e.name === "NotAllowedError") throw e;
      firstErr = firstErr || e;
    }
  }
  await new Promise((r) => setTimeout(r, 400));   // a just-released device can need a moment
  const inputs = (await navigator.mediaDevices.enumerateDevices())
    .filter((d) => d.kind === "audioinput" && d.deviceId && d.deviceId !== "communications");
  for (const d of inputs) {
    try { return await getMic(d.deviceId); } catch (e) {
      if (e.name === "NotAllowedError") throw e;
    }
  }
  throw firstErr;
}

// Fills the microphone list. Labels are only available once the page has
// microphone permission, so before that the list stays hidden.
async function showMics() {
  const track = micStream && micStream.getAudioTracks()[0];
  micLabel = track ? track.label : micLabel;
  const current = track ? track.getSettings().deviceId : savedMic();
  let inputs = [];
  try {
    inputs = (await navigator.mediaDevices.enumerateDevices())
      .filter((d) => d.kind === "audioinput" && d.deviceId && d.label && d.deviceId !== "communications");
  } catch (_) {}
  if (!inputs.length) return;
  const sel = $("mic-select");
  sel.innerHTML = "";
  for (const d of inputs) {
    const o = document.createElement("option");
    o.value = d.deviceId;
    o.textContent = d.label;
    sel.appendChild(o);
  }
  if (inputs.some((d) => d.deviceId === current)) sel.value = current;
  $("mic-row").hidden = false;
}

$("mic-select").addEventListener("change", async (e) => {
  const id = e.target.value;
  try { localStorage.setItem("va_mic", id); } catch (_) {}
  if (!ctx || !micNode || testMode) return;   // takes effect on the next Start
  const myCtx = ctx;
  let stream;
  try { stream = await getMic(id); } catch (err) { notice(micErrorMessage(err), "error"); return; }
  if (ctx !== myCtx) { stream.getTracks().forEach((t) => t.stop()); return; }   // stopped meanwhile
  if (micSource) micSource.disconnect();
  if (micStream) micStream.getTracks().forEach((t) => t.stop());
  micStream = stream;
  micSource = ctx.createMediaStreamSource(stream);
  micSource.connect(micNode);
  lastMicSound = performance.now();
  notice(null);
  showMics();
});

function micErrorMessage(err) {
  const name = err && err.name;
  if (name === "NotAllowedError") return "Microphone access was blocked. Allow it in the address bar and press Start again.";
  // Permission was fine but the OS couldn't open the device: on Windows this
  // is usually another app holding the mic in exclusive mode.
  if (name === "NotReadableError") {
    return "Couldn't open any microphone: they seem to be busy. Close other apps that use the mic " +
      "(Zoom, Teams, Discord, the Windows Sound settings window), then press Start again.";
  }
  if (name === "NotFoundError" || name === "OverconstrainedError") return "No microphone was found. Plug one in and press Start again.";
  return `Couldn't start the microphone (${err && err.message ? err.message : err}).`;
}

// TEST_MODE only: play data/prompt_NN.wav into the worklet as if spoken.
// decodeAudioData resamples the 16 kHz file to the context rate (usually
// 48 kHz), so the worklet's anti-aliasing + downsampling runs for real.
async function playTestPrompt(n) {
  if (!ctx || !micNode) throw new Error("press Start talking first");
  const r = await fetch(`/test/prompts/prompt_${String(n).padStart(2, "0")}.wav`);
  if (!r.ok) throw new Error(`recording ${n} not found (HTTP ${r.status})`);
  const buf = await ctx.decodeAudioData(await r.arrayBuffer());
  const s = ctx.createBufferSource();
  s.buffer = buf;
  s.connect(micNode);
  s.start();
  return buf.duration;
}

function stopAudio() {
  if (micStream) micStream.getTracks().forEach((t) => t.stop());
  if (ctx) ctx.close();
  ctx = null; micStream = null; micNode = null; micSource = null; playEnd = 0;
  micHint(null);
}

function micHint(msg) {
  const h = $("mic-hint");
  if (h.textContent === (msg || "")) return;
  h.className = msg ? "notice error" : "notice";
  h.textContent = msg || "";
}

// Logarithmic: speech spans roughly 100-4000 rms, and on a linear scale quiet
// speech barely moved the bar, so a working mic looked dead.
function levelPercent(rms) {
  const lo = Math.log(20), hi = Math.log(4000);
  return Math.max(0, Math.min(100, ((Math.log(Math.max(rms, 1)) - lo) / (hi - lo)) * 100));
}

function onMicBlock({ pcm, rms }) {
  $("level").style.width = `${levelPercent(rms)}%`;
  if (rms >= MIC_DEAD_RMS) lastMicSound = performance.now();
  // Half-duplex: send only while the server is listening AND the reply has
  // finished playing, otherwise the agent would hear (and answer) itself.
  if (state !== "listening" || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(pcm);
  turn.sentBytes += pcm.byteLength;
  turn.sendLog.push([turn.sentBytes, performance.now()]);
}

function playFrame(buf) {
  if (!ctx) return;
  const pcm = new Int16Array(buf);
  const audio = ctx.createBuffer(1, pcm.length, TTS_RATE);
  const ch = audio.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
  const src = ctx.createBufferSource();
  src.buffer = audio;
  src.connect(ctx.destination);
  // Gapless: each frame starts exactly where the previous one ends.
  const first = turn && turn.firstSound == null;
  const startAt = Math.max(playEnd, ctx.currentTime + (first ? PLAYBACK_LEAD_S : 0.02));
  src.start(startAt);
  playEnd = startAt + audio.duration;
  const outLatency = ctx.outputLatency || ctx.baseLatency || 0;
  const audibleIn = (startAt - ctx.currentTime + outLatency) * 1000;
  if (first) turn.firstSound = performance.now() + audibleIn;
  // The server sends each sentence's text just before its audio, and audio
  // arrives faster than it plays, so show the text when this frame is heard.
  if (turn && turn.pendingText.length) {
    const text = turn.pendingText.splice(0).join(" ");
    const t = turn;
    setTimeout(() => showReplyText(t, text), Math.max(0, audibleIn));
  }
  if (state !== "speaking") setState("speaking");
}

function showReplyText(t, text) {
  if (t.textDone) return;   // finishReplyText already showed the whole reply
  if (!t.agentBubble) t.agentBubble = bubble("agent", text);
  else t.agentBubble.textContent += " " + text;
}

function isPlaying() {
  return ctx && ctx.currentTime < playEnd;
}

function maybeStartListening() {
  if (!serverListening || isPlaying() || userStopped) return;
  if (state === "listening") return;
  turn = newTurn();
  setState("listening");
}

setInterval(() => {
  if (turn && turn.audioEnded && !isPlaying()) finishReplyText(turn);
  if (state === "speaking" && !isPlaying() && turn && turn.audioEnded) maybeStartListening();
  checkMic();
}, 100);

// Once the voice has finished, show the exact full reply (the per-sentence
// pieces are the TTS chunks, whose whitespace can differ from the LLM text).
// Also covers a reply whose audio failed and so never revealed its text.
function finishReplyText(t) {
  if (t.fullReply == null) return;
  if (!t.agentBubble) t.agentBubble = bubble("agent", t.fullReply);
  else t.agentBubble.textContent = t.fullReply;
  t.fullReply = null; t.pendingText = []; t.textDone = true;
}

// Runs on a timer, not per block: a suspended AudioContext delivers no blocks
// at all, so a per-block check would never fire in exactly that case.
function checkMic() {
  if (state !== "listening" || testMode || !ctx) { micHint(null); return; }
  if (ctx.state === "suspended") {
    lastMicSound = performance.now();   // the mic can't be judged while paused
    micHint("Your browser has paused audio on this page. Click anywhere on the page to turn it on.");
  } else if (performance.now() - lastMicSound > MIC_SILENT_WARN_MS) {
    micHint(`No sound is coming from your microphone${micLabel ? ` (${micLabel})` : ""}. ` +
      "Check it isn't muted, or choose another one in the Microphone list above.");
  } else {
    micHint(null);
  }
}

// ------------------------------------------------------------- connection
async function wakeServer() {
  // On Render's free tier the server sleeps when idle and takes up to a
  // minute to boot. Ping /health first so the wait is explained, not silent.
  const deadline = Date.now() + WAKE_GIVE_UP_MS;
  let shown = false;
  while (Date.now() < deadline && !userStopped) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), WAKE_POLL_MS);
    try {
      const r = await fetch("/health", { signal: ctl.signal, cache: "no-store" });
      clearTimeout(t);
      if (r.ok) return true;
    } catch (_) { clearTimeout(t); }
    if (!shown) { setState("waking"); notice("The server was asleep and is starting up. This can take up to a minute the first time.", "info"); shown = true; }
    await new Promise((r) => setTimeout(r, WAKE_POLL_MS));
  }
  return false;
}

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  setState(reconnectAttempt ? "reconnecting" : "connecting");

  ws.onopen = () => ws.send(JSON.stringify({ type: "hello", code: $("code").value.trim() }));

  ws.onmessage = (e) => {
    if (typeof e.data !== "string") { playFrame(e.data); return; }
    const ev = JSON.parse(e.data);
    switch (ev.type) {
      case "ready":
        reconnectAttempt = 0; terminalError = null;
        notice(null); setState("ready");
        break;
      case "listening":
        serverListening = true;
        // A turn that failed before a transcript (e.g. nothing intelligible)
        // never left "listening"; start its replacement fresh.
        if (state === "listening") turn = newTurn();
        maybeStartListening();
        break;
      case "transcript":
        serverListening = false;
        if (!turn) turn = newTurn();
        turn.speechEnd = sentAt(turn, ev.speech_end_byte);
        turn.text = ev.text;
        turn.agentBubble = null;
        bubble("you", ev.text);
        setState("thinking");
        break;
      case "reply_chunk":
        if (turn) turn.pendingText.push(ev.text);
        break;
      case "reply":
        if (turn && ev.text) turn.fullReply = ev.text;
        break;
      case "audio_end":
        if (turn) turn.audioEnded = true;
        break;
      case "metrics":
        renderMetrics(ev);
        break;
      case "notice":
        notice(ev.message, "info");
        break;
      case "error":
        notice(ev.message, "error");
        if (TERMINAL_ERRORS.has(ev.code)) terminalError = ev.code;
        break;
    }
  };

  ws.onclose = () => {
    ws = null; serverListening = false;
    if (userStopped) { setState("stopped"); return; }
    if (terminalError) {
      // E.g. the turn limit arrives right after the last reply's audio:
      // let it finish playing before releasing the speakers and mic.
      userStopped = true;
      const remainingMs = ctx ? Math.max(0, (playEnd - ctx.currentTime) * 1000) : 0;
      setTimeout(() => { stopAudio(); setState("error", "Stopped"); }, remainingMs + 200);
      return;
    }
    if (reconnectAttempt >= RECONNECT_DELAYS_MS.length) { giveUp(); return; }
    const delay = RECONNECT_DELAYS_MS[reconnectAttempt++];
    setState("reconnecting", `Connection lost — reconnecting in ${Math.round(delay / 1000)} s (attempt ${reconnectAttempt})…`);
    reconnectTimer = setTimeout(async () => {
      if (userStopped) return;
      if (await wakeServer()) connect();
      else if (!userStopped) giveUp();   // never leave the page stuck on "waking up"
    }, delay);
  };
}

function giveUp() {
  userStopped = true; stopAudio(); setState("error", "Couldn't reconnect");
  notice("Lost the connection to the server and couldn't get it back. Press Start talking to try again.", "error");
}

async function start() {
  const code = $("code").value.trim();
  if (!code) { notice("Enter the access code first.", "error"); $("code").focus(); return; }
  try { localStorage.setItem("va_code", code); } catch (_) {}
  userStopped = false; terminalError = null; reconnectAttempt = 0;
  // Each start gets a token; stop() bumps it. After every await we check it,
  // so pressing Stop while starting cancels cleanly instead of the aborted
  // start reporting a bogus "couldn't start the microphone" error.
  const token = ++startToken;
  const cancelled = () => token !== startToken;
  notice(null);
  setState("connecting");
  try {
    await startAudio();
  } catch (err) {
    if (cancelled()) return;
    userStopped = true; stopAudio(); setState("error");
    notice(micErrorMessage(err), "error");
    return;
  }
  if (cancelled()) return;
  if (!(await wakeServer())) {
    if (cancelled()) return;
    userStopped = true; stopAudio(); setState("error");
    notice("The server didn't respond. Try again in a minute.", "error");
    return;
  }
  if (cancelled()) return;
  connect();
}

function stop() {
  startToken++;
  userStopped = true;
  clearTimeout(reconnectTimer);
  if (ws) ws.close(1000);
  stopAudio();
  setState("stopped");
}

$("talk").addEventListener("click", () => (userStopped ? start() : stop()));
try { $("code").value = localStorage.getItem("va_code") || ""; } catch (_) {}
showMics();   // lists the mics before Start if permission was granted earlier
setState("idle");

fetch("/health", { cache: "no-store" }).then((r) => r.json()).then((h) => {
  if (!h.test_mode) return;
  testMode = true;
  for (let i = 1; i <= 20; i++) $("test-prompt").add(new Option(`prompt_${String(i).padStart(2, "0")}.wav`, i));
  $("test-panel").hidden = false;
  $("test-play").addEventListener("click", () =>
    playTestPrompt(+$("test-prompt").value).catch((err) => notice(`Test playback failed: ${err.message}`, "error")));
}).catch(() => {});
