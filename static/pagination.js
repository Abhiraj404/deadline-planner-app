// Pager controls and paged, filtered list loading for every list page.

import { $, get, report, updateText } from './client.js';

// Previous/next buttons, page summary and page size for one list.
export function createPager(id, onChange) {
  const root = $(id);
  root.innerHTML = `<div class="row pager" hidden>
    <button class="secondary" type="button" data-page="prev">Previous</button>
    <span aria-live="polite"></span>
    <button class="secondary" type="button" data-page="next">Next</button>
    <label class="inline">Per page<select aria-label="Records per page">
      <option>10</option><option selected>25</option><option>50</option>
    </select></label></div>`;
  const bar = root.firstElementChild;
  const prev = root.querySelector('[data-page="prev"]');
  const next = root.querySelector('[data-page="next"]');
  const summary = root.querySelector('span');
  const limit = root.querySelector('select');
  const state = { page: 1, size: 25 };
  let pages = 1;
  prev.onclick = () => {
    if (state.page > 1) {
      state.page--;
      onChange();
    }
  };
  next.onclick = () => {
    if (state.page < pages) {
      state.page++;
      onChange();
    }
  };
  limit.onchange = () => {
    state.size = Number(limit.value);
    state.page = 1;
    onChange();
  };

  return {
    state,
    update(total) {
      pages = Math.max(1, Math.ceil(total / state.size));
      prev.disabled = state.page <= 1;
      next.disabled = state.page >= pages;
      updateText(summary, `Page ${state.page} of ${pages} · ${total} records`);
      bar.hidden = false;
    },
    slice(rows) {
      state.page = Math.min(state.page, Math.max(1, Math.ceil(rows.length / state.size)));
      this.update(rows.length);
      return rows.slice((state.page - 1) * state.size, state.page * state.size);
    },
  };
}

// Load a list page by page, reloading when a filter changes (search is debounced).
export function pagedList({ key, endpoint, filters = {}, render }) {
  let controller;
  let timer;
  const fields = Object.entries(filters).map(([name, id]) => [name, $(id)]);
  const pager = createPager(`${key}-pager`, () => load().catch(report));

  // Fetch the current page, cancelling any request still in flight.
  async function load() {
    clearTimeout(timer);
    controller?.abort();
    controller = new AbortController();
    const { signal } = controller;
    const query = Object.fromEntries(
      fields.map(([name, field]) => [name, name === 'q' ? field.value.trim() : field.value]),
    );
    const { page, size } = pager.state;
    try {
      const result = await get(endpoint, { ...query, page, size }, signal);
      if (signal.aborted) return;
      const lastPage = Math.max(1, Math.ceil(result.total / size));
      if (result.page > lastPage) {
        pager.state.page = lastPage;
        await load();
        return;
      }
      pager.state.page = result.page;
      render(result.items, query);
      pager.update(result.total);
    } catch (error) {
      if (!signal.aborted) throw error;
    }
  }

  for (const [, field] of fields) {
    const update = () => {
      pager.state.page = 1;
      controller?.abort();
      clearTimeout(timer);
      if (field.type === 'search') {
        timer = setTimeout(() => load().catch(report), 250);
      } else {
        load().catch(report);
      }
    };
    field.addEventListener(field.type === 'search' ? 'input' : 'change', update);
  }

  return load;
}
