// Entry point: shows the current page, polls sync status and loads page modules on demand.

import { $, action, api, localDay, metricText, report, updateText } from '/static/client.js';

const route = location.pathname;
const views = {
  '/': () => import('/static/summary.js').then((module) => module.overviewView()),
  '/assignments': () => import('/static/assignments.js').then((module) => module.assignmentsView()),
  '/calendar': () => import('/static/summary.js').then((module) => module.calendarView()),
  '/history': () => import('/static/summary.js').then((module) => module.historyView()),
  '/library': () => import('/static/library.js').then((module) => module.libraryView()),
  '/modules': () => import('/static/summary.js').then((module) => module.modulesView()),
  '/setup': () => import('/static/setup.js').then((module) => module.setupView()),
  '/settings': async () => {
    const { settingsView } = await import('/static/settings.js');
    settings = settingsView(refreshAfterAction);
    settings.setBusy(busy);
    return settings.refresh;
  },
};

let settings;
let busy = false;
let poll;
let timer;
let revision;
let pollError;

const current = [...document.querySelectorAll('[data-page]')].find(
  (page) => page.dataset.page === route,
);
if (current) current.hidden = false;
const navigation = [...document.querySelectorAll('nav a')].find((link) => link.pathname === route);
navigation?.setAttribute('aria-current', 'page');

const initializeView = views[route];
let refreshView;

// Show the demo-mode notice, or point new users to Setup.
async function showBanner() {
  const info = await api('/api/app');
  const banner = $('app-banner');
  if (info.demo) {
    banner.textContent =
      'Demo mode: every module, deadline, email and document here is synthetic. Sync is disabled.';
  } else if (!info.setup_complete && route !== '/setup') {
    banner.innerHTML =
      'New here? <a href="/setup">Open Setup</a> to choose integrations and reminders. All are optional.';
  }
  banner.hidden = !banner.textContent;
}

// Update sync status and refresh the page when a job finishes or the day changes.
async function checkStatus() {
  const info = await api('/api/status');
  const job = info.job;
  busy = Boolean(info.running);
  $('sync').disabled = busy || info.demo;
  $('stop').disabled = !job || job.stopping;
  settings?.setBusy(busy);
  updateText(
    $('status'),
    job
      ? job.stopping
        ? 'Stopping after the current request…'
        : `Running ${job.mode}…`
      : Object.entries(info.sources || {})
          .map(([source, state]) => `${source} — ${state}`)
          .join(' · ') || 'Ready',
  );
  updateText(
    $('metrics'),
    job
      ? metricText(job.metrics) + (job.metrics.last_warning ? ' · ' + job.metrics.last_warning : '')
      : '',
  );

  const currentRevision = JSON.stringify([info.last_job_id, localDay(new Date())]);
  if (revision !== currentRevision && initializeView) {
    refreshView ??= await initializeView();
    await refreshView();
    revision = currentRevision;
  }
  if (pollError && $('notice').textContent === pollError) updateText($('notice'), '');
  pollError = undefined;
}

// Poll status: every 2 s while a job runs, every 15 s otherwise.
function refreshStatus() {
  if (poll) return poll;
  clearTimeout(timer);
  poll = checkStatus()
    .catch((error) => {
      pollError = error.message;
      report(error);
    })
    .finally(() => {
      poll = undefined;
      if (!document.hidden) timer = setTimeout(refreshStatus, busy ? 2000 : 15000);
    });
  return poll;
}

// Refresh status straight after a user action.
async function refreshAfterAction() {
  if (poll) await poll;
  await refreshStatus();
}

$('sync').onclick = () =>
  action(async () => {
    await api('/api/sync', 'POST');
    await refreshAfterAction();
  });
$('stop').onclick = () =>
  action(async () => {
    await api('/api/sync/stop', 'POST');
    await refreshAfterAction();
  });
document.addEventListener('visibilitychange', () => {
  if (document.hidden) clearTimeout(timer);
  else refreshStatus();
});
window.addEventListener('pageshow', (event) => {
  if (event.persisted) refreshStatus();
});
showBanner().catch(report);
refreshStatus();
