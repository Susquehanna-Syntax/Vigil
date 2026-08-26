// ── Reboot-required marker (phase 08b) ──────────────────────────────────────
// A host whose agent reports reboot_required gets a pending marker on its
// card. --peach is the fleet's existing "pending" colour; no new colour
// literal is introduced.
document.addEventListener('DOMContentLoaded', () => {
  for (const card of document.querySelectorAll('.host-card')) {
    if (card.dataset.rebootRequired !== '1') continue;
    const chip = document.createElement('span');
    chip.textContent = 'Reboot pending';
    chip.title = 'Agent reports a reboot is required';
    chip.style.cssText =
      'font-size:10px;font-weight:700;letter-spacing:0.4px;text-transform:uppercase;' +
      'padding:2px 10px;border-radius:20px;background:rgba(240,184,136,0.12);' +
      'color:var(--peach);animation:pulse 3s ease-in-out infinite;white-space:nowrap;';
    const row = card.querySelector('.host-card-id-row');
    if (row) row.appendChild(chip);
  }
});
