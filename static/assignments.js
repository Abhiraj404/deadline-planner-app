// Assignments page: deadline form, filters, progress and manual deletion.

import {
  $,
  action,
  api,
  esc,
  fileLink,
  installModuleFilter,
  setContent,
  updateText,
} from './client.js';
import { pagedList } from './pagination.js';

// Link to the source page, only for http(s) URLs.
function originalLink(url) {
  return /^https?:\/\//i.test(url || '')
    ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Original source</a>`
    : '';
}

// HTML for one deadline. Every value is escaped.
function taskCard(task) {
  const id = Number(task.id);
  const progress = Number(task.progress) || 0;
  const files = (task.related_documents || [])
    .map((doc) => fileLink(doc, doc.title))
    .filter(Boolean);
  return `<article>
    <div class="row between"><h3>${esc(task.title)}</h3><small>${esc(task.source)} ${task.priority ? '· ' + esc(task.priority) : ''}</small></div>
    <p>${task.category ? esc(task.category) + ' · ' : ''}${task.due ? esc(task.due) : 'Deadline unavailable'}
    ${task.weighting ? ' · ' + esc(task.weighting) + ' weighting' : ''}</p>
    ${task.review ? '<p class="warn">Imported estimate. Verify the deadline.</p>' : ''}
    ${task.notes ? `<details data-key="${esc(id || task.key)}"><summary>Assignment description</summary><p class="description">${esc(task.notes)}</p></details>` : ''}
    ${files.length ? `<ul>${files.map((link) => `<li>${link}</li>`).join('')}</ul>` : ''}
    ${
      id
        ? `<progress max="100" value="${progress}"></progress> ${progress}%
      <div class="row"><button data-task="progress" data-id="${id}">${progress === 100 ? 'Reopen' : 'Complete'}</button>
      <button class="secondary" data-task="edit" data-id="${id}">Edit / review</button>
      ${task.source === 'Manual' ? `<button class="secondary" data-task="delete" data-id="${id}">Delete</button>` : ''}</div>`
        : ''
    }
    ${originalLink(task.link)}</article>`;
}

// Set up the Assignments page and return its refresh function.
export function assignmentsView() {
  const list = $('list');
  const form = $('form');
  let tasks = [];
  let editing = null;
  const params = new URLSearchParams(location.search);
  $('task-start').value = params.get('from') || '';
  $('task-end').value = params.get('to') || '';
  const loadModules = installModuleFilter('module');
  const load = pagedList({
    key: 'task',
    endpoint: '/api/tasks',
    filters: {
      q: 'task-search',
      status: 'filter',
      source: 'source',
      module: 'module',
      start: 'task-start',
      end: 'task-end',
      sort: 'task-sort',
    },
    render(rows) {
      tasks = rows;
      setContent(list, rows.map(taskCard).join('') || '<p>No matching assignments.</p>');
    },
  });

  // Reload the module filter and the current page of deadlines.
  async function refresh() {
    await loadModules();
    await load();
  }

  // Reset the form back to "Add deadline".
  function clearForm() {
    editing = null;
    form.reset();
    updateText($('formtitle'), 'Add deadline');
  }

  $('cancel').onclick = clearForm;
  form.onsubmit = (event) => {
    event.preventDefault();
    action(async () => {
      const data = Object.fromEntries(new FormData(form));
      data.progress = Number(data.progress);
      await api('/api/tasks' + (editing ? '/' + editing : ''), editing ? 'PUT' : 'POST', data);
      clearForm();
      await refresh();
    });
  };
  list.onclick = (event) => {
    const button = event.target.closest('[data-task]');
    if (!button) return;
    const row = tasks.find((task) => Number(task.id) === Number(button.dataset.id));
    if (!row) return;
    if (button.dataset.task === 'edit') {
      editing = row.id;
      for (const key of ['title', 'due', 'category', 'notes', 'priority', 'progress']) {
        form.elements[key].value = row[key] ?? '';
      }
      updateText($('formtitle'), 'Edit deadline');
      form.scrollIntoView({ behavior: 'smooth' });
    } else if (button.dataset.task === 'progress') {
      action(async () => {
        await api('/api/tasks/' + row.id, 'PUT', {
          ...row,
          progress: row.progress === 100 ? 0 : 100,
        });
        await refresh();
      });
    } else if (confirm('Delete this one manually entered deadline?')) {
      action(async () => {
        await api('/api/tasks/' + row.id + '?confirmed=true', 'DELETE');
        await refresh();
      });
    }
  };
  return refresh;
}
