// @ts-check
// vigil-ui-kit.js
// Owns: the /ui-kit/ page only — the theme switch, the swatch grid, and the
//   demo handlers behind each data-act in ui_kit.html.
// Depends on: vigil-utils.js (setTheme, showToast, confirmModal),
//   vigil-modal.js (shakeEl).

//: Every token the page shows a chip of, in the order the grid fills them.
const _KIT_TOKENS = [
  '--bg', '--s1', '--s2', '--s3', '--border',
  '--text-1', '--text-2', '--text-3',
  '--mint', '--mint-ink',
  '--rose', '--rose-ink',
  '--sky', '--sky-ink',
  '--lavender', '--lavender-ink',
  '--peach', '--peach-ink',
  '--lemon', '--lemon-ink',
  '--coral', '--coral-ink',
];

/** @param {string} name */
function kitTheme(name) {
  if (typeof setTheme === 'function') {
    setTheme(name);
  } else {
    document.documentElement.dataset.theme = name;
  }
}

function kitRise() { _openKitDialog('kit-rise'); }
function kitPop() { _openKitDialog('kit-pop'); }
function kitSheet() { _openKitDialog('kit-sheet'); }
function closeKitRise() { _closeKitDialog('kit-rise'); }
function closeKitPop() { _closeKitDialog('kit-pop'); }
function closeKitSheet() { _closeKitDialog('kit-sheet'); }

/** @param {string} id */
function _openKitDialog(id) {
  const overlay = document.getElementById(id + '-overlay');
  const modal = document.getElementById(id + '-modal');
  if (overlay) overlay.classList.add('open');
  if (modal) modal.classList.add('open');
}

/** @param {string} id */
function _closeKitDialog(id) {
  const overlay = document.getElementById(id + '-overlay');
  const modal = document.getElementById(id + '-modal');
  if (overlay) overlay.classList.remove('open');
  if (modal) modal.classList.remove('open');
}

async function kitConfirm() {
  // confirmModal's own buttons carry no data-act, and this page must not name
  // one that does not resolve: its handlers are the ten in ui_kit.html.
  const answer = await confirmModal(t('Delete the example?'), {
    title: t('Delete the example?'),
    confirmText: t('Delete'),
    danger: true,
  });
  showToast(answer ? 'Nothing was deleted — it was only an example.' : 'Cancelled.',
            answer ? 'success' : 'info');
}

function kitShake() {
  const input = /** @type {HTMLInputElement|null} */ (document.getElementById('kit-text'));
  shakeEl(input);
}

/** @param {string} type */
function kitToast(type) {
  showToast(t('A {type} toast.', { type }), type);
}

//: One <div class="kit-swatch"> per token: a block painted from var(--token),
//: so it repaints when the theme changes, and a caption with the token name.
function kitSwatches() {
  const grid = document.getElementById('kit-swatches');
  if (!grid) return;
  grid.textContent = '';
  _KIT_TOKENS.forEach((token) => {
    const cell = document.createElement('div');
    cell.className = 'kit-swatch';
    const swatch = document.createElement('div');
    swatch.className = 'kit-swatch-block';
    swatch.style.background = `var(${token})`;
    const caption = document.createElement('div');
    caption.className = 'kit-swatch-caption';
    caption.textContent = token;
    cell.appendChild(swatch);
    cell.appendChild(caption);
    grid.appendChild(cell);
  });
}

document.addEventListener('DOMContentLoaded', kitSwatches);
