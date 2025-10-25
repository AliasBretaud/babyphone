const fs = require('fs');
const path = require('path');
const http = require('http');
const https = require('https');
const express = require('express');
const WebSocket = require('ws');

const app = express();
app.use(express.static(path.join(__dirname, 'public')));
app.get('/', (_req, res) => res.sendFile(path.join(__dirname, 'public', 'index.html')));
app.get('/viewer', (_req, res) => res.sendFile(path.join(__dirname, 'public', 'viewer.html')));
app.get('/broadcaster', (_req, res) => res.sendFile(path.join(__dirname, 'public', 'broadcaster.html')));

// HTTP redirector -> HTTPS:3443
const httpServer = http.createServer((req, res) => {
  const host = (req.headers.host || '').split(':')[0] || 'localhost';
  res.statusCode = 301;
  res.setHeader('Location', `https://${host}:3443${req.url}`);
  res.end();
});
httpServer.listen(3000, () => console.log('HTTP redirector on :3000 -> https://<host>:3443'));

// HTTPS server with certs
const keyPath = process.env.CERT_KEY || path.join(process.env.CERT_DIR || path.join(__dirname, 'certs'), 'server.key');
const crtPath = process.env.CERT_CRT || path.join(process.env.CERT_DIR || path.join(__dirname, 'certs'), 'server.crt');

if (!fs.existsSync(keyPath) || !fs.existsSync(crtPath)) {
  console.error('Missing TLS files. Expected at:', { keyPath, crtPath });
  console.error('If using docker compose, a certgen init service should create them automatically.');
  process.exit(1);
}

const httpsServer = https.createServer({
  key: fs.readFileSync(keyPath),
  cert: fs.readFileSync(crtPath),
}, app);

const wss = new WebSocket.Server({ server: httpsServer, path: '/ws' });

// --- Signaling (rooms in memory) ---
const rooms = new Map();
function getRoom(name){ if(!rooms.has(name)) rooms.set(name,{broadcasters:new Set(), viewers:new Set()}); return rooms.get(name); }
function safeSend(ws,obj){ try{ ws.readyState===WebSocket.OPEN && ws.send(JSON.stringify(obj)); }catch(_){} }

wss.on('connection', (ws) => {
  ws.meta = { role: null, room: null, id: Math.random().toString(36).slice(2) };
  ws.on('message', (data) => {
    let msg; try { msg = JSON.parse(data); } catch { return; }
    const { type } = msg;
    if (type === 'join') {
      const { room, role } = msg;
      ws.meta.role = role; ws.meta.room = room;
      const r = getRoom(room);
      if (role === 'broadcaster') r.broadcasters.add(ws); else r.viewers.add(ws);
      if (role === 'viewer') r.broadcasters.forEach(bws => safeSend(bws, { type: 'viewer-joined', viewerId: ws.meta.id }));
      return;
    }
    if (type === 'offer' || type === 'answer' || type === 'candidate') {
      const r = getRoom(ws.meta.room); if(!r) return;
      const targets = new Set();
      if (msg.targetId) { [...r.broadcasters, ...r.viewers].forEach(p => p.meta?.id===msg.targetId && targets.add(p)); }
      else { (ws.meta.role==='broadcaster'? r.viewers : r.broadcasters).forEach(p => targets.add(p)); }
      targets.forEach(peer => safeSend(peer, { ...msg, fromId: ws.meta.id }));
    }
  });
  ws.on('close', () => {
    const { room, role, id } = ws.meta || {}; if (!room) return;
    const r = getRoom(room);
    (role==='broadcaster' ? r.broadcasters : r.viewers).delete(ws);
    [...r.broadcasters, ...r.viewers].forEach(peer => safeSend(peer, { type:'peer-left', peerId:id }));
  });
});

httpsServer.listen(3443, () => console.log('HTTPS BabyPhone on https://0.0.0.0:3443'));
