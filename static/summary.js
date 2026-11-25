// Overview, modules, history and calendar pages.

import {
  $,
  api,
  esc,
  fileLink,
  get,
  installModuleFilter,
  localDay,
  metricText,
  report,
  setContent,
} from './client.js';
import { pagedList } from './pagination.js';
import { renderReminders } from './reminders.js';

// Modules page: paged list with links to their deadlines and documents.
export function modulesView() {
  return pagedList({
    key: 'module',
    endpoint: '/api/moodle/modules',
    filters: { q: 'module-search' },
    render(rows) {
      setContent(
        $('module-list'),
        rows
          .map(
            (module) => `<article><h3>${esc(module.name)}</h3>
      <p>${Number(module.task_count)} assignments · ${Number(module.document_count)} documents</p>
      <div class="row"><a href="/assignments?module=id:${encodeURIComponent(module.id)}">Assignments</a>
      <a href="/library?module=id:${encodeURIComponent(module.id)}">documents</a></div></article>`,
          )
          .join('') || '<p>No modules found.</p>',
      );
    },
  });
}

// Sync history page: paged list of past jobs.
export function historyView() {
  return pagedList({
    key: 'history',
    endpoint: '/api/sync/history',
    render(rows) {
      setContent(
        $('history'),
        rows
          .map(
            (entry) => `<article><h3>${esc(entry.mode)} · ${esc(entry.outcome)}</h3>
      <p>${esc(new Date(entry.started).toLocaleString())} – ${esc(new Date(entry.finished).toLocaleString())}</p>
      <p>${esc(metricText(entry.metrics))}</p><p>${esc(entry.metrics.last_warning || '')} ${esc(entry.errors.join(' · '))}</p></article>`,
          )
          .join('') || '<p>No sync jobs yet.</p>',
      );
    },
  });
}

// Overview page: counts, reminders, upcoming deadlines and recent documents.
export function overviewView() {
  return async () => {
    const [{ upcoming, recent, counts }] = await Promise.all([
      api('/api/overview'),
      renderReminders(),
    ]);
    const stats = [
      ['Upcoming', counts.upcoming],
      ['Overdue', counts.overdue],
      ['Modules', counts.modules],
      ['Assessment documents', counts.documents],
    ];
    setContent(
      $('stats'),
      stats
        .map(
          ([title, count]) =>
            `<section><strong>${esc(count)}</strong><small>${title}</small></section>`,
        )
        .join(''),
    );
    setContent(
      $('upcoming'),
      upcoming
        .map(
          (task) =>
            `<p><strong>${esc(task.title)}</strong> · ${esc(task.category)} · ${esc(task.due)}</p>`,
        )
        .join('') || '<p>No upcoming deadlines.</p>',
    );
    setContent(
      $('recent'),
      recent
        .map(
          (doc) =>
            `<p><strong>${esc(doc.title)}</strong> · ${esc(doc.module)} ${fileLink(doc)}</p>`,
        )
        .join('') || '<p>No assessment documents.</p>',
    );
  };
}

// Calendar page: one month of deadlines with module and source filters.
export function calendarView() {
  const calendar = $('month');
  let month = new Date();
  let controller;
  const loadModules = installModuleFilter('calendar-module');

  // Draw the month grid, starting weeks on Monday.
  function render(year, monthIndex, days, byDay) {
    const offset = (new Date(year, monthIndex, 1).getDay() + 6) % 7;
    setContent(
      calendar,
      `<section><div class="row between"><button id="month-prev">Previous</button>
      <strong>${month.toLocaleDateString('en-GB', { month: 'long', year: 'numeric' })}</strong><button id="month-next">Next</button></div>
      <div class="calendar">${['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((day) => `<small>${day}</small>`).join('')}
      ${'<div></div>'.repeat(offset)}${Array.from({ length: days }, (_, index) => {
        const date = localDay(new Date(year, monthIndex, index + 1));
        const { titles, count } = byDay.get(date) || { titles: [], count: 0 };
        const destination = '/assignments?from=' + date + '&to=' + date;
        return `<div class="day">${index + 1}${titles.map((title) => `<a href="${destination}">${esc(title)}</a>`).join('')}
        ${count > 4 ? `<a href="${destination}">+${count - 4} more</a>` : ''}</div>`;
      }).join('')}</div></section>`,
    );
  }

  // Fetch every deadline in the visible month, page by page.
  async function load() {
    controller?.abort();
    controller = new AbortController();
    const { signal } = controller;
    const year = month.getFullYear();
    const monthIndex = month.getMonth();
    const days = new Date(year, monthIndex + 1, 0).getDate();
    const fields = {
      module: $('calendar-module').value,
      source: $('calendar-source').value,
      start: localDay(new Date(year, monthIndex, 1)),
      end: localDay(new Date(year, monthIndex, days)),
      size: 50,
    };
    const byDay = new Map();
    try {
      let remaining;
      let page = 1;
      do {
        const { items, total } = await get('/api/tasks', { ...fields, page }, signal);
        if (signal.aborted) return;
        for (const task of items) {
          if (!byDay.has(task.due)) byDay.set(task.due, { count: 0, titles: [] });
          const entry = byDay.get(task.due);
          entry.count++;
          if (entry.titles.length < 4) entry.titles.push(task.title);
        }
        remaining = items.length > 0 && page * fields.size < total;
        page++;
      } while (remaining);
      render(year, monthIndex, days, byDay);
    } catch (error) {
      if (!signal.aborted) throw error;
    }
  }

  calendar.onclick = (event) => {
    if (!['month-prev', 'month-next'].includes(event.target.id)) return;
    const step = event.target.id === 'month-prev' ? -1 : 1;
    month = new Date(month.getFullYear(), month.getMonth() + step, 1);
    load().catch(report);
  };
  for (const id of ['calendar-module', 'calendar-source'])
    $(id).onchange = () => load().catch(report);
  return async () => {
    await loadModules();
    await load();
  };
}
