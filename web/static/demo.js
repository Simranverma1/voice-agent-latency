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

// Same rule as web/vad.py and agent/audio_io.py: calibrate on the first
// 25 blocks, then speech = energy above 3.5x the ambient floor. Fed the
// exact blocks sent to the server, so both sides agree on "speech".
class EnergyVAD {
  constructor(multiplier = 3.5, floorMin = 180, calibBlocks = 25) {
    this.multiplier = multiplier; this.floorMin = floorMin; this.calibBlocks = calibBlocks;
    this.samples = []; this.threshold = floorMin * multiplier;
  }
  isSpeech(rms) {
    if (this.samples.length < this.calibBlocks) {
      this.samples.push(rms);
      if (this.samples.length === this.calibBlocks) {
        const s = [...this.samples].sort((a, b) => a - b);
        const mid = s.length / 2;
        const median = s.length % 2 ? s[Math.floor(mid)] : (s[mid - 1] + s[mid]) / 2;
        this.threshold = Math.max(median, this.floorMin) * this.multiplier;
      }
      return false;
    }
    return rms > this.threshold;
  }
}

let ctx = null, micStream = null, micNode = null;
let ws = null, vad = null;
let state = "idle";
let userStopped = true, terminalError = null, reconnectAttempt = 0, reconnectTimer = null;
let serverListening = false;
let playEnd = 0;           // AudioContext time when queued reply audio finishes
let turn = null;           // browser-side record of the current turn
let turnCount = 0;

function newTurn() {
  return { lastSpeech: null, speechEnd: null, firstSound: null, audioEnded: false, text: "" };
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
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  const src = ctx.createMediaStreamSource(micStream);
  micNode = new AudioWorkletNode(ctx, "mic-downsampler");
  // A worklet only runs while its output is pulled; route it to the
  // speakers through a muted gain so it's pulled without being heard.
  const mute = ctx.createGain();
  mute.gain.value = 0;
  src.connect(micNode).connect(mute).connect(ctx.destination);
  micNode.port.onmessage = (e) => onMicBlock(e.data);
  if (ctx.state === "suspended") await ctx.resume();
}

function stopAudio() {
  if (micStream) micStream.getTracks().forEach((t) => t.stop());
  if (ctx) ctx.close();
  ctx = null; micStream = null; micNode = null; playEnd = 0;
}

function onMicBlock({ pcm, rms }) {
  $("level").style.width = `${Math.min(100, (rms / 4000) * 100)}%`;
  // Half-duplex: send only while the server is listening AND the reply has
  // finished playing, otherwise the agent would hear (and answer) itself.
  if (state !== "listening" || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(pcm);
  if (vad.isSpeech(rms)) turn.lastSpeech = performance.now();
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
  if (first) {
    const outLatency = ctx.outputLatency || ctx.baseLatency || 0;
    turn.firstSound = performance.now() + (startAt - ctx.currentTime + outLatency) * 1000;
  }
  if (state !== "speaking") setState("speaking");
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
  if (state === "speaking" && !isPlaying() && turn && turn.audioEnded) maybeStartListening();
}, 100);

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
        reconnectAttempt = 0; terminalError = null; vad = new EnergyVAD();
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
        turn.speechEnd = turn.lastSpeech;
        turn.text = ev.text;
        turn.agentBubble = null;
        bubble("you", ev.text);
        setState("thinking");
        break;
      case "reply_chunk":
        if (!turn.agentBubble) turn.agentBubble = bubble("agent", ev.text);
        else turn.agentBubble.textContent += " " + ev.text;
        break;
      case "reply":
        if (turn.agentBubble) turn.agentBubble.textContent = ev.text;
        else if (ev.text) turn.agentBubble = bubble("agent", ev.text);
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
    if (terminalError) { userStopped = true; stopAudio(); setState("error", "Stopped"); return; }
    if (reconnectAttempt >= RECONNECT_DELAYS_MS.length) {
      userStopped = true; stopAudio(); setState("error", "Couldn't reconnect");
      notice("Lost the connection to the server and couldn't get it back. Press Start talking to try again.", "error");
      return;
    }
    const delay = RECONNECT_DELAYS_MS[reconnectAttempt++];
    setState("reconnecting", `Connection lost — reconnecting in ${Math.round(delay / 1000)} s (attempt ${reconnectAttempt})…`);
    reconnectTimer = setTimeout(async () => {
      if (userStopped) return;
      if (await wakeServer()) connect();
    }, delay);
  };
}

async function start() {
  const code = $("code").value.trim();
  if (!code) { notice("Enter the access code first.", "error"); $("code").focus(); return; }
  try { localStorage.setItem("va_code", code); } catch (_) {}
  userStopped = false; terminalError = null; reconnectAttempt = 0;
  notice(null);
  setState("connecting");
  try {
    await startAudio();
  } catch (err) {
    userStopped = true; stopAudio(); setState("error");
    notice(err && err.name === "NotAllowedError"
      ? "Microphone access was blocked. Allow it in the address bar and press Start again."
      : `Couldn't start the microphone (${err && err.message ? err.message : err}).`, "error");
    return;
  }
  if (!(await wakeServer())) {
    userStopped = true; stopAudio(); setState("error");
    notice("The server didn't respond. Try again in a minute.", "error");
    return;
  }
  connect();
}

function stop() {
  userStopped = true;
  clearTimeout(reconnectTimer);
  if (ws) ws.close(1000);
  stopAudio();
  setState("stopped");
}

$("talk").addEventListener("click", () => (userStopped ? start() : stop()));
try { $("code").value = localStorage.getItem("va_code") || ""; } catch (_) {}
setState("idle");
