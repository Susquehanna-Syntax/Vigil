// vigil-branding.js
// Owns: Settings → Branding pane, and the client-side apply of branding on
//   every authed page (title, accent, footer, sidebar identity).
// HTML: templates/pages/_settings.html (data-pane="branding"), base.html (#vigil-footer)
// Depends on: vigil-utils.js (apiJson, showToast)
//
// Branding fields are user-supplied, so every one of them lands via
// textContent or a typed attribute — never innerHTML. The server validates
// accent as #RRGGBB and the URLs as URLs; this file assumes neither.

const _BR_FIELDS = ['product_name', 'logo_url', 'accent', 'footer_text', 'support_url'];

let _brandingLicensed = false;

async function _brandingFeatureActive() {
  try {
    const lic = await apiJson('/api/v1/license/');
    const f = (lic.features || []).find((x) => x.name === 'branding');
    return !!(f && f.active);
  } catch (e) { return false; }
}

// Apply branding to the live page. Safe to call with partial/empty data —
// anything blank leaves the stock Vigil identity alone.
function applyBranding(b) {
  if (!b) return;

  if (b.product_name) {
    // Keep the page-specific suffix ("— Dashboard") and swap only the name.
    const suffix = document.title.includes('—')
      ? document.title.slice(document.title.indexOf('—'))
      : '';
    document.title = suffix ? `${b.product_name} ${suffix}` : b.product_name;

    const avatar = document.querySelector('.sidebar-avatar');
    if (avatar) avatar.textContent = b.product_name.trim().charAt(0).toUpperCase();

    const logoImg = document.querySelector('.sidebar-logo img');
    if (logoImg) logoImg.alt = b.product_name;
  }

  if (b.logo_url) {
    const logoImg = document.querySelector('.sidebar-logo img');
    if (logoImg) logoImg.src = b.logo_url;
  }

  if (b.accent) {
    document.documentElement.style.setProperty('--brand', b.accent);
  }

  const footer = document.getElementById('vigil-footer');
  if (footer) {
    footer.textContent = '';
    if (b.footer_text) {
      if (b.support_url) {
        const a = document.createElement('a');
        a.href = b.support_url;
        a.textContent = b.footer_text;
        a.rel = 'noopener noreferrer';
        footer.appendChild(a);
      } else {
        footer.textContent = b.footer_text;
      }
    }
  }
}

function _fillBrandingForm(b) {
  const product = document.getElementById('br-product');
  if (!product) return;   // not on the settings page
  product.value = b.product_name || '';
  document.getElementById('br-logo').value = b.logo_url || '';
  document.getElementById('br-accent').value = b.accent || '';
  document.getElementById('br-footer').value = b.footer_text || '';
  document.getElementById('br-support').value = b.support_url || '';

  const picker = document.getElementById('br-accent-picker');
  if (picker && /^#[0-9A-Fa-f]{6}$/.test(b.accent || '')) picker.value = b.accent;

  const note = document.getElementById('br-note');
  const save = document.getElementById('br-save');
  ['br-product', 'br-logo', 'br-accent', 'br-footer', 'br-support', 'br-accent-picker']
    .forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.disabled = !_brandingLicensed;
    });
  if (save) save.disabled = !_brandingLicensed;
  if (note) {
    note.textContent = _brandingLicensed
      ? '' : 'Branding is a Business feature — Free installs use the Vigil identity.';
  }
}

async function loadBranding() {
  let b;
  try {
    b = await apiJson('/api/v1/branding/');
  } catch (e) {
    return;   // never let branding break a page
  }
  applyBranding(b);

  if (document.getElementById('br-product')) {
    _brandingLicensed = await _brandingFeatureActive();
    _fillBrandingForm(b);
  }
}

async function saveBranding() {
  const note = document.getElementById('br-note');
  const accent = document.getElementById('br-accent').value.trim();
  if (accent && !/^#[0-9A-Fa-f]{6}$/.test(accent)) {
    note.textContent = 'Accent must be a hex color in the format #RRGGBB.';
    return;
  }
  const body = {
    product_name: document.getElementById('br-product').value.trim(),
    logo_url: document.getElementById('br-logo').value.trim(),
    accent: accent,
    footer_text: document.getElementById('br-footer').value.trim(),
    support_url: document.getElementById('br-support').value.trim(),
  };
  try {
    const saved = await apiJson('/api/v1/branding/', {
      method: 'PATCH', body: JSON.stringify(body),
    });
    note.textContent = '';
    showToast('Branding saved', 'success');
    applyBranding(saved);
  } catch (e) {
    note.textContent = e.message;   // 402 upgrade body, 403, or validation error
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const save = document.getElementById('br-save');
  if (save) save.addEventListener('click', saveBranding);

  // Keep the color picker and the hex field in step.
  const picker = document.getElementById('br-accent-picker');
  const hex = document.getElementById('br-accent');
  if (picker && hex) {
    picker.addEventListener('input', () => { hex.value = picker.value; });
    hex.addEventListener('input', () => {
      if (/^#[0-9A-Fa-f]{6}$/.test(hex.value.trim())) picker.value = hex.value.trim();
    });
  }

  loadBranding();
});
