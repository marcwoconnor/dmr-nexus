"""
Unit tests for the access_control module.

Each test defines its own config inline so tests don't break
when config_sample.json changes.
"""

import unittest
from hblink4.access_control import (
    RepeaterMatcher, RepeaterConfig, InvalidPatternError, BlacklistError
)


def _make_config(patterns=None, default=None, blacklist_patterns=None):
    """Build a config dict for RepeaterMatcher."""
    cfg = {
        'repeater_configurations': {
            'patterns': patterns or []
        }
    }
    if default is not None:
        cfg['repeater_configurations']['default'] = default
    if blacklist_patterns:
        cfg['blacklist'] = {'patterns': blacklist_patterns}
    return cfg


class TestSpecificIdMatch(unittest.TestCase):
    """Test matching by specific repeater IDs."""

    def setUp(self):
        self.config = _make_config(patterns=[
            {
                'name': 'Core Repeaters',
                'match': {'ids': [312000, 312001]},
                'config': {
                    'passphrase': 'core-key',
                    'slot1_talkgroups': [8, 9],
                    'slot2_talkgroups': [3120, 3121]
                }
            },
            {
                'name': 'Club Network',
                'match': {'ids': [312100, 312101, 312102]},
                'config': {
                    'passphrase': 'club-key',
                    'slot1_talkgroups': [8],
                    'slot2_talkgroups': [3100]
                }
            }
        ])
        self.matcher = RepeaterMatcher(self.config)

    def test_exact_id_match(self):
        config = self.matcher.get_repeater_config(312000, 'TEST')
        self.assertEqual(config.passphrase, 'core-key')
        self.assertEqual(config.slot1_talkgroups, [8, 9])

    def test_second_pattern_match(self):
        config = self.matcher.get_repeater_config(312100, 'TEST')
        self.assertEqual(config.passphrase, 'club-key')

    def test_no_match_returns_none(self):
        config = self.matcher.get_repeater_config(999999, 'TEST')
        self.assertIsNone(config)


class TestIdRangeMatch(unittest.TestCase):
    """Test matching by ID ranges."""

    def setUp(self):
        self.config = _make_config(patterns=[
            {
                'name': 'Regional Network',
                'match': {'id_ranges': [[310000, 310999], [312000, 312999]]},
                'config': {
                    'passphrase': 'regional-key',
                    'slot1_talkgroups': [1, 2, 3],
                    'slot2_talkgroups': [3100, 3120]
                }
            }
        ])
        self.matcher = RepeaterMatcher(self.config)

    def test_in_first_range(self):
        config = self.matcher.get_repeater_config(310500, 'TEST')
        self.assertEqual(config.passphrase, 'regional-key')

    def test_in_second_range(self):
        config = self.matcher.get_repeater_config(312050, 'TEST')
        self.assertEqual(config.passphrase, 'regional-key')

    def test_range_start_boundary(self):
        config = self.matcher.get_repeater_config(310000, 'TEST')
        self.assertEqual(config.passphrase, 'regional-key')

    def test_range_end_boundary(self):
        config = self.matcher.get_repeater_config(312999, 'TEST')
        self.assertEqual(config.passphrase, 'regional-key')

    def test_outside_range(self):
        config = self.matcher.get_repeater_config(311000, 'TEST')
        self.assertIsNone(config)


class TestCallsignMatch(unittest.TestCase):
    """Test matching by callsign wildcard patterns."""

    def setUp(self):
        self.config = _make_config(patterns=[
            {
                'name': 'WA0EDA Repeaters',
                'match': {'callsigns': ['WA0EDA*']},
                'config': {
                    'passphrase': 'wa0eda-key',
                    'slot1_talkgroups': [8],
                    'slot2_talkgroups': [31201]
                }
            }
        ])
        self.matcher = RepeaterMatcher(self.config)

    def test_callsign_wildcard_match(self):
        config = self.matcher.get_repeater_config(999999, 'WA0EDA-1')
        self.assertEqual(config.passphrase, 'wa0eda-key')

    def test_callsign_exact_match(self):
        config = self.matcher.get_repeater_config(999999, 'WA0EDA')
        self.assertEqual(config.passphrase, 'wa0eda-key')

    def test_callsign_case_insensitive(self):
        config = self.matcher.get_repeater_config(999999, 'wa0eda-2')
        self.assertEqual(config.passphrase, 'wa0eda-key')

    def test_callsign_no_match(self):
        config = self.matcher.get_repeater_config(999999, 'KB1ABC')
        self.assertIsNone(config)


class TestDefaultConfig(unittest.TestCase):
    """Test default config fallback."""

    def test_default_used_when_no_match(self):
        config = _make_config(
            patterns=[{
                'name': 'Specific',
                'match': {'ids': [1]},
                'config': {'passphrase': 'specific-key'}
            }],
            default={'passphrase': 'default-key', 'slot1_talkgroups': [8], 'slot2_talkgroups': [8]}
        )
        matcher = RepeaterMatcher(config)
        result = matcher.get_repeater_config(999999, 'TEST')
        self.assertEqual(result.passphrase, 'default-key')
        self.assertEqual(result.slot1_talkgroups, [8])
        self.assertEqual(result.slot2_talkgroups, [8])

    def test_no_default_returns_none(self):
        config = _make_config(patterns=[{
            'name': 'Specific',
            'match': {'ids': [1]},
            'config': {'passphrase': 'specific-key'}
        }])
        matcher = RepeaterMatcher(config)
        result = matcher.get_repeater_config(999999, 'TEST')
        self.assertIsNone(result)

    def test_match_takes_precedence_over_default(self):
        config = _make_config(
            patterns=[{
                'name': 'Specific',
                'match': {'ids': [312000]},
                'config': {'passphrase': 'specific-key'}
            }],
            default={'passphrase': 'default-key'}
        )
        matcher = RepeaterMatcher(config)
        result = matcher.get_repeater_config(312000, 'TEST')
        self.assertEqual(result.passphrase, 'specific-key')


class TestMatchPriority(unittest.TestCase):
    """Test that first matching pattern wins (order matters)."""

    def setUp(self):
        self.config = _make_config(patterns=[
            {
                'name': 'Narrow Range',
                'match': {'id_ranges': [[312000, 312099]]},
                'config': {'passphrase': 'narrow-key'}
            },
            {
                'name': 'Wide Range',
                'match': {'id_ranges': [[310000, 312999]]},
                'config': {'passphrase': 'wide-key'}
            },
            {
                'name': 'Club IDs',
                'match': {'ids': [312050]},
                'config': {'passphrase': 'club-key'}
            }
        ])
        self.matcher = RepeaterMatcher(self.config)

    def test_first_pattern_wins(self):
        # 312050 matches both Narrow Range (first) and Wide Range and Club IDs
        config = self.matcher.get_repeater_config(312050, 'TEST')
        self.assertEqual(config.passphrase, 'narrow-key')

    def test_second_pattern_when_first_doesnt_match(self):
        # 311000 only matches Wide Range
        config = self.matcher.get_repeater_config(311000, 'TEST')
        self.assertEqual(config.passphrase, 'wide-key')

    def test_no_match_outside_all(self):
        config = self.matcher.get_repeater_config(999999, 'TEST')
        self.assertIsNone(config)


class TestMultipleMatchTypes(unittest.TestCase):
    """Test patterns with multiple match types (OR logic)."""

    def test_matches_on_id(self):
        config = _make_config(patterns=[{
            'name': 'Multi',
            'match': {'ids': [312100], 'callsigns': ['WA0EDA*']},
            'config': {'passphrase': 'multi-key'}
        }])
        matcher = RepeaterMatcher(config)
        result = matcher.get_repeater_config(312100, 'OTHER')
        self.assertEqual(result.passphrase, 'multi-key')

    def test_matches_on_callsign(self):
        config = _make_config(patterns=[{
            'name': 'Multi',
            'match': {'ids': [312100], 'callsigns': ['WA0EDA*']},
            'config': {'passphrase': 'multi-key'}
        }])
        matcher = RepeaterMatcher(config)
        result = matcher.get_repeater_config(999999, 'WA0EDA-1')
        self.assertEqual(result.passphrase, 'multi-key')

    def test_no_match_on_either(self):
        config = _make_config(patterns=[{
            'name': 'Multi',
            'match': {'ids': [312100], 'callsigns': ['WA0EDA*']},
            'config': {'passphrase': 'multi-key'}
        }])
        matcher = RepeaterMatcher(config)
        result = matcher.get_repeater_config(999999, 'KB1ABC')
        self.assertIsNone(result)


class TestBlacklist(unittest.TestCase):
    """Test blacklist enforcement."""

    def _make_blacklist_config(self, blacklist_patterns, repeater_patterns=None):
        return _make_config(
            patterns=repeater_patterns or [],
            blacklist_patterns=blacklist_patterns
        )

    def test_blacklist_specific_id(self):
        config = self._make_blacklist_config([{
            'name': 'Blocked IDs',
            'description': 'Bad actors',
            'match': {'ids': [1, 2]},
            'reason': 'Abuse'
        }])
        matcher = RepeaterMatcher(config)
        with self.assertRaises(BlacklistError) as ctx:
            matcher.get_repeater_config(1, 'TEST')
        self.assertEqual(ctx.exception.pattern_name, 'Blocked IDs')
        self.assertEqual(ctx.exception.reason, 'Abuse')

    def test_blacklist_id_range(self):
        config = self._make_blacklist_config([{
            'name': 'Blocked Range',
            'description': 'Unauthorized',
            'match': {'id_ranges': [[315000, 315999]]},
            'reason': 'Unauthorized range'
        }])
        matcher = RepeaterMatcher(config)
        with self.assertRaises(BlacklistError):
            matcher.get_repeater_config(315123, 'TEST')

    def test_blacklist_callsign(self):
        config = self._make_blacklist_config([{
            'name': 'Blocked Callsigns',
            'description': 'Banned',
            'match': {'callsigns': ['BADACTOR*', 'SPAM*']},
            'reason': 'Network abuse'
        }])
        matcher = RepeaterMatcher(config)
        with self.assertRaises(BlacklistError) as ctx:
            matcher.get_repeater_config(123456, 'BADACTOR123')
        self.assertEqual(ctx.exception.pattern_name, 'Blocked Callsigns')

    def test_blacklist_checked_before_patterns(self):
        """Blacklist takes priority even if a pattern would match."""
        config = self._make_blacklist_config(
            blacklist_patterns=[{
                'name': 'Blocked',
                'description': 'Blocked',
                'match': {'ids': [312000]},
                'reason': 'Banned'
            }],
            repeater_patterns=[{
                'name': 'Core',
                'match': {'ids': [312000]},
                'config': {'passphrase': 'core-key'}
            }]
        )
        matcher = RepeaterMatcher(config)
        with self.assertRaises(BlacklistError):
            matcher.get_repeater_config(312000, 'TEST')

    def test_blacklist_multiple_ranges(self):
        config = self._make_blacklist_config([{
            'name': 'Multi-Range Block',
            'description': 'Multiple ranges',
            'match': {'id_ranges': [[100000, 109999], [200000, 209999]]},
            'reason': 'Unauthorized'
        }])
        matcher = RepeaterMatcher(config)
        with self.assertRaises(BlacklistError):
            matcher.get_repeater_config(100500, 'TEST')
        with self.assertRaises(BlacklistError):
            matcher.get_repeater_config(200500, 'TEST')
        # Outside ranges should be fine
        result = matcher.get_repeater_config(150000, 'TEST')
        self.assertIsNone(result)

    def test_non_blacklisted_passes(self):
        config = self._make_blacklist_config([{
            'name': 'Blocked',
            'description': 'Blocked',
            'match': {'ids': [1]},
            'reason': 'Banned'
        }])
        matcher = RepeaterMatcher(config)
        # Should not raise
        result = matcher.get_repeater_config(312000, 'TEST')
        self.assertIsNone(result)


class TestPatternValidation(unittest.TestCase):
    """Test that invalid patterns are rejected."""

    def test_empty_match_rejected(self):
        config = _make_config(patterns=[{
            'name': 'Empty',
            'match': {},
            'config': {'passphrase': 'test'}
        }])
        with self.assertRaises(InvalidPatternError):
            RepeaterMatcher(config)

    def test_invalid_range_rejected(self):
        config = _make_config(patterns=[{
            'name': 'Bad Range',
            'match': {'id_ranges': [[999, 1]]},  # start > end
            'config': {'passphrase': 'test'}
        }])
        with self.assertRaises(InvalidPatternError):
            RepeaterMatcher(config)


class TestGetPatternForRepeater(unittest.TestCase):
    """Test get_pattern_for_repeater returns the matched pattern object."""

    def test_returns_matching_pattern(self):
        config = _make_config(patterns=[{
            'name': 'Core',
            'match': {'ids': [312000]},
            'config': {'passphrase': 'core-key'}
        }])
        matcher = RepeaterMatcher(config)
        pattern = matcher.get_pattern_for_repeater(312000, 'TEST')
        self.assertIsNotNone(pattern)
        self.assertEqual(pattern.name, 'Core')

    def test_returns_none_for_default(self):
        config = _make_config(
            patterns=[{
                'name': 'Core',
                'match': {'ids': [312000]},
                'config': {'passphrase': 'core-key'}
            }],
            default={'passphrase': 'default-key'}
        )
        matcher = RepeaterMatcher(config)
        pattern = matcher.get_pattern_for_repeater(999999, 'TEST')
        self.assertIsNone(pattern)


if __name__ == '__main__':
    unittest.main()
