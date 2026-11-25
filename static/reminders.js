import { $, api, esc, localDay, setContent } from './client.js';

// Reminders are calculated by the server from saved preferences. Browser
// notifications only fire while a planner tab is open; there is no background
// service, so nothing runs when the planner is closed.

function dueText(item) {
  if (item.overdue) return `${-item.days_remaining} day(s) overdue`;
  if (item.days_remaining === 0) return 'due today';
  return `due in ${item.days_remaining} day(s)`;
}

// Show each reminder as a browser notification at most once a day.
function notifyOncePerDay(items) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  const key = `planner-notified-${localDay(new Date())}`;
  const seen = new Set(JSON.parse(localStorage.getItem(key) || '[]'));
  for (const item of items) {
    if (seen.has(item.id)) continue;
    new Notification('Deadline Planner', {
      body: `${item.title} is ${dueText(item)}`,
      tag: `deadline-${item.id}`,
    });
    seen.add(item.id);
  }
  // Keep only today's record so localStorage does not grow.
  for (const stored of Object.keys(localStorage)) {
    if (stored.startsWith('planner-notified-') && stored !== key) localStorage.removeItem(stored);
  }
  localStorage.setItem(key, JSON.stringify([...seen]));
}

// Show the reminders section and offer browser notifications.
export async function renderReminders() {
  const section = $('reminder-section');
  const { enabled, browser_notifications: browser, items } = await api('/api/reminders');
  section.hidden = !enabled;
  if (!enabled) return;
  setContent(
    $('reminders'),
    items
      .map(
        (item) =>
          `<p class="${item.overdue ? 'warn' : ''}"><strong>${esc(item.title)}</strong> · ${esc(item.category || item.source)} · ${esc(item.due)} · ${esc(dueText(item))}</p>`,
      )
      .join('') || '<p>No deadlines inside your reminder window.</p>',
  );
  const button = $('enable-notifications');
  button.hidden = !browser || !('Notification' in window) || Notification.permission !== 'default';
  button.onclick = async () => {
    await Notification.requestPermission();
    button.hidden = true;
    notifyOncePerDay(items);
  };
  if (browser) notifyOncePerDay(items);
}
