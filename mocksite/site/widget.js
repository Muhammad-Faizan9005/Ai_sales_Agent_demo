/* Systematic IT sales agent — chat widget.
 *
 * One <script defer>, no build step, no framework. Everything lives in a
 * Shadow DOM so the host page's CSS cannot restyle us and ours cannot leak
 * out (WordPress + Elementor ship a lot of aggressive global CSS).
 *
 * The hard part is NOT the chat UI, it is surviving navigation. This runs on
 * a real multi-page site: clicking a link destroys the document and the
 * widget remounts from zero. So the transcript, the session id, and the
 * open/closed state all live in sessionStorage and are restored on mount.
 * Phase 3 exit criteria: chat on page A -> navigate to B -> thread intact
 * and still open.
 *
 * sessionStorage (not localStorage) on purpose: the thread should die with
 * the tab, matching the server's in-memory session lifetime.
 */
(() => {
  'use strict';

  const API = window.SALES_AGENT_API || 'http://localhost:8002';
  const KEY = 'sysit_agent_v1';
  const MAX_TURNS = 40; // keep sessionStorage well under its ~5MB cap

  // ---------------------------------------------------------------- state

  const blank = () => ({
    sid: 'w-' + Math.random().toString(36).slice(2, 12) + Date.now().toString(36),
    turns: [],
    open: false,
    greeted: false,
  });

  function load() {
    try {
      const raw = sessionStorage.getItem(KEY);
      if (!raw) return blank();
      const s = JSON.parse(raw);
      // A session id shorter than 8 chars is rejected by the API (422), so a
      // corrupted or hand-edited value must not be trusted.
      if (typeof s.sid !== 'string' || s.sid.length < 8) return blank();
      s.turns = Array.isArray(s.turns) ? s.turns.slice(-MAX_TURNS) : [];
      return s;
    } catch {
      return blank(); // private mode / quota / bad JSON — never block the page
    }
  }

  function save() {
    try {
      sessionStorage.setItem(
        KEY,
        JSON.stringify({ ...state, turns: state.turns.slice(-MAX_TURNS) })
      );
    } catch {
      /* storage full or blocked: degrade to in-memory only */
    }
  }

  const state = load();
  let streaming = false;

  // ------------------------------------------------------------------ dom

  const host = document.createElement('div');
  host.id = 'sysit-agent-host';
  // Fixed here rather than in the sheet: the host is the one element the page
  // can see, and it must not participate in the page's layout at all.
  host.style.cssText = 'position:fixed;inset:auto 0 0 auto;z-index:2147483000';
  const root = host.attachShadow({ mode: 'open' });

  root.innerHTML = `
<style>
  :host, * { box-sizing: border-box; }
  :host {
    /* all-initial would also nuke our own inherited text rendering, so we
       set the few properties Elementor is most likely to have poisoned. */
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 15px; line-height: 1.5; color: #17232e;
  }
  button { font: inherit; cursor: pointer; border: 0; background: none; color: inherit; }

  .launcher {
    position: fixed; right: 20px; bottom: 20px;
    display: flex; align-items: center; gap: 10px;
    padding: 13px 20px 13px 17px; border-radius: 999px;
    background: #0d5cab; color: #fff; font-weight: 600;
    box-shadow: 0 6px 22px rgba(13, 92, 171, .34);
    transition: transform .15s ease, box-shadow .15s ease;
  }
  .launcher:hover { transform: translateY(-2px); box-shadow: 0 9px 26px rgba(13,92,171,.42); }
  .launcher[hidden] { display: none; }
  .launcher svg { width: 19px; height: 19px; flex: none; }

  .panel {
    position: fixed; right: 20px; bottom: 20px;
    width: 384px; height: 588px; max-height: calc(100vh - 40px);
    display: flex; flex-direction: column; overflow: hidden;
    background: #fff; border-radius: 16px;
    border: 1px solid #dde4ec;
    box-shadow: 0 18px 52px rgba(16, 32, 48, .22);
  }
  .panel[hidden] { display: none; }

  header {
    display: flex; align-items: center; gap: 11px;
    padding: 14px 15px; background: #0d5cab; color: #fff; flex: none;
  }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: #46d67f; flex: none; }
  .who { font-weight: 600; font-size: 15px; }
  .sub { font-size: 12px; opacity: .82; }
  header button { margin-left: auto; font-size: 22px; line-height: 1; opacity: .85; padding: 0 4px; }
  header button:hover { opacity: 1; }

  .log { flex: 1; overflow-y: auto; padding: 16px 15px 4px; background: #f7f9fc; }
  .row { display: flex; margin-bottom: 11px; }
  .row.me { justify-content: flex-end; }
  .bub {
    max-width: 82%; padding: 10px 13px; border-radius: 14px;
    white-space: pre-wrap; overflow-wrap: anywhere;
  }
  .them .bub { background: #fff; border: 1px solid #e3e9f0; border-bottom-left-radius: 5px; }
  .me   .bub { background: #0d5cab; color: #fff; border-bottom-right-radius: 5px; }
  .note {
    font-size: 12.5px; color: #5c7085; text-align: center;
    padding: 5px 12px 9px; font-style: italic;
  }
  .cursor::after {
    content: '▍'; margin-left: 1px; opacity: .55;
    animation: blink 1.05s steps(2, start) infinite;
  }
  @keyframes blink { 50% { opacity: 0; } }
  @media (prefers-reduced-motion: reduce) {
    .cursor::after { animation: none; }
    .launcher { transition: none; }
  }

  form { display: flex; gap: 8px; padding: 11px; border-top: 1px solid #e6ebf1; flex: none; background: #fff; }
  textarea {
    flex: 1; resize: none; max-height: 104px; padding: 9px 11px;
    border: 1px solid #cfd8e3; border-radius: 10px; font: inherit; color: inherit;
  }
  textarea:focus { outline: 2px solid #0d5cab; outline-offset: -1px; border-color: #0d5cab; }
  .send {
    align-self: flex-end; width: 39px; height: 39px; border-radius: 10px;
    background: #0d5cab; color: #fff; display: grid; place-items: center; flex: none;
  }
  .send:disabled { opacity: .45; cursor: default; }
  .send svg { width: 17px; height: 17px; }

  .consent { padding: 0 12px 10px; font-size: 11.5px; color: #6b7c8f; text-align: center; background: #fff; flex: none; }
  .consent a { color: #0d5cab; }

  /* Req 12: usable at 375px. Below 420 the panel goes edge to edge. */
  @media (max-width: 420px) {
    .panel {
      right: 0; bottom: 0; width: 100vw; height: 100dvh;
      max-height: none; border-radius: 0; border: 0;
    }
    .launcher { right: 14px; bottom: 14px; }
  }
</style>

<button class="launcher" part="launcher" aria-label="Chat with our team">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
       stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9.9 9.9 0 0 1-3.4-.6L3 21l1.9-4.8A8.4 8.4 0 0 1 12 3a8.4 8.4 0 0 1 9 8.5Z"/>
  </svg>
  <span>Chat with us</span>
</button>

<section class="panel" role="dialog" aria-label="Chat with Systematic IT Solutions" hidden>
  <header>
    <span class="dot" aria-hidden="true"></span>
    <div>
      <div class="who">Systematic IT Solutions</div>
      <div class="sub">Usually replies in a few seconds</div>
    </div>
    <button class="x" aria-label="Close chat">&times;</button>
  </header>
  <div class="log" role="log" aria-live="polite" aria-atomic="false"></div>
  <form>
    <textarea rows="1" placeholder="Ask about our services…" aria-label="Your message"
              maxlength="4000" enterkeyhint="send"></textarea>
    <button class="send" type="submit" aria-label="Send">
      <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <path d="M3.2 20.6 22 12 3.2 3.4l.02 6.7L16 12 3.22 13.9Z"/>
      </svg>
    </button>
  </form>
  <div class="consent">
    You're chatting with an AI assistant. Details you share are used to follow up
    on your enquiry.
  </div>
</section>`;

  const $ = (s) => root.querySelector(s);
  const launcher = $('.launcher');
  const panel = $('.panel');
  const log = $('.log');
  const form = $('form');
  const input = $('textarea');
  const sendBtn = $('.send');

  // --------------------------------------------------------------- render

  const atBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 60;
  const toBottom = () => { log.scrollTop = log.scrollHeight; };

  function bubble(role, text) {
    const row = document.createElement('div');
    row.className = 'row ' + (role === 'user' ? 'me' : 'them');
    const b = document.createElement('div');
    b.className = 'bub';
    b.textContent = text;
    row.append(b);
    log.append(row);
    return b;
  }

  function note(text) {
    const n = document.createElement('div');
    n.className = 'note';
    n.textContent = text;
    log.append(n);
  }

  function paint() {
    log.textContent = '';
    for (const t of state.turns) {
      if (t.role === 'note') note(t.text);
      else bubble(t.role, t.text);
    }
    toBottom();
  }

  function push(role, text) {
    state.turns.push({ role, text });
    save();
  }

  // ----------------------------------------------------------- open/close

  function open() {
    state.open = true;
    save();
    launcher.hidden = true;
    panel.hidden = false;
    if (!state.greeted && !state.turns.length) {
      state.greeted = true;
      push(
        'assistant',
        "Hi! I can walk you through what Systematic IT Solutions does and point you to the right service. What are you working on?"
      );
      paint();
    }
    toBottom();
    // Don't steal focus on a rehydrated mount — the visitor was reading the
    // page, not the widget.
    if (!prefersNoFocus) input.focus();
  }

  function close() {
    state.open = false;
    save();
    panel.hidden = true;
    launcher.hidden = false;
  }

  let prefersNoFocus = false;

  launcher.addEventListener('click', () => open());
  $('.x').addEventListener('click', close);

  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 104) + 'px';
  });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
    if (e.key === 'Escape') close();
  });

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || streaming) return;
    input.value = '';
    input.style.height = 'auto';
    send(text);
  });

  // ------------------------------------------------------------ streaming

  /* fetch + manual SSE parse rather than EventSource: EventSource is GET-only
   * and cannot send a JSON body. The server frames events as
   *   event: <name>\n data: <json>\n\n
   * with names token | tool | handoff | error | done (api/chat.py). */
  async function send(text) {
    push('user', text);
    bubble('user', text);
    toBottom();

    streaming = true;
    sendBtn.disabled = true;

    const bub = bubble('assistant', '');
    bub.classList.add('cursor');
    let answer = '';
    let handoff = false;

    try {
      const res = await fetch(API + '/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: text,
          session_id: state.sid,
          page_path: location.pathname,
        }),
      });

      if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);

      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';

      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });

        // Frames are \n\n-delimited; the tail may be a partial frame.
        const frames = buf.split('\n\n');
        buf = frames.pop() ?? '';

        for (const frame of frames) {
          let name = 'message';
          const data = [];
          for (const line of frame.split('\n')) {
            if (line.startsWith('event:')) name = line.slice(6).trim();
            else if (line.startsWith('data:')) data.push(line.slice(5).trim());
          }
          if (!data.length) continue;

          let payload;
          try {
            payload = JSON.parse(data.join('\n'));
          } catch {
            continue;
          }

          if (name === 'token' && payload.text) {
            answer += payload.text;
            bub.textContent = answer;
            if (atBottom()) toBottom();
          } else if (name === 'tool') {
            // Only surface tools the visitor should feel. save_lead and
            // check_page are internal bookkeeping and stay silent.
            const said = {
              book_meeting: 'Booking your meeting…',
              get_available_slots: 'Checking the calendar…',
              request_proposal: 'Preparing your proposal request…',
            }[payload.name];
            if (said) note(said);
          } else if (name === 'handoff') {
            handoff = true;
          } else if (name === 'error') {
            throw new Error(payload.message || 'stream error');
          }
        }
      }
    } catch (err) {
      if (!answer) {
        answer =
          "Sorry — I couldn't reach our system just then. Please try again, or email info@systematicitsolutions.com.";
        bub.textContent = answer;
      }
      console.warn('[sales-agent]', err);
    } finally {
      bub.classList.remove('cursor');
      streaming = false;
      sendBtn.disabled = false;
    }

    if (answer) push('assistant', answer);
    if (handoff) {
      const msg = 'Connecting you with someone from the team…';
      note(msg);
      push('note', msg);
      toBottom();
      handoffToHuman();
    }
  }

  /* Tawk.to is already installed on the client's site. If it loaded, hand the
   * visitor over; if not, this is a no-op and the transcript line above is
   * the only trace. */
  function handoffToHuman() {
    try {
      if (window.Tawk_API && typeof window.Tawk_API.maximize === 'function') {
        window.Tawk_API.maximize();
      }
    } catch {
      /* Tawk not ready — nothing to do */
    }
  }

  // Best-effort session close so the server can finalise + emit its event.
  // keepalive lets the request outlive the document being torn down.
  addEventListener('pagehide', () => {
    if (!state.turns.length) return;
    try {
      fetch(API + '/api/end', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: state.sid }),
        keepalive: true,
      }).catch(() => {});
    } catch {
      /* ignore */
    }
  });

  // ----------------------------------------------------------------- mount

  document.documentElement.append(host);

  // Restore the thread *before* deciding to open, so a rehydrated panel is
  // never briefly empty. This is the phase 3 exit criterion.
  if (state.turns.length) paint();
  if (state.open) {
    prefersNoFocus = true;
    open();
    prefersNoFocus = false;
  }
})();
