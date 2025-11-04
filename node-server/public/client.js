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
      this.currentBroadcasterId = null;
      this.shutdownButton = null;
      this.awaitingShutdownAck = false;
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
