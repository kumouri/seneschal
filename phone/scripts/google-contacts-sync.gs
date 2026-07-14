/**
 * Google Apps Script — pushes your Google Contacts to the AI call screener so
 * the allowlist stays in sync automatically. Runs as YOU (your own Google
 * account), so there is no OAuth app, no verification, and no token expiry.
 *
 * SETUP (see docs/contacts-sync.md for the full walkthrough):
 *   1. https://script.google.com  ->  New project  ->  paste this file.
 *   2. Editor left rail -> "Services" (+)  ->  add "People API".
 *   3. Project Settings -> Script Properties -> add two properties:
 *        SCREENER_URL = https://seneschal-screener.<your-subdomain>.workers.dev/sync-contacts
 *        SYNC_SECRET  = <the CONTACTS_SYNC_SECRET value from .dev.vars>
 *   4. Run `syncContacts` once and approve the permission prompt.
 *   5. Triggers (alarm clock icon) -> Add Trigger -> syncContacts, Time-driven,
 *      Hour timer, Every hour.
 */
function syncContacts() {
  var props = PropertiesService.getScriptProperties();
  var url = props.getProperty('SCREENER_URL');
  var secret = props.getProperty('SYNC_SECRET');
  if (!url || !secret) {
    throw new Error('Set SCREENER_URL and SYNC_SECRET in Project Settings -> Script Properties.');
  }

  var contacts = [];
  var pageToken = null;
  do {
    var resp = People.People.Connections.list('people/me', {
      personFields: 'names,phoneNumbers',
      pageSize: 1000,
      pageToken: pageToken,
    });
    (resp.connections || []).forEach(function (person) {
      var name = (person.names && person.names[0] && person.names[0].displayName) || '';
      (person.phoneNumbers || []).forEach(function (ph) {
        var e164 = toE164(ph.canonicalForm || ph.value || '');
        if (e164) contacts.push({ numberE164: e164, name: name });
      });
    });
    pageToken = resp.nextPageToken;
  } while (pageToken);

  var res = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + secret },
    payload: JSON.stringify({ contacts: contacts }),
    muteHttpExceptions: true,
  });
  Logger.log('Pushed %s contacts -> HTTP %s %s', contacts.length, res.getResponseCode(), res.getContentText());
}

/** Best-effort US/Canada E.164. People API canonicalForm is usually already E.164. */
function toE164(raw) {
  if (!raw) return '';
  if (raw.charAt(0) === '+') return raw.replace(/[^\d+]/g, '');
  var digits = raw.replace(/\D/g, '');
  if (digits.length === 10) return '+1' + digits;
  if (digits.length === 11 && digits.charAt(0) === '1') return '+' + digits;
  return '';
}
