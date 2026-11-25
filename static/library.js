// Document library: import, search, previews and extraction review.

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
import { reviewPanel } from './review.js';

const extractionLabels = {
  complete: 'Text fully indexed',
  partial: 'Text partly indexed (limit reached or unreadable pages)',
  no_text: 'No selectable text (scanned document?)',
  error: 'Some pages could not be read',
  not_recorded: 'Indexed before completeness tracking',
};

// FTS snippets mark matches with \x01 and \x02. Escape first, then add <mark>.
function highlighted(snippet) {
  return esc(snippet).replaceAll('\x01', '<mark>').replaceAll('\x02', '</mark>');
}

// HTML for structured preview blocks (headings, lists, tables, text).
function previewBlocks(blocks) {
  return blocks
    .map((block) => {
      if (block.type === 'heading') {
        const level = Math.min(6, Math.max(4, Number(block.level) + 3));
        return `<h${level}>${esc(block.text)}</h${level}>`;
      }
      if (block.type === 'list_item') return `<li>${esc(block.text)}</li>`;
      if (block.type === 'table') {
        return `<table>${block.rows
          .map((row) => `<tr>${row.map((cell) => `<td>${esc(cell)}</td>`).join('')}</tr>`)
          .join('')}</table>`;
      }
      const label = block.type === 'header' || block.type === 'footer' ? `${block.type}: ` : '';
      return `<p class="description">${esc(label + block.text)}</p>`;
    })
    .join('');
}

// HTML for one document card. Every value is escaped.
function docCard(doc, moduleNames) {
  const names = [
    ...new Set(
      [doc.module, ...(doc.module_ids || []).map((id) => moduleNames.get(`id:${id}`))].filter(
        Boolean,
      ),
    ),
  ];
  const status = extractionLabels[doc.extraction_status] || '';
  const warn = doc.extraction_status !== 'complete' && doc.extraction_status !== 'not_recorded';
  return `<article><details data-key="${esc(doc.id)}"><summary><strong>${esc(doc.title)}</strong>
    <small>${esc((doc.file_type || 'pdf').toUpperCase())} · ${esc(doc.source || 'Moodle')}</small></summary>
    ${doc.snippet ? `<p class="snippet">${highlighted(doc.snippet)}</p>` : ''}
    <p>${esc(names.join(', '))} ${doc.assignment ? '· ' + esc(doc.assignment) : ''}</p>
    <p>${doc.document_date ? esc(doc.document_date) : 'No reliable date'}</p>
    <p class="${warn ? 'warn' : ''}">${esc(status)}${doc.ocr_used ? ' · OCR used' : ''}</p>
    <div class="row">${fileLink(doc) || '<p>Local document unavailable</p>'}
    <button type="button" class="secondary" data-extract="${esc(doc.id)}" data-module="${esc(doc.module)}">Review deadline details</button></div>
    <details data-key="${esc(doc.id + '-text')}" data-document="${esc(doc.id)}"><summary>Document preview</summary>
    <div class="preview" aria-live="polite"></div></details>
    </details></article>`;
}

// Set up the library page and return its refresh function.
export function libraryView() {
  const list = $('docs');
  const loadModules = installModuleFilter('docmodule');
  const previews = new Map();
  const load = pagedList({
    key: 'doc',
    endpoint: '/api/assessment-documents',
    filters: {
      q: 'docsearch',
      module: 'docmodule',
      start: 'doc-start',
      end: 'doc-end',
      sort: 'doc-sort',
      dated: 'doc-dated',
    },
    render(rows, { sort }) {
      const moduleNames = new Map(
        [...$('docmodule').options].map((option) => [option.value, option.textContent]),
      );
      let group;
      setContent(
        list,
        rows
          .map((doc) => {
            const name = [
              doc.module || 'Unassigned module',
              doc.assignment || 'Standalone documents',
            ].join(' · ');
            const heading = sort === 'group' && name !== group ? `<h3>${esc(name)}</h3>` : '';
            group = name;
            return heading + docCard(doc, moduleNames);
          })
          .join('') || '<p>No matching assessment documents.</p>',
      );
      const visible = new Set(rows.map((doc) => doc.id));
      for (const id of previews.keys()) {
        if (!visible.has(id)) previews.delete(id);
      }
    },
  });
  const review = reviewPanel(async () => {
    await loadModules();
    await load();
  });

  $('upload-form').onsubmit = (event) => {
    event.preventDefault();
    action(async () => {
      const file = $('upload-file').files[0];
      if (!file) return;
      if (file.size > 30_000_000) throw Error('Documents are limited to 30 MB');
      const word = file.name.toLowerCase().endsWith('.docx');
      const query = new URLSearchParams({ filename: file.name, module: $('upload-module').value });
      const response = await fetch(`/api/assessment-documents/upload?${query}`, {
        method: 'POST',
        headers: {
          'Content-Type': word
            ? 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
            : 'application/pdf',
        },
        body: file,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw Error(payload.detail || 'Upload failed');
      $('upload-form').reset();
      updateText(
        $('notice'),
        payload.created ? 'Document imported.' : 'This document is already in the library.',
      );
      await loadModules();
      await load();
      review(payload.document.id, payload.extraction, payload.document.module);
    });
  };

  list.addEventListener('click', (event) => {
    const button = event.target.closest('[data-extract]');
    if (!button) return;
    action(async () => {
      const id = button.dataset.extract;
      review(id, await api(`/api/assessment-documents/${id}/extraction`), button.dataset.module);
    });
  });

  list.addEventListener(
    'toggle',
    async (event) => {
      const detail = event.target;
      const id = detail.dataset.document;
      if (!detail.open || !id || !/^[a-f0-9]{64}$/.test(id)) return;
      const target = detail.querySelector('.preview');
      let request = previews.get(id);
      if (!request) {
        request = api(`/api/assessment-documents/${id}/preview`);
        previews.set(id, request);
      }
      updateText(target, 'Loading preview…');
      try {
        const payload = await request;
        setContent(
          target,
          (payload.text_incomplete
            ? '<p class="warn">The preview is incomplete. Open the original document for the full text.</p>'
            : '') +
            (previewBlocks(payload.blocks) ||
              '<p>No selectable text found. Open the document to read it.</p>'),
        );
      } catch (error) {
        if (previews.get(id) === request) previews.delete(id);
        updateText(target, error.message);
      }
    },
    true,
  );

  return async () => {
    await loadModules();
    await load();
  };
}
