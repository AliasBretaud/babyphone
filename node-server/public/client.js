(() => {
  const ICE = [{ urls: "stun:stun.l.google.com:19302" }];

  function qs(id) {
    return document.getElementById(id);
  }
  function getRoom() {
    const p = new URLSearchParams(location.search);
    return p.get("room") || "baby";
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
      qs("local").srcObject = null;
      qs("startBtn").disabled = false;
      qs("stopBtn").disabled = true;
    }
    onWS(msg) {
      if (msg.type === "viewer-joined") {
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
      }
    }
    createOfferForViewer(viewerId) {
      const pc = new RTCPeerConnection({ iceServers: ICE });
      this.stream
        .getTracks()
        .forEach((track) => {
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
        if (["failed", "disconnected", "closed"].includes(pc.connectionState)) {
          pc.close();
          this.peers.delete(viewerId);
        }
      };
      this.peers.set(viewerId, pc);
      pc.createOffer({ offerToReceiveAudio: false, offerToReceiveVideo: false })
        .then((offer) => pc.setLocalDescription(offer).then(() => offer))
        .then((offer) =>
          this.ws.send(
            JSON.stringify({ type: "offer", targetId: viewerId, offer })
          )
        )
        .catch(console.error);
    }
  }

  class Viewer {
    async join(room) {
      this.ws = setupWebSocket(room, "viewer");
      this.ws.onmessage = (ev) => this.onWS(JSON.parse(ev.data));
      this.pc = new RTCPeerConnection({ iceServers: ICE });
      const remoteVideo = qs("remote");
      this.pc.ontrack = (e) => {
        if (!remoteVideo.srcObject) remoteVideo.srcObject = e.streams[0];
      };
      this.pc.onicecandidate = (e) => {
        if (e.candidate)
          this.ws.send(
            JSON.stringify({ type: "candidate", candidate: e.candidate })
          );
      };
      this.pc.addTransceiver("video", { direction: "recvonly" });
      this.pc.addTransceiver("audio", { direction: "recvonly" });
      qs("joinBtn").disabled = true;
      qs("leaveBtn").disabled = false;
    }
    leave() {
      this.ws?.close();
      this.pc?.close();
      const v = qs("remote");
      v.pause();
      v.srcObject = null;
      qs("joinBtn").disabled = false;
      qs("leaveBtn").disabled = true;
    }
    onWS(msg) {
      if (msg.type === "offer") {
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
          .catch(console.error);
      } else if (msg.type === "candidate") {
        msg.candidate &&
          this.pc
            .addIceCandidate(new RTCIceCandidate(msg.candidate))
            .catch(() => {});
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
    }
  };
})();
