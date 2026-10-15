from email.message import EmailMessage
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from automated_workflow import kalshi_requests as bot


def notification(subject=bot.SUBJECT, body=None):
    message = EmailMessage()
    message['Subject'] = subject
    message.set_content(body or 'Name: Viewer\nEmail: viewer@example.com\nMessage:\nPlease send the bot.')
    return message.as_bytes()


class KalshiRequestsTests(unittest.TestCase):
    def test_dedicated_subject_and_required_fields(self):
        self.assertEqual(bot.parse_request(notification())['email'], 'viewer@example.com')
        for subject in ['Re: ' + bot.SUBJECT, 'New Website Contact', 'New Sale']:
            self.assertIsNone(bot.parse_request(notification(subject)))
        self.assertIsNone(bot.parse_request(notification(body='Name: Viewer\nEmail: invalid')))

    def test_message_cannot_replace_contact_email(self):
        request = bot.parse_request(notification(body='Name: Viewer\nEmail: viewer@example.com\nMessage:\nEmail: attacker@example.com'))
        self.assertEqual(request['email'], 'viewer@example.com')
        self.assertIsNone(bot.parse_request(notification(body='Name: Viewer\nEmail: one@example.com\nEmail: two@example.com')))

    def test_queue_deduplicates_and_holds(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = bot.open_queue(Path(directory) / 'requests.sqlite3')
            try:
                request = bot.parse_request(notification())
                self.assertTrue(bot.queue_request(connection, request))
                self.assertFalse(bot.queue_request(connection, request))
                self.assertEqual(connection.execute('SELECT status FROM requests').fetchall(), [('held_pending_setup',)])
            finally:
                connection.close()

    def test_disabled_scanner_never_connects(self):
        with patch.object(bot, 'load_mail_settings', return_value={}), patch.object(bot.imaplib, 'IMAP4_SSL') as connect:
            self.assertEqual(bot.scan_requests()['status'], 'setup_pending')
            connect.assert_not_called()

    def test_enabled_scan_reads_without_marking_seen(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = bot.open_queue(Path(directory) / 'queue.sqlite3')
            with patch.object(bot, 'load_mail_settings', return_value={'KALSHI_SCAN_ENABLED': 'true', 'EMAIL_USERNAME': 'user', 'EMAIL_PASSWORD': 'secret'}), patch.object(bot, 'open_queue', return_value=connection), patch.object(bot.imaplib, 'IMAP4_SSL') as connect:
                mailbox = connect.return_value.__enter__.return_value
                mailbox.select.return_value = ('OK', [])
                mailbox.uid.side_effect = [('OK', [b'1']), ('OK', [(b'1', notification())])]
                self.assertEqual(bot.scan_requests()['added'], 1)
                mailbox.select.assert_called_once_with('INBOX', readonly=True)
                self.assertEqual(mailbox.uid.call_args_list[-1].args, ('fetch', b'1', '(BODY.PEEK[])'))

    def test_canvas_wiring_produces_held_draft(self):
        workflow = json.loads((bot.ROOT / 'data/workflows/kalshi-btc-bot-kalshibt/workflow.json').read_text())
        self.assertFalse(workflow['recurring']['enabled'])
        values = {}
        with patch.object(bot, 'load_mail_settings', return_value={}):
            for node in workflow['nodes']:
                inputs = {edge['targetHandle']: values[edge['source']][edge['sourceHandle']]
                          for edge in workflow['edges'] if edge['target'] == node['id']}
                if node['type'] == 'output':
                    self.assertFalse(inputs['input']['delivery_enabled'])
                    self.assertEqual(inputs['input']['status'], 'draft_only')
                    self.assertEqual(inputs['input']['gate']['scan']['status'], 'setup_pending')
                else:
                    namespace = {'inputs': inputs}
                    exec(node['code'], namespace)
                    values[node['id']] = namespace['result']


if __name__ == '__main__':
    unittest.main()
