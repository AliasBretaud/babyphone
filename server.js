const express = require("express");
const http = require("http");
const path = require("path");
const WebSocket = require("ws");

const app = express();
const server = http.createServer(app);
const wss = new WebSocket.Server({ server, path: "/ws" });

// Static + routes
app.use(express.static(path.join(__dirname, "public")));
app.get("/", (_req, res) =>
  res.sendFile(path.join(__dirname, "public", "index.html"))
);
app.get("/viewer", (_req, res) =>
  res.sendFile(path.join(__dirname, "public", "viewer.html"))
);
app.get("/broadcaster", (_req, res) =>
  res.sendFile(path.join(__dirname, "public", "broadcaster.html"))
);

// Simple in-memory rooms
const rooms = new Map(); // room => { broadcasters:Set, viewers:Set }
function getRoom(name) {
  if (!rooms.has(name))
    rooms.set(name, { broadcasters: new Set(), viewers: new Set() });
  return rooms.get(name);
}
function safeSend(ws, obj) {
  try {
    ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify(obj));
  } catch (_) {}
}

wss.on("connection", (ws) => {
  ws.meta = { role: null, room: null, id: Math.random().toString(36).slice(2) };

  ws.on("message", (data) => {
    let msg;
    try {
      msg = JSON.parse(data);
    } catch {
      return;
    }
    const { type } = msg;

    if (type === "join") {
      const { room, role } = msg;
      ws.meta.role = role;
      ws.meta.room = room;
      const r = getRoom(room);
      if (role === "broadcaster") r.broadcasters.add(ws);
      else r.viewers.add(ws);
      if (role === "viewer")
        r.broadcasters.forEach((bws) =>
          safeSend(bws, { type: "viewer-joined", viewerId: ws.meta.id })
        );
      return;
    }

    if (type === "offer" || type === "answer" || type === "candidate") {
      const r = getRoom(ws.meta.room);
      if (!r) return;
      const targets = new Set();
      if (msg.targetId) {
        [...r.broadcasters, ...r.viewers].forEach((peer) => {
          if (peer.meta?.id === msg.targetId) targets.add(peer);
        });
      } else {
        const pool =
          ws.meta.role === "broadcaster" ? r.viewers : r.broadcasters;
        pool.forEach((p) => targets.add(p));
      }
      targets.forEach((peer) => safeSend(peer, { ...msg, fromId: ws.meta.id }));
      return;
    }
  });

  ws.on("close", () => {
    const { room, role, id } = ws.meta || {};
    if (!room) return;
    const r = getRoom(room);
    (role === "broadcaster" ? r.broadcasters : r.viewers).delete(ws);
    [...r.broadcasters, ...r.viewers].forEach((peer) =>
      safeSend(peer, { type: "peer-left", peerId: id })
    );
  });
});

const PORT = process.env.PORT || 3000;
server.listen(PORT, () =>
  console.log(`BabyPhone server on http://0.0.0.0:${PORT}`)
);
