// vigil-nav.js
// Owns: page navigation (sidebar icons → page sections) and tab-bar switching.
// Depends on: vigil-utils.js
// Note: feature files (monitor, tasks, deploy, vulns, settings, inventory) wrap
//   navigateTo() to fire page-specific load actions. Each wrap stacks on the
//   previous one, so script load order in base.html matters.

/* ── Sidebar navigation ──────────────────────────────────────────────── */
const sidebarIcons = document.querySelectorAll('.sidebar-icon[data-page]');
const pages = document.querySelectorAll('.page');

function navigateTo(pageName) {
  pages.forEach(p => p.classList.remove('active'));
  sidebarIcons.forEach(i => i.classList.remove('active'));
  const page = document.getElementById('page-' + pageName);
  const icon = document.querySelector(`.sidebar-icon[data-page="${pageName}"]`);
  if (page) page.classList.add('active');
  if (icon) icon.classList.add('active');
  document.querySelector('.main').scrollTop = 0;
}

sidebarIcons.forEach(icon => {
  icon.addEventListener('click', () => navigateTo(icon.dataset.page));
});

/* ── Tab bars (alerts, tasks history) ────────────────────────────────── */
document.querySelectorAll('.tab-bar').forEach(bar => {
  bar.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      bar.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const group = bar.closest('.page');
      group.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      const target = document.getElementById(tab.dataset.tab);
      if (target) target.classList.add('active');
    });
  });
});
