// Shared helpers: DOM access, HTML escaping, API calls and status messages.

// Shorthand for getElementById.
export const $ = (id) => document.getElementById(id);

// Escape text for safe use inside HTML.
export const esc = (value) =>
  String(value ?? '').replace(
    /[&<>"']/g,
    (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char],
  );

// A date as YYYY-MM-DD in the browser's timezone.
export function localDay(date) {
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, '0'),
    String(date.getDate()).padStart(2, '0'),
  ].join('-');
}

// Call the local API with JSON and turn error responses into exceptions.
export async function api(url, method = 'GET', data, signal) {
  const response = await fetch(url, {
    method,
    signal,
    ...(data === undefined
      ? {}
      : {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(data),
        }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = Array.isArray(payload.detail)
      ? payload.detail.map((item) => item.msg).join(' ')
      : payload.detail;
    throw Error(message || 'Request failed');
  }
  return payload;
}

// GET with query parameters, leaving out empty ones.
export function get(path, params, signal) {
  const query = new URLSearchParams(Object.entries(params).filter(([, value]) => value !== ''));
  return api(`${path}?${query}`, 'GET', undefined, signal);
}

// Change text only when it differs, to avoid needless repaints.
export function updateText(target, value) {
  if (target.textContent !== value) target.textContent = value;
}

// Show an error message in the notice area.
export function report(error) {
  updateText($('notice'), error.message);
}

// Run a user action and report any error instead of throwing.
export async function action(work) {
  updateText($('notice'), '');
  try {
    return await work();
  } catch (error) {
    report(error);
  }
}

const markup = new WeakMap();

// Replace HTML only when it changed, keeping open <details> sections open.
export function setContent(target, html) {
  if (markup.get(target) === html) return;
  markup.set(target, html);
  const open = new Set(
    [...target.querySelectorAll('details[open][data-key]')].map((el) => el.dataset.key),
  );
  target.innerHTML = html;
  for (const detail of target.querySelectorAll('details[data-key]')) {
    detail.open = open.has(detail.dataset.key);
  }
}

// Link to a stored PDF or Word file, if it is available.
export function fileLink(doc, text) {
  const word = doc.file_type === 'docx';
  const extension = word ? 'docx' : 'pdf';
  const label = text ?? (word ? 'Download Word file' : 'Open PDF');
  return doc.pdf_available && /^[a-f0-9]{64}$/.test(doc.id)
    ? `<a href="/api/assessment-documents/${doc.id}/${extension}" target="_blank" rel="noopener">${esc(label)}</a>`
    : '';
}

const metricNames = {
  modules: 'Modules discovered',
  assignments: 'Assignments discovered',
  new_pdfs_downloaded: 'New documents downloaded',
  existing_pdfs_skipped: 'Existing documents skipped',
  records_updated: 'Records updated',
  duplicates_prevented: 'Duplicates prevented',
  documents_outside_range: 'Documents outside range',
  warnings: 'Errors encountered',
};

// Readable summary of a sync job's counters.
export function metricText(stats) {
  return Object.entries(metricNames)
    .map(([key, title]) => `${title} ${Number(stats[key] || 0)}`)
    .join(' · ');
}

// Fill a module <select>; returns a function that reloads its options.
export function installModuleFilter(id) {
  const field = $(id);
  let requested = new URLSearchParams(location.search).get('module');
  return async () => {
    const choices = await api('/api/modules/options');
    const selected = requested ?? field.value;
    requested = null;
    setContent(
      field,
      '<option value="">All modules</option>' +
        choices
          .map((choice) => `<option value="${esc(choice.value)}">${esc(choice.name)}</option>`)
          .join(''),
    );
    if (choices.some((choice) => choice.value === selected)) field.value = selected;
  };
}
