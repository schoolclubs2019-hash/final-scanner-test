"""Saved canary policy is immutable in both configuration directions."""
import unittest

from production_worker import validate_saved_policy
from scan_policy import normalize_policy
from catalogue_audit import normalize_audit_policy


class SavedPolicyIdentityTest(unittest.TestCase):
    def setUp(self):
        self.policy = {
            'execution_policy': normalize_policy({}),
            'sitemap_audit_policy': normalize_audit_policy({'mode': 'cross_check'}),
        }

    def test_legacy_without_policy_remains_supported(self):
        validate_saved_policy({}, {})

    def test_unchanged_normalized_policy_is_accepted(self):
        validate_saved_policy(self.policy, self.policy)
        validate_saved_policy(self.policy, {
            'execution_policy': {},
            'sitemap_audit_policy': {'mode': 'cross_check'},
        })

    def test_removing_all_policy_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Saved execution_policy differs'):
            validate_saved_policy(self.policy, {})

    def test_removing_execution_policy_is_rejected(self):
        config = {'sitemap_audit_policy': self.policy['sitemap_audit_policy']}
        with self.assertRaisesRegex(ValueError, 'Saved execution_policy differs'):
            validate_saved_policy(self.policy, config)

    def test_removing_sitemap_policy_is_rejected(self):
        config = {'execution_policy': self.policy['execution_policy']}
        with self.assertRaisesRegex(ValueError, 'Saved sitemap_audit_policy differs'):
            validate_saved_policy(self.policy, config)

    def test_null_cannot_disable_saved_policy(self):
        for key in self.policy:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'differs'):
                validate_saved_policy(self.policy, {**self.policy, key: None})

    def test_adding_policy_to_a_legacy_run_is_rejected(self):
        for key, value in self.policy.items():
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'differs'):
                validate_saved_policy({}, {key: value})

    def test_changed_visit_limit_or_required_audit_is_rejected(self):
        for key, change in (
            ('execution_policy', {'max_revisit_passes': 4}),
            ('sitemap_audit_policy', {'audit_required': True}),
        ):
            config = {**self.policy, key: {**self.policy[key], **change}}
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'differs'):
                validate_saved_policy(self.policy, config)


if __name__ == '__main__':
    unittest.main()
