"""A routine change must not publish packages, create artifacts or schedule scans."""
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CIResourcePolicy(unittest.TestCase):
    def test_original_acceptance_recipes_are_immutable_and_archived(self):
        data = json.loads((ROOT / 'docs/workflow-archive/2026-10-23/manifest.json').read_text())
        self.assertEqual(len(data['files']), 4)
        for row in data['files']:
            self.assertEqual(hashlib.sha256((ROOT / row['archived_path']).read_bytes()).hexdigest(),
                             row['sha256'], row['original_path'])

    def test_routine_ci_is_one_offline_job_without_storage(self):
        paths = sorted(path.name for path in (ROOT / '.github/workflows').glob('*.yml'))
        self.assertEqual(paths, ['checks.yml', 'release-safety.yml'])
        text = (ROOT / '.github/workflows/checks.yml').read_text()
        self.assertIn('timeout-minutes: 5', text)
        self.assertIn('cancel-in-progress: true', text)
        self.assertIn('python -B -m scripts.check_offline', text)
        self.assertIn('--no-cache-dir', text)
        for forbidden in ('schedule:', '  push:', 'cache: pip', 'upload-artifact@',
                          'download-artifact@', 'docker ', 'secrets.', 'contents: write'):
            self.assertNotIn(forbidden, text)

    def test_release_safety_is_explicit_and_keeps_real_restore_and_upload_gates(self):
        text = (ROOT / '.github/workflows/release-safety.yml').read_text()
        triggers = text.split('on:\n', 1)[1].split('\npermissions:', 1)[0]
        self.assertEqual(triggers.strip(), 'workflow_dispatch:')
        self.assertIn('scripts.check_containers --image', text)
        self.assertIn('scripts/check_full_backup.py --image', text)
        self.assertIn('needs: offline', text)
        self.assertIn('Upload container evidence', text)
        self.assertIn('test -s dist/checks/containers-report.json', text)
        self.assertIn('test -s dist/checks/full-state-backup.json', text)
        self.assertEqual(text.count('if-no-files-found: error'), 2)
        self.assertNotIn('continue-on-error', text)
        self.assertNotIn('secrets.', text)


if __name__ == '__main__':
    unittest.main()
