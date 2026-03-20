(() => {
  const ICE = [{ urls: "stun:stun.l.google.com:19302" }];

  function qs(id) {
    return document.getElementById(id);
  }
  function getRoom() {
    const p = new URLSearchParams(location.search);
    return p.get("room") || "baby";
  }

  function setViewerError(message) {
    const el = qs("viewerError");
    if (!el) return;
    if (!message) {
      el.textContent = "";
      el.classList.add("hidden");
    } else {
      el.textContent = message;
      el.classList.remove("hidden");
    }
  }

  async function ensureMediaAvailable() {
    const ok =
      "mediaDevices" in navigator && "getUserMedia" in navigator.mediaDevices;
    if (!ok) {
      const isHttps = location.protocol === "https:";
      const isLocalhost = ["localhost", "127.0.0.1"].includes(
        location.hostname
      );
      const reason =
        isHttps || isLocalhost
          ? "Your browser does not seem to support getUserMedia."
          : "On Android, open this page over HTTPS to access the camera/mic.";
      throw new Error(reason);
    }
  }

  function setupWebSocket(room, role) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.addEventListener("open", () =>
      ws.send(JSON.stringify({ type: "join", room, role }))
    );
    return ws;
  }

  class Broadcaster {
    constructor() {
      this.peers = new Map();
      this.stream = null;
      this.ws = null;
      this.activeViewers = new Set();
      this.viewerRetries = new Map(); // viewerId => { attempts, timeout }
      this.retryPolicy = { maxAttempts: 5, intervalMs: 1000 };
    }
    async start(room) {
      await ensureMediaAvailable();
      this.ws = setupWebSocket(room, "broadcaster");
      this.ws.onmessage = (ev) => this.onWS(JSON.parse(ev.data));
      this.stream = await navigator.mediaDevices.getUserMedia({
        video: {
          width: { ideal: 1920, max: 3840 },
          height: { ideal: 1080, max: 2160 },
          frameRate: { ideal: 30, max: 60 },
        },
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
          sampleRate: 48000,
        },
      });
      qs("local").srcObject = this.stream;
      qs("startBtn").disabled = true;
      qs("stopBtn").disabled = false;
    }
    stop() {
      this.ws?.close();
      this.stream?.getTracks().forEach((t) => t.stop());
      this.peers.forEach((pc) => pc.close());
      this.peers.clear();
      this.activeViewers.clear();
      this.viewerRetries.forEach(({ timeout }) => clearTimeout(timeout));
      this.viewerRetries.clear();
      qs("local").srcObject = null;
      qs("startBtn").disabled = false;
      qs("stopBtn").disabled = true;
    }
    onWS(msg) {
      if (msg.type === "viewer-joined") {
        this.activeViewers.add(msg.viewerId);
        this.clearViewerRetry(msg.viewerId);
        this.createOfferForViewer(msg.viewerId);
      } else if (msg.type === "answer") {
        const pc = this.peers.get(msg.fromId);
        pc && pc.setRemoteDescription(new RTCSessionDescription(msg.answer));
      } else if (msg.type === "candidate") {
        const pc = this.peers.get(msg.fromId);
        pc &&
          msg.candidate &&
          pc
            .addIceCandidate(new RTCIceCandidate(msg.candidate))
            .catch(() => {});
      } else if (msg.type === "peer-left") {
        const pc = this.peers.get(msg.peerId);
        if (pc) {
          pc.close();
          this.peers.delete(msg.peerId);
        }
        if (this.activeViewers.has(msg.peerId)) {
          this.activeViewers.delete(msg.peerId);
          this.clearViewerRetry(msg.peerId);
        }
      }
    }
    createOfferForViewer(viewerId) {
      if (!this.stream || !this.ws || this.ws.readyState !== WebSocket.OPEN) {
        return;
      }
      const existing = this.peers.get(viewerId);
      if (existing) {
        existing.close();
        this.peers.delete(viewerId);
      }
      const pc = new RTCPeerConnection({ iceServers: ICE });
      this.stream.getTracks().forEach((track) => {
        const sender = pc.addTrack(track, this.stream);
        if (track.kind === "video") {
          const params = sender.getParameters();
          params.encodings = params.encodings || [{}];
          params.encodings[0].maxBitrate = 4_000_000; // ~4 Mbps
          params.encodings[0].maxFramerate = 60;
          sender.setParameters(params).catch(() => {});
        }
      });
      pc.onicecandidate = (e) => {
        if (e.candidate)
          this.ws.send(
            JSON.stringify({
              type: "candidate",
              targetId: viewerId,
              candidate: e.candidate,
            })
          );
      };
      pc.onconnectionstatechange = () => {
        const state = pc.connectionState;
        if (state === "connected") {
          this.clearViewerRetry(viewerId);
        }
        if (["failed", "disconnected", "closed"].includes(state)) {
          pc.close();
          this.peers.delete(viewerId);
          if (this.activeViewers.has(viewerId)) {
            this.scheduleViewerRetry(viewerId, state);
          }
        }
      };
      this.peers.set(viewerId, pc);
      pc
        .createOffer({ offerToReceiveAudio: false, offerToReceiveVideo: false })
        .then((offer) => pc.setLocalDescription(offer).then(() => offer))
        .then((offer) =>
          this.ws.send(
            JSON.stringify({ type: "offer", targetId: viewerId, offer })
          )
        )
        .catch(console.error);
    }

    scheduleViewerRetry(viewerId, reason) {
      if (!this.activeViewers.has(viewerId) || this.viewerRetries.has(viewerId)) {
        return;
      }
      const entry = { attempts: 0, timeout: null };
      const attempt = () => {
        if (!this.activeViewers.has(viewerId)) {
          this.clearViewerRetry(viewerId);
          return;
        }
        entry.attempts += 1;
        if (entry.attempts > this.retryPolicy.maxAttempts) {
          console.error(
            `Impossible de rétablir le flux vers ${viewerId} (${reason}).`
          );
          this.clearViewerRetry(viewerId);
          return;
        }
        if (!this.peers.has(viewerId)) {
          this.createOfferForViewer(viewerId);
        }
        entry.timeout = setTimeout(attempt, this.retryPolicy.intervalMs);
      };
      entry.timeout = setTimeout(attempt, this.retryPolicy.intervalMs);
      this.viewerRetries.set(viewerId, entry);
    }

    clearViewerRetry(viewerId) {
      const entry = this.viewerRetries.get(viewerId);
      if (entry?.timeout) {
        clearTimeout(entry.timeout);
      }
      this.viewerRetries.delete(viewerId);
    }
  }

  class Viewer {
    constructor() {
      this.ws = null;
      this.pc = null;
      this.room = null;
      this.active = false;
      this.remoteVideo = qs("remote");
      this.retryIntervalMs = 1000;
      this.maxRetries = 5;
      this.retryTimer = null;
      this.retryAttempts = 0;
      this.streamHealthTimer = null;
      this.streamHealthIntervalMs = 2000;
      this.streamStallThresholdMs = 8000;
      this.lastVideoBytes = null;
      this.lastVideoFrames = null;
      this.lastVideoProgressAt = 0;
      this.currentBroadcasterId = null;
      this.shutdownButton = null;
      this.awaitingShutdownAck = false;
      this.feedEl = qs("eventFeed");
      this.feedPlaceholder =
        this.feedEl && this.feedEl.querySelector("[data-placeholder]");
      this.maxFeedItems = 100;
      this.timeFormatter =
        typeof Intl !== "undefined" && Intl.DateTimeFormat
          ? new Intl.DateTimeFormat(undefined, {
              hour: "2-digit",
              minute: "2-digit",
            })
          : null;
      this.speech = {
        enabled: typeof window !== "undefined" && "speechSynthesis" in window,
        voice: null,
        queue: [],
      };
      if (this.speech.enabled) {
        const handleVoicesChanged = () => {
          this._selectSpeechVoice();
          this._flushSpeechQueue();
        };
        window.speechSynthesis.addEventListener(
          "voiceschanged",
          handleVoicesChanged
        );
        this._selectSpeechVoice();
      }
    }

    async join(room) {
      if (this.active) return;
      this.room = room;
      this.active = true;
      setViewerError(null);
      this._initWebSocket();
      this._createPeerConnection();
      qs("joinBtn").disabled = true;
      qs("leaveBtn").disabled = false;
    }

    leave() {
      this.active = false;
      this._stopRetry();
      this._stopStreamHealthCheck();
      this.currentBroadcasterId = null;
      this.ws?.close();
      this.pc?.close();
      if (this.remoteVideo) {
        this.remoteVideo.pause();
        this.remoteVideo.srcObject = null;
      }
      qs("joinBtn").disabled = false;
      qs("leaveBtn").disabled = true;
      setViewerError(null);
      if (this.shutdownButton) {
        this.shutdownButton.disabled = true;
        this.awaitingShutdownAck = false;
      }
    }

    _initWebSocket() {
      if (!this.room) return;
      if (this.ws) {
        this.ws.onmessage = null;
        this.ws.onclose = null;
        this.ws.onerror = null;
        if (
          this.ws.readyState === WebSocket.OPEN ||
          this.ws.readyState === WebSocket.CONNECTING
        ) {
          this.ws.close();
        }
      }
      this.ws = setupWebSocket(this.room, "viewer");
      this.ws.onmessage = (ev) => this.onWS(JSON.parse(ev.data));
      this.ws.onclose = () => this._handleStreamInterrupted("signal perdu");
      this.ws.onerror = () => this._handleStreamInterrupted("signal perdu");
    }

    _createPeerConnection() {
      if (this.pc) {
        this.pc.onconnectionstatechange = null;
        this.pc.close();
      }
      const pc = new RTCPeerConnection({ iceServers: ICE });
      this.pc = pc;
      this._resetStreamHealthState();
      if (this.remoteVideo) {
        this.remoteVideo.srcObject = null;
      }
      pc.ontrack = (e) => {
        if (this.remoteVideo && !this.remoteVideo.srcObject) {
          this.remoteVideo.srcObject = e.streams[0];
        }
      };
      pc.onicecandidate = (e) => {
        if (e.candidate && this.ws?.readyState === WebSocket.OPEN) {
          this.ws.send(
            JSON.stringify({ type: "candidate", candidate: e.candidate })
          );
        }
      };
      pc.onconnectionstatechange = () => {
        const state = pc.connectionState;
        if (state === "connected") {
          this._clearRetryState();
        } else if (
          ["failed", "disconnected", "closed"].includes(state) &&
          this.active
        ) {
          this._handleStreamInterrupted(`webrtc ${state}`);
        }
      };
      pc.addTransceiver("video", { direction: "recvonly" });
      pc.addTransceiver("audio", { direction: "recvonly" });
      this._startStreamHealthCheck();
    }

    _handleStreamInterrupted(reason) {
      if (!this.active || this.retryTimer) return;
      console.warn(`Flux interrompu (${reason}), tentative de reconnexion...`);
      this._createPeerConnection();
      this._startRetry();
      if (this.shutdownButton) {
        this.shutdownButton.disabled = true;
      }
    }

    _startRetry() {
      this.retryAttempts = 0;
      this.retryTimer = setInterval(
        () => this._attemptReconnect(),
        this.retryIntervalMs
      );
    }

    _attemptReconnect() {
      if (!this.active) {
        this._stopRetry();
        return;
      }
      this.retryAttempts += 1;
      if (this.retryAttempts > this.maxRetries) {
        this._stopRetry();
        setViewerError(
          "Impossible de rétablir le flux après plusieurs tentatives."
        );
        return;
      }
      if (!this.ws || this.ws.readyState === WebSocket.CLOSED) {
        this._initWebSocket();
        return;
      }
      if (this.ws.readyState === WebSocket.CONNECTING) {
        return;
      }
      this.ws.send(
        JSON.stringify({ type: "join", room: this.room, role: "viewer" })
      );
    }

    _stopRetry() {
      if (this.retryTimer) {
        clearInterval(this.retryTimer);
        this.retryTimer = null;
      }
    }

    _clearRetryState() {
      this._stopRetry();
      this.retryAttempts = 0;
      setViewerError(null);
      if (this.shutdownButton) {
        this.shutdownButton.disabled = false;
        this.awaitingShutdownAck = false;
      }
    }

    _resetStreamHealthState() {
      this.lastVideoBytes = null;
      this.lastVideoFrames = null;
      this.lastVideoProgressAt = Date.now();
    }

    _startStreamHealthCheck() {
      this._stopStreamHealthCheck();
      this.streamHealthTimer = setInterval(
        () => this._checkRemoteStreamHealth(),
        this.streamHealthIntervalMs
      );
    }

    _stopStreamHealthCheck() {
      if (this.streamHealthTimer) {
        clearInterval(this.streamHealthTimer);
        this.streamHealthTimer = null;
      }
    }

    async _checkRemoteStreamHealth() {
      if (!this.active || !this.pc || this.retryTimer) {
        return;
      }
      if (this.pc.connectionState !== "connected") {
        return;
      }
      let stats;
      try {
        stats = await this.pc.getStats();
      } catch {
        return;
      }
      let inboundVideo = null;
      stats.forEach((report) => {
        if (
          !inboundVideo &&
          report.type === "inbound-rtp" &&
          report.kind === "video"
        ) {
          inboundVideo = report;
        }
      });
      if (!inboundVideo) return;

      const now = Date.now();
      const bytes = Number.isFinite(inboundVideo.bytesReceived)
        ? inboundVideo.bytesReceived
        : null;
      const frames = Number.isFinite(inboundVideo.framesDecoded)
        ? inboundVideo.framesDecoded
        : null;

      if (this.lastVideoBytes === null && this.lastVideoFrames === null) {
        this.lastVideoBytes = bytes;
        this.lastVideoFrames = frames;
        this.lastVideoProgressAt = now;
        return;
      }

      const bytesProgress =
        bytes !== null && this.lastVideoBytes !== null && bytes > this.lastVideoBytes;
      const framesProgress =
        frames !== null &&
        this.lastVideoFrames !== null &&
        frames > this.lastVideoFrames;

      if (bytesProgress || framesProgress) {
        this.lastVideoProgressAt = now;
      } else if (now - this.lastVideoProgressAt >= this.streamStallThresholdMs) {
        this._handleStreamInterrupted("flux vidéo figé");
        return;
      }

      this.lastVideoBytes = bytes;
      this.lastVideoFrames = frames;
    }

    onWS(msg) {
      if (msg.type === "offer") {
        this.currentBroadcasterId = msg.fromId;
        if (this.shutdownButton && !this.awaitingShutdownAck) {
          this.shutdownButton.disabled = false;
        }
        const offer = new RTCSessionDescription(msg.offer);
        this.pc
          .setRemoteDescription(offer)
          .then(() => this.pc.createAnswer())
          .then((answer) =>
            this.pc.setLocalDescription(answer).then(() => answer)
          )
          .then((answer) =>
            this.ws.send(
              JSON.stringify({ type: "answer", answer, targetId: msg.fromId })
            )
          )
          .then(() => this._clearRetryState())
          .catch(console.error);
      } else if (msg.type === "candidate" && this.pc) {
        msg.candidate &&
          this.pc
            .addIceCandidate(new RTCIceCandidate(msg.candidate))
            .catch(() => {});
      } else if (
        msg.type === "peer-left" &&
        this.currentBroadcasterId === msg.peerId
      ) {
        this.currentBroadcasterId = null;
        this._handleStreamInterrupted("diffuseur indisponible");
        if (this.shutdownButton) {
          this.shutdownButton.disabled = true;
        }
      } else if (msg.type === "shutdown-ack") {
        this.awaitingShutdownAck = false;
        alert(
          "Le Raspberry Pi va s'éteindre. Le flux sera interrompu sous peu."
        );
      } else if (msg.type === "shutdown-denied") {
        this.awaitingShutdownAck = false;
        if (this.shutdownButton) {
          this.shutdownButton.disabled = false;
        }
        const reason = msg.reason ? String(msg.reason) : "non spécifiée";
        alert(
          `La demande d'arrêt a été refusée par le diffuseur (motif: ${reason}).`
        );
      } else if (msg.type === "shutdown-error") {
        this.awaitingShutdownAck = false;
        if (this.shutdownButton) {
          this.shutdownButton.disabled = false;
        }
        alert(
          "Le diffuseur n'a pas pu exécuter la commande d'arrêt (voir les logs)."
        );
      } else if (msg.type === "event-log") {
        this._handleAnalyzerEvent(msg.event);
      }
    }

    async requestShutdown() {
      if (!this.currentBroadcasterId) {
        throw new Error("Aucun diffuseur n'est connecté.");
      }
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        throw new Error("Connexion de signalisation indisponible.");
      }
      this.awaitingShutdownAck = true;
      this.ws.send(
        JSON.stringify({
          type: "shutdown",
          targetId: this.currentBroadcasterId,
        })
      );
    }

    attachShutdownButton(button) {
      this.shutdownButton = button;
      if (!button) return;
      button.onclick = async () => {
        if (
          !confirm(
            "Confirmer l'arrêt complet du Raspberry Pi ?\nAssurez-vous que personne n'utilise le flux."
          )
        ) {
          return;
        }
        if (this.awaitingShutdownAck) {
          return;
        }
        button.disabled = true;
        try {
          await this.requestShutdown();
        } catch (err) {
          console.error(err);
          alert(
            "Impossible d'envoyer la demande d'arrêt (vérifiez la connexion)."
          );
          button.disabled = false;
        }
      };
    }

    _handleAnalyzerEvent(event) {
      if (!event || typeof event !== "object") {
        console.debug("[viewer] Ignored analyzer event (invalid)", event);
        return;
      }
      console.debug("[viewer] Analyzer event received", event);
      this._appendFeedEvent(event);
    }

    _appendFeedEvent(event) {
      if (!this.feedEl) return;
      if (this.feedPlaceholder) {
        this.feedPlaceholder.remove();
        this.feedPlaceholder = null;
      }
      const label = typeof event.label === "string" ? event.label.toLowerCase() : "";
      const li = document.createElement("li");
      li.className = "chat-entry";
      if (label === "awake") li.classList.add("chat-entry--awake");
      else if (label === "cry") li.classList.add("chat-entry--cry");
      else li.classList.add("chat-entry--movement");

      const meta = document.createElement("div");
      meta.className = "chat-entry__meta";
      meta.textContent = `${this._formatEventTime(event.timestamp)} • ${this._labelForEvent(label)}`;
      li.appendChild(meta);

      const message = document.createElement("div");
      message.className = "chat-entry__message";
      message.textContent = this._messageForEvent(label, event.description);
      li.appendChild(message);

      const detailText = this._detailTextForEvent(event);
      if (detailText) {
        const details = document.createElement("div");
        details.className = "chat-entry__details";
        details.textContent = detailText;
        li.appendChild(details);
      }

      if (event.trace_id) {
        li.dataset.trace = String(event.trace_id);
      }

      this.feedEl.appendChild(li);
      while (this.feedEl.children.length > this.maxFeedItems) {
        this.feedEl.removeChild(this.feedEl.firstChild);
      }
      this.feedEl.scrollTop = this.feedEl.scrollHeight;
      console.debug("[viewer] Feed updated with", label, event);
      if (label === "cry" || label === "awake") {
        this._speakNotification("ハムハムがおきてるでござる");
      }
    }

    _formatEventTime(timestamp) {
      const ms =
        typeof timestamp === "number" && Number.isFinite(timestamp)
          ? timestamp * 1000
          : Date.now();
      if (this.timeFormatter) {
        return this.timeFormatter.format(new Date(ms));
      }
      const date = new Date(ms);
      const hours = date.getHours().toString().padStart(2, "0");
      const minutes = date.getMinutes().toString().padStart(2, "0");
      return `${hours}:${minutes}`;
    }

    _labelForEvent(label) {
      switch (label) {
        case "cry":
          return "Cry";
        case "awake":
          return "Awake";
        case "movement":
          return "Movement";
        default:
          return "Event";
      }
    }

    _messageForEvent(label, description) {
      if (label === "awake") {
        return "Baby is awake!";
      }
      if (label === "cry") {
        return "Baby is crying";
      }
      if (label === "movement") {
        return "Movement detected";
      }
      if (description && typeof description === "string") {
        return description.charAt(0).toUpperCase() + description.slice(1);
      }
      return "Activity detected";
    }

    _detailTextForEvent(event) {
      const extras =
        event && typeof event === "object" && event.extras
          ? event.extras
          : {};
      const parts = [];
      const score = extras.movement_score;
      if (typeof score === "number" && Number.isFinite(score)) {
        parts.push(`score ${score.toFixed(3)}`);
      }
      const streak = extras.movement_streak_seconds;
      if (typeof streak === "number" && Number.isFinite(streak)) {
        parts.push(`streak ${streak.toFixed(1)}s`);
      }
      const energy = extras.energy;
      if (typeof energy === "number" && Number.isFinite(energy)) {
        parts.push(`energy ${energy.toFixed(3)}`);
      }
      const ratio = extras.ratio_mid_band;
      if (typeof ratio === "number" && Number.isFinite(ratio)) {
        parts.push(`ratio ${ratio.toFixed(2)}`);
      }
      return parts.join(" · ");
    }

    _selectSpeechVoice() {
      if (!this.speech.enabled) return;
      const voices = window.speechSynthesis.getVoices() || [];
      if (!voices.length) {
        this.speech.voice = null;
        console.debug("[viewer] speech: no voices available yet");
        return;
      }
      const googleJapanese = voices.find(
        (voice) => voice.voiceURI === "Google 日本語"
      );
      const kyoko = voices.find(
        (voice) =>
          voice.voiceURI === "urn:moz-tts:osx:com.apple.voice.compact.ja-JP.Kyoko"
      );
      const fallback = voices.find(
        (voice) => voice.lang && voice.lang.toLowerCase().startsWith("ja")
      );
      this.speech.voice = googleJapanese || kyoko || fallback || null;
      console.debug(
        "[viewer] speech: voice selected",
        this.speech.voice ? this.speech.voice.voiceURI : "none"
      );
    }

    _flushSpeechQueue() {
      if (!this.speech.enabled) return;
      if (!this.speech.voice) return;
      const synth = window.speechSynthesis;
      while (this.speech.queue.length) {
        const text = this.speech.queue.shift();
        const utterance = new SpeechSynthesisUtterance(text);
        utterance.voice = this.speech.voice;
        synth.speak(utterance);
        console.debug("[viewer] speech: queued utterance played", text);
      }
    }

    _speakNotification(text) {
      if (!this.speech.enabled || !text) return;
      if (!this.speech.voice) {
        this.speech.queue.push(text);
        this._selectSpeechVoice();
        if (this.speech.voice) {
          this._flushSpeechQueue();
        } else {
          return;
        }
      }
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.voice = this.speech.voice;
      try {
        window.speechSynthesis.speak(utterance);
      } catch (err) {
        console.warn("Unable to play speech notification:", err);
      }
    }
  }

  window.initBabyPhone = ({ role }) => {
    const room = getRoom();
    if (role === "broadcaster") {
      const b = new Broadcaster();
      qs("startBtn").onclick = () => b.start(room);
      qs("stopBtn").onclick = () => b.stop();
    } else {
      const v = new Viewer();
      const remote = qs("remote");
      const overlay = qs("playOverlay");
      const shutdownBtn = qs("shutdownBtn");
      qs("joinBtn").onclick = () => v.join(room);
      qs("leaveBtn").onclick = () => v.leave();
      qs("playBtn").onclick = async () => {
        try {
          await remote.play();
          overlay.classList.add("hidden");
        } catch (e) {
          alert("Tap Play to authorize audio playback.");
        }
      };
      qs("fsBtn").onclick = async () => {
        const el = remote;
        if (document.fullscreenElement) {
          document.exitFullscreen();
        } else {
          await el.requestFullscreen().catch(() => {});
        }
      };
      remote.addEventListener("playing", () => overlay.classList.add("hidden"));
      remote.addEventListener("pause", () =>
        overlay.classList.remove("hidden")
      );
      if (shutdownBtn) {
        shutdownBtn.disabled = true;
        v.attachShutdownButton(shutdownBtn);
      }
    }
  };
})();
