"""The fixed cleanup refuses descriptor/config confusion and extra ownership."""
import copy
import unittest
from unittest.mock import patch
from scripts import cleanup_pr19_staging as cleanup


class CleanupOwnershipTests(unittest.TestCase):
    def image(self):
        config = {'rootfs': {'type': 'layers', 'diff_ids': ['sha256:layer']},
                  'config': {'Labels': {'infra-ops.scope': 'laptop-pr19-issue24',
                      'infra-ops.run-id': cleanup.RUN, 'org.opencontainers.image.revision': cleanup.APP}}}
        image = {'Id': cleanup.IMAGE, 'RepoTags': [cleanup.TAG],
                 'RootFS': {'Type': 'layers', 'Layers': ['sha256:layer']},
                 'Config': copy.deepcopy(config['config'])}
        return image, config

    def test_manifest_id_may_differ_from_verified_archive_config_id(self):
        image, config = self.image()
        self.assertNotEqual(cleanup.IMAGE, cleanup.CONFIG)
        cleanup.image_owned(image, config)

    def test_foreign_tag_revision_layers_or_config_refused(self):
        for field in ('tag', 'revision', 'layers', 'config', 'id'):
            image, config = self.image()
            if field == 'tag': image['RepoTags'].append('other-project:latest')
            if field == 'revision': image['Config']['Labels']['org.opencontainers.image.revision'] = 'other'
            if field == 'layers': image['RootFS']['Layers'] = ['other']
            if field == 'config': image['Config']['Cmd'] = ['other']
            if field == 'id': image['Id'] = cleanup.CONFIG
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                cleanup.image_owned(image, config)

    def test_removal_refuses_replaced_root_and_preserves_unknown_children(self):
        import tempfile
        from pathlib import Path
        for replace in (True, False):
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder)/'stage';root.mkdir()
                (root/'data').mkdir();(root/'evidence').mkdir()
                (root/'candidate.tar').write_bytes(b'archive')
                st=root.stat()
                inv={'device':st.st_dev,'inode':st.st_ino,'archive_inode':(root/'candidate.tar').stat().st_ino}
                if replace:
                    original=Path(folder)/'original';root.rename(original)
                    root.symlink_to(original, target_is_directory=True)
                    with self.assertRaises(OSError):cleanup.remove_stage(inv, root)
                    self.assertTrue((original/'candidate.tar').exists())
                else:
                    (root/'data'/'unknown').write_text('preserve')
                    with self.assertRaises(OSError):cleanup.remove_stage(inv, root)
                    self.assertTrue((root/'data'/'unknown').exists())
                    self.assertTrue((root/'candidate.tar').exists())

    def test_default_is_inventory_without_mutations(self):
        import json
        image, config = self.image()
        with patch.object(cleanup.os, 'geteuid', return_value=0), \
             patch.object(cleanup, 'production', return_value={'id': cleanup.PRODUCTION}), \
             patch.object(cleanup, 'tree', return_value={'archive_bytes': 123}), \
             patch.object(cleanup, 'archive_config', return_value=config), \
             patch.object(cleanup, 'references'), \
             patch.object(cleanup.shutil, 'disk_usage') as disk, \
             patch.object(cleanup, 'command', return_value=json.dumps([image])) as command, \
             patch('builtins.print'):
            disk.return_value.free = 1000
            cleanup.main([])
        self.assertEqual(command.call_args_list[0].args[0], ['docker', 'image', 'inspect', cleanup.TAG])
        self.assertEqual(command.call_count, 1)
