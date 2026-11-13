#!/usr/bin/env node
// A stand-in for Chromium that speaks just enough CDP over a WebSocket for bp: one page target, a version, and Page.captureScreenshot,
// which it answers with a one-pixel png, or never answers when FAKE_SHOT=hang (what Chrome for Testing 149 did on macOS).
// Env: FAKE_SHOT=ok|hang, FAKE_PRODUCT. Flag: --remote-debugging-port=<n>.
import { createServer } from 'node:http';
import { createHash } from 'node:crypto';

const port = Number(process.argv.find((a) => a.startsWith('--remote-debugging-port='))?.split('=')[1]);
const hang = process.env.FAKE_SHOT === 'hang';
const product = process.env.FAKE_PRODUCT || 'FakeChrome/1.0';
const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==';

const reply = (sock, msg) => {
  const body = Buffer.from(JSON.stringify(msg));
  const head = body.length < 126 ? Buffer.from([0x81, body.length]) : Buffer.from([0x81, 126, body.length >> 8, body.length & 255]);
  sock.write(Buffer.concat([head, body]));
};
const answer = (sock, m) => {
  const result = {
    'Target.getTargets': { targetInfos: [{ type: 'page', targetId: 'T0FAKE0000', url: 'about:blank', title: '' }] },
    'Target.attachToTarget': { sessionId: 'S0FAKE' },
    'Browser.getVersion': { product },
    'Page.captureScreenshot': { data: PNG },
  }[m.method] ?? {};
  if (m.method === 'Page.captureScreenshot' && hang) return;
  reply(sock, { id: m.id, result, ...(m.sessionId && { sessionId: m.sessionId }) });
};

const server = createServer((req, res) => res.end(req.url === '/json/version' ? JSON.stringify({ Browser: product, webSocketDebuggerUrl: `ws://127.0.0.1:${port}/devtools/browser/fake` }) : '{}'));
server.on('upgrade', (req, sock) => {
  const accept = createHash('sha1').update(req.headers['sec-websocket-key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
  sock.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
  let buf = Buffer.alloc(0);
  sock.on('data', (d) => {
    buf = Buffer.concat([buf, d]);
    for (;;) {  // client frames are masked text frames; a message may arrive in pieces or several at once
      if (buf.length < 2) return;
      let len = buf[1] & 127; let at = 2;
      if (len === 126) { if (buf.length < 4) return; len = buf.readUInt16BE(2); at = 4; }
      if (buf.length < at + 4 + len) return;
      const mask = buf.subarray(at, at + 4); const payload = Buffer.from(buf.subarray(at + 4, at + 4 + len)).map((b, i) => b ^ mask[i % 4]);
      const opcode = buf[0] & 15;
      buf = buf.subarray(at + 4 + len);
      if (opcode === 8) { sock.end(Buffer.from([0x88, 0])); return; }  // close: answer it, or the client waits for the handshake
      if (opcode === 1) answer(sock, JSON.parse(payload.toString()));
    }
  });
  sock.on('error', () => {});
});
server.listen(port, '127.0.0.1');
