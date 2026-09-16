"""Cached cockpit enrichment must stay read-only and fit the HTTP work budget."""
import copy
import hashlib

from smartmoney import api
from smartmoney.db import Store
from tests.test_db_offline import _save, AAPL, MSFT


def test_bulk_enrichment_preserves_amendments_prior_holdings_and_signal_fields(tmp_path):
    path = tmp_path / 'market.db'
    with Store(str(path)) as store:
        # Low-weight AAPL was already held by One. Two previously held an option,
        # which must not count as a prior ordinary-share holding.
        for cik, label, put in (('0000000001', 'One', ''), ('0000000002', 'Two', 'PUT')):
            _save(store, cik, label, 'Fixture', f'{cik}-25-000001', '13F-HR',
                  '2025-11-14', '2025-09-30', [('Fixture', AAPL, 100, 10, put)])
            _save(store, cik, label, 'Fixture', f'{cik}-26-000001', '13F-HR',
                  '2026-05-14', '2026-03-31',
                  [('Fixture', AAPL, 100, 10, ''), ('Large fixture', MSFT, 9900, 990, '')])
        _save(store, '0000000001', 'One', 'Fixture', '0000000001-26-000002', '13F-HR/A',
              '2026-05-15', '2026-03-31', [('Fixture', AAPL, 100, 10, '')], 'NEW HOLDINGS', 1)
        _save(store, '0000000003', 'Missing current', 'Fixture', '0000000003-25-000001', '13F-HR',
              '2025-11-14', '2025-09-30', [('Fixture', AAPL, 800, 80, '')])
        store.conn.execute('UPDATE holdings SET ticker=? WHERE cusip=?', ('aapl', AAPL))
        store.conn.execute('UPDATE holdings SET ticker=? WHERE cusip=?', ('MSFT', MSFT))
    original = {
        'metadata': {'observed_at': '2026-05-16T12:00:00Z'},
        'kpis': {'n_signals': 3, 'window_days': 90},
        'signals': [
            {'ticker': 'AAPL', 'score': 17.25, 'insider': {'buy_value_usd': 123.45},
             'institutional': {'fund_labels': ['One', 'Two', 'Missing current'], 'funds_accumulating': 2}},
            {'ticker': 'MSFT', 'score': 12, 'institutional': {'fund_labels': ['One']}},
            {'ticker': 'UNMAPPED', 'score': 8, 'institutional': {'fund_labels': ['One'], 'total_value_usd': 5}},
        ],
    }
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = api._enrich_confluence_cache_payload(str(path), copy.deepcopy(original))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    aapl, msft, unmapped = result['signals']
    assert aapl['institutional']['total_value_usd'] == 300
    assert aapl['institutional']['avg_weight_pct'] == ((200 / 10100 + 100 / 10000) / 2 * 100)
    assert aapl['institutional']['conviction_funds'] == 1
    assert msft['institutional']['total_value_usd'] == 9900
    assert msft['institutional']['conviction_funds'] == 1
    assert unmapped == original['signals'][2]
    assert result['kpis'] == original['kpis']
    assert result['metadata']['observed_at'] == original['metadata']['observed_at']
    assert aapl['score'] == 17.25 and aapl['insider'] == original['signals'][0]['insider']


def test_many_cached_signals_do_not_rescan_all_portfolios_per_ticker(tmp_path, monkeypatch):
    path = tmp_path / 'volume.db'
    labels = [f'Fixture fund {n}' for n in range(24)]
    quarters = ['2024-12-31', '2025-03-31', '2025-06-30', '2025-09-30', '2025-12-31', '2026-03-31']
    with Store(str(path)) as store:
        for fund, label in enumerate(labels):
            cik = f'{fund + 1:010d}'
            store.conn.execute('INSERT INTO funds(cik,label) VALUES (?,?)', (cik, label))
            for quarter, day in enumerate(quarters):
                accession = f'{cik}-26-{quarter:06d}'
                store.conn.execute('INSERT INTO filings(accession,cik,form,report_date,filing_date,total_value,n_positions) '
                                   'VALUES (?,?,\'13F-HR\',?,?,8000,80)', (accession, cik, day, day))
                store.conn.executemany('INSERT INTO holdings(accession,cusip,ticker,value_usd,shares,weight) '
                                       'VALUES (?,?,?,100,10,0.0125)',
                                       [(accession, f'{ticker:09d}', f'T{ticker}') for ticker in range(80)])
    payload = {'signals': [{'ticker': f'T{ticker}', 'institutional': {'fund_labels': labels}}
                           for ticker in range(80)]}
    steps = 0

    def budget_store(*args, **kwargs):
        store = Store(*args, **kwargs)

        def progress():
            nonlocal steps
            steps += 1000
            return steps > 6_000_000

        store.conn.set_progress_handler(progress, 1000)
        return store

    monkeypatch.setattr(api, 'Store', budget_store)
    result = api._enrich_confluence_cache_payload(str(path), payload)
    assert len(result['signals']) == 80
    assert all(signal['institutional']['total_value_usd'] == 2400 for signal in result['signals'])
    assert all(signal['institutional']['conviction_funds'] == 0 for signal in result['signals'])
    assert steps <= 6_000_000
