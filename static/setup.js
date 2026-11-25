import { $, action, api, updateText } from './client.js';

// First-run configuration. Integration values are written to .env by the server;
// reminder preferences and the default sync range are stored in SQLite.

function status(target, text) {
  updateText($(target), text);
}

// Set up the Setup page and return its refresh function.
export function setupView() {
  const form = $('setup-form');
  const fields = form.elements;

  // Load integration status, preferences and sync settings into the form.
  async function refresh() {
    const [info, prefs, sync] = await Promise.all([
      api('/api/setup'),
      api('/api/preferences'),
      api('/api/settings'),
    ]);
    const { moodle, outlook, gmail, timezone } = info.integrations;
    $('setup-demo').hidden = !info.demo;
    for (const name of [
      'moodle_url',
      'moodle_ics_url',
      'clear_ics',
      'outlook_client_id',
      'outlook_tenant',
    ]) {
      fields[name].disabled = info.demo;
    }
    fields.moodle_url.value = moodle.url;
    fields.moodle_ics_url.value = '';
    fields.clear_ics.checked = false;
    fields.outlook_client_id.value = outlook.client_id;
    fields.outlook_tenant.value = outlook.tenant;
    fields.timezone.value = timezone;
    fields.preset.value = ['all', '7', '30', '90'].includes(sync.preset) ? sync.preset : 'all';
    fields.reminders_enabled.checked = prefs.reminders_enabled;
    fields.reminder_days.value = prefs.reminder_days.join(', ');
    fields.include_overdue.checked = prefs.include_overdue;
    fields.browser_notifications.checked = prefs.browser_notifications;
    status(
      'moodle-status',
      `Calendar URL ${moodle.calendar_configured ? 'saved' : 'not set'} · Sign-in session ${moodle.session_saved ? 'saved' : 'not saved'}`,
    );
    status('outlook-status', `Outlook token ${outlook.token_saved ? 'saved' : 'not saved'}`);
    status(
      'gmail-status',
      `credentials.json ${gmail.credentials_present ? 'found' : 'not found'} · Gmail token ${gmail.token_saved ? 'saved' : 'not saved'}`,
    );
    return { info, prefs, sync };
  }

  form.onsubmit = (event) => {
    event.preventDefault();
    action(async () => {
      const { info, sync } = await refresh.current;
      const days = fields.reminder_days.value
        .split(',')
        .map((value) => value.trim())
        .filter(Boolean)
        .map(Number);
      if (days.some((day) => !Number.isInteger(day))) throw Error('Enter whole numbers of days');
      if (fields.browser_notifications.checked && 'Notification' in window) {
        await Notification.requestPermission();
      }
      if (!info.demo) {
        const integrations = {
          moodle_url: fields.moodle_url.value,
          outlook_client_id: fields.outlook_client_id.value,
          outlook_tenant: fields.outlook_tenant.value,
          timezone: fields.timezone.value,
        };
        if (fields.clear_ics.checked) integrations.moodle_ics_url = '';
        else if (fields.moodle_ics_url.value)
          integrations.moodle_ics_url = fields.moodle_ics_url.value;
        await api('/api/setup', 'PUT', integrations);
      }
      await api('/api/settings', 'PUT', { ...sync, preset: fields.preset.value });
      await api('/api/preferences', 'PUT', {
        setup_complete: true,
        reminders_enabled: fields.reminders_enabled.checked,
        reminder_days: days,
        include_overdue: fields.include_overdue.checked,
        browser_notifications: fields.browser_notifications.checked,
      });
      refresh.current = refresh();
      await refresh.current;
      updateText($('notice'), 'Setup saved.');
    });
  };

  return async () => {
    refresh.current = refresh();
    await refresh.current;
  };
}
