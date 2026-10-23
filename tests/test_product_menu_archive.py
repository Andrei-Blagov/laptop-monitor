"""Product navigation, historical identity and daily status; no real messages."""
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import control_bot as bot
import history_archive as archive
from daily_status import daily_status, format_daily_status
from model_price_history import assert_history_readonly
from tests import test_confirmed_price_history as history_fixture


class MenuTests(unittest.TestCase):
    def test_main_and_published_menu_are_small_and_consistent(self):
        self.assertEqual([c for c, _ in bot.BOT_COMMANDS], ['top', 'history', 'status', 'help'])
        callbacks = {b['callback_data'] for row in bot._keyboard()['inline_keyboard'] for b in row}
        self.assertTrue({bot.BTN_TOP, bot.BTN_HISTORY, bot.BTN_STATUS, bot.BTN_HELP} <= callbacks)
        self.assertNotIn(bot.BTN_RUN, callbacks)
        self.assertNotIn(bot.BTN_TH_SCAN, callbacks)
        with patch.object(bot.config, 'get_telegram_admin_chat_ids', return_value={'FAKE_CHAT'}), patch.object(
            bot, '_post_telegram', return_value={'ok': True}
        ) as api:
            self.assertTrue(bot.register_commands(Mock(), 'FAKE'))
        self.assertEqual(api.call_args.args[2], 'setMyCommands')
        self.assertEqual(api.call_args.args[3]['scope'], {'type': 'chat', 'chat_id': 'FAKE_CHAT'})

    def test_parameters_are_explained_not_ignored_and_unknown_buttons_explained(self):
        with patch.object(bot, '_is_admin', return_value=True), patch.object(bot, 'send_message') as send, patch.object(
            bot, 'handle_history_list'
        ) as history, patch.object(bot, 'answer_callback'):
            bot.process_update(Mock(), 'FAKE', {'message': {'chat': {'id': 1}, 'text': '/history SKU'}})
            history.assert_not_called()
            self.assertIn('не принимает параметры', send.call_args.args[3])
            bot.process_update(Mock(), 'FAKE', {'callback_query': {'id': 'fake', 'data': 'old:unknown',
                              'message': {'chat': {'id': 1}, 'message_id': 1}}})
            self.assertIn('устарела', send.call_args.args[3])

    def test_empty_fresh_history_still_has_archive_button_and_renamed_refresh(self):
        with patch.object(bot, 'build_history_picker_items', return_value=[]), patch.object(bot, '_reply_or_edit') as reply:
            bot.handle_history_list(Mock(), 'FAKE', 1)
        self.assertIn('не удалены', reply.call_args.args[3])
        buttons = reply.call_args.kwargs['reply_markup']['inline_keyboard']
        self.assertEqual(buttons[0][0]['callback_data'], 'ha:page:0')
        from model_price_history import history_nav_keyboard
        self.assertIn('из базы', history_nav_keyboard(1)['inline_keyboard'][0][1]['text'])

    def test_status_reports_source_age_and_daily_ack_without_raw_errors(self):
        from datetime import datetime, timezone
        latest={'citilink':{'status':'failed','attempted_at':datetime.now(timezone.utc).isoformat(),
                            'error_message':'SECRET_PERSONAL_VALUE: unsafe details'}}
        with patch.object(bot,'pipeline_status',return_value=[]), patch.object(bot,'get_latest_store_runs_readonly',return_value=latest), patch.object(
            bot,'unique_russian_top',return_value=[]
        ), patch.object(bot,'russian_top_empty_reason',return_value='Нет свежих предложений'):
            text=bot.build_status_text('unused-product-status.db')
        self.assertIn('попытка',text)
        self.assertIn('Ежедневная сводка сегодня',text)
        self.assertNotIn('SECRET_PERSONAL_VALUE',text)


class ArchiveTests(unittest.TestCase):
    setUp = history_fixture.ConfirmedPriceHistoryTests.setUp
    tearDown = history_fixture.ConfirmedPriceHistoryTests.tearDown
    write_variant = history_fixture.ConfirmedPriceHistoryTests.write_variant
    publish = history_fixture.ConfirmedPriceHistoryTests.publish
    point = history_fixture.ConfirmedPriceHistoryTests.point

    def test_delisted_configuration_and_legacy_remain_separate_readonly(self):
        self.point(250000, 10)
        self.point(260000, ram=64)
        before = assert_history_readonly(self.db)
        with patch('control_bot.unique_russian_top', return_value=[]):
            items = archive.entries(self.db)
        confirmed = [item for item in items if item['key']]
        legacy = [item for item in items if not item['key']]
        self.assertEqual(len(confirmed), 2)
        self.assertEqual(len(legacy), 1)
        self.assertEqual({len(item['rows']) for item in confirmed}, {1})
        old = next(item for item in confirmed if item['rows'][0]['price'] == 250000)
        card, markup = archive.card(old)
        self.assertIn('250 000', card)
        self.assertNotIn('260 000', card)
        self.assertIn('Текущая цена', card)
        self.assertNotIn('ram_gb: 64', card)
        png, caption = archive.chart(old, 'all')
        self.assertTrue(png.startswith(b'\x89PNG'))
        self.assertIn('Текущая цена не подтверждена', caption)
        legacy_card, legacy_markup = archive.card(legacy[0])
        self.assertIn('состав товара неизвестен', legacy_card)
        self.assertNotIn('График', str(legacy_markup))
        self.assertEqual(assert_history_readonly(self.db), before)

    def test_pages_and_callback_limits_and_invalid_links(self):
        from storage import open_db
        with open_db(self.db) as conn:
            pid = conn.execute('SELECT id FROM products LIMIT 1').fetchone()[0]
            for i in range(20):
                conn.execute('INSERT INTO price_history(product_id,price,available,checked_at,configuration_key) VALUES(?,?,?,?,?)',
                             (pid, 240000+i, 1, self.now.isoformat(), hashlib.sha256(str(i).encode()).hexdigest()))
            conn.commit()
        picked = set()
        for index in range(3):
            text, markup = archive.page(self.db, index)
            self.assertLess(len(text), 3500)
            for row in markup['inline_keyboard']:
                for button in row:
                    data = button['callback_data']
                    self.assertLessEqual(len(data.encode()), 64)
                    if data.startswith(archive.PICK):
                        self.assertNotIn(data, picked)
                        picked.add(data)
                        self.assertIsNotNone(archive.resolve(self.db, data[len(archive.PICK):]))
        self.assertEqual(len(picked), 21)
        self.assertIn('устарела', archive.page(self.db, 999)[0])
        with patch.object(bot, 'send_message') as send:
            bot.handle_saved_history(Mock(), 'FAKE', 1, 'ha:pick:bad')
        self.assertIn('некорректна', send.call_args.args[3])


class DailyStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime.now(timezone.utc)
        self.day = self.now.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y')
        (self.root/'archive').mkdir()
        (self.root/'receipts').mkdir()

    def receipt(self, indexes=(0,), *, parts=1):
        recipient = 'FAKE_CHAT'
        signature = [{'text': 'daily '+self.day+' part '+str(i), 'reply_markup': None} for i in range(parts)]
        key = hashlib.sha256(json.dumps(['result',recipient,signature],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        content = {'result_id':'result', 'recipient':recipient, 'signature':signature, 'acknowledged_indexes':list(indexes)}
        (self.root/'receipts'/('content-'+key+'.json')).write_text(json.dumps(content),encoding='utf-8')
        event = {'trigger_type':'daily','created_at':self.now.isoformat(),'result_id':'result','recipients':[recipient],
                 'delivery_state':'sent','delivered_part_ids':[{'id':f'{recipient}:daily#{i}','text':signature[i]['text'],
                  'content_receipt_id':key,'reused':False} for i in indexes]}
        (self.root/'archive'/'event.json').write_text(json.dumps(event),encoding='utf-8')
        return key

    def test_complete_ack_and_unknown_are_not_confused_with_price_deliveries(self):
        self.assertEqual(daily_status(self.root,now=self.now)['today_ack'],0)
        self.receipt()
        saved=daily_status(self.root,now=self.now)
        self.assertEqual((saved['today_ack'],saved['today_parts'],saved['invalid']),(1,1,0))
        self.assertIsNotNone(saved['last_ack_persisted_at'])
        self.assertNotIn('FAKE_CHAT',json.dumps(saved))

    def test_missing_part_or_corrupt_binding_never_count_as_complete(self):
        self.receipt(parts=2)
        partial=daily_status(self.root,now=self.now)
        self.assertEqual(partial['today_ack'],0)
        self.assertEqual((partial['today_parts'],partial['today_parts_expected']),(1,2))
        self.receipt(indexes=(0,1),parts=2)
        key=self.receipt()
        (self.root/'receipts'/('content-'+key+'.json')).write_text('{}')
        saved=daily_status(self.root,now=self.now)
        self.assertEqual(saved['today_ack'],0)
        self.assertGreater(saved['invalid'],0)

    def test_queue_and_yesterday_are_reported_honestly(self):
        self.receipt()
        saved=daily_status(self.root,now=self.now+timedelta(days=1))
        self.assertEqual(saved['today_ack'],0)
        self.assertIsNotNone(saved['last_day'])
        (self.root/'deferred').mkdir()
        (self.root/'deferred'/'event.json').write_text(json.dumps({'trigger_type':'daily','created_at':self.now.isoformat()}))
        self.assertEqual(daily_status(self.root,now=self.now)['pending'],1)

    def test_other_chat_ack_does_not_prove_current_recipient(self):
        self.receipt()
        status=daily_status(self.root,now=self.now,expected_recipients={'OTHER_FAKE_CHAT'})
        self.assertEqual(status['today_ack'],0)
        self.assertGreater(status['invalid'],0)

    def test_historical_invalid_ack_does_not_invalidate_today_or_change_records(self):
        self.receipt()
        old = {'trigger_type':'daily', 'created_at':(self.now-timedelta(days=5)).isoformat(),
               'delivery_state':'sent', 'recipients':['FAKE_CHAT'], 'delivered_part_ids':[{}]}
        path = self.root/'archive'/'old.json'
        original = json.dumps(old).encode()
        path.write_bytes(original)
        status = daily_status(self.root,now=self.now)
        self.assertEqual((status['today_ack'],status['invalid'],status['historical_invalid']),(1,0,1))
        self.assertIn('сегодня: подтверждена',format_daily_status(status))
        self.assertEqual(path.read_bytes(),original)
        old['created_at'] = self.now.isoformat()
        path.write_text(json.dumps(old),encoding='utf-8')
        status = daily_status(self.root,now=self.now)
        self.assertEqual((status['invalid'],status['historical_invalid']),(1,0))
        self.assertIn('аномалия подтверждений',format_daily_status(status))

    def test_unknown_or_future_date_cannot_be_hidden_as_historical(self):
        self.receipt()
        for name,created in [('unknown','invalid'),('naive',self.now.replace(tzinfo=None).isoformat()),
                             ('future',(self.now+timedelta(days=1)).isoformat())]:
            event = {'trigger_type':'daily','created_at':created,'delivery_state':'sent'}
            (self.root/'archive'/(name+'.json')).write_text(json.dumps(event),encoding='utf-8')
        status = daily_status(self.root,now=self.now)
        self.assertEqual((status['invalid'],status['undated_invalid'],status['historical_invalid']),(3,2,0))
        self.assertIn('аномалия подтверждений',format_daily_status(status))
