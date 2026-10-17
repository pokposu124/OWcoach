"""Validate the review-only support criteria pack; does not load/enable hero profiles."""
from pathlib import Path
import collections
import json
import sqlite3
import sys

import yaml

HERE = Path(__file__).resolve().parent
ANALYZER = HERE.parents[1]
sys.path.insert(0, str(ANALYZER))
from owcoach.heroes import Criterion


def main():
    db = sqlite3.connect(f'file:{ANALYZER / "data/overwatch_stats/overwatch.sqlite"}?mode=ro', uri=True)
    supports = {row[0] for row in db.execute("SELECT hero_id FROM heroes WHERE role='Support'")}
    sources = yaml.safe_load((HERE / 'sources.yaml').read_text())
    assert set(sources['heroes']) == supports, 'source coverage differs from DB supports'
    files = sorted((HERE / 'criteria').glob('*.yaml'))
    assert {p.stem for p in files} == supports, 'hero coverage differs from DB supports'
    ids = set()
    counts = collections.Counter()
    for path in files + [HERE / 'common_criteria.yaml']:
        data = yaml.safe_load(path.read_text())
        assert data['review_status'] == 'draft'
        assert data['profile_type'] == 'criterion_fragment_not_HeroProfile'
        hero = data.get('hero_id')
        if hero:
            assert hero == path.stem
            assert len(data['criteria']) == 8
            assert data['patch_version'] is None
            source = sources['heroes'][hero]['mechanics_source']
            assert source['revision_url'] and source['license'] and source['sha256']
            for local_id, ref in data['ability_refs'].items():
                assert ref['database_id'] == f'{hero}:{local_id.replace("_", "-")}'
                row = db.execute('SELECT name_en,review_required FROM abilities WHERE hero_id=? AND ability_id=?',
                                 (hero, ref['database_id'])).fetchone()
                assert row and row[0] == ref['name_en']
                assert bool(row[1]) == ref['review_required']
        else:
            assert len(data['criteria']) == 15
        for raw in data['criteria']:
            assert not set(raw) - set(Criterion.model_fields), 'silently ignored criterion fields'
            c = Criterion.model_validate(raw)
            assert c.id not in ids, f'duplicate criterion: {c.id}'
            ids.add(c.id)
            assert c.status == 'draft' and c.source.reference and c.source.patch_version is None
            assert c.bad_signs and c.cannot_judge_when
            assert '적용 상황:' in c.description and '예외:' in c.description
            if hero:
                assert c.id.startswith(f'{hero}.{c.category}.')
                assert c.good_signs and '필수 근거:' in c.description
                if c.ability:
                    assert c.ability in data['ability_refs']
            counts[c.category] += 1
    examples = yaml.safe_load((HERE / 'review_examples.yaml').read_text())
    assert examples['example_type'] == 'synthetic_not_observed_gameplay'
    example_ids = set()
    for example in examples['examples']:
        assert example['id'] not in example_ids
        example_ids.add(example['id'])
        assert example['hero_id'] in supports
        assert example['criterion_id'] in ids
        assert example['review_decision'] in {'supported_good', 'supported_error', 'acceptable_exception', 'insufficient_evidence'}
        assert example['observed'] and example['reason']
        assert example['not_a_production_grade'] is True
    assert {x['hero_id'] for x in examples['examples']} == supports
    assert counts == {'positioning': 63, 'skill_usage': 64, 'ultimate_usage': 16}, counts
    print(json.dumps({'status': 'passed', 'heroes': len(supports), 'criteria': len(ids),
                      'categories': dict(counts), 'synthetic_examples': len(example_ids),
                      'gameplay_accuracy_tested': False}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
