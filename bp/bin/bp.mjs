#!/usr/bin/env node
// bp: drive a Chromium with an unpacked extension over raw CDP. No dependencies. Node 22+.
// Usage: bp <command> [args]   (bp help)
import { writeFileSync, appendFileSync, mkdirSync, existsSync, readdirSync, readFileSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import path from 'node:path';

// No default port: a shared default let a teammate attach to the product owner's own browser. Every command names its own port.
const PORT = process.env.BP_PORT;
const needPort = () => { if (!/^\d+$/.test(PORT || '')) throw new Error('set BP_PORT to a debugging port only you use (a browser you did not launch on it is not yours to attach to)'); };
// Newest Playwright Chromium on macOS; Chrome 154+ ignores --load-extension, so the branded browser cannot be used.
const playwrightChromium = () => { const dir = `${process.env.HOME}/Library/Caches/ms-playwright`; const v = existsSync(dir) ? readdirSync(dir).filter((d) => /^chromium-\d+$/.test(d)).sort((a, b) => b.slice(9) - a.slice(9))[0] : null;
  return v ? `${dir}/${v}/chrome-mac-${process.arch === 'arm64' ? 'arm64' : 'x64'}/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing` : `${dir}/chromium-*`; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function connect() {
  needPort();
  const v = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json();
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(v.webSocketDebuggerUrl); let id = 0; const pending = new Map();
    ws.onopen = () => resolve({ send: (method, params = {}, sessionId) => new Promise((res, rej) => { const msg = { id: ++id, method, params }; if (sessionId) msg.sessionId = sessionId; pending.set(msg.id, { res, rej }); ws.send(JSON.stringify(msg)); }), close: () => ws.close() });
    ws.onerror = reject;
    ws.onmessage = (m) => { const d = JSON.parse(m.data); if (d.id && pending.has(d.id)) { const p = pending.get(d.id); pending.delete(d.id); d.error ? p.rej(new Error(JSON.stringify(d.error))) : p.res(d.result); } };
  });
}
async function target(b, sel) {
  const { targetInfos } = await b.send('Target.getTargets');
  const pages = targetInfos.filter((t) => t.type === 'page');
  const t = sel ? pages.find((x) => x.targetId.startsWith(sel) || x.url.includes(sel)) : pages[0];
  if (!t) throw new Error(`no page target matching "${sel}"`);
  const { sessionId } = await b.send('Target.attachToTarget', { targetId: t.targetId, flatten: true });
  return { t, s: sessionId };
}
const evalIn = async (b, s, expression) => { const r = await b.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, s); if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || 'eval error'); return r.result.value; };
const shot = async (b, s, file) => { mkdirSync(path.dirname(file), { recursive: true }); const { data } = await b.send('Page.captureScreenshot', { format: 'png' }, s); writeFileSync(file, Buffer.from(data, 'base64')); return file; };

// Snapshot of interactive elements, walking open shadow roots and same-origin iframes.
const SNAP_JS = (hostSel) => `(() => {
  const out = []; const seen = new Set();
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = (e) => (e.getAttribute('aria-label') || e.getAttribute('placeholder') || e.innerText || e.value || e.getAttribute('title') || '').trim().replace(/\\s+/g, ' ').slice(0, 90);
  const walk = (root, p) => { for (const e of root.querySelectorAll('*')) {
    if (e.shadowRoot) walk(e.shadowRoot, p + '>' + (e.id ? '#' + e.id : e.tagName.toLowerCase()));
    if (e.tagName === 'IFRAME') { try { if (e.contentDocument) walk(e.contentDocument, p + '>iframe'); } catch {} }
    const role = e.getAttribute('role');
    if (!/^(A|BUTTON|INPUT|SELECT|TEXTAREA)$/.test(e.tagName) && !/button|link|tab|menuitem|option|checkbox|radio|switch|textbox|combobox/.test(role || '') && !e.hasAttribute('contenteditable')) continue;
    if (!vis(e)) continue; const r = e.getBoundingClientRect();
    const k = p + '|' + e.tagName + '|' + txt(e) + '|' + Math.round(r.x) + ',' + Math.round(r.y); if (seen.has(k)) continue; seen.add(k);
    out.push({ i: out.length, tag: e.tagName.toLowerCase(), type: e.type || role || '', text: txt(e), id: e.id || '', x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), checked: e.checked ?? e.getAttribute('aria-checked') ?? null, path: p });
  } };
  const root = ${hostSel ? `(() => { const h = document.querySelector(${JSON.stringify(hostSel)}); return h && h.shadowRoot ? h.shadowRoot : document; })()` : 'document'};
  walk(root, '');
  return { url: location.href, title: document.title, vw: innerWidth, vh: innerHeight, n: out.length, els: out.slice(0, 300) };
})()`;
// Text + controls of every open shadow root whose text is long enough (extension panels live here).
const HOSTS_JS = (min) => `(() => { const hs = [...document.querySelectorAll('*')].filter(e => e.shadowRoot); const txt = (e) => (e.getAttribute('aria-label') || e.getAttribute('placeholder') || e.innerText || e.value || '').trim().replace(/\\s+/g, ' ').slice(0, 90);
  return hs.map((h, i) => { const r = h.shadowRoot; const rc = h.getBoundingClientRect(); const text = (r.textContent || '').replace(/\\s+/g, ' ').trim(); if (text.length < ${min}) return null;
    const els = []; for (const e of r.querySelectorAll('button,a,input,select,textarea,[role=button],[role=tab],[role=switch],[role=checkbox],[role=radio],[role=combobox]')) { const b = e.getBoundingClientRect(); const cs = getComputedStyle(e); if (b.width < 2 || b.height < 2 || cs.visibility === 'hidden') continue; els.push({ tag: e.tagName.toLowerCase(), type: e.type || e.getAttribute('role') || '', text: txt(e), id: e.id || '', x: Math.round(b.x + b.width / 2), y: Math.round(b.y + b.height / 2) }); }
    return { i, host: h.tagName.toLowerCase() + (h.id ? '#' + h.id : '') + (typeof h.className === 'string' && h.className ? '.' + h.className.split(' ')[0] : ''), box: Math.round(rc.x) + ',' + Math.round(rc.y) + ' ' + Math.round(rc.width) + 'x' + Math.round(rc.height), text: text.slice(0, 2500), els }; }).filter(Boolean); })()`;
const printEls = (els) => els.forEach((e) => console.log(String(e.i ?? '').padStart(3), e.tag.padEnd(8), String(e.type).padEnd(9), `(${e.x},${e.y})`, JSON.stringify(e.text), e.id ? '#' + e.id : '', e.checked != null ? '[' + e.checked + ']' : ''));

const cmds = {
  async launch([url], flags) {
    const ext = flags.ext || process.env.BP_EXT; const profile = flags.profile || process.env.BP_PROFILE || `/tmp/bp-profile-${PORT}`;
    const bin = process.env.BP_CHROME || playwrightChromium();
    if (!existsSync(bin)) throw new Error(`Chromium not found at ${bin}; set BP_CHROME. Chrome 154+ ignores --load-extension, use Playwright's build.`);
    needPort();
    if (await fetch(`http://127.0.0.1:${PORT}/json/version`).then(() => true, () => false)) throw new Error(`a browser is already on :${PORT}; it is not yours to reuse, pick a free BP_PORT`);
    const vp = flags.viewport === undefined ? null : String(flags.viewport).match(/^(\d+)x(\d+)$/);
    if (flags.viewport !== undefined && !vp) throw new Error('--viewport takes WIDTHxHEIGHT, for example 390x844');
    const args = [`--user-data-dir=${profile}`, '--no-first-run', `--remote-debugging-port=${PORT}`]; if (ext) args.push(`--load-extension=${ext}`); if (vp) args.push(`--window-size=${vp[1]},${vp[2]}`); if (flags.headless || process.env.BP_HEADLESS === '1') args.push('--headless=new'); args.push(url || 'about:blank');
    const child = spawn(bin, args, { detached: true, stdio: 'ignore' }); child.unref();
    for (let i = 0; i < 40; i++) { try { await fetch(`http://127.0.0.1:${PORT}/json/version`); console.log(`up on :${PORT} pid ${child.pid} profile ${profile}${ext ? ' ext ' + ext : ''}`); return; } catch { await sleep(250); } }
    throw new Error('Chromium did not open the debug port');
  },
  async list() { const b = await connect(); const { targetInfos } = await b.send('Target.getTargets'); for (const t of targetInfos) console.log(t.type.padEnd(14), t.targetId.slice(0, 8), t.url.slice(0, 100), t.title ? '| ' + t.title.slice(0, 50) : ''); b.close(); },
  async open([url], flags) { const b = await connect(); const { targetId } = await b.send('Target.createTarget', { url }); await sleep(Number(flags.wait || 5000)); if (flags.out) { const { sessionId } = await b.send('Target.attachToTarget', { targetId, flatten: true }); console.log('shot', await shot(b, sessionId, flags.out)); } console.log('target', targetId.slice(0, 8)); b.close(); },
  async snap([sel], flags) { const b = await connect(); const { t, s } = await target(b, sel); const v = await evalIn(b, s, SNAP_JS(flags.host)); console.log(v.url.slice(0, 100), '|', v.title.slice(0, 50), '| viewport', v.vw + 'x' + v.vh, '|', v.n, 'interactive'); printEls(v.els); if (flags.out) console.log('shot', await shot(b, s, flags.out)); b.close(); },
  async hosts([sel], flags) { const b = await connect(); const { t, s } = await target(b, sel); const hs = await evalIn(b, s, HOSTS_JS(Number(flags.min || 20))); console.log(t.url.slice(0, 100), '|', hs.length, 'shadow hosts'); for (const h of hs) { console.log(`\n=== host ${h.i} ${h.host} @${h.box}\n--- text ---\n${h.text}\n--- controls ---`); printEls(h.els.map((e, i) => ({ i, ...e }))); } if (flags.out) console.log('shot', await shot(b, s, flags.out)); b.close(); },
  async text([sel], flags) { const b = await connect(); const { s } = await target(b, sel); console.log(await evalIn(b, s, `document.body.innerText.split('\\n').map(s=>s.trim()).filter(s=>s.length>1&&s.length<${flags.max || 200}).slice(0,${flags.lines || 80}).join('\\n')`)); b.close(); },
  async click([sel, x, y]) { const b = await connect(); const { t, s } = await target(b, sel); await b.send('Target.activateTarget', { targetId: t.targetId }); for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased']) await b.send('Input.dispatchMouseEvent', { type, x: +x, y: +y, button: 'left', clickCount: 1 }, s); await sleep(300); console.log('clicked', x, y); b.close(); },
  async clicktext([sel, text], flags) { const b = await connect(); const { s } = await target(b, sel); const r = await evalIn(b, s, `(() => { const re = new RegExp(${JSON.stringify(text)}, 'i'); const roots = [document, ...[...document.querySelectorAll('*')].filter(e=>e.shadowRoot).map(e=>e.shadowRoot)]; for (const root of roots) { const els = [...root.querySelectorAll('button,a,[role=button],[role=tab],label,span,div')].filter(e => e.children.length <= 2 && re.test((e.textContent||'').trim()) && e.getBoundingClientRect().width > 4); const e = els[0]; if (e) { (e.closest('button,a,[role=button],[role=tab],label') || e).click(); return 'clicked ' + e.tagName + ' ' + JSON.stringify(e.textContent.trim().slice(0,60)); } } return 'not found'; })()`); console.log(r); await sleep(Number(flags.wait || 500)); b.close(); },
  async type([sel, text], flags) {
    const b = await connect(); const { s } = await target(b, sel);
    // insertText goes to whatever has focus, so focus the target first. Searches open shadow roots, where extension panels live.
    if (flags.selector) { const found = await evalIn(b, s, `(() => { const find = (r) => r.querySelector(${JSON.stringify(flags.selector)}) || [...r.querySelectorAll('*')].filter((e) => e.shadowRoot).map((e) => find(e.shadowRoot)).find(Boolean); const e = find(document); if (e) e.focus(); return !!e; })()`); if (!found) throw new Error(`no element matching "${flags.selector}"`); }
    await b.send('Input.insertText', { text }, s); console.log('typed', JSON.stringify(text)); b.close();
  },
  async key([sel, key]) {
    // A key only triggers its default action (Enter/Space activating a button, Enter submitting a form) when the event carries `text`.
    const keys = { Enter: [13, 'Enter', '\r'], ' ': [32, 'Space', ' '], Space: [32, 'Space', ' '], Escape: [27, 'Escape'], Tab: [9, 'Tab'], Backspace: [8, 'Backspace'],
      ArrowUp: [38, 'ArrowUp'], ArrowDown: [40, 'ArrowDown'], ArrowLeft: [37, 'ArrowLeft'], ArrowRight: [39, 'ArrowRight'] };
    const [vk, code, text] = keys[key] || [0, key];
    const k = key === 'Space' ? ' ' : key;
    const b = await connect(); const { s } = await target(b, sel);
    await b.send('Input.dispatchKeyEvent', { type: text ? 'keyDown' : 'rawKeyDown', key: k, code, windowsVirtualKeyCode: vk, ...(text && { text, unmodifiedText: text }) }, s);
    await b.send('Input.dispatchKeyEvent', { type: 'keyUp', key: k, code, windowsVirtualKeyCode: vk }, s);
    console.log('key', key); b.close();
  },
  async scroll([sel, dy]) { const b = await connect(); const { s } = await target(b, sel); await b.send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: 400, y: 400, deltaX: 0, deltaY: +dy }, s); console.log('scrolled', dy); b.close(); },
  async eval([sel, expr]) { const b = await connect(); const { s } = await target(b, sel); const v = await evalIn(b, s, expr); console.log(typeof v === 'string' ? v : JSON.stringify(v, null, 1)); b.close(); },
  async upload([sel, file], flags) { const b = await connect(); const { s } = await target(b, sel); const { root } = await b.send('DOM.getDocument', { depth: -1, pierce: true }, s); const { nodeId } = await b.send('DOM.querySelector', { nodeId: root.nodeId, selector: flags.selector || 'input[type=file]' }, s); if (!nodeId) throw new Error('no file input'); await b.send('DOM.setFileInputFiles', { files: [path.resolve(file)], nodeId }, s); console.log('file set'); b.close(); },
  async shot([sel, out]) { const b = await connect(); const { s } = await target(b, sel); console.log('shot', await shot(b, s, out)); b.close(); },
  async reload([sel], flags) { const b = await connect(); const { s } = await target(b, sel); await b.send('Page.reload', {}, s); await sleep(Number(flags.wait || 5000)); console.log('reloaded'); b.close(); },
  async form([sel]) { const b = await connect(); const { s } = await target(b, sel); console.log(await evalIn(b, s, `(() => { const f=[...document.querySelectorAll('input,textarea,select')].filter(e=>e.type!=='hidden'&&e.getBoundingClientRect().height>0); const filled=f.filter(e=>e.type==='checkbox'||e.type==='radio'?e.checked:(e.value||'').length>0); return 'fields '+f.length+' filled '+filled.length+'\\n'+f.map(e=>(e.type||e.tagName)+' '+JSON.stringify((e.labels&&e.labels[0]&&e.labels[0].innerText||e.placeholder||e.name||e.id||'').slice(0,50))+' => '+((e.type==='checkbox'||e.type==='radio')?e.checked:((e.value||'').length?'[filled '+(e.value||'').length+']':'[empty]'))).join('\\n'); })()`)); b.close(); },
  async cookies([sel, action], flags) { const b = await connect(); const { s } = await target(b, sel); const urls = (flags.urls || '').split(',').filter(Boolean); const { cookies } = await b.send('Network.getCookies', urls.length ? { urls } : {}, s); if (action === 'clear') { for (const c of cookies) await b.send('Network.deleteCookies', { name: c.name, domain: c.domain }, s); console.log('cleared', cookies.length); } else console.log(cookies.map((c) => c.name + '@' + c.domain + (c.httpOnly ? '(httpOnly)' : '')).join('\n')); b.close(); },
  async record([sel, dir], flags) { mkdirSync(dir, { recursive: true }); const b = await connect(); let last = ''; let n = 0; const skip = flags.exclude ? new RegExp(flags.exclude) : null; console.log('recording', sel, '→', dir, '(Ctrl-C to stop)'); for (;;) { try { const { targetInfos } = await b.send('Target.getTargets'); const cands = targetInfos.filter((x) => x.type === 'page' && x.url.includes(sel) && !(skip && skip.test(x.url))).sort((a, c) => c.url.length - a.url.length); const t = cands[0]; if (t) { const { sessionId } = await b.send('Target.attachToTarget', { targetId: t.targetId, flatten: true }); const { data } = await b.send('Page.captureScreenshot', { format: 'png' }, sessionId); const h = createHash('md5').update(data).digest('hex').slice(0, 10); if (h !== last) { last = h; n++; const f = path.join(dir, String(n).padStart(3, '0') + '.png'); writeFileSync(f, Buffer.from(data, 'base64')); const line = `${new Date().toISOString().slice(11, 19)} ${path.basename(f)} ${t.url.slice(0, 100)} | ${(t.title || '').slice(0, 60)}`; console.log(line); appendFileSync(path.join(dir, 'index.md'), line + '\n'); } await b.send('Target.detachFromTarget', { sessionId }); } } catch {} await sleep(Number(flags.interval || 2000)); } },
  async close([sel]) { const b = await connect(); const { t } = await target(b, sel); await b.send('Target.closeTarget', { targetId: t.targetId }); console.log('closed', t.url.slice(0, 80)); b.close(); },
  async goal([url, ...goals], flags) {
    needPort();
    // Run compose's jev (../../jev) against THIS bp Chromium: browser-harness attaches via BU_CDP_URL, jev picks actions with TypeSafe.
    const jev = process.env.JEV_DIR || path.resolve(import.meta.dirname, '../../jev');
    if (!goals.length) throw new Error('usage: bp goal <url> "<goal>" ["<goal 2>" ...]');
    const envFile = `${process.env.HOME}/.compose/.env`;
    // jev's runner prints a line per step, then why it stopped and a JEV_RESULT line, and exits 0 only when the goal is done.
    const args = ['run', '--quiet', ...(existsSync(envFile) ? ['--env-file', envFile] : []), 'python', '-m', 'jev_ultrafast', '--progress',
      ...(flags.secrets ? ['--secrets', flags.secrets] : []), '--', url, ...goals];
    // Keys for the jev child come from the compose config dir ($COMPOSE_CONFIG's dir, else ~/.compose): the active workspace's
    // OpenRouter key, else default's, plus the top-level TypeSafe key. Missing or unreadable config/auth: run as today, no keys.
    const cfgDir = process.env.COMPOSE_CONFIG ? path.dirname(process.env.COMPOSE_CONFIG) : `${process.env.HOME}/.compose`;
    const keys = {};
    try {
      const ws = JSON.parse(readFileSync(path.join(cfgDir, 'config.json'), 'utf8')).workspace || 'default';
      const a = JSON.parse(readFileSync(path.join(cfgDir, 'auth.json'), 'utf8'));
      const or = a.workspaces?.[ws]?.openrouter ?? a.workspaces?.default?.openrouter;
      if (a.typesafe) keys.TYPESAFE_API_KEY = a.typesafe;
      if (or) keys.OPENROUTER_API_KEY = keys.TEXT_MODEL_API_KEY = or;
    } catch {}
    // A caller's BU_NAME stays (a verify stage ends the daemons by its name); bp<port> otherwise.
    const env = { ...process.env, ...keys, BU_CDP_URL: `http://127.0.0.1:${PORT}`, BU_NAME: process.env.BU_NAME || `bp${PORT}` };
    console.log(`jev → ${url} on :${PORT} | goals: ${goals.join(' | ')}`);
    // goal's exit code is jev's, so a script or an e2e driver can tell a reached goal from a failed or blocked one.
    await new Promise((resolve) => {
      const c = spawn('uv', args, { cwd: jev, env, stdio: 'inherit' });
      c.on('error', (e) => { console.error('jev did not start:', e.message); process.exitCode = 1; resolve(); });
      c.on('exit', (code, signal) => { if (code || signal) { console.error('jev exited', code ?? signal); process.exitCode = code || 1; } resolve(); });
    });
  },
  async kill() { needPort(); spawn('pkill', ['-f', `remote-debugging-port=${PORT}`]); console.log('killed Chromium on', PORT); },
  async help() { console.log(`bp - drive Chromium + an unpacked extension over CDP. Env: BP_PORT (required, a port only you use), BP_EXT, BP_PROFILE, BP_CHROME.
  launch [url] --ext <dir> --profile <dir> [--headless] [--viewport WxH]   start Chromium with the extension, debug port on (--headless or BP_HEADLESS=1: no window; --viewport 390x844: window size, for mobile shots; refuses a port that already has a browser)
  list                                         targets (pages, workers, iframes); page ids are the <sel> below
  open <url> [--wait ms] [--out shot.png]      new tab
  snap <sel> [--host <css>] [--out png]        interactive elements + coords (walks shadow roots/iframes); --host limits to one shadow root
  hosts <sel> [--min n] [--out png]            text + controls of every shadow root (extension panels live here)
  text <sel> [--lines n]                       page text
  form <sel>                                   every form field and whether it is filled (no values)
  click <sel> <x> <y> | clicktext <sel> <regex> | type <sel> <text> [--selector css] | key <sel> <Enter|Space|Escape|Tab|Arrow*> | scroll <sel> <dy>
  eval <sel> <js>                              run JS in the page, prints the value
  upload <sel> <file> [--selector css]         set a file input
  shot <sel> <out.png> | reload <sel> [--wait ms] | close <sel>
  cookies <sel> [clear] [--urls a,b]           list or clear cookies (logs the user out of that site)
  record <url-substr> <dir> [--interval ms] [--exclude regex]   screenshot on every change + index.md
  goal <url> "<goal>" ["<goal 2>"...] [--secrets '{"Password":"$E2E_PASSWORD"}']   run compose's jev against this Chromium toward a goal;
                                               say where to stop in the goal. jev types each secret field from that env var itself.
                                               Ends with a JEV_RESULT line; exits non-zero unless the goal is done (keeps a set BU_NAME)
  kill                                         stop the Chromium on this port
<sel> is a target id prefix or a URL substring.`); },
};
const [cmd = 'help', ...rest] = process.argv.slice(2);
const flags = {}; const args = [];
for (let i = 0; i < rest.length; i++) { if (rest[i].startsWith('--')) { flags[rest[i].slice(2)] = rest[i + 1] && !rest[i + 1].startsWith('--') ? rest[++i] : true; } else args.push(rest[i]); }
if (['--help', '-h'].includes(cmd) || flags.help || args.includes('-h')) { await cmds.help(); process.exit(0); }
if (!cmds[cmd]) { console.error('unknown command', cmd); await cmds.help(); process.exit(2); }
try { await cmds[cmd](args, flags); } catch (e) { console.error('error:', e.message); process.exit(1); }
