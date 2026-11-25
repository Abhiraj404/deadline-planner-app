// Settings page: sync scope, import preview, selective downloads and data management.

import { $, action, api, esc, setContent, updateText } from './client.js';
import { createPager } from './pagination.js';

const separator = ' · ';

// Set up the Settings page and return its controls.
export function settingsView(refreshStatus) {
  const form = $('sync-settings');
  const candidates = $('preview-list');
  const selected = new Set();
  const taskPager = createPager('preview-task-pager', renderPreviewTasks);
  const docPager = createPager('preview-doc-pager', renderPreviewDocs);
  let settingsLoaded = false;
  let pendingAction;
  let busy = false;
  let preview = { documents: [], matches: [] };

  // Fill the module list, keeping the current selection.
  function renderModules(modules) {
    const field = form.elements.modules;
    const values = new Set([...field.selectedOptions].map((option) => option.value));
    setContent(
      field,
      modules
        .map((module) => `<option value="${esc(module.id)}">${esc(module.name)}</option>`)
        .join(''),
    );
    for (const option of field.options) option.selected = values.has(option.value);
  }

  // Show saved settings in the form.
  function fillSettings(value) {
    const elements = form.elements;
    for (const name of ['minutes', 'preset', 'start', 'end', 'semester_start', 'semester_end']) {
      elements[name].value = value[name] ?? '';
    }
    for (const name of ['automatic', 'include_undated'])
      elements[name].checked = Boolean(value[name]);
    for (const control of form.querySelectorAll('[name=sources]'))
      control.checked = value.sources.includes(control.value);
    const selectedModules = new Set(value.modules.map(String));
    for (const option of elements.modules.options)
      option.selected = selectedModules.has(option.value);
    settingsLoaded = true;
  }

  // Read the form into a settings object for the API.
  function readSettings() {
    const data = new FormData(form);
    const elements = form.elements;
    const value = {
      automatic: elements.automatic.checked,
      include_undated: elements.include_undated.checked,
      minutes: Number(elements.minutes.value),
      preset: elements.preset.value,
      sources: data.getAll('sources'),
      modules: data.getAll('modules'),
    };
    for (const field of ['start', 'end', 'semester_start', 'semester_end'])
      value[field] = elements[field].value || null;
    return value;
  }

  // Show the current page of deadlines found by the preview.
  function renderPreviewTasks() {
    $('preview-deadlines').hidden = !preview.matches.length;
    if (!preview.matches.length) return;
    setContent(
      $('preview-task-list'),
      taskPager
        .slice(preview.matches)
        .map(
          (row) =>
            `<p><strong>${esc(row.title)}</strong>${separator}${esc(row.module || row.source)}${separator}${esc(row.due || 'Deadline unavailable')}</p>`,
        )
        .join(''),
    );
  }

  // Show the current page of documents found by the preview, with checkboxes.
  function renderPreviewDocs() {
    $('preview-doc-pager').hidden = preview.documents.length <= 10;
    setContent(
      candidates,
      docPager
        .slice(preview.documents)
        .map(
          (doc) => `<label class="inline">
      <input type="checkbox" value="${esc(doc.key)}" ${selected.has(doc.key) ? 'checked' : ''}>
      ${esc(doc.title)}${separator}${esc(doc.module)}${separator}${esc(doc.date || 'No reliable date')}</label>`,
        )
        .join(''),
    );
    for (const field of candidates.querySelectorAll('input'))
      field.checked = selected.has(field.value);
  }

  // Disable job buttons while a sync or download runs.
  function setBusy(value) {
    busy = value;
    $('preview-start').disabled = busy;
    $('download-selected').disabled = busy || !preview.documents.length;
    $('download-all').disabled = busy || !preview.documents.length;
  }

  // Reload modules, the latest preview and (once) the saved settings.
  async function refresh() {
    const [modules, value, saved] = await Promise.all([
      api('/api/moodle/modules'),
      api('/api/sync/preview'),
      settingsLoaded ? undefined : api('/api/settings'),
    ]);
    const choices = new Map(modules.map((module) => [String(module.id), module]));
    for (const module of value.modules || []) choices.set(String(module.id), module);
    renderModules([...choices.values()]);
    if (saved) fillSettings(saved);
    if (preview.id !== value.id) {
      selected.clear();
      taskPager.state.page = docPager.state.page = 1;
    }
    preview = value;
    const matching = new Set(value.documents.map((doc) => doc.key));
    for (const key of selected) if (!matching.has(key)) selected.delete(key);
    updateText(
      $('preview-summary'),
      value.id
        ? [
            ...Object.entries(value.records).map(([source, count]) => `${source} ${count} records`),
            `${value.documents.length} downloadable candidates`,
          ].join(separator)
        : 'Run a preview with your selected settings.',
    );
    renderPreviewTasks();
    renderPreviewDocs();
    setBusy(busy);
  }

  candidates.onchange = (event) => {
    const field = event.target;
    if (field.checked) selected.add(field.value);
    else selected.delete(field.value);
  };
  form.onsubmit = (event) => {
    event.preventDefault();
    action(async () => {
      await api('/api/settings', 'PUT', readSettings());
      updateText($('notice'), 'Synchronisation settings saved.');
    });
  };
  $('preview-start').onclick = () =>
    action(async () => {
      if (!form.reportValidity()) return;
      await api('/api/settings', 'PUT', readSettings());
      await api('/api/sync/preview', 'POST');
      await refreshStatus();
    });

  // Download the chosen preview documents, or all when keys is null.
  function download(keys) {
    return action(async () => {
      if (keys?.length === 0) throw Error('Select at least one document.');
      await api('/api/sync/download', 'POST', { preview_id: preview.id, keys });
      await refreshStatus();
    });
  }

  $('download-selected').onclick = () => download([...selected]);
  $('download-all').onclick = () => download(null);
  $('data-action').onchange = () => {
    $('manual-option').hidden = $('data-action').value !== 'full';
    $('include-manual').checked = false;
  };
  $('data-preview').onclick = () =>
    action(async () => {
      const request = {
        action: $('data-action').value,
        include_manual: $('include-manual').checked,
      };
      const plan = await api('/api/data/preview', 'POST', request);
      pendingAction = { ...request, confirmation: plan.confirmation };
      updateText($('confirmation-title'), $('data-action').selectedOptions[0].textContent);
      setContent(
        $('affected'),
        Object.entries(plan.counts)
          .map(([name, count]) => `<li>${esc(count)} ${esc(name.replaceAll('_', ' '))}</li>`)
          .join(''),
      );
      $('manual-warning').hidden = !request.include_manual;
      $('confirmation').showModal();
    });
  $('cancel-data').onclick = () => $('confirmation').close();
  $('confirm-data').onclick = () =>
    action(async () => {
      const button = $('confirm-data');
      button.disabled = true;
      try {
        const result = await api('/api/data/apply', 'POST', pendingAction);
        $('confirmation').close();
        updateText($('notice'), 'Data management complete. Backup saved as ' + result.backup + '.');
        settingsLoaded = false;
        await refresh();
      } finally {
        button.disabled = false;
      }
    });
  return { refresh, setBusy };
}
