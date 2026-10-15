# Kalshi BTC bot request workflow

Prepared workflow: **Kalshi BTC Bot Requests**. Refresh the workflow dashboard to
load it. Run it to view setup status and the draft email. Recurring scans are off.

## Ready now

- Dedicated exact subject: `Kalshi BTC Bot Request` (case insensitive).
- Read-only IMAP scanner reuses mail settings from `workflow-scripts/initial-contact/.env`.
  Override its path with `KALSHI_MAIL_ENV_FILE`. Credentials never enter workflow JSON.
- Separate SQLite request queue: `data/kalshi-btc/requests.sqlite3`.
  Exact duplicate messages are ignored, including concurrent inserts.
- Access gate always holds requests; final node previews a download email only.
  Neither connecting the canvas nodes nor enabling scanning sends emails.

## Website handoff (not published)

Website source: your own site's contact-form handler.
The existing `/contact` route uses `interest` and sends notifications through
`send_contact_message`. Keep its existing security checks and recipient handling.

Add a contact-form interest option with value `kalshibtc`, label `Kalshi BTC Bot`.
In `send_contact_message`, choose `Kalshi BTC Bot Request` only when
`contact.get('interest') == 'kalshibtc'`; retain the existing subject for other
interests. A button on the bot's website section should preselect that option.
Do not change CONTACT_NOTIFY_SUBJECT globally: consulting requests need their
current subject. The new subject also avoids the existing contact scanner's filter.

The scanner expects the existing plain-text notification layout:

```text
Subject: Kalshi BTC Bot Request

New Website Contact

Name: Example Viewer
Email: viewer@example.com
Interested In: kalshibtc
Message:
I would like the Kalshi BTC bot.
```

Name and Email must appear before Message. A supplied YouTube channel is only
a claim, never verification. Inbox contents are untrusted; this intake queue
does not authenticate requesters or approve them for delivery.

## Subscriber verification decision

YouTube subscription resources expose channel identifiers, not subscriber email
addresses. Connecting only the creator account cannot reliably map an incoming
email to a subscriber. The API also notes that returned subscriber lists may be
limited. Viewer OAuth could technically check their own subscription using
`mine=true` and `forChannelId`, but that is a separate sign-in flow.

More fundamentally, YouTube's API developer policies prohibit incentives or
rewards for subscribing. A free bot conditioned on subscribing appears to fall
under that restriction. Resolve the offer before implementing that gate.
An alternative for the owner's decision: free downloads by email request, with
subscribing encouraged voluntarily. This alternative is NOT enabled here.

Sources checked September 14, 2026:
- https://developers.google.com/youtube/v3/docs/subscriptions
- https://developers.google.com/youtube/v3/docs/subscriptions/list
- https://developers.google.com/youtube/terms/developer-policies

## Remaining launch work

1. Decide the access rule and final video/website copy.
2. Transfer the bot package and its setup instructions from the laptop.
3. Choose download hosting; build delivery and any required email verification.
   A static download URL can be shared by recipients.
4. Apply the website changes above and test a request with an address you control.
5. Set `KALSHI_SCAN_ENABLED=true` in the workflow service environment (or the
   selected mail env file). Restart the service if changing its environment.
   The scanner searches the past 30 days including read messages; it does not
   mark mail read. Run manually first, then enable recurrence if wanted.
6. Implement and test approved delivery with persistent send tracking before
   launching. Currently there is no sender, OAuth connection, or hosted download.

The queue contains personal information; keep it local and delete it when no
longer needed. Repeated requests with different message contents remain separate
so a later fulfillment step must also deduplicate deliveries by recipient.
