/* ── The web terminal (QA-58) ────────────────────────────────────────────────
   A shell on a managed host, in the browser. Not loaded by base.html:
   loadTerminalAssets() in vigil-host-cards.js appends this file (and xterm)
   the first time an operator opens a session, so nobody pays for 260 KB of
   vendor JS they never use.

   The pipe is apps/remote/ws.py: it sends status, out and mode frames and
   takes in and resize. Output goes to xterm through term.write() and never
   into markup — a shell's output is arbitrary bytes, and any path from those
   bytes to an element's content would let a crafted command line run script
   with the operator's session cookie.

   Windows agents have no PTY (QA-57 reports that with {"t":"mode","pty":false}),
   so there is no line editor on the far end: this side echoes what is typed,
   edits a buffer on Backspace, and sends a whole line on Enter. Typing into
   such a session one character at a time would look like a dead terminal. */

const termSession = { ws: null, term: null, fit: null, observer: null,
                      lineMode: false, buffer: '', closed: false, debounce: null };

async function openSession(hostId, kind, container) {
  const isContainer = kind === 'container';
  const code = await totpPrompt(
    isContainer ? `Open a shell in container ${container}` : 'Open a web shell on this host',
    { title: 'Start a session', confirmText: 'Connect' });
  if (!code) return;

  let started;
  try {
    started = await apiJson('/api/v1/sessions/', {
      method: 'POST',
      body: JSON.stringify({ host: hostId, kind: kind, container: container, totp: code }),
    });
  } catch (err) {
    showToast(err.message, 'error');
    return;
  }
  _mountTermDialog(started, kind, container);
}

function _termTitle(kind, host, container) {
  const name = host || 'the agent';
  return kind === 'container' ? `${container} on ${name}` : `Shell — ${name}`;
}

function _mountTermDialog(started, kind, container) {
  const m = mountModal('term', { xwide: true });
  m.modal.classList.add('term-modal');
  m.setBody(`<div class="modal-title"><span id="term-title"></span>
      <button class="modal-close" type="button" id="term-x" aria-label="Close">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button></div>
    <div class="term-bar">
      <span class="run-state t-lemon" id="term-state">Waiting for the agent…</span>
      ${started.recorded ? '<span class="chip chip-muted" title="What you type here is written to the session recording">Recorded</span>' : ''}
      <button class="btn btn-outline btn-rose-ghost btn-xs" type="button" id="term-end">End session</button>
    </div>
    <div class="term-host" id="term-host" data-keeps-tab></div>`);
  m.modal.querySelector('#term-title').textContent =
    _termTitle(kind, started.host, container);

  const stateEl = m.modal.querySelector('#term-state');
  const hostEl = m.modal.querySelector('#term-host');
  const setState = (text, cls) => {
    stateEl.textContent = text;
    stateEl.className = 'run-state ' + cls;
  };

  const term = new Terminal({
    cursorBlink: true,
    fontFamily: 'IBM Plex Mono, ui-monospace, monospace',
    fontSize: 13,
    theme: {
      background: cssVar('--bg') || '#15161b',
      foreground: cssVar('--text-1') || '#d7d9e0',
      cursor: cssVar('--mint') || '#7fcf8a',
      selectionBackground: 'rgba(127, 207, 138, 0.25)',
    },
  });
  const fit = new window.FitAddon.FitAddon();
  term.loadAddon(fit);

  Object.assign(termSession, {
    term: term, fit: fit, lineMode: false, buffer: '', closed: false,
  });

  term.open(hostEl);
  fit.fit();
  term.focus();

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}${started.ws}`);
  ws.binaryType = 'arraybuffer';
  termSession.ws = ws;

  // Line mode is this side's editor, so the keystrokes need echoing here —
  // there is no tty on a Windows agent to echo them back.
  const echo = (s) => {
    term.write(s);
    fit.fit();
  };

  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.t === 'status') _termStatus(msg, stateEl, term, setState);
    else if (msg.t === 'out') term.write(_bytesFromB64(msg.d || ''));
    else if (msg.t === 'mode' && msg.pty === false) {
      termSession.lineMode = true;
      term.writeln('[no terminal on this agent — press Enter to send each line]');
    }
  };
  ws.onclose = () => {
    // The dialog stays up with the reason on it: closing it out from under a
    // read would take the last screenful of output with it.
    termSession.closed = true;
    setState('Closed', 't-rose');
  };
  ws.onerror = () => setState('The connection failed', 't-rose');

  term.onData((s) => {
    if (termSession.closed || ws.readyState !== WebSocket.OPEN) return;
    if (!termSession.lineMode) { _sendInput(ws, s); return; }
    for (const ch of s) {
      if (ch === '\r') {
        term.write('\r\n');
        ws.send(JSON.stringify({ t: 'in', d: _b64FromUtf8(termSession.buffer + '\r\n') }));
        termSession.buffer = '';
      } else if (ch === '\x7f') {          // Backspace
        if (!termSession.buffer) continue;
        termSession.buffer = termSession.buffer.slice(0, -1);
        term.write('\b \b');
      } else if (ch >= ' ') {
        termSession.buffer += ch;
        echo(ch);
      }
    }
  });

  // A drag on the dialog's grip, a window resize, a sidebar opening: all of
  // them mean xterm should re-measure and tell the agent its new size. One
  // resize per settled frame, or a drag sends dozens of SIGWINCHes.
  const observer = new ResizeObserver(() => {
    clearTimeout(termSession.debounce);
    termSession.debounce = setTimeout(() => {
      fit.fit();
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ t: 'resize', cols: term.cols, rows: term.rows }));
      }
    }, 150);
  });
  observer.observe(m.modal);
  termSession.observer = observer;

  // Named so teardown can remove it: a listener left on document would end
  // the *next* session too, since termSession is shared.
  const onKey = (ev) => {
    if (ev.key === 'Escape' && m.modal.classList.contains('open')) end();
  };
  const teardown = () => {
    document.removeEventListener('keydown', onKey);
    if (termSession.closed) return;
    termSession.closed = true;
    clearTimeout(termSession.debounce);
    observer.disconnect();
    termSession.observer = null;
    try { ws.close(); } catch (e) {}
    try { term.dispose(); } catch (e) {}
    termSession.term = null;
    termSession.ws = null;
  };

  const end = () => { teardown(); _endSession(started.id); m.close(); };
  m.modal.querySelector('#term-end').addEventListener('click', end);
  m.modal.querySelector('#term-x').addEventListener('click', end);
  // addEventListener rather than assigning overlay.onclick: the text that
  // bans inline handlers scans lines containing "onclick =", and an assignment
  // would have to be carved out of it for no benefit.
  m.overlay.addEventListener('click', () => { teardown(); m.close(); });
  document.addEventListener('keydown', onKey);

  m.open();
  requestAnimationFrame(() => { fit.fit(); term.focus(); });
}

function _termStatus(msg, stateEl, term, setState) {
  if (msg.state === 'waiting') {
    setState('Waiting for the agent…', 't-lemon');
  } else if (msg.state === 'open') {
    setState('Connected', 't-mint');
  } else if (msg.state === 'closed') {
    setState(`Closed: ${msg.reason || 'the session ended'}`, 't-rose');
    term.write(`\r\n\x1b[2m[session closed: ${_plain(msg.reason || 'the session ended')}]\x1b[0m`);
  }
}

//: The one piece of server text this terminal formats, and it is still plain:
//: no escape byte may reach xterm from a reason string.
function _plain(text) {
  // Every control character, not just colour codes: ESC starts sequences
  // that can retitle the window or rewrite the screen.
  return String(text).replace(/[\x00-\x1f\x7f-\x9f]/g, '').slice(0, 120);
}

function _sendInput(ws, s) {
  ws.send(JSON.stringify({ t: 'in', d: _b64FromUtf8(s) }));
}

async function _endSession(sessionId) {
  try {
    await apiJson(`/api/v1/sessions/${sessionId}/close/`, { method: 'POST' });
  } catch (e) {
    // The socket we just closed is the authoritative end; a 404 here means it
    // had already finished, which is what the operator asked for.
  }
}

//: Encode to UTF-8 bytes before encoding them, because the base64 step throws
//: on anything above U+00FF — and a UTF-8 filename pasted into a shell is an
//: ordinary thing to type.
function _b64FromUtf8(text) {
  const bytes = new TextEncoder().encode(text);
  let binary = '';
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function _bytesFromB64(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
  return out;
}
