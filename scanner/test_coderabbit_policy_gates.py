"""CodeRabbit regression checks for score gating and immutable publication contract."""
import unittest
from unittest.mock import Mock, patch

from evidence_ai_gate import verify_item
from publication_parity import ContractError, propagate


class ReviewSafetyTest(unittest.TestCase):
    def test_non_scoreable_product_never_calls_model(self):
        row = {'terminal_state': 'undisclosed', 'scoreable': False, 'score': None}
        with patch('evidence_ai_gate.source_packet', side_effect=AssertionError('AI review forbidden')):
            self.assertIs(verify_item({'title': 'Unspecified product'}, row), row)

    def test_mid_propagation_configuration_change_is_rejected_before_any_write(self):
        store = Mock()
        store.run.return_value = {
            'canonical': {'htt_id': '42', 'brand_id': '7'},
            'integrity_status': 'PASS',
            'publish_eligible': True,
            'propagation_contract_hash': 'previous-contract-digest',
        }
        transport = Mock()
        config = {'publication': {'contract': 'htt-generations-v1'}, 'environment': 'staging'}
        with self.assertRaisesRegex(ContractError, 'Publication contract changed'):
            propagate(store, 'saved-run', config, transport, [])
        transport.request.assert_not_called()
        store.update.assert_not_called()
