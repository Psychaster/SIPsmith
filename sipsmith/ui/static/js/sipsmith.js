/* SIPsmith — vanilla JS helpers. No framework, no CDN. */

'use strict';

// ── Alert helper ─────────────────────────────────────────────────────────

function showAlert(msg, type = 'error') {
  const el = document.createElement('div');
  el.className = `alert alert-${type}`;
  el.textContent = msg;
  const body = document.querySelector('.content-body') || document.querySelector('.auth-card');
  if (body) body.prepend(el);
  setTimeout(() => el.remove(), 5000);
}

// ── Top-bar time display ─────────────────────────────────────────────────

function updateTimeDisplay(event) {
  const el = document.getElementById('time-display');
  if (!el || !event.detail || event.detail.xhr.status !== 200) return;
  try {
    const data = JSON.parse(event.detail.xhr.responseText);
    const timeOk = data.time_ok;
    const offset = data.time_tracking && data.time_tracking['System time']
      ? data.time_tracking['System time']
      : '—';
    el.innerHTML =
      `<span class="status-badge ${timeOk ? 'status-ok' : 'status-warn'}">` +
        (timeOk ? '✓ Time OK' : '⚠ Time skew') +
      `</span> &nbsp;` +
      `<span class="mono">${new Date().toLocaleTimeString()}</span>`;
  } catch (e) {}
}

// Tick the clock every second from local time (visual only)
setInterval(function () {
  const el = document.getElementById('time-display');
  if (!el) return;
  const badge = el.querySelector('.status-badge');
  const mono = el.querySelector('.mono');
  if (mono) mono.textContent = new Date().toLocaleTimeString();
}, 1000);

// ── HTMX JSON encoder extension (needed for login form) ──────────────────

(function () {
  if (typeof htmx === 'undefined') return;
  htmx.defineExtension('json-enc', {
    onEvent: function (name, evt) {
      if (name === 'htmx:configRequest') {
        evt.detail.headers['Content-Type'] = 'application/json';
      }
    },
    encodeParameters: function (xhr, parameters, elt) {
      xhr.overrideMimeType('text/json');
      return JSON.stringify(parameters);
    },
  });
})();
