import { $, action, api, esc, setContent, updateText } from './client.js';

// Shows suggested assessment details and saves a deadline only after the user
// confirms. Every suggestion is shown with the source text it came from.

const confidenceText = { high: 'High confidence', medium: 'Check this', low: 'Low confidence' };

// One list item showing a value, its confidence and its source text.
function sourceLine(label, field) {
  if (!field) return '';
  return `<li><strong>${esc(label)}:</strong> ${esc(field.value ?? 'Not stated')}
    <small>(${esc(confidenceText[field.confidence] || field.confidence)})</small>
    <br><small>“${esc(field.source_text)}”</small></li>`;
}

// A titled bullet list for the notes field.
function listNotes(title, items) {
  return items.length ? `${title}\n${items.map((item) => `- ${item.value}`).join('\n')}\n` : '';
}

// Set up the review form; returns a function that shows it for a document.
export function reviewPanel(onSaved) {
  const panel = $('review');
  const form = $('review-form');
  const candidates = $('review-candidates');
  let documentId = null;

  candidates.onchange = () => {
    if (candidates.value) form.elements.due.value = candidates.value;
  };
  $('review-cancel').onclick = () => {
    panel.hidden = true;
    documentId = null;
  };
  form.onsubmit = (event) => {
    event.preventDefault();
    action(async () => {
      const data = Object.fromEntries(new FormData(form));
      await api('/api/tasks', 'POST', { ...data, progress: 0, document_id: documentId });
      panel.hidden = true;
      documentId = null;
      updateText($('notice'), 'Deadline added. Find it under Assignments.');
      await onSaved?.();
    });
  };

  return function show(id, extraction, moduleName = '') {
    documentId = id;
    form.reset();
    const deadline = extraction.submission_deadline;
    const time = extraction.submission_time?.value;
    form.elements.title.value = extraction.title?.value || '';
    form.elements.due.value = deadline?.value || '';
    form.elements.category.value = extraction.module_name?.value || moduleName || '';
    form.elements.weighting.value = extraction.weighting?.value || '';
    form.elements.notes.value = [
      time ? `Submission time: ${time}\n` : '',
      extraction.word_count ? `Word count: ${extraction.word_count.value}\n` : '',
      ...extraction.presentation_dates.map(
        (item) =>
          `Presentation: ${item.value || item.matched_text}${item.time ? ' ' + item.time : ''}\n`,
      ),
      listNotes('Deliverables:', extraction.deliverables),
      listNotes('Marking criteria:', extraction.marking_criteria),
      listNotes('Important instructions:', extraction.instructions),
    ]
      .join('')
      .trim();
    const options = extraction.deadline_candidates.filter((item) => item.value);
    candidates.disabled = options.length < 2;
    setContent(
      candidates,
      '<option value="">Choose a date found in the document</option>' +
        options
          .map(
            (item) =>
              `<option value="${esc(item.value)}">${esc(item.value)} · ${esc(item.source_text)}</option>`,
          )
          .join(''),
    );
    const warnings = [...extraction.warnings];
    if (extraction.needs_confirmation && !warnings.length) {
      warnings.push('Check the deadline against the document before confirming.');
    }
    setContent($('review-warnings'), warnings.map((text) => `<li>${esc(text)}</li>`).join(''));
    setContent(
      $('review-sources'),
      `<details open><summary>Where each value came from</summary><ul>${[
        sourceLine('Title', extraction.title),
        sourceLine('Module', extraction.module_name),
        sourceLine('Module code', extraction.module_code),
        sourceLine('Weighting', extraction.weighting),
        sourceLine('Submission deadline', deadline),
        sourceLine('Word count', extraction.word_count),
        ...extraction.other_dates.map((item) => sourceLine(`Other date (${item.label})`, item)),
      ].join('')}</ul></details>`,
    );
    panel.hidden = false;
    panel.scrollIntoView({ behavior: 'smooth' });
  };
}
