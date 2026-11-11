"""Desired countries and acting countries are different questions.

The panel used to show only what the owner asked for. After an apply, the pool can reach a
narrower set, and the card said nothing about it.
"""
import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parents[1] / 'assets'


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


apply = _load('apply_acting', 'geomask-apply.py')


def node(name, countries):
    return {'name': name, 'status': 'eligible',
            'probes': {service: {'status': 'ok', 'country': cc} for service, cc in countries.items()}}


class ActingCountriesTests(unittest.TestCase):
    def test_collects_distinct_measured_countries(self):
        pool = [node('a', {'cloudflare': 'DE'}), node('b', {'cloudflare': 'NL'}),
                node('c', {'cloudflare': 'de'})]
        self.assertEqual(apply._acting_countries(pool), ['DE', 'NL'])

    def test_ignores_probes_without_a_country(self):
        pool = [node('a', {'cloudflare': 'DE'}),
                {'name': 'b', 'status': 'eligible', 'probes': {'cloudflare': {'status': 'unknown', 'country': None}}}]
        self.assertEqual(apply._acting_countries(pool), ['DE'])

    def test_reads_every_service_not_only_the_first(self):
        pool = [node('a', {'cloudflare': 'US', 'chatgpt': 'US', 'google': 'NL'})]
        self.assertEqual(apply._acting_countries(pool), ['NL', 'US'])

    def test_an_empty_pool_reports_nothing(self):
        self.assertEqual(apply._acting_countries([]), [])
        self.assertEqual(apply._acting_countries(None), [])

    def test_malformed_country_values_are_ignored(self):
        pool = [{'name': 'a', 'probes': {'cloudflare': {'country': 'Germany'}}},
                {'name': 'b', 'probes': {'cloudflare': {'country': 'D3'}}}]
        self.assertEqual(apply._acting_countries(pool), [])


if __name__ == '__main__':
    unittest.main()
