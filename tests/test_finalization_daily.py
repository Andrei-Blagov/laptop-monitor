"""Approved compact daily: real snapshot adapters and renderer, offline."""
import copy
import unittest
from datetime import timedelta
from tests.test_thai_catalog_reuse import NOW, _fx, _offer
from thailand.models import StoreScanResult
from thai_catalog import build_thai_catalog, rankings_from_catalog, comparison_from_catalog
from thailand.market_messages import format_market_messages, iter_market_sends

class DailyDigest(unittest.TestCase):
    def result(self, count=5):
        offers=[_offer(str(i), f'SKU-UNIQUE-{i:04}', 80000+i*10, NOW) for i in range(count)]
        scan=StoreScanResult(store='jib', ok=True, offers=offers)
        catalog=build_thai_catalog([scan],now=NOW,discovery_checked_at=NOW,instance_id='test')
        return {'result_id':'daily-result','rankings':rankings_from_catalog(catalog,_fx(),now=NOW),
                'comparison':comparison_from_catalog(catalog,None,_fx(),now=NOW),'fx':_fx()}
    def test_thailand_alone_does_not_displace_russian_digest(self):
        result=self.result();event={'trigger_type':'daily','requested_view':'general_top','recipients':['fixture']}
        parts=format_market_messages(result,event,now=NOW)
        self.assertEqual(len(parts),1)
        self.assertIn('Россия · TOP-0',parts[0]['text'])
        self.assertNotIn('SKU-UNIQUE',parts[0]['text'])
        self.assertIn('МСК',parts[0]['text'])
        outgoing=list(iter_market_sends(result,event,lambda:NOW))
        self.assertEqual([x['text'] for x in outgoing],[x['text'] for x in parts])
    def test_missing_evidence_stays_out_of_daily(self):
        result=self.result(1)
        for row in result['rankings']['eligible_models']:
            row['offers'][0]['specs']['ram_gb']['evidence']=None
        text=format_market_messages(result,{'trigger_type':'daily'},now=NOW)[0]['text']
        self.assertIn('Подтверждённых предложений сейчас нет',text)
        self.assertNotIn('SKU-UNIQUE',text)
    def test_stale_send_does_not_recommend_saved_card(self):
        text=format_market_messages(self.result(1),{'trigger_type':'daily'},now=NOW+timedelta(hours=7))[0]['text']
        self.assertIn('Подтверждённых предложений сейчас нет',text)
    def test_unsupported_alternative_is_not_a_daily_card(self):
        result=self.result(0)
        result['comparison']['pairs']=[{'match_class':'ALTERNATIVE','th_observation':{'name':'Bad SSD 96'}}]
        text=format_market_messages(result,{'trigger_type':'daily'},now=NOW)[0]['text']
        self.assertNotIn('Bad SSD 96',text)

if __name__=='__main__':unittest.main()
