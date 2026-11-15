/* Sign-in second step: tap a passkey or security key (login_mfa.py).
 *
 * Runs on the pre-auth page, so it carries its own base64url helpers rather
 * than loading vigil-utils.js and the dashboard it assumes. */
(function () {
  const btn = document.getElementById('login-key-btn');
  if (!btn) return;
  const errBox = document.getElementById('login-key-error');
  const csrfInput = document.querySelector('input[name=csrfmiddlewaretoken]');
  const csrf = csrfInput ? csrfInput.value : '';

  function b64urlToBytes(value) {
    const s = String(value).replace(/-/g, '+').replace(/_/g, '/');
    const bin = atob(s + '='.repeat((4 - (s.length % 4)) % 4));
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function bytesToB64url(buf) {
    const bytes = new Uint8Array(buf);
    let bin = '';
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  function showError(message) {
    errBox.textContent = message;
    errBox.style.display = '';
  }

  async function post(url, body) {
    const r = await fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
      body: JSON.stringify(body || {}),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || 'The key could not be checked. Try again.');
    return data;
  }

  async function useKey() {
    errBox.style.display = 'none';
    btn.disabled = true;
    try {
      const opts = await post(btn.dataset.options);
      const cred = await navigator.credentials.get({ publicKey: {
        challenge: b64urlToBytes(opts.challenge).buffer,
        timeout: opts.timeout || 60000,
        rpId: opts.rpId,
        userVerification: opts.userVerification || 'preferred',
        allowCredentials: (opts.allowCredentials || []).map((c) => ({
          type: c.type || 'public-key', id: b64urlToBytes(c.id).buffer,
        })),
      } });
      const r = cred.response;
      const result = await post(btn.dataset.verify, { credential: {
        id: cred.id || bytesToB64url(cred.rawId),
        rawId: bytesToB64url(cred.rawId),
        type: cred.type || 'public-key',
        response: {
          clientDataJSON: bytesToB64url(r.clientDataJSON),
          authenticatorData: bytesToB64url(r.authenticatorData),
          signature: bytesToB64url(r.signature),
          userHandle: r.userHandle ? bytesToB64url(r.userHandle) : null,
        },
      } });
      window.location.assign(result.next || '/');
    } catch (e) {
      // NotAllowedError is the browser's word for "cancelled or timed out".
      showError(e && e.name === 'NotAllowedError'
        ? 'The key request was cancelled or timed out.'
        : (e && e.message) || 'The key could not be checked. Try again.');
      btn.disabled = false;
    }
  }

  if (!window.PublicKeyCredential) {
    btn.disabled = true;
    showError('This browser does not support passkeys or security keys.');
    return;
  }
  btn.addEventListener('click', useKey);
})();
