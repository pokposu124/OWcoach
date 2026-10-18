"""Read-only validation of review criteria; never enables runtime hero profiles."""
from pathlib import Path
from collections import Counter
import hashlib
import json
import re
import sqlite3
import sys

import yaml

HERE = Path(__file__).resolve().parent
ANALYZER = HERE.parents[1]
sys.path.insert(0, str(ANALYZER))
from owcoach.heroes import Criterion


def main():
    db_path = ANALYZER / 'data/overwatch_stats/overwatch.sqlite'
    db = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    expected = dict(db.execute("SELECT hero_id,lower(role) FROM heroes WHERE role IN ('Damage','Tank')"))
    manifest = yaml.safe_load((HERE / 'manifest.yaml').read_text())
    sources = yaml.safe_load((HERE / 'sources.yaml').read_text())
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == manifest['database_sha256'], 'DB snapshot changed: review pack before use'
    assert sources['database_sha256'] == manifest['database_sha256']
    files = sorted((HERE / 'criteria').glob('*.yaml'))
    assert {p.stem for p in files} == set(expected), 'Missing or unexpected hero'
    assert set(sources['heroes']) == set(expected), 'Mechanics source coverage'
    counts, roles, ids = Counter(), Counter(), set()
    fields = set(Criterion.model_fields)
    for path in files + [HERE / 'damage_common.yaml', HERE / 'tank_common.yaml']:
        data = yaml.safe_load(path.read_text())
        assert data['review_status'] == 'draft'
        assert data['profile_type'] == 'criterion_fragment_not_HeroProfile'
        hero = data.get('hero_id')
        owner = hero or data['role']
        if hero:
            assert hero == path.stem and expected[hero] == data['role']
            roles[data['role']] += 1
            assert data['patch_version'] is None
            expected_length = 7 if hero in {'anran', 'dmon', 'dva'} else 6
            assert len(data['criteria']) == expected_length
            s = sources['heroes'][hero]
            assert s['coach_validated'] is False and s['match_patch_verified'] is False
            m = s['mechanics_source']
            assert m['revision_url'] and m['revision_id'] and m['license'] and m['sha256']
            for ref in s['hero_specific_community_references'] + s['role_level_references']:
                assert ref in sources['references']
            used = {c['ability'] for c in data['criteria'] if c['ability']}
            assert used == set(data['ability_refs'])
            for local, ref in data['ability_refs'].items():
                assert ref['database_id'] == f'{hero}:{local.replace("_", "-")}'
                row = db.execute('SELECT name_en,kind,review_required FROM abilities WHERE hero_id=? AND ability_id=? AND is_perk=0',
                                 (hero, ref['database_id'])).fetchone()
                assert row and row[0] == ref['name_en'] and row[1] == ref['kind']
                assert bool(row[2]) == ref['review_required']
        else:
            assert len(data['criteria']) == 12
        for raw in data['criteria']:
            assert not set(raw) - fields, f'Ignored criterion field: {raw["id"]}'
            c = Criterion.model_validate(raw)
            assert c.id not in ids, c.id
            ids.add(c.id)
            assert c.id.startswith(f'{owner}.{c.category}.')
            assert c.status == 'draft' and c.source.patch_version is None
            assert c.source.reference and c.good_signs and c.bad_signs and c.cannot_judge_when
            assert all(label in c.description for label in ['적용 상황:', '예외:', '필수 근거:'])
            counts[c.category] += 1
    assert roles == {'damage': 23, 'tank': 15}, roles
    assert counts == {'positioning': 138, 'skill_usage': 76, 'ultimate_usage': 41}, counts
    assert len(ids) == manifest['counts']['total'] == 255
    examples = yaml.safe_load((HERE / 'review_examples.yaml').read_text())
    assert examples['example_type'] == 'synthetic_not_observed_gameplay'
    example_ids = set()
    for case in examples['examples']:
        assert case['id'] not in example_ids
        example_ids.add(case['id'])
        assert case['criterion_id'] in ids and case['hero_id'] in expected
        assert case['review_decision'] in {'supported_good', 'supported_error', 'acceptable_exception', 'insufficient_evidence'}
        assert case['observed'] and case['reason'] and case['not_a_production_grade'] is True
    for path in HERE.rglob('*.md'):
        for target in re.findall(r'\]\(([^)]+)\)', path.read_text()):
            if '://' not in target and not target.startswith('#'):
                assert (path.parent / target.split('#')[0]).exists(), (path, target)
    print(json.dumps({'status': 'passed', 'heroes': dict(roles), 'criteria': dict(counts),
                      'total': len(ids), 'synthetic_examples': len(example_ids),
                      'runtime_enabled': False, 'gameplay_accuracy_tested': False}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
