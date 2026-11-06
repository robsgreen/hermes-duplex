// Hermes Duplex page: sign in with a token, stream the microphone to /ws, and play the replies.
//
// The socket carries PCM16 24 kHz mono audio both ways as binary messages. The server also sends
// JSON: {type: "interrupt"} flushes queued playback; {type: "status", ...} updates the transcript
// ({transcript: {speaker, text}}), the task list ({tasks: [{number, profile, state, text}]}), the
// status line ({message}), or the profile picker ({active_profile, profiles: [name]}). Choosing a
// profile in the picker sends {type: "profile", name}.
"use strict";

const TOKEN_KEY = "hermes-duplex-token";
const MAX_TRANSCRIPT_LINES = 200;

const form = document.getElementById("sign-in");
const tokenInput = document.getElementById("token");
const connectButton = document.getElementById("connect");
const disconnectButton = document.getElementById("disconnect");
const statusLine = document.getElementById("status");
const transcript = document.getElementById("transcript");
const tasks = document.getElementById("tasks");
const profilePicker = document.getElementById("profile");

let session = null; // {socket, context, microphone} while connected

function readStoredToken() {
  try {
    return sessionStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

function storeToken(token) {
  try {
    sessionStorage.setItem(TOKEN_KEY, token);
  } catch {
    // Storage can be unavailable (private windows); the token then has to be typed again.
  }
}

function setConnected(connected) {
  connectButton.disabled = connected;
  disconnectButton.disabled = !connected;
  tokenInput.disabled = connected;
  if (!connected) profilePicker.replaceChildren();
  profilePicker.disabled = !connected || profilePicker.options.length === 0;
}

function socketUrl() {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${location.host}/ws`;
}

// Prefer the bearer subprotocol so the token stays out of URLs and logs; a token with characters
// a subprotocol cannot carry falls back to the query string.
function openSocket(token) {
  try {
    return new WebSocket(socketUrl(), ["bearer", token]);
  } catch {
    return new WebSocket(`${socketUrl()}?token=${encodeURIComponent(token)}`);
  }
}

async function connect(token) {
  setConnected(true);
  statusLine.textContent = "Connecting…";
  try {
    const microphone = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    const context = new AudioContext();
    await context.audioWorklet.addModule("/static/worklet.js");
    const node = new AudioWorkletNode(context, "duplex-processor", {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
    });
    context.createMediaStreamSource(microphone).connect(node);
    node.connect(context.destination);

    const socket = openSocket(token);
    socket.binaryType = "arraybuffer";
    session = { socket, context, microphone };

    node.port.onmessage = (event) => {
      if (socket.readyState === WebSocket.OPEN) socket.send(event.data);
    };
    socket.onopen = () => {
      statusLine.textContent = "Connected. Start talking.";
    };
    socket.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        node.port.postMessage(event.data, [event.data]);
      } else {
        handleControl(JSON.parse(event.data), node);
      }
    };
    socket.onclose = (event) => {
      const reason = event.reason || (event.code === 1006 ? "connection refused or lost" : "closed");
      teardown(`Disconnected: ${reason}.`);
    };
  } catch (error) {
    teardown(`Could not connect: ${error.message}`);
  }
}

function handleControl(message, node) {
  if (message.type === "interrupt") {
    node.port.postMessage({ type: "flush" });
  } else if (message.type === "status") {
    if (message.transcript) addTranscriptLine(message.transcript);
    if (Array.isArray(message.tasks)) showTasks(message.tasks);
    if (typeof message.message === "string") statusLine.textContent = message.message;
    if (typeof message.active_profile === "string" && Array.isArray(message.profiles)) {
      showProfiles(message.active_profile, message.profiles);
    }
  }
}

function showProfiles(active, names) {
  profilePicker.replaceChildren(
    ...names.map((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      return option;
    }),
  );
  profilePicker.value = active;
  profilePicker.disabled = names.length === 0;
}

function addTranscriptLine({ speaker, text }) {
  const item = document.createElement("li");
  const name = document.createElement("span");
  name.className = "speaker";
  name.textContent = speaker || "";
  item.append(name, document.createTextNode(text || ""));
  transcript.append(item);
  while (transcript.children.length > MAX_TRANSCRIPT_LINES) transcript.firstElementChild.remove();
  transcript.scrollTop = transcript.scrollHeight;
}

function showTasks(list) {
  tasks.replaceChildren(
    ...list.map(({ number, profile, state, text }) => {
      const item = document.createElement("li");
      item.textContent = `#${number} ${profile} (${state}): ${text}`;
      return item;
    }),
  );
}

function teardown(message) {
  if (session) {
    const { socket, context, microphone } = session;
    session = null;
    socket.onclose = null;
    if (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN) socket.close();
    microphone.getTracks().forEach((track) => track.stop());
    context.close();
  }
  setConnected(false);
  statusLine.textContent = message;
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const token = tokenInput.value.trim();
  if (!token || session) return;
  storeToken(token);
  connect(token);
});

disconnectButton.addEventListener("click", () => teardown("Not connected."));

// The server answers with a status message: the new active profile, or, when the switch did not
// happen, the reason and the profile still active, which puts the picker back.
profilePicker.addEventListener("change", () => {
  if (session && session.socket.readyState === WebSocket.OPEN) {
    session.socket.send(JSON.stringify({ type: "profile", name: profilePicker.value }));
  }
});

tokenInput.value = readStoredToken();
