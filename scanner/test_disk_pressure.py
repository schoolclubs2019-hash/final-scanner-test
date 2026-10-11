import pathlib,tempfile,unittest
from unittest.mock import patch
from types import SimpleNamespace
from scanner_store import Store
from scan_policy import ScanPaused
from catalogue_discovery import Response
class DiskPressureTest(unittest.TestCase):
 def test_pause_preserves_pending_evidence_then_resumes_same_database(self):
  with tempfile.TemporaryDirectory() as root:
   store=Store(pathlib.Path(root)/'state.db',minimum_free_bytes=1000)
   self.addCleanup(store.close)
   with patch('scanner_store.shutil.disk_usage',return_value=SimpleNamespace(free=999)):
    with self.assertRaises(ScanPaused) as held:store.save_page('run','https://brand.test',Response('https://brand.test','source',{}))
    self.assertEqual(held.exception.reason,'disk_pressure')
    self.assertIsNone(store.page('run','https://brand.test'))
   with patch('scanner_store.shutil.disk_usage',return_value=SimpleNamespace(free=2000)):
    store.save_page('run','https://brand.test',Response('https://brand.test','source',{}))
   self.assertEqual(store.page('run','https://brand.test')['text'],'source')
 def test_write_reserves_space_for_sqlite_and_journal(self):
  with tempfile.TemporaryDirectory() as root:
   store=Store(pathlib.Path(root)/'state.db',minimum_free_bytes=1000);self.addCleanup(store.close)
   with patch('scanner_store.shutil.disk_usage',return_value=SimpleNamespace(free=1100)):
    with self.assertRaises(ScanPaused):store.check_capacity(100)
 def test_invalid_reserve_is_rejected_before_database_is_created(self):
  with tempfile.TemporaryDirectory() as root:
   path=pathlib.Path(root)/'state.db'
   for reserve in (-1,True,'not-a-byte-count'):
    with self.assertRaises(ValueError):Store(path,minimum_free_bytes=reserve)
   self.assertFalse(path.exists())
